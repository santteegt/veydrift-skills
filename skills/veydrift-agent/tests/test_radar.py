"""Tests for radar.py — the attack/harvest mission radar tracker.

Covers both entry points' shared core (`check_targets`): the three independent signal
types (incoming fleet, resolved attack, debris), target resolution (own-wallet and
alliance-id expansion), the resolved-attack de-duplication cursor, the exit-code
contract, and the standalone `vd radar check` CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from veydrift_agent import cli, http, radar
from veydrift_agent.models import PlanetSnapshot, RadarReport, WatchTarget
from veydrift_agent.state import RadarState, WalletRadarState, load_radar_state

BASE = http.API_BASE_URL
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
OTHER_WALLET = "0x64665f1c4a70ff223151fa447cf081aa3c13c031"
FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    return home


def _planets_payload(wallet: str, *rows) -> dict:
    return {"wallet": wallet, "planets": list(rows)}


def _planet_row(planet_id: int, *, galaxy=7, system=181, position=14) -> dict:
    return {"planetId": str(planet_id), "galaxy": galaxy, "system": system, "position": position}


# --------------------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------------------


@respx.mock
def test_resolve_targets_for_wallet_explicit_list_filters_to_named_planets():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664), _planet_row(701)))
    )

    targets = radar.resolve_targets_for_wallet(WALLET, [664])

    assert [t.planet_id for t in targets] == [664]
    assert targets[0].galaxy == 7 and targets[0].system == 181 and targets[0].position == 14


@respx.mock
def test_resolve_targets_for_wallet_empty_list_discovers_every_planet():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664), _planet_row(701)))
    )

    targets = radar.resolve_targets_for_wallet(WALLET, None)

    assert sorted(t.planet_id for t in targets) == [664, 701]


@respx.mock
def test_resolve_targets_for_wallet_raises_on_http_error():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(http.VeydriftHTTPError):
        radar.resolve_targets_for_wallet(WALLET)


def test_targets_from_planet_snapshots_parses_coordinates_string():
    planet = PlanetSnapshot(planet_id=664, coordinates="7:181:14")

    targets = radar.targets_from_planet_snapshots(WALLET, [planet])

    assert targets == [WatchTarget(wallet=WALLET, planet_id=664, galaxy=7, system=181, position=14)]


def test_targets_from_planet_snapshots_filters_to_named_planet_ids():
    planets = [PlanetSnapshot(planet_id=664, coordinates="7:181:14"), PlanetSnapshot(planet_id=701, coordinates="7:181:15")]

    targets = radar.targets_from_planet_snapshots(WALLET, planets, [664])

    assert [t.planet_id for t in targets] == [664]


def test_targets_from_planet_snapshots_leaves_coordinates_none_when_unparseable():
    planet = PlanetSnapshot(planet_id=664, coordinates=None)

    targets = radar.targets_from_planet_snapshots(WALLET, [planet])

    assert targets[0].galaxy is None and targets[0].system is None and targets[0].position is None


@respx.mock
def test_resolve_targets_for_alliance_expands_every_member_planet():
    respx.get(f"{BASE}/alliance/29").mock(
        return_value=httpx.Response(
            200,
            json={
                "alliance": {
                    "allianceId": "29",
                    "members": [{"address": WALLET, "role": "owner"}, {"address": OTHER_WALLET, "role": "member"}],
                }
            },
        )
    )
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{OTHER_WALLET}/planets").mock(
        return_value=httpx.Response(200, json=_planets_payload(OTHER_WALLET, _planet_row(701, galaxy=1, system=1, position=1)))
    )

    targets, errors = radar.resolve_targets_for_alliance(29)

    assert errors == []
    assert {(t.wallet, t.planet_id) for t in targets} == {(WALLET, 664), (OTHER_WALLET, 701)}


@respx.mock
def test_resolve_targets_for_alliance_one_members_planets_fetch_failing_does_not_abort_the_others():
    respx.get(f"{BASE}/alliance/29").mock(
        return_value=httpx.Response(
            200,
            json={
                "alliance": {
                    "members": [{"address": WALLET, "role": "owner"}, {"address": OTHER_WALLET, "role": "member"}]
                }
            },
        )
    )
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{OTHER_WALLET}/planets").mock(return_value=httpx.Response(500, text="boom"))

    targets, errors = radar.resolve_targets_for_alliance(29)

    assert [t.planet_id for t in targets] == [664]
    assert len(errors) == 1 and OTHER_WALLET in errors[0]


@respx.mock
def test_resolve_targets_for_alliance_raises_when_the_alliance_itself_cannot_be_resolved():
    """Confirmed live during this feature's planning: /alliance/{id} 404s for an id the
    indexer has never seen. Unlike a single member's planets fetch failing, this must
    propagate -- there is no member list to even attempt without it."""
    respx.get(f"{BASE}/alliance/999999").mock(return_value=httpx.Response(404, json={"error": "alliance_not_found"}))

    with pytest.raises(http.VeydriftHTTPError):
        radar.resolve_targets_for_alliance(999999)


# --------------------------------------------------------------------------------------
# check_targets — incoming_fleet signal
# --------------------------------------------------------------------------------------


def _empty_missions():
    return httpx.Response(200, json={"rows": []})


def _empty_universe(galaxy=7, system=181):
    return httpx.Response(200, json={"galaxy": galaxy, "system": system, "planets": []})


@respx.mock
def test_check_targets_reports_every_incoming_row_regardless_of_mission_type():
    """The live-incident insight this feature exists to capture: even a non-Attack
    mission (Harvest) targeting your planet is diagnostic, not noise -- so it must be
    reported, not filtered down to a `hostile` flag that's hardcoded True for every row
    anyway (read.py's IncomingFleet.hostile)."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(
        return_value=httpx.Response(
            200,
            json={
                "incoming": [
                    {"missionId": "1", "missionType": "Harvest", "targetPlanetId": "664", "originPlanetId": "164", "arrivalAt": "1786947731"}
                ]
            },
        )
    )
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.kind == "incoming_fleet"
    assert "Harvest" in finding.detail


