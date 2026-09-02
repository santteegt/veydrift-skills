"""Tests for `veydrift_agent.read` against recorded fixtures.

Fixtures under `tests/fixtures/` were captured by probing the live, unauthenticated
Veydrift API (wallet `0x224aba5d489675a7bd3ce07786fada466b46fa0f`, planet `664`) on
2026-08-12, then trimmed of the ~30-field `indexer` bookkeeping block that repeats
byte-for-byte on every wallet route. `wallet_infrastructure_active_queue.json`,
`wallet_overview_incoming.json` and `health_unhealthy.json` are synthetic -- the probed
account is zero-state (every queue null, no incoming fleets, always healthy), so those
three are hand-built against the backend source types (`QueueState` /
`FleetMissionSummary` in `apps/backend/src/evm.ts`) instead of a live sample. See
references/api-routes.md for the same caveat.

No live network calls: every test runs under `respx.mock`, which replaces httpx's
transport; an unmocked request raises rather than reaching the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
import typer
from typer.testing import CliRunner

from veydrift_agent import http
from veydrift_agent.read import (
    _fetch_or_exit,
    _parse_datetime,
    _randomness_readiness,
    app,
    fetch_activity,
    fetch_alliance_by_id,
    fetch_alliance_state,
    fetch_fleet_visibility,
    fetch_missions,
)

BASE = http.API_BASE_URL
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
PLANET = 664
FIXTURES = Path(__file__).parent / "fixtures"

runner = CliRunner()


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYDRIFT_HOME", str(tmp_path))
    monkeypatch.delenv("VEYDRIFT_WALLET", raising=False)
    yield tmp_path


# --------------------------------------------------------------------------------------
# Basic single-route commands
# --------------------------------------------------------------------------------------


@respx.mock
def test_health_ok_exits_zero_and_notes_reader_replica():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=load("health.json")))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "ok=True" in result.stdout
    assert "reader replica" in result.stdout  # worker.role == "reader" in the fixture


@respx.mock
def test_health_unhealthy_exits_2():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=load("health_unhealthy.json")))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 2


@respx.mock
def test_config_summary():
    respx.get(f"{BASE}/runtime-config").mock(return_value=httpx.Response(200, json=load("config.json")))

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert "chainId 8453" in result.stdout


@respx.mock
def test_settlement_requires_wallet():
    result = runner.invoke(app, ["settlement"])

    assert result.exit_code == 4
    assert "--wallet" in result.stdout


@respx.mock
def test_settlement_json_roundtrips_fixture():
    fixture = load("wallet_settlement.json")
    respx.get(f"{BASE}/wallet/{WALLET}/settlement").mock(return_value=httpx.Response(200, json=fixture))

    result = runner.invoke(app, ["settlement", "--wallet", WALLET, "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == fixture


@respx.mock
def test_planets_summary():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )

    result = runner.invoke(app, ["planets", "--wallet", WALLET])

    assert result.exit_code == 0


@respx.mock
def test_infrastructure_requires_planet_id():
    result = runner.invoke(app, ["infrastructure", "--wallet", WALLET])

    assert result.exit_code == 4
    assert "--planet-id" in result.stdout


@respx.mock
def test_infrastructure_out_writes_file(tmp_path):
    fixture = load("wallet_infrastructure.json")
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=fixture)
    )
    out_file = tmp_path / "infra.json"

    result = runner.invoke(
        app, ["infrastructure", "--wallet", WALLET, "--planet-id", str(PLANET), "--out", str(out_file)]
    )

    assert result.exit_code == 0
    assert json.loads(out_file.read_text()) == fixture
    assert "infra.json" in result.stdout


@respx.mock
def test_moon_unavailable_reason_surfaces_in_generic_summary():
    respx.get(f"{BASE}/wallet/{WALLET}/moon", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_moon.json"))
    )

    result = runner.invoke(app, ["moon", "--wallet", WALLET, "--planet-id", str(PLANET)])

    assert result.exit_code == 0
    assert "moonAvailable: True" in result.stdout


# --------------------------------------------------------------------------------------
# battle-reports / highscores: mandatory --out, refuse stdout unconditionally
# --------------------------------------------------------------------------------------


@respx.mock
def test_battle_reports_without_out_exits_nonzero_and_prints_nothing_huge():
    result = runner.invoke(app, ["battle-reports"])

    assert result.exit_code == 4
    assert "--out" in result.stdout
    assert len(result.stdout) < 500  # never the payload itself


@respx.mock
def test_battle_reports_with_out_writes_file(tmp_path):
    fixture = load("battle_reports.json")
    respx.get(f"{BASE}/battle-reports").mock(return_value=httpx.Response(200, json=fixture))
    out_file = tmp_path / "reports.json"

    result = runner.invoke(app, ["battle-reports", "--out", str(out_file)])

    assert result.exit_code == 0
    assert json.loads(out_file.read_text()) == fixture


@respx.mock
def test_highscores_without_out_exits_nonzero():
    result = runner.invoke(app, ["highscores"])

    assert result.exit_code == 4
    assert len(result.stdout) < 500


@respx.mock
def test_highscores_with_out_writes_file(tmp_path):
    fixture = load("highscores.json")
    respx.get(f"{BASE}/highscores").mock(return_value=httpx.Response(200, json=fixture))
    out_file = tmp_path / "highscores.json"

    result = runner.invoke(app, ["highscores", "--out", str(out_file)])

    assert result.exit_code == 0
    assert json.loads(out_file.read_text()) == fixture


@respx.mock
def test_highscores_has_no_json_summary_flag_at_all():
    """`battle-reports`/`highscores` don't even expose --json/--summary (SPEC.md §5.2:
    "refuse stdout" -- no flag combination may create a loophole). Click's own
    unrecognized-option handling rejects `--json` before our code runs at all, which is
    a stronger guarantee than an application-level check; it exits 2 (click's usage-error
    convention), not our exit-4 bad-args convention -- see references/api-routes.md's
    exit-code table for the overlap this creates with "API unhealthy"."""
    result = runner.invoke(app, ["highscores", "--json"])

    assert result.exit_code != 0
    assert len(result.stdout) < 500


# --------------------------------------------------------------------------------------
# universe: two-step resolution (planets -> galaxy:system) -- not a bare passthrough
# --------------------------------------------------------------------------------------


@respx.mock
def test_universe_resolves_coordinates_then_fetches_the_system():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(
        return_value=httpx.Response(200, json=load("universe_galaxy_system.json"))
    )

    result = runner.invoke(app, ["universe", "--wallet", WALLET, "--planet-id", str(PLANET)])

    assert result.exit_code == 0
    assert "galaxy: 7" in result.stdout
    assert "system: 181" in result.stdout


@respx.mock
def test_universe_unknown_planet_id_fails_with_exit_4():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )

    result = runner.invoke(app, ["universe", "--wallet", WALLET, "--planet-id", "999999"])

    assert result.exit_code == 4


# --------------------------------------------------------------------------------------
# snapshot: the composed target and primary consumer of this work package
# --------------------------------------------------------------------------------------


def _mock_snapshot_routes(overview_fixture: str = "wallet_overview.json"):
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=load("health.json")))
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/research", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_research.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/overview", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load(overview_fixture))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_infrastructure.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/shipyard", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_shipyard.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/defenses", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_defenses.json"))
    )


@respx.mock
def test_snapshot_summary_is_within_the_2kb_budget_and_has_required_content():
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET)])

    assert result.exit_code == 0
    encoded = result.stdout.encode("utf-8")
    assert len(encoded) <= 2048, f"snapshot summary is {len(encoded)} bytes, over the SPEC.md §5.2 budget"
    # Required digest content per SPEC.md §5.2: levels, energy + scale_bps, production/hr,
    # hours-to-cap, queue ETAs (idle here), incoming fleets, fields used/total.
    assert "fields 0/174" in result.stdout
    assert "energy: 0/0 (scale 10000)" in result.stdout
    assert "production/hr:" in result.stdout
    assert "hours-to-cap:" in result.stdout
    assert "affordable now:" in result.stdout
    assert "incoming: none" in result.stdout


@respx.mock
def test_snapshot_json_is_a_valid_snapshot_model(tmp_path):
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["wallet"] == WALLET
    assert data["health_ok"] is True
    assert data["planets"][0]["planet_id"] == PLANET
    assert data["planets"][0]["coordinates"] == "7:181:14"
    assert data["planets"][0]["fields_total"] == 174


@respx.mock
def test_snapshot_parses_missile_silo_level_and_crawler_production():
    """Phase 3 of the general-strategy-engine program (docs/SPEC.md §5.4): both fields
    are fetched by `snapshot` already (`/defenses`'s `missileSiloLevel`, `/infrastructure`'s
    `crawlerProduction` block) and were previously discarded. `wallet_defenses.json` /
    `wallet_infrastructure.json` are real captures that already carry both fields."""
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    planet = data["planets"][0]
    assert planet["missile_silo_level"] == 0
    assert planet["crawler_production"] == {
        "total": 0,
        "effective": 0,
        "max_effective": 0,
        "boost_bps": 0,
        "capped": False,
    }


@respx.mock
def test_snapshot_exits_2_when_health_unhealthy():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=load("health_unhealthy.json")))
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/research", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_research.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/overview", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_overview.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_infrastructure.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/shipyard", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_shipyard.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/defenses", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_defenses.json"))
    )

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET)])

    assert result.exit_code == 2


@respx.mock
def test_snapshot_parses_game_maintenance_paused():
    """`health_paused.json` is a hand-edited copy of a live 2026-08-20 capture with
    `gameMaintenance.paused: true` and `readiness.degradationReasons: ["game_paused"]`
    (see the addendum's dated entry). Confirms all three new Snapshot fields come
    through -- and that a paused game still parses `health_ok=True` (reads keep working
    during a pause, confirmed live)."""
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=load("health_paused.json")))
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/research", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_research.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/overview", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_overview.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_infrastructure.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/shipyard", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_shipyard.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/defenses", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_defenses.json"))
    )

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0  # health.ok/readiness.ready are both true in this fixture
    data = json.loads(result.stdout)
    assert data["health_ok"] is True
    assert data["game_paused"] is True
    assert data["game_maintenance"]["paused"] is True
    assert data["game_maintenance"]["paused_since"] == "2026-08-20T22:46:52.727000Z"
    assert data["game_maintenance"]["pause_age_seconds"] == 754
    assert data["degradation_reasons"] == ["game_paused"]


@respx.mock
def test_snapshot_parses_game_paused_false_and_none_maintenance_on_older_backend_shape():
    """`health.json` (the 2026-08-12 capture) predates `gameMaintenance` entirely -- the
    fail-closed-but-not-crashing case: `_game_maintenance` must not raise on a response
    that never had this key, and must report `game_maintenance=None` (unconfirmed), not
    silently invent a `paused=False` GameMaintenance object."""
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["game_paused"] is False
    assert data["game_maintenance"] is None
    assert data["degradation_reasons"] == []


# --------------------------------------------------------------------------------------
# randomnessReadiness / the storage-cap-style "combat-only degradation" fix (2026-08-22).
# `health_randomness_degraded.json` is a live capture (curl'd directly during planning,
# not synthesized) of a real, persistent condition: /health returns HTTP 503 with a
# well-formed JSON body -- ok:false, readiness.ready:true, readiness.degradationReasons:
# [], gameMaintenance.paused:false, randomnessReadiness.ready:false. See _fetch_or_exit's
# `_recover_health_body` and Snapshot.combat_only_degradation.
# --------------------------------------------------------------------------------------


def test_randomness_readiness_parses_from_a_real_payload():
    data = load("health_randomness_degraded.json")

    rr = _randomness_readiness(data)

    assert rr is not None
    assert rr.ready is False
    assert "randomness safety check" in rr.reasons[0]


def test_randomness_readiness_none_when_absent():
    assert _randomness_readiness({}) is None


def test_randomness_readiness_ready_true_on_the_2026_08_12_capture():
    """health.json's randomnessReadiness is {ready: true, reasons: []} -- the healthy
    shape, distinct from health_randomness_degraded.json's ready:false case above."""
    rr = _randomness_readiness(load("health.json"))
    assert rr is not None
    assert rr.ready is True
    assert rr.reasons == []


@respx.mock
def test_fetch_or_exit_recovers_a_parseable_5xx_health_body():
    body = load("health_randomness_degraded.json")
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(503, json=body))

    result = _fetch_or_exit("/health", max_age=0)

    assert result == body


