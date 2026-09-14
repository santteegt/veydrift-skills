"""Tests for opportunities.py — attack/missile/colonize/foreign-harvest candidates
surfaced independent of plan.py's ladder outcome.

Fixture shapes mirror tests/test_candidates.py's own known-working fixtures for each
family's generator (the underlying generators are already exhaustively unit-tested
there; these tests verify scan_opportunities' own wiring/aggregation logic: it calls the
right generator with the right kwarg, builds OpportunityFindings correctly, aggregates
across planets, and needs no gating logic of its own since every generator already
self-gates on its own policy flag).
"""

from __future__ import annotations

import json
from pathlib import Path

from veydrift_agent import candidates, ids, opportunities
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    Entity,
    Limits,
    PlanetSnapshot,
    Policy,
    QueueEntry,
    QueueKind,
    RandomnessReadiness,
    Resources,
    Snapshot,
    StorageCfg,
    StrategyCfg,
)

WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
FIXTURES = Path(__file__).parent / "fixtures"


def load_snapshot(name: str) -> Snapshot:
    """Mirrors tests/test_candidates.py's/test_plan.py's own helper of the same name --
    `planet_664.json` is the shared real, live-derived fixture those files already prove
    `select_building_candidate`/`select_research_candidate` behave correctly against; the
    ladder-band survey tests below reuse it rather than re-deriving a synthetic one."""
    return Snapshot.model_validate(json.loads((FIXTURES / name).read_text()))


def make_policy(**overrides) -> Policy:
    base = {
        "wallet": WALLET,
        "planets": [],
        "limits": Limits(gas_per_tx_wei=3_000_000_000_000_000, gas_per_day_wei=20_000_000_000_000_000, eth_gas_floor_wei=2_000_000_000_000_000),
        "actions": ActionsCfg(allow_building=True, allow_research=True, allow_defense=False, allow_ships=False),
        "storage": StorageCfg(hours_to_cap_trigger=2.0),
    }
    base.update(overrides)
    return Policy(**base)


def _planet(planet_id: int, coordinates: str, **overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=planet_id,
        coordinates=coordinates,
        resources_as_of_now=Resources(),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(),
        buildings=[],
        ships=[],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def _snapshot(planets: list[PlanetSnapshot], **overrides) -> Snapshot:
    base = dict(
        taken_at="2026-01-01T12:00:00Z",
        wallet=WALLET,
        health_ok=True,
        planets=planets,
    )
    base.update(overrides)
    return Snapshot(**base)


_ATTACK_TARGET = {23: ("7:181:20", Resources(metal=5_000, crystal=2_000, deuterium=1_000), True)}
_MISSILE_TARGET = {23: ("7:181:20", {ids.Defense.ROCKET_LAUNCHER: 6}, True)}
_COLONIZE_TARGETS = [("7:181:20", 12_000)]
_FOREIGN_TARGET = {700: ("7:181:20", Resources(metal=5_000, crystal=2_000))}

_NO_TARGETS: dict = {}
_EMPTY_KWARGS = dict(attack_targets={}, missile_targets={}, foreign_debris_targets={}, colonize_targets=[])


def test_scan_opportunities_empty_report_when_every_flag_is_off():
    """Live target data present but every gating flag at its default (off) -- proves
    scan_opportunities needs no gating logic of its own; each generator already
    no-ops internally."""
    planet = _planet(
        664,
        "7:181:14",
        ships=[
            Entity(id=ids.Ship.LIGHT_FIGHTER, name="Light Fighter", count=10, cost=Resources(metal=3_000, crystal=1_000)),
            Entity(id=ids.Ship.COLONY_SHIP, name="Colony Ship", count=1, cost=Resources()),
            Entity(id=ids.Ship.RECYCLER, name="Recycler", count=1, cost=Resources()),
        ],
        defenses=[Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=10, cost=Resources())],
    )
    snapshot = _snapshot([planet], randomness_readiness=RandomnessReadiness(ready=True))
    policy = make_policy(planets=[664])  # every actions/strategy flag defaults False

    report = opportunities.scan_opportunities(
        snapshot,
        policy,
        attack_targets=_ATTACK_TARGET,
        missile_targets=_MISSILE_TARGET,
        foreign_debris_targets=_FOREIGN_TARGET,
        colonize_targets=_COLONIZE_TARGETS,
    )

    assert report.findings == []


def test_scan_opportunities_attack_finding():
    planet = _planet(664, "7:181:14", ships=[Entity(id=ids.Ship.LIGHT_FIGHTER, name="Light Fighter", count=10, cost=Resources(metal=3_000, crystal=1_000))])
    snapshot = _snapshot([planet], randomness_readiness=RandomnessReadiness(ready=True))
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_combat=True))

    report = opportunities.scan_opportunities(snapshot, policy, **{**_EMPTY_KWARGS, "attack_targets": _ATTACK_TARGET})

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.family == "attack"
    assert finding.origin_planet_id == 664
    assert finding.target_planet_id == 23
    assert finding.target_coordinates == "7:181:20"
    assert finding.detail


