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

from veydrift_agent import opportunities
from veydrift_agent.models import (
    ActionsCfg,
    Entity,
    Limits,
    PlanetSnapshot,
    Policy,
    RandomnessReadiness,
    Resources,
    Snapshot,
    StorageCfg,
    StrategyCfg,
)
from veydrift_agent import ids

WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"


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