@respx.mock
def test_fetch_or_exit_still_exits_2_on_an_unparseable_5xx_health_body():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(503, text="bad gateway"))

    with pytest.raises(typer.Exit) as excinfo:
        _fetch_or_exit("/health", max_age=0)

    assert excinfo.value.exit_code == 2


@respx.mock
def test_fetch_or_exit_never_recovers_a_5xx_on_a_non_health_route():
    """Recovery is scoped narrowly to /health -- confirms every other route's 5xx
    behaviour through _fetch_or_exit is completely unaffected, even with a body shaped
    exactly like a recoverable health response."""
    body = load("health_randomness_degraded.json")
    respx.get(f"{BASE}/wallet/{WALLET}/overview", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(503, json=body)
    )

    with pytest.raises(typer.Exit) as excinfo:
        _fetch_or_exit(f"/wallet/{WALLET}/overview", {"planetId": PLANET}, max_age=0)

    assert excinfo.value.exit_code == 2


@respx.mock
def test_snapshot_parses_randomness_readiness_and_readiness_ready_from_a_recovered_5xx():
    """The full snapshot() command still exits 2 on health_ok=False regardless of
    combat_only_degradation (that decision belongs to plan.py/guard.py, not this CLI
    command's own exit code) -- but the recovered 5xx body is still fully parsed onto
    the Snapshot, which is what tick.py's _fetch_snapshot actually consumes."""
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(503, json=load("health_randomness_degraded.json")))
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/research", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_research.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/overview", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_overview.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_infrastructure.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/shipyard", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_shipyard.json"))
    )
    respx.get(f"{BASE}/wallet/{WALLET}/defenses", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_defenses.json"))
    )

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 2  # unchanged CLI convention -- health_ok is still False
    data = json.loads(result.stdout)
    assert data["health_ok"] is False
    assert data["readiness_ready"] is True
    assert data["randomness_readiness"]["ready"] is False
    assert data["degradation_reasons"] == []
    assert data["game_maintenance"]["paused"] is False