def test_scan_opportunities_missile_finding():
    planet = _planet(
        664, "7:181:14", defenses=[Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=10, cost=Resources())]
    )
    snapshot = _snapshot(
        [planet], technologies=[Entity(id=ids.Technology.IMPULSE_DRIVE, name="Impulse Drive", level=5, cost=Resources())]
    )
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_combat=True))

    report = opportunities.scan_opportunities(snapshot, policy, **{**_EMPTY_KWARGS, "missile_targets": _MISSILE_TARGET})

    assert len(report.findings) == 1
    assert report.findings[0].family == "missile"
    assert report.findings[0].origin_planet_id == 664


def test_scan_opportunities_colonize_finding():
    planet = _planet(
        664,
        "7:181:14",
        ships=[Entity(id=ids.Ship.COLONY_SHIP, name="Colony Ship", count=1, cost=Resources(metal=10_000, crystal=20_000, deuterium=10_000))],
    )
    snapshot = _snapshot([planet], owned_planet_count=0)
    policy = make_policy(planets=[664], strategy=StrategyCfg(colonize=True))

    report = opportunities.scan_opportunities(snapshot, policy, **{**_EMPTY_KWARGS, "colonize_targets": _COLONIZE_TARGETS})

    assert len(report.findings) == 1
    assert report.findings[0].family == "colonize"
    assert report.findings[0].origin_planet_id == 664


def test_scan_opportunities_foreign_harvest_finding():
    planet = _planet(664, "7:181:14", ships=[Entity(id=ids.Ship.RECYCLER, name="Recycler", count=1, cost=Resources(metal=10_000, crystal=6_000, deuterium=2_000))])
    snapshot = _snapshot([planet])
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_fleet_noncombat=True))

    report = opportunities.scan_opportunities(snapshot, policy, **{**_EMPTY_KWARGS, "foreign_debris_targets": _FOREIGN_TARGET})

    assert len(report.findings) == 1
    assert report.findings[0].family == "foreign_harvest"
    assert report.findings[0].origin_planet_id == 664


def test_scan_opportunities_multi_planet_produces_one_finding_per_reachable_planet():
    ships = [Entity(id=ids.Ship.LIGHT_FIGHTER, name="Light Fighter", count=10, cost=Resources(metal=3_000, crystal=1_000))]
    planet_a = _planet(664, "7:181:14", ships=ships)
    planet_b = _planet(665, "7:181:15", ships=ships)
    snapshot = _snapshot([planet_a, planet_b], randomness_readiness=RandomnessReadiness(ready=True))
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_combat=True))

    report = opportunities.scan_opportunities(snapshot, policy, **{**_EMPTY_KWARGS, "attack_targets": _ATTACK_TARGET})

    assert len(report.findings) == 2
    origins = {f.origin_planet_id for f in report.findings}
    assert origins == {664, 665}
    assert all(f.family == "attack" for f in report.findings)


def test_scan_opportunities_transport_finding():
    """ACS defense coordination plan's scope decision: Transport moved from "deliberately
    excluded" to a real, surfaced family -- `generate_transport_candidates` takes
    `target_planets` as a required positional 4th argument (unlike the other four
    generators' keyword-only shape), so this exercises `_scan_transport`'s own dedicated
    call path, not `_scan_planet`'s dispatch dict."""
    origin = _planet(
        664, "7:181:14",
        resources_as_of_now=Resources(metal=5000, crystal=0, deuterium=0),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        ships=[Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources(metal=2000, crystal=2000))],
    )
    destination = _planet(665, "7:181:15")
    snapshot = _snapshot([origin, destination])
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True), reserves=Resources(metal=100))

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.family == "transport"
    assert finding.origin_planet_id == 664
    assert finding.target_coordinates == "7:181:15"
    assert finding.detail


def test_scan_opportunities_transport_empty_when_flag_is_off():
    origin = _planet(
        664, "7:181:14",
        resources_as_of_now=Resources(metal=5000, crystal=0, deuterium=0),
        ships=[Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources(metal=2000, crystal=2000))],
    )
    destination = _planet(665, "7:181:15")
    snapshot = _snapshot([origin, destination])
    policy = make_policy(planets=[664, 665])  # allow_fleet_noncombat defaults False

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    assert report.findings == []


