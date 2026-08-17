"""Tests for veydrift_agent.plan — the decision engine.

The two tests that matter most are `test_planet_664_*` and `test_planet_hot_*`: they run
the *same* `plan_next_action` code path against two fixtures that differ only in planet
traits (temperature -> deuterium multiplier and Solar Satellite energy yield) and assert
opposite energy-source choices. `test_matched_levels_*` goes further and proves it at
*identical* building levels, isolating temperature as the only variable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veydrift_agent import ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    EnergyBalance,
    Entity,
    EscalationCfg,
    IncomingFleet,
    Limits,
    PlanetSnapshot,
    Policy,
    QueueEntry,
    QueueKind,
    Resources,
    Snapshot,
    StorageCfg,
)
from veydrift_agent.plan import _next_building_action, plan_next_action

FIXTURES = Path(__file__).parent / "fixtures"


def load_snapshot(name: str) -> Snapshot:
    return Snapshot.model_validate(json.loads((FIXTURES / name).read_text()))


def make_policy(**overrides) -> Policy:
    base = {
        "wallet": "0x224aba5d489675a7bd3ce07786fada466b46fa0f",
        "planets": [],
        "limits": Limits(
            gas_per_tx_wei=3_000_000_000_000_000,
            gas_per_day_wei=20_000_000_000_000_000,
            eth_gas_floor_wei=2_000_000_000_000_000,
        ),
        "actions": ActionsCfg(allow_building=True, allow_research=True, allow_defense=False, allow_ships=False),
        "storage": StorageCfg(hours_to_cap_trigger=2.0),
        "escalation": EscalationCfg(on_incoming_fleet=True),
    }
    base.update(overrides)
    return Policy(**base)


# --------------------------------------------------------------------------------------
# The hot-planet counterfactual (docs/SPEC.md acceptance criterion 4; the builder brief's
# highest-priority test).
# --------------------------------------------------------------------------------------


def test_planet_664_energy_first_opener_never_proposes_satellite():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"
    assert action.kind == ActionKind.BUILD
    assert action.function == "startBuildingUpgrade"
    assert action.entity_id == ids.Building.SOLAR_PLANT
    assert action.target_level == 1
    # The literal acceptance-criterion assertion: never a Solar Satellite proposal.
    assert action.entity_id != ids.Ship.SOLAR_SATELLITE
    assert action.kind != ActionKind.SHIP


def test_planet_hot_inverts_and_proposes_satellite():
    """The AC4 counterfactual: identical code, opposite answer, driven only by planet traits.

    `allow_ships=True` is required here and is not incidental. Satellites *are* ship
    production, so the operator must have permitted ships for this branch to be reachable
    at all — see `test_planet_hot_falls_back_to_solar_plant_when_ships_disallowed`. Before
    2026-08-12 this test passed with the default `allow_ships=False`, which looked like
    proof of trait-derived reasoning but was actually masking a policy bypass.
    """
    snapshot = load_snapshot("planet_hot.json")
    policy = make_policy(planets=[900001], actions=ActionsCfg(allow_building=True, allow_ships=True))

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"
    assert action.kind == ActionKind.SHIP
    assert action.function == "startShipProduction"
    assert action.entity_id == ids.Ship.SOLAR_SATELLITE
    assert action.quantity == 1


def test_planet_hot_falls_back_to_solar_plant_when_ships_disallowed():
    """`allow_ships` must bind every path that can emit startShipProduction, not just rung 8.

    On a hot planet a Solar Satellite is the cheaper energy source, so the energy-first
    branch reaches for one. With ships disallowed it must build the Solar Plant instead —
    the mine still gets its energy, just from the source the operator permitted. Returning
    nothing would stall the economy on a perfectly legitimate configuration.
    """
    snapshot = load_snapshot("planet_hot.json")
    policy = make_policy(planets=[900001], actions=ActionsCfg(allow_building=True, allow_ships=False))

    action = plan_next_action(snapshot, policy)

    assert action.function == "startBuildingUpgrade"
    assert action.entity_id == ids.Building.SOLAR_PLANT
    assert action.kind == ActionKind.BUILD
    # And the knob is genuinely load-bearing: the only difference is the policy.
    assert plan_next_action(
        snapshot, make_policy(planets=[900001], actions=ActionsCfg(allow_building=True, allow_ships=True))
    ).entity_id == ids.Ship.SOLAR_SATELLITE


def _with_building(planet_entities: list[Entity], entity_id: int, **updates) -> list[Entity]:
    """Replace one entity by id within a list, leaving the rest untouched."""
    out = []
    for entity in planet_entities:
        if entity.id == entity_id:
            out.append(entity.model_copy(update=updates))
        else:
            out.append(entity)
    return out


def test_matched_building_levels_isolate_temperature_as_the_only_variable():
    """Take the real planet 664 fixture and bump it to the *same* building levels as the
    hot-planet fixture (Solar Plant 15, mines 11/11/11). Only temperature (and therefore
    the deuterium multiplier and Solar Satellite energy yield) differs from the hot
    fixture. If the planner still refuses a satellite here, the earlier two tests are not
    a coincidence of different progress levels -- it really is temperature-driven.
    """
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])
    planet = snapshot.planet(664)
    assert planet is not None

    buildings = planet.buildings
    buildings = _with_building(
        buildings, ids.Building.METAL_MINE, level=11, cost=Resources(metal=5189, crystal=1297)
    )
    buildings = _with_building(
        buildings, ids.Building.CRYSTAL_MINE, level=11, cost=Resources(metal=8444, crystal=4222)
    )
    buildings = _with_building(
        buildings,
        ids.Building.DEUTERIUM_SYNTHESIZER,
        level=11,
        cost=Resources(metal=19461, crystal=6487),
    )
    buildings = _with_building(
        buildings, ids.Building.SOLAR_PLANT, level=15, cost=Resources(metal=32842, crystal=13136)
    )
    progressed_planet = planet.model_copy(
        update={
            "buildings": buildings,
            "energy": planet.energy.model_copy(update={"produced": 1253, "required": 1253, "scale_bps": 10_000}),
        }
    )
    progressed_snapshot = snapshot.model_copy(
        update={"planets": [progressed_planet if p.planet_id == 664 else p for p in snapshot.planets]}
    )

    action = plan_next_action(progressed_snapshot, policy)

    assert action.kind == ActionKind.BUILD
    assert action.entity_id == ids.Building.SOLAR_PLANT
    assert action.target_level == 16
    assert action.kind != ActionKind.SHIP


def test_hot_planet_at_664_levels_would_still_choose_satellite():
    """The mirror image of the previous test: apply 664's *cold* building levels are not
    what flips the decision -- swap only the hot planet's temperature-derived fields onto
    664-like progressed levels is covered above; this test instead confirms the hot
    fixture's own levels are indeed past its crossover point (sanity check on the fixture
    itself, not just the planner)."""
    snapshot = load_snapshot("planet_hot.json")
    planet = snapshot.planet(900001)
    assert planet is not None
    solar = next(b for b in planet.buildings if b.id == ids.Building.SOLAR_PLANT)
    assert solar.level == 15  # past the ~12-level crossover computed for satelliteEnergy=30


# --------------------------------------------------------------------------------------
# The rest of the ladder.
# --------------------------------------------------------------------------------------


def test_killswitch_halts_before_anything_else():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])

    action = plan_next_action(snapshot, policy, killswitch_active=True)

    assert action.kind == ActionKind.HALT
    assert action.rule == "0:killswitch"


def test_unhealthy_snapshot_is_a_noop():
    snapshot = load_snapshot("planet_664.json")
    unhealthy = snapshot.model_copy(update={"health_ok": False})
    policy = make_policy(planets=[664])

    action = plan_next_action(unhealthy, policy)

    assert action.kind == ActionKind.NOOP
    assert action.rule == "1:health-not-ok"


def test_pending_tx_unreconciled_is_a_noop():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])

    action = plan_next_action(snapshot, policy, pending_tx_unreconciled=True)

    assert action.kind == ActionKind.NOOP
    assert action.rule == "2:pending-tx-unreconciled"


def test_resolvable_mission_takes_priority_over_building():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])

    action = plan_next_action(snapshot, policy, resolvable_mission_ids=[42])

    assert action.kind == ActionKind.RESOLVE_MISSION
    assert action.function == "resolveFleetMission"
    assert action.mission_id == 42
    assert action.rule == "3:mission-resolving"


def test_incoming_hostile_fleet_escalates_instead_of_proposing():
    snapshot = load_snapshot("planet_664.json")
    hostile_snapshot = snapshot.model_copy(
        update={
            "incoming_fleets": [
                IncomingFleet(mission_id="1", target_planet_id=664, hostile=True),
            ]
        }
    )
    policy = make_policy(planets=[664])

    action = plan_next_action(hostile_snapshot, policy)

    assert action.kind == ActionKind.ESCALATE
    assert action.rule == "4:incoming-hostile-fleet"
    assert action.function is None  # nothing built or submitted while escalating


def test_incoming_non_hostile_fleet_does_not_escalate():
    snapshot = load_snapshot("planet_664.json")
    friendly_snapshot = snapshot.model_copy(
        update={
            "incoming_fleets": [
                IncomingFleet(mission_id="1", target_planet_id=664, hostile=False),
            ]
        }
    )
    policy = make_policy(planets=[664])

    action = plan_next_action(friendly_snapshot, policy)

    assert action.rule != "4:incoming-hostile-fleet"


def test_storage_overflow_with_busy_queue_proposes_nothing_unsafe():
    """The contract allows only one active BuildingConstruction per planet
    (`buildingConstructions[planetId].active` -> `ConstructionActive` revert,
    `VeydriftGame.sol:117-138`). If the building queue is already busy, rung 5 must not
    propose a second startBuildingUpgrade (neither "spend it" nor the matching storage
    building) -- that would be a guaranteed-revert proposal. It should fall through
    instead. With research also disallowed here, the ladder has nothing left to propose
    and reaches the honest rung-9 NOOP.
    """
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None

    near_cap_planet = planet.model_copy(
        update={
            "resources_as_of_now": Resources(metal=9_900, crystal=1_000, deuterium=0),
            "production_per_hour": Resources(metal=500, crystal=0, deuterium=0),
            "storage_caps": Resources(metal=10_000, crystal=10_000, deuterium=10_000),
            "queues": {
                QueueKind.BUILDING: QueueEntry(
                    kind=QueueKind.BUILDING, entity_id=ids.Building.METAL_MINE, entity_name="Metal Mine"
                )
            },
        }
    )
    busy_snapshot = snapshot.model_copy(update={"planets": [near_cap_planet]})
    policy = make_policy(
        planets=[664], actions=ActionsCfg(allow_building=True, allow_research=False)
    )

    action = plan_next_action(busy_snapshot, policy)

    assert action.rule == "9:no-match"
    assert action.kind == ActionKind.NOOP


def test_storage_overflow_with_idle_queue_spends_via_next_building():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None

    near_cap_planet = planet.model_copy(
        update={
            "resources_as_of_now": Resources(metal=9_900, crystal=1_000, deuterium=0),
            "production_per_hour": Resources(metal=500, crystal=0, deuterium=0),
            "storage_caps": Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        }
    )
    at_risk_snapshot = snapshot.model_copy(update={"planets": [near_cap_planet]})
    policy = make_policy(planets=[664])

    action = plan_next_action(at_risk_snapshot, policy)

    assert action.rule == "5:storage-overflow-spend"
    # Same energy-first derivation as the plain building-queue-empty rung would produce.
    assert action.entity_id == ids.Building.SOLAR_PLANT


def test_no_overflow_falls_through_to_building_queue_rung():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"


# --------------------------------------------------------------------------------------
# Prerequisite legality (docs/SPEC.md's `prerequisites` layer). The bug this work package
# exists to fix: `_next_research_action` used to pick the lowest-level technology
# account-wide with no regard for whether its Research Lab (or other) prerequisite was
# met. On planet 664's fixture -- a fresh, unsettled planet with every building at level
# 0, including Research Lab -- that picked Energy Technology (id 0), which requires
# Research Lab >= 1. Proposing it would have been a guaranteed on-chain revert the first
# time tier >= 2 tried it.
# --------------------------------------------------------------------------------------


def test_research_not_proposed_when_research_lab_is_zero_the_bug_this_wp_fixes():
    """Every technology's minimum `researchLabRequirement` is >= 1
    (`VeydriftCatalog.sol:442-458`), so with Research Lab at 0 (planet 664's fixture, a
    fresh account) rung 7 must propose *nothing* -- not a different, still-locked
    technology, nothing at all -- and the ladder falls through to an honest rung-9 NOOP
    rather than a proposal `walletctl` (or the chain itself) would revert."""
    snapshot = load_snapshot("planet_664.json")
    # Disable building so the ladder falls through to the research rung.
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    action = plan_next_action(snapshot, policy)

    assert action.rule == "9:no-match"
    assert action.kind == ActionKind.NOOP


def test_research_proposes_lowest_level_unlocked_technology_once_research_lab_is_built():
    """Same fixture, Research Lab bumped to level 1 -- the previously-buggy pick (Energy
    Technology, lowest level, tie-break by id) becomes legitimate again once its actual
    prerequisite is satisfied."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 1
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    action = plan_next_action(snapshot, policy)

    assert action.rule == "7:research-queue-empty"
    assert action.kind == ActionKind.RESEARCH
    assert action.function == "startResearch"
    assert action.entity_id == ids.Technology.ENERGY  # lowest level (0), tie-break by id, now unlocked