@respx.mock
def test_snapshot_discovers_planets_when_planet_id_omitted():
    respx.get(f"{BASE}/wallet/{WALLET}/planets").mock(
        return_value=httpx.Response(200, json=load("wallet_planets.json"))
    )
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert [p["planet_id"] for p in data["planets"]] == [PLANET]


@respx.mock
def test_snapshot_parses_a_populated_building_queue():
    _mock_snapshot_routes()
    respx.get(f"{BASE}/wallet/{WALLET}/infrastructure", params={"planetId": str(PLANET)}).mock(
        return_value=httpx.Response(200, json=load("wallet_infrastructure_active_queue.json"))
    )

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    building_queue = data["planets"][0]["queues"]["building"]
    assert building_queue["entity_name"] == "Metal Mine"
    assert building_queue["target_level"] == 1
    assert building_queue["seconds_remaining"] == 3720


@respx.mock
def test_snapshot_reports_an_incoming_hostile_fleet():
    _mock_snapshot_routes(overview_fixture="wallet_overview_incoming.json")

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data["incoming_fleets"]) == 1
    fleet = data["incoming_fleets"][0]
    assert fleet["mission_type_name"] == "Attack"
    assert fleet["mission_type"] == 3  # RESEARCH-ADDENDUM.md §3 FleetMissionType enum
    assert fleet["hostile"] is True

    summary = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET)])
    assert "Attack from 23" in summary.stdout