def test_scan_opportunities_family_with_no_viable_target_contributes_nothing():
    """attack_targets empty while every other family has a viable target -- only the
    families with real data produce findings, no placeholder for the empty one."""
    planet = _planet(
        664,
        "7:181:14",
        ships=[
            Entity(id=ids.Ship.COLONY_SHIP, name="Colony Ship", count=1, cost=Resources(metal=10_000, crystal=20_000, deuterium=10_000)),
        ],
    )
    snapshot = _snapshot([planet], owned_planet_count=0, randomness_readiness=RandomnessReadiness(ready=True))
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_combat=True), strategy=StrategyCfg(colonize=True))

    report = opportunities.scan_opportunities(
        snapshot, policy, attack_targets={}, missile_targets={}, foreign_debris_targets={}, colonize_targets=_COLONIZE_TARGETS
    )

    assert [f.family for f in report.findings] == ["colonize"]


# --------------------------------------------------------------------------------------
# policy.strategy.planet_rotation's own known gap (2026-09): Band 2 (building)
# unconditionally precedes Bands 3-4 (research, shipyard/defense, unlock-chain) with no
# policy-configurable weight between them -- these five families surface each of Bands
# 1-4's own winner independent of which one the real ladder picked this tick, so a human
# or agent can see what's queued up behind whatever band is currently winning. See
# opportunities.py's module docstring and references/opportunities.md's "Bands 1-4"
# section for the full rationale.
# --------------------------------------------------------------------------------------


def _origin_planet_664() -> tuple[Snapshot, PlanetSnapshot]:
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    return snapshot, planet


def test_scan_ladder_bands_building_finding_from_real_fixture():
    """Same fixture and policy as
    test_candidates.py::test_select_building_candidate_matches_planet_664s_solar_plant_pick
    -- the underlying selector is already proven correct there; this only proves the
    survey wires it up and labels it "building"."""
    snapshot, _planet664 = _origin_planet_664()
    policy = make_policy(planets=[664])

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    building_findings = [f for f in report.findings if f.family == "building"]
    assert len(building_findings) == 1
    assert building_findings[0].origin_planet_id == 664
    assert "Solar Plant" in building_findings[0].detail or building_findings[0].detail


def test_scan_ladder_bands_building_skipped_when_queue_is_busy():
    """plan.py itself checks `queues.get(QueueKind.BUILDING) is None` before ever calling
    select_building_candidate (that function has no such precondition of its own) -- the
    survey must replicate the exact same external gate, or it would surface a "winner"
    the account could not actually submit right now (ConstructionActive would revert a
    second startBuildingUpgrade)."""
    snapshot, planet = _origin_planet_664()
    busy_planet = planet.model_copy(
        update={
            "queues": {
                QueueKind.BUILDING: QueueEntry(kind=QueueKind.BUILDING, entity_id=ids.Building.METAL_MINE, entity_name="Metal Mine")
            }
        }
    )
    busy_snapshot = snapshot.model_copy(update={"planets": [busy_planet]})
    policy = make_policy(planets=[664])

    report = opportunities.scan_opportunities(busy_snapshot, policy, **_EMPTY_KWARGS)

    assert not [f for f in report.findings if f.family == "building"]


def test_scan_ladder_bands_research_finding():
    """Mirrors test_candidates.py::test_research_fallback_is_explicitly_labelled_default
    exactly (Research Lab bumped to level 1, no research_priority declared -> Energy
    Technology wins by the lowest-level/id fallback)."""
    snapshot, planet = _origin_planet_664()
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 1
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    research_findings = [f for f in report.findings if f.family == "research"]
    assert len(research_findings) == 1
    assert research_findings[0].origin_planet_id == 664


def test_scan_ladder_bands_research_skipped_when_research_queue_is_busy():
    """plan.py checks `snapshot.research_queue is None` before calling
    select_research_candidate (an account-wide queue, unlike building's per-planet one)
    -- the survey replicates that exact gate rather than surfacing a pick the account
    could not currently submit."""
    snapshot, planet = _origin_planet_664()
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 1
    busy_snapshot = snapshot.model_copy(
        update={"research_queue": QueueEntry(kind=QueueKind.RESEARCH, entity_id=ids.Technology.ENERGY, entity_name="Energy Technology")}
    )
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    report = opportunities.scan_opportunities(busy_snapshot, policy, **_EMPTY_KWARGS)

    assert not [f for f in report.findings if f.family == "research"]


def test_scan_ladder_bands_storage_finding():
    """Mirrors test_plan.py::test_storage_overflow_with_idle_queue_spends_via_next_building
    -- a resource within policy.storage.hours_to_cap_trigger of its cap, queue idle, so
    Band 1 "spends it" via the ordinary next-building pick (Solar Plant, same as the
    plain building test above)."""
    snapshot, planet = _origin_planet_664()
    near_cap_planet = planet.model_copy(
        update={
            "resources_as_of_now": Resources(metal=9_900, crystal=1_000, deuterium=0),
            "production_per_hour": Resources(metal=500, crystal=0, deuterium=0),
            "storage_caps": Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        }
    )
    at_risk_snapshot = snapshot.model_copy(update={"planets": [near_cap_planet]})
    policy = make_policy(planets=[664])

    report = opportunities.scan_opportunities(at_risk_snapshot, policy, **_EMPTY_KWARGS)

    storage_findings = [f for f in report.findings if f.family == "storage"]
    assert len(storage_findings) == 1
    assert storage_findings[0].origin_planet_id == 664