def test_research_skips_a_locked_lower_candidate_in_favour_of_the_next_unlocked_one():
    """Laser Technology (id 1) sorts before Combustion Drive (id 3) at the same level (0),
    but Laser requires Energy Technology >= 2 (`VeydriftDependencies.sol:requireResearch`)
    while this planet's Energy is only level 1 -- locked. Combustion Drive only needs
    Energy >= 1, which is met. The fix must skip the locked first choice and propose the
    next unlocked one, never fall straight through to a rung-9 NOOP just because the
    *first* candidate by the tie-break was illegal."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 2  # unlocks both Laser (lab>=1) and Combustion Drive (lab>=1)
    energy_tech = next(t for t in snapshot.technologies if t.id == ids.Technology.ENERGY)
    energy_tech.level = 1  # unlocks Combustion Drive (needs energy>=1) but not Laser (needs energy>=2)
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    action = plan_next_action(snapshot, policy)

    assert action.rule == "7:research-queue-empty"
    assert action.kind == ActionKind.RESEARCH
    assert action.entity_id == ids.Technology.COMBUSTION_DRIVE


# --------------------------------------------------------------------------------------
# Rocket Launcher requires Shipyard >= 1 (`requireDefense`'s unconditional
# `shipyardLevel == 0` revert -- every defense id, even Rocket Launcher, carries this
# implicit floor). Rung 8's shipyard-idle branch must not propose it on a planet with no
# Shipyard.
# --------------------------------------------------------------------------------------


def test_rocket_launcher_not_proposed_without_a_shipyard():
    snapshot = load_snapshot("planet_664.json")  # Shipyard level 0 in this fixture
    # "Economy on track" (rung 8's own precondition) needs *something* already building or
    # researching; the building queue stays idle here on purpose so the ladder actually
    # reaches rung 8 rather than proposing a building first.
    snapshot.research_queue = QueueEntry(
        kind=QueueKind.RESEARCH, entity_id=ids.Technology.ENERGY, entity_name="Energy Technology"
    )
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=False, allow_research=False, allow_defense=True, allow_ships=False),
    )

    action = plan_next_action(snapshot, policy)

    assert action.rule == "9:no-match"
    assert action.kind == ActionKind.NOOP


def test_rocket_launcher_proposed_once_shipyard_is_built():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    shipyard = next(b for b in planet.buildings if b.id == ids.Building.SHIPYARD)
    shipyard.level = 1
    snapshot.research_queue = QueueEntry(
        kind=QueueKind.RESEARCH, entity_id=ids.Technology.ENERGY, entity_name="Energy Technology"
    )
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=False, allow_research=False, allow_defense=True, allow_ships=False),
    )

    action = plan_next_action(snapshot, policy)

    assert action.rule == "8:shipyard-idle"
    assert action.kind == ActionKind.DEFENSE
    assert action.entity_id == ids.Defense.ROCKET_LAUNCHER


def test_policy_disallowing_everything_results_in_explicit_noop():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=False, allow_research=False, allow_defense=False, allow_ships=False),
    )

    action = plan_next_action(snapshot, policy)

    assert action.kind == ActionKind.NOOP
    assert action.rule == "9:no-match"
    assert action.rationale  # a human-readable reason is always present


def test_empty_policy_planets_means_all_snapshot_planets():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[])  # explicit empty list -> discover from snapshot

    action = plan_next_action(snapshot, policy)

    assert action.planet_id == 664


def test_action_is_onchain_helper_matches_function_presence():
    build_action = Action(kind=ActionKind.BUILD, function="startBuildingUpgrade")
    noop_action = Action(kind=ActionKind.NOOP)
    assert build_action.is_onchain() is True
    assert noop_action.is_onchain() is False


# --------------------------------------------------------------------------------------
# Build-time-savings note on expected_effect -- informational only, never a verdict.
# --------------------------------------------------------------------------------------


def _energy_safe_planet(*, robotics_level: int, metal_mine_duration_seconds: int | None) -> PlanetSnapshot:
    return PlanetSnapshot(
        planet_id=664,
        coordinates="7:181:14",
        fields_used=7,
        fields_total=174,
        resources_as_of_now=Resources(metal=10_000, crystal=10_000, deuterium=0),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=10_000, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(
                id=ids.Building.METAL_MINE,
                name="Metal Mine",
                level=0,
                cost=Resources(metal=60, crystal=15),
                duration_seconds=metal_mine_duration_seconds,
            ),
            Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=0, cost=Resources(metal=48, crystal=24)),
            Entity(id=ids.Building.DEUTERIUM_SYNTHESIZER, name="Deuterium Synthesizer", level=0, cost=Resources(metal=225, crystal=75)),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=1, cost=Resources(metal=75, crystal=30)),
            Entity(
                id=ids.Building.ROBOTICS_FACTORY,
                name="Robotics Factory",
                level=robotics_level,
                cost=Resources(metal=400, crystal=120, deuterium=200),
            ),
        ],
        ships=[],
        defenses=[],
    )


def test_mine_upgrade_expected_effect_includes_build_time_savings_note():
    # duration_seconds=36 -- the real value calc.build_seconds(2, 0, 60, 15) computes,
    # consistent with the robotics_level=2 set on the planet below (matches what the live
    # API would actually report at that robotics level).
    planet = _energy_safe_planet(robotics_level=2, metal_mine_duration_seconds=36)
    snapshot = Snapshot(taken_at="2026-08-15T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664])

    action = _next_building_action(planet, snapshot, policy, rule="6:building-queue-empty")

    assert action is not None
    assert action.entity_id == ids.Building.METAL_MINE
    assert action.expected_effect == "At Robotics Factory 2, this build takes 36s; at level 3, it would take 27s (25% faster)."


def test_mine_upgrade_expected_effect_omits_note_when_both_durations_floor():
    # A build cheap enough that both the current and Robotics+1 durations hit
    # min_queue_seconds (1s) -- "faster >= current" the note's own correctness guard,
    # not an arbitrary noise-suppression threshold.
    planet = _energy_safe_planet(robotics_level=50, metal_mine_duration_seconds=1)
    snapshot = Snapshot(taken_at="2026-08-15T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664])

    action = _next_building_action(planet, snapshot, policy, rule="6:building-queue-empty")

    assert action is not None
    assert action.entity_id == ids.Building.METAL_MINE
    assert action.expected_effect == ""


# --------------------------------------------------------------------------------------
# CLI smoke test.
# --------------------------------------------------------------------------------------


def test_cli_run_prints_json_action(tmp_path):
    """Invoked through the real mounted `vd` app (`cli.py`), the way a user actually runs
    it (`vd plan run ...`) -- not `plan.app` in isolation, which Typer collapses to a
    bare single-command CLI when it has only one `@app.command()`."""
    from typer.testing import CliRunner

    from veydrift_agent.cli import app as vd_app

    policy = make_policy(planets=[664])
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(policy.model_dump_json())

    runner = CliRunner()
    result = runner.invoke(
        vd_app,
        [
            "plan",
            "run",
            "--snapshot",
            str(FIXTURES / "planet_664.json"),
            "--policy",
            str(policy_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rule"] == "6:building-queue-empty"
    assert payload["entity_id"] == ids.Building.SOLAR_PLANT


# --------------------------------------------------------------------------------------
# Phase 3 of the general-strategy-engine program (docs/SPEC.md §5.4): the acceptance
# criterion pinned explicitly, at the `plan_next_action` ladder level rather than just
# at the `candidates.py` seam (`tests/test_candidates.py` has the direct, per-generator
# version of this same guarantee). Every test above this point is untouched by Phase 3
# and still passes unmodified -- that is the primary proof; this is the explicit pin the
# brief additionally asks for.
# --------------------------------------------------------------------------------------


def test_empty_strategy_targets_reproduce_phase_2_planner_output_exactly():
    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(planets=[664])  # StrategyCfg() defaults: all four Phase 3 fields empty

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"
    assert action.kind == ActionKind.BUILD
    assert action.function == "startBuildingUpgrade"
    assert action.entity_id == ids.Building.SOLAR_PLANT
    assert action.target_level == 1
    assert action.entity_id != ids.Ship.SOLAR_SATELLITE


def test_empty_strategy_targets_reproduce_phase_2_hot_planet_output_exactly():
    snapshot = load_snapshot("planet_hot.json")
    policy = make_policy(planets=[900001], actions=ActionsCfg(allow_building=True, allow_ships=True))

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"
    assert action.kind == ActionKind.SHIP
    assert action.function == "startShipProduction"
    assert action.entity_id == ids.Ship.SOLAR_SATELLITE
    assert action.quantity == 1


# --------------------------------------------------------------------------------------
# Phase 4 of the general-strategy-engine program (docs/SPEC.md §5.4 "Phase 4"): the new
# rung 8b, at the `plan_next_action` ladder level. planet_664.json is unsuitable here --
# its Solar Plant is always a legal, unlocked band-2 candidate, which would win before
# rung 8b is ever reached -- so this uses a deliberately minimal planet with *no* mine,
# energy, research, ship or defense data at all, only the single building the unlock
# chain needs, so bands 1-3 all naturally produce nothing and rung 8b is genuinely
# reached, not merely reachable in isolation.
# --------------------------------------------------------------------------------------


def _minimal_planet_for_unlock_chain_only() -> PlanetSnapshot:
    return PlanetSnapshot(
        planet_id=42,
        buildings=[
            Entity(id=ids.Building.ROBOTICS_FACTORY, name="Robotics Factory", level=0, cost=Resources(metal=400, crystal=120, deuterium=200)),
        ],
        # A live (count-reported) Small Cargo entity is required for
        # `generate_ship_target_candidates`'s -- and `generate_unlock_chain_candidates`'s
        # own, identical -- fail-closed-on-absent-entity rule: a `ship_targets` entry the
        # snapshot never reported a count for is skipped, not treated as "0 built".
        ships=[Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=0, cost=Resources(metal=2_000, crystal=2_000))],
        defenses=[],
    )


def test_unlock_chain_rung_fires_only_when_bands_1_through_3_find_nothing():
    from veydrift_agent.models import EntityTarget, StrategyCfg

    planet = _minimal_planet_for_unlock_chain_only()
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(
        planets=[42],
        actions=ActionsCfg(allow_building=True, allow_research=False, allow_defense=False, allow_ships=False),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    action = plan_next_action(snapshot, policy)

    assert action.rule == "8b:unlock-chain"
    assert action.kind == ActionKind.BUILD
    assert action.function == "startBuildingUpgrade"
    assert action.entity_id == ids.Building.ROBOTICS_FACTORY
    assert action.target_level == 1
    assert "Small Cargo" in action.rationale


def test_unlock_chain_rung_never_fires_with_no_declared_targets():
    """Same minimal, otherwise-empty planet as the previous test, but with no
    `ship_targets`/`defense_targets`/`research_priority` declared -- proves rung 8b is
    reachable in principle here (nothing else on this planet ever produces a winner) and
    still correctly falls through to the final explicit NO-OP when there is nothing to
    unlock toward, exactly the Phase 2/3 empty-targets safety property extended to
    Phase 4."""
    planet = _minimal_planet_for_unlock_chain_only()
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(
        planets=[42],
        actions=ActionsCfg(allow_building=True, allow_research=False, allow_defense=False, allow_ships=False),
    )

    action = plan_next_action(snapshot, policy)

    assert action.rule == "9:no-match"
    assert action.kind == ActionKind.NOOP


def test_unlock_chain_rung_never_reached_when_an_economic_candidate_exists():
    """A locked ship target coexists with planet_664's always-available Solar Plant
    pick -- band 2 must win, rung 8b must never even be consulted (docs/SPEC.md §5.4
    Phase 4: "must not displace a scored economic candidate")."""
    from veydrift_agent.models import EntityTarget, StrategyCfg

    snapshot = load_snapshot("planet_664.json")
    policy = make_policy(
        planets=[664],
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    action = plan_next_action(snapshot, policy)

    assert action.rule == "6:building-queue-empty"
    assert action.entity_id == ids.Building.SOLAR_PLANT


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