# --------------------------------------------------------------------------------------
# fetch_activity() -- the internal, CLI-bypassing helper tick.py's human-activity
# reconciliation check calls directly (never through `vd read activity`).
# --------------------------------------------------------------------------------------


@respx.mock
def test_fetch_activity_wires_since_param():
    route = respx.get(f"{BASE}/wallet/{WALLET}/activity", params={"since": "1786121739"}).mock(
        return_value=httpx.Response(200, json=load("wallet_activity.json"))
    )

    data = fetch_activity(WALLET, since="1786121739")

    assert route.called
    assert data["items"][0]["kind"] == "planet-started"


@respx.mock
def test_fetch_activity_without_since_sends_no_since_param():
    route = respx.get(f"{BASE}/wallet/{WALLET}/activity").mock(return_value=httpx.Response(200, json=load("wallet_activity.json")))

    fetch_activity(WALLET)

    assert route.called
    assert "since" not in route.calls.last.request.url.params


@respx.mock
def test_fetch_activity_raises_on_http_error_rather_than_exiting():
    respx.get(f"{BASE}/wallet/{WALLET}/activity").mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(http.VeydriftHTTPError):
        fetch_activity(WALLET)


# --------------------------------------------------------------------------------------
# Phase 5 of the general-strategy-engine program (docs/SPEC.md §5.4): archetype
# enrichment via /universe/galaxies/{g}/systems/{s}, gated behind --universe-cadence-hours
# being explicitly set (opt-in, so a bare `vd read snapshot` gains no new network call).
# --------------------------------------------------------------------------------------