@respx.mock
def test_check_targets_incoming_fleet_filtered_to_tracked_planets_only():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(
        return_value=httpx.Response(
            200,
            json={"incoming": [{"missionId": "1", "missionType": "Attack", "targetPlanetId": "999", "originPlanetId": "1"}]},
        )
    )
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert report.findings == []


@respx.mock
def test_check_targets_incoming_fleet_fetch_failure_becomes_an_error_not_a_crash():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(500, text="boom"))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert report.findings == []
    assert len(report.errors) == 1


# --------------------------------------------------------------------------------------
# check_targets — resolved_attack signal (the live-incident-motivated signal:
# incoming_fleet structurally cannot see an attack that has already resolved)
# --------------------------------------------------------------------------------------


def _battle_report_row(mission_id: str, *, target_planet_id: int = 664, block_number: int = 100, outcome: str = "AttackerWin"):
    """Shape confirmed live 2026-09-02 against this project's own account/planet 664 --
    the exact real incident this feature exists to catch (references/radar.md): a
    resolved Attack arrives as `kind: "mission"` with a top-level `report` object
    attached, NOT as a separate `kind: "battleReport"` row (api-routes.md §3.14's
    documented union's other half has never actually been observed). `blockNumber`
    arrives as a decimal string on the real API, matched here."""
    return {
        "kind": "mission",
        "mission": {"missionId": mission_id},
        "report": {
            "missionId": mission_id,
            "targetPlanetId": str(target_planet_id),
            "outcome": outcome,
            "blockNumber": str(block_number),
        },
    }


@respx.mock
def test_check_targets_finds_a_resolved_attack_even_with_no_incoming_fleets():
    """Direct reproduction of the live incident this feature exists to catch: a
    resolved Attack that has already fallen out of `incoming_fleets` is still visible
    via /wallet/{addr}/missions' `report`-bearing rows."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [_battle_report_row("61740")]})
    )

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert len(report.findings) == 1
    assert report.findings[0].kind == "resolved_attack"
    assert "61740" in report.findings[0].detail


@respx.mock
def test_check_targets_against_the_real_pinned_incident_fixture():
    """Pin against `wallet_missions_resolved_attack.json` -- a real, live response
    fetched 2026-09-02 from this project's own account, captured at the exact moment
    the live incident that motivated this feature was being investigated. Row 1 (a
    Harvest mission, no `report`) must be ignored; row 2 (the resolved Attack, mission
    61740, `report.blockNumber` a decimal STRING) must produce exactly one finding.
    This is the direct, non-synthetic regression guard against re-introducing the
    `kind: "battleReport"` filter bug this fixture caught during implementation (real
    live data never carries that `kind` value -- only a `report` attached to a
    `kind: "mission"` row)."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json=load_fixture("wallet_missions_resolved_attack.json"))
    )

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.kind == "resolved_attack"
    assert "61740" in finding.detail
    assert "AttackerWin" in finding.detail