def test_scan_ladder_bands_shipyard_finding(monkeypatch):
    """select_shipyard_candidate self-gates on `economy_on_track` (research_queue set or
    some planet's building queue busy) -- mirrors
    test_candidates.py::test_select_shipyard_candidate_picks_from_whichever_planet_is_first_in_the_given_order's
    monkeypatch pattern rather than re-deriving a real ship-unlock fixture, since this
    test is only about the survey's own wiring, not shipyard scoring itself."""
    planet = _planet(664, "7:181:14")
    snapshot = _snapshot(
        [planet], research_queue=QueueEntry(kind=QueueKind.RESEARCH, entity_id=0, entity_name="Energy Technology")
    )
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_ships=True))

    def _fake_ship_candidates(snap, pol, planet):
        return [
            candidates.Candidate(
                action=Action(kind=ActionKind.SHIP, function="startShipProduction", planet_id=planet.planet_id, quantity=1),
                family="ship",
                score=1.0,
                score_basis="test fixture",
            )
        ]

    monkeypatch.setattr(candidates, "generate_ship_candidates", _fake_ship_candidates)
    monkeypatch.setattr(candidates, "generate_defense_candidates", lambda *a, **kw: [])

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    shipyard_findings = [f for f in report.findings if f.family == "shipyard"]
    assert len(shipyard_findings) == 1
    assert shipyard_findings[0].origin_planet_id == 664


def test_scan_ladder_bands_shipyard_skipped_when_economy_not_on_track():
    """No research_queue and no planet with a busy building queue -- economy_on_track is
    False, so select_shipyard_candidate self-gates to (None, []) with no further calls;
    the survey must not surface a shipyard finding here regardless of ship_targets."""
    planet = _planet(664, "7:181:14")
    snapshot = _snapshot([planet])
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_ships=True))

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    assert not [f for f in report.findings if f.family == "shipyard"]


def test_scan_ladder_bands_unlock_chain_finding(monkeypatch):
    """Mirrors
    test_candidates.py::test_select_unlock_chain_candidate_picks_from_whichever_planet_is_first_in_the_given_order's
    monkeypatch pattern -- this test is only about the survey's own wiring, not
    unlock-chain step derivation itself."""
    planet = _planet(664, "7:181:14")
    snapshot = _snapshot([planet])
    policy = make_policy(planets=[664])

    def _fake_unlock_candidates(snap, pol, planet):
        return [
            candidates.Candidate(
                action=Action(kind=ActionKind.BUILD, function="startBuildingUpgrade", planet_id=planet.planet_id),
                family="unlock",
                score=None,
                score_basis="test fixture",
            )
        ]

    monkeypatch.setattr(candidates, "generate_unlock_chain_candidates", _fake_unlock_candidates)

    report = opportunities.scan_opportunities(snapshot, policy, **_EMPTY_KWARGS)

    unlock_findings = [f for f in report.findings if f.family == "unlock_chain"]
    assert len(unlock_findings) == 1
    assert unlock_findings[0].origin_planet_id == 664


def test_scan_ladder_bands_findings_coexist_with_late_band_opportunities():
    """The ladder-band survey and the pre-existing late-band survey are independent and
    additive -- a tick with both a live raid target (attack, normally invisible because
    combat is the ladder's most conservative band) and an idle building queue (normally
    what the real ladder would pick first) should surface both, in the same report."""
    snapshot, _planet664 = _origin_planet_664()
    # planet_664.json has no ships of its own combat-capable type by default; graft one
    # on so generate_attack_candidates has something to launch with, mirroring
    # test_scan_opportunities_attack_finding's own fixture shape.
    combat_planet = snapshot.planet(664).model_copy(
        update={"ships": [Entity(id=ids.Ship.LIGHT_FIGHTER, name="Light Fighter", count=10, cost=Resources(metal=3_000, crystal=1_000))]}
    )
    combat_snapshot = snapshot.model_copy(
        update={"planets": [combat_planet], "randomness_readiness": RandomnessReadiness(ready=True)}
    )
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=True, allow_research=True, allow_combat=True))

    report = opportunities.scan_opportunities(combat_snapshot, policy, **{**_EMPTY_KWARGS, "attack_targets": _ATTACK_TARGET})

    families = {f.family for f in report.findings}
    assert "building" in families
    assert "attack" in families