@respx.mock
def test_snapshot_populates_archetype_when_universe_cadence_is_set():
    _mock_snapshot_routes()
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(
        return_value=httpx.Response(200, json=load("universe_galaxy_system.json"))
    )

    result = runner.invoke(
        app,
        ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json", "--universe-cadence-hours", "24"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    # universe_galaxy_system.json's position-14 slot (planet 664's own coordinates,
    # "7:181:14") carries archetype "frozen-ice" -- confirmed live 2026-08-17.
    assert data["planets"][0]["archetype"] == "frozen-ice"


@respx.mock
def test_snapshot_leaves_archetype_none_when_universe_cadence_is_not_requested():
    """Default (pre-Phase-5) behaviour, byte-for-byte: no --universe-cadence-hours flag
    means no /universe/* call at all -- respx would raise on an unmocked request if one
    were made, so this test doubles as a "no new network call by surprise" regression
    guard."""
    _mock_snapshot_routes()

    result = runner.invoke(app, ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["planets"][0]["archetype"] is None


@respx.mock
def test_snapshot_archetype_stays_none_when_universe_route_errors():
    """Missing/failed enrichment must never abort the whole snapshot -- archetype is
    informational, never load-bearing for a guard/plan decision."""
    _mock_snapshot_routes()
    respx.get(f"{BASE}/universe/galaxies/7/systems/181").mock(return_value=httpx.Response(500, text="boom"))

    result = runner.invoke(
        app,
        ["snapshot", "--wallet", WALLET, "--planet-id", str(PLANET), "--json", "--universe-cadence-hours", "24"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["planets"][0]["archetype"] is None


# --------------------------------------------------------------------------------------
# fetch_fleet_visibility() -- the internal, CLI-bypassing helper tick.py's revived
# rung-3 wiring (`_resolvable_mission_ids`) calls directly, mirroring fetch_activity.
# --------------------------------------------------------------------------------------


@respx.mock
def test_fetch_fleet_visibility_returns_the_raw_dict():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(
        return_value=httpx.Response(200, json=load("wallet_fleet_visibility.json"))
    )

    data = fetch_fleet_visibility(WALLET)

    assert data["wallet"] == WALLET
    assert data["outgoing"] == []


@respx.mock
def test_fetch_fleet_visibility_raises_on_http_error_rather_than_exiting():
    respx.get(f"{BASE}/wallet/{WALLET}/fleet-visibility").mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(http.VeydriftHTTPError):
        fetch_fleet_visibility(WALLET)


@respx.mock
def test_fetch_alliance_state_returns_the_raw_dict_with_no_query_params():
    route = respx.get(f"{BASE}/wallet/{WALLET}/alliance").mock(
        return_value=httpx.Response(200, json={"wallet": WALLET, "membership": {"allianceId": "0", "role": "none"}})
    )

    data = fetch_alliance_state(WALLET)

    assert data["wallet"] == WALLET
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
def test_fetch_alliance_state_raises_on_http_error_rather_than_exiting():
    respx.get(f"{BASE}/wallet/{WALLET}/alliance").mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(http.VeydriftHTTPError):
        fetch_alliance_state(WALLET)


@respx.mock
def test_fetch_alliance_by_id_returns_the_raw_dict_with_no_query_params():
    route = respx.get(f"{BASE}/alliance/29").mock(
        return_value=httpx.Response(200, json={"alliance": {"allianceId": "29", "members": []}})
    )

    data = fetch_alliance_by_id(29)

    assert data["alliance"]["allianceId"] == "29"
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
def test_fetch_alliance_by_id_raises_on_http_error_rather_than_exiting():
    """Confirmed live during this feature's planning: /alliance/{id} 404s
    ({"error": "alliance_not_found"}) for an id the indexer has never seen."""
    respx.get(f"{BASE}/alliance/999999").mock(
        return_value=httpx.Response(404, json={"error": "alliance_not_found"})
    )

    with pytest.raises(http.VeydriftHTTPError):
        fetch_alliance_by_id(999999)


@respx.mock
def test_fetch_missions_default_params():
    route = respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(
        return_value=httpx.Response(200, json={"wallet": WALLET, "rows": []})
    )

    data = fetch_missions(WALLET)

    assert data["rows"] == []
    assert dict(route.calls.last.request.url.params) == {"page": "1", "pageSize": "25"}


@respx.mock
def test_fetch_missions_passes_optional_planet_id_and_status():
    route = respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=httpx.Response(200, json={"rows": []}))

    fetch_missions(WALLET, planet_id=664, status="resolved", page=2, page_size=10)

    params = dict(route.calls.last.request.url.params)
    assert params == {"planetId": "664", "status": "resolved", "page": "2", "pageSize": "10"}


@respx.mock
def test_fetch_missions_raises_on_http_error_rather_than_exiting():
    respx.get(f"{BASE}/wallet/{WALLET}/missions").mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(http.VeydriftHTTPError):
        fetch_missions(WALLET)


# --------------------------------------------------------------------------------------
# _parse_datetime -- the live API's real timestamp shape is a decimal-string unix
# epoch ("1786947731"), not ISO 8601. Confirmed live 2026-08-17 against
# /wallet/{addr}/fleet-visibility and /wallet/{addr}/activity (wallet_activity.json's
# real, non-synthetic fixture already carried this shape and nothing previously
# exercised it through this parser). The two synthetic fixtures
# (wallet_infrastructure_active_queue.json, wallet_overview_incoming.json) guessed ISO
# instead -- both shapes must parse.
# --------------------------------------------------------------------------------------


def test_parse_datetime_accepts_decimal_string_epoch_seconds():
    dt = _parse_datetime("1786947731")
    assert dt is not None
    assert dt.timestamp() == 1786947731


def test_parse_datetime_still_accepts_iso_strings():
    dt = _parse_datetime("2026-08-12T08:00:00.000Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 8 and dt.day == 12


def test_parse_datetime_accepts_raw_int_epoch_seconds():
    dt = _parse_datetime(1786947731)
    assert dt is not None
    assert dt.timestamp() == 1786947731


def test_parse_datetime_returns_none_for_garbage():
    assert _parse_datetime("not-a-date") is None
    assert _parse_datetime(None) is None