@respx.mock
def test_check_targets_resolved_attack_filtered_to_tracked_planets_only():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [_battle_report_row("61740", target_planet_id=999)]})
    )

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert report.findings == []


@respx.mock
def test_check_targets_mission_rows_with_no_report_are_ignored():
    """A `kind: "mission"` row with no `report` attached (e.g. the confirmed-live
    Harvest mission in the same incident -- a non-combat mission never carries one) must
    not be mistaken for a resolved attack."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [{"kind": "mission", "mission": {"missionId": "1"}}]})
    )

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())

    assert report.findings == []


@respx.mock
def test_check_targets_deduplicates_a_previously_seen_resolved_attack():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [_battle_report_row("61740", block_number=100)]})
    )
    state = RadarState()
    state.wallets[WALLET] = WalletRadarState(last_seen_mission_id="61740")

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], state)

    assert report.findings == []
    assert state.wallets[WALLET].last_seen_mission_id == "61740"  # unchanged, nothing newer


@respx.mock
def test_check_targets_reports_only_battle_reports_newer_than_the_cursor():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [
                    _battle_report_row("62000", block_number=200),  # newer -- should be reported
                    _battle_report_row("61740", block_number=100),  # the cursor -- already seen
                ]
            },
        )
    )
    state = RadarState()
    state.wallets[WALLET] = WalletRadarState(last_seen_mission_id="61740")

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], state)

    assert len(report.findings) == 1
    assert "62000" in report.findings[0].detail
    assert state.wallets[WALLET].last_seen_mission_id == "62000"


@respx.mock
def test_check_targets_first_ever_check_reports_every_qualifying_row_and_advances_cursor():
    """`last_seen_mission_id is None` (a wallet never checked before) must report every
    qualifying row seen this run, not suppress them -- fail-closed toward reporting, not
    toward silence, on absent cursor data (AGENTS.md §5's guardrail posture applied to
    a monitoring feature)."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [_battle_report_row("61740", block_number=100)]})
    )
    state = RadarState()

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], state)

    assert len(report.findings) == 1
    assert state.wallets[WALLET].last_seen_mission_id == "61740"


# --------------------------------------------------------------------------------------
# check_targets — debris signal
# --------------------------------------------------------------------------------------


@respx.mock
def test_check_targets_finds_a_populated_debris_field_on_a_tracked_planets_own_slot():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(
        return_value=httpx.Response(
            200, json={"galaxy": 7, "system": 181, "planets": [{"position": 14, "debrisField": {"metal": "2400", "crystal": "2400"}}]}
        )
    )

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664, galaxy=7, system=181, position=14)], RadarState())

    assert len(report.findings) == 1
    assert report.findings[0].kind == "debris"


@respx.mock
def test_check_targets_ignores_a_null_or_zero_debris_field():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(
        return_value=httpx.Response(
            200,
            json={
                "galaxy": 7,
                "system": 181,
                "planets": [{"position": 14, "debrisField": None}, {"position": 15, "debrisField": {"metal": "0", "crystal": "0"}}],
            },
        )
    )

    report = radar.check_targets(
        [
            WatchTarget(wallet=WALLET, planet_id=664, galaxy=7, system=181, position=14),
            WatchTarget(wallet=WALLET, planet_id=665, galaxy=7, system=181, position=15),
        ],
        RadarState(),
    )

    assert report.findings == []


@respx.mock
def test_check_targets_skips_debris_check_for_a_target_missing_coordinates():
    """No debris (universe-route) fetch at all for a target with unresolved
    coordinates -- deliberately leaves /universe/... unmocked under `@respx.mock`
    (strict by default), so a stray fetch would raise `AllMockedAssertionError`, not
    silently pass or (worse, absent the decorator entirely) silently hit the real
    network -- an earlier version of this test lacked `@respx.mock` and was actually
    making live requests without anyone noticing."""
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664)], RadarState())
    assert report.findings == []
    assert report.errors == []


@respx.mock
def test_check_targets_universe_fetch_failure_becomes_an_error_not_a_crash():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=httpx.Response(500, text="boom"))

    report = radar.check_targets([WatchTarget(wallet=WALLET, planet_id=664, galaxy=7, system=181, position=14)], RadarState())

    assert report.findings == []
    assert len(report.errors) == 1


@respx.mock
def test_check_targets_fetches_each_system_only_once_for_multiple_planets():
    route = respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    universe_route = respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(
        return_value=httpx.Response(200, json={"galaxy": 7, "system": 181, "planets": []})
    )

    radar.check_targets(
        [
            WatchTarget(wallet=WALLET, planet_id=664, galaxy=7, system=181, position=14),
            WatchTarget(wallet=WALLET, planet_id=665, galaxy=7, system=181, position=15),
        ],
        RadarState(),
    )

    assert universe_route.call_count == 1
    assert route.call_count == 1  # fleet-visibility is also fetched once per wallet, not per planet


# --------------------------------------------------------------------------------------
# exit_code_for_report — pure function, the actual notification contract
# --------------------------------------------------------------------------------------


def test_exit_code_clean_report_is_zero():
    assert radar.exit_code_for_report(RadarReport()) == 0


def test_exit_code_findings_take_priority_over_errors():
    from veydrift_agent.models import RadarFinding

    report = RadarReport(
        findings=[RadarFinding(kind="debris", wallet=WALLET, planet_id=664, detail="x")], errors=["some other wallet failed"]
    )
    assert radar.exit_code_for_report(report) == 1


def test_exit_code_errors_with_no_findings_is_two():
    report = RadarReport(errors=["fetch failed"])
    assert radar.exit_code_for_report(report) == 2


# --------------------------------------------------------------------------------------
# vd radar check — CLI
# --------------------------------------------------------------------------------------


def test_check_requires_exactly_one_of_wallet_or_alliance_id():
    result = runner.invoke(cli.app, ["radar", "check"])
    assert result.exit_code == 4

    result = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET, "--alliance-id", "29"])
    assert result.exit_code == 4


def test_check_rejects_planets_flag_with_alliance_id():
    result = runner.invoke(cli.app, ["radar", "check", "--alliance-id", "29", "--planets", "664"])
    assert result.exit_code == 4


@respx.mock
def test_check_wallet_mode_exits_zero_on_a_clean_report():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=_empty_universe())

    result = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET])

    assert result.exit_code == 0, result.output


@respx.mock
def test_check_wallet_mode_exits_one_on_findings_and_prints_json():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(
        return_value=httpx.Response(
            200, json={"incoming": [{"missionId": "1", "missionType": "Attack", "targetPlanetId": "664", "originPlanetId": "1"}]}
        )
    )
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=_empty_universe())

    result = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET, "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert len(payload["findings"]) == 1


@respx.mock
def test_check_persists_radar_state_across_invocations():
    """The de-dup cursor must survive between two separate `vd radar check` processes
    -- that's the entire point of radar-state.json existing on disk rather than only
    in memory."""
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"rows": [_battle_report_row("61740")]})
    )
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=_empty_universe())

    first = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET])
    assert first.exit_code == 1, first.output

    second = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET])
    assert second.exit_code == 0, second.output  # same battleReport row, already seen -- no new finding

    assert load_radar_state().wallets[WALLET].last_seen_mission_id == "61740"


@respx.mock
def test_check_alliance_mode_resolves_every_member_planet():
    respx.get(f"{BASE}/alliance/29").mock(
        return_value=httpx.Response(200, json={"alliance": {"members": [{"address": WALLET, "role": "owner"}]}})
    )
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET, _planet_row(664))))
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(200, json={"incoming": []}))
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=_empty_missions())
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=_empty_universe())

    result = runner.invoke(cli.app, ["radar", "check", "--alliance-id", "29"])

    assert result.exit_code == 0, result.output


@respx.mock
def test_check_no_targets_resolved_exits_zero_with_a_message():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(return_value=httpx.Response(200, json=_planets_payload(WALLET)))

    result = runner.invoke(cli.app, ["radar", "check", "--wallet", WALLET])

    assert result.exit_code == 0
    assert "nothing to check" in result.output


@respx.mock
def test_check_alliance_id_unresolvable_exits_two():
    respx.get(f"{BASE}/alliance/999999").mock(return_value=httpx.Response(404, json={"error": "alliance_not_found"}))

    result = runner.invoke(cli.app, ["radar", "check", "--alliance-id", "999999"])

    assert result.exit_code == 2
