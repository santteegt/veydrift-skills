"""Tests for `candidates.collect_pool` -- the every-band candidate pool a comparing decision
engine (`jev_engine.py`) chooses from, in place of the ladder's early-return chain.

The property tests at the bottom are the ones that matter: they hold the pool to `guard.py`
itself (a pooled entry must never be something the guard BLOCKs for a snapshot-knowable
reason) and to the ladder (what the ladder would pick is in the pool, or was refused for a
reason that mirrors a guard BLOCK).
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from veydrift_agent import candidates, guard, ids
from veydrift_agent.candidates import PoolEntry, collect_pool
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    EnergyBalance,
    Entity,
    EntityTarget,
    GameMaintenance,
    GuardStatus,
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
    Tier,
)
from veydrift_agent.plan import _target_planets, plan_next_action
from veydrift_agent.state import AgentState

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
LIVE_ADDR = "0xf397910F005151b09644228573a4353818D3755d"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Builders.
# --------------------------------------------------------------------------------------


def load_snapshot(name: str) -> Snapshot:
    return Snapshot.model_validate(json.loads((FIXTURES / name).read_text()))


def make_policy(**overrides) -> Policy:
    """Everything the pool can act on is switched on; individual tests switch things off."""
    base = {
        "wallet": WALLET,
        "planets": [],
        "limits": Limits(
            gas_per_tx_wei=3_000_000_000_000_000,
            gas_per_day_wei=20_000_000_000_000_000,
            eth_gas_floor_wei=2_000_000_000_000_000,
        ),
        "actions": ActionsCfg(
            allow_building=True,
            allow_research=True,
            allow_defense=True,
            allow_ships=True,
            allow_fleet_noncombat=True,
            allow_combat=True,
        ),
        "storage": StorageCfg(hours_to_cap_trigger=2.0),
        "tier": Tier.OPERATOR,
    }
    base.update(overrides)
    return Policy(**base)


def _set(entities: list[Entity], entity_id: int, **updates) -> None:
    for index, entity in enumerate(entities):
        if entity.id == entity_id:
            entities[index] = entity.model_copy(update=updates)
            return
    raise AssertionError(f"entity {entity_id} not in list")


def rich_planet(planet_id: int = 664, coordinates: str = "7:181:14") -> PlanetSnapshot:
    """`planet_664.json`'s planet with enough built that every generator has something to say:
    mines/solar/infrastructure/storage levels, an idle shipyard with ships and a missile, deep
    holdings, a healthy energy balance and free fields."""
    planet = load_snapshot("planet_664.json").planet(664)
    assert planet is not None
    planet = planet.model_copy(deep=True)
    for building_id, level in {
        ids.Building.METAL_MINE: 3,
        ids.Building.CRYSTAL_MINE: 3,
        ids.Building.DEUTERIUM_SYNTHESIZER: 2,
        ids.Building.SOLAR_PLANT: 6,
        ids.Building.ROBOTICS_FACTORY: 3,
        ids.Building.SHIPYARD: 8,
        ids.Building.RESEARCH_LAB: 6,
        ids.Building.METAL_STORAGE: 1,
        ids.Building.CRYSTAL_STORAGE: 1,
        ids.Building.DEUTERIUM_TANK: 1,
        ids.Building.MISSILE_SILO: 3,
    }.items():
        _set(planet.buildings, building_id, level=level)
    for ship_id, count in {
        ids.Ship.SMALL_CARGO: 5,
        ids.Ship.RECYCLER: 2,
        ids.Ship.COLONY_SHIP: 1,
        ids.Ship.CRUISER: 3,
    }.items():
        _set(planet.ships, ship_id, count=count)
    _set(planet.defenses, ids.Defense.INTERPLANETARY_MISSILE, count=3)
    return planet.model_copy(
        update={
            "planet_id": planet_id,
            "coordinates": coordinates,
            "fields_used": 10,
            "fields_total": 100,
            "resources": Resources(metal=5_000_000, crystal=5_000_000, deuterium=5_000_000),
            "resources_as_of_now": Resources(metal=5_000_000, crystal=5_000_000, deuterium=5_000_000),
            "storage_caps": Resources(metal=10_000_000, crystal=10_000_000, deuterium=10_000_000),
            "production_per_hour": Resources(metal=1_000, crystal=500, deuterium=200),
            "energy": EnergyBalance(produced=1_000, required=100, scale_bps=10_000, solar_satellite_energy=4),
            "queues": {kind: None for kind in QueueKind},
            "missile_silo_level": 3,
        }
    )


def rich_snapshot(*planets: PlanetSnapshot) -> Snapshot:
    base = load_snapshot("planet_664.json")
    technologies = [t.model_copy(update={"level": 2}) for t in base.technologies]
    _set(technologies, ids.Technology.IMPULSE_DRIVE, level=3)
    _set(technologies, ids.Technology.ASTROPHYSICS, level=3)
    chosen = list(planets) or [rich_planet(), rich_planet(665, "7:182:3")]
    return base.model_copy(
        update={
            "taken_at": NOW,
            "health_ok": True,
            "readiness_ready": True,
            "game_maintenance": GameMaintenance(paused=False),
            "randomness_readiness": RandomnessReadiness(ready=True),
            "technologies": technologies,
            "research_lab_level": 6,
            "research_queue": None,
            "fleet_slots_active": 0,
            "fleet_slots_limit": 3,
            "owned_planet_count": len(chosen),
            "planets": chosen,
            "incoming_fleets": [],
            "deployment_abi_hash": guard.PINNED_ABI_HASH,
        }
    )


def rich_strategy(**overrides) -> StrategyCfg:
    base = {
        "colonize": True,
        "enable_crawler": True,
        "building_priority": ["Robotics Factory"],
        "ship_targets": [EntityTarget(name="Light Fighter", count=3)],
        "defense_targets": [EntityTarget(name="Rocket Launcher", count=3)],
        "fleet_home_planet_id": 665,
    }
    base.update(overrides)
    return StrategyCfg(**base)


def target_kwargs() -> dict:
    """One target of each caller-supplied kind, all reachable from planet 664."""
    return {
        "own_planet_debris": {664: Resources(metal=500, crystal=300)},
        "foreign_debris_targets": {9001: ("7:183:4", Resources(metal=800, crystal=400))},
        "colonize_targets": [("7:181:9", 10_000)],
        "attack_targets": {7001: ("7:185:2", Resources(metal=5_000, crystal=2_000, deuterium=500), True)},
        "missile_targets": {7002: ("7:184:5", {ids.Defense.ROCKET_LAUNCHER: 5}, True)},
    }


def pool_of(snapshot: Snapshot, policy: Policy, **kwargs) -> tuple[list[PoolEntry], dict[str, int]]:
    return collect_pool(snapshot, policy, _target_planets(snapshot, policy), **kwargs)


def families(pool: list[PoolEntry]) -> set[str]:
    return {e.candidate.family for e in pool}


def find(pool: list[PoolEntry], *, function: str | None = None, entity_id: int | None = None, planet_id: int | None = None):
    return [
        e
        for e in pool
        if (function is None or e.candidate.action.function == function)
        and (entity_id is None or e.candidate.action.entity_id == entity_id)
        and (planet_id is None or e.candidate.action.planet_id == planet_id)
    ]


def action_kinds(pool: list[PoolEntry]) -> set[ActionKind]:
    return {e.candidate.action.kind for e in pool}


# --------------------------------------------------------------------------------------
# The shape of the maps, and a smoke test that the rich fixture really exercises every family.
# --------------------------------------------------------------------------------------


def test_the_rich_fixture_exercises_every_band_the_pool_spans():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy())

    pool, _ = pool_of(snapshot, policy, **target_kwargs(), high_stakes_only_when_idle=False, max_candidates=200)

    assert families(pool) >= {
        "mine",
        "energy",
        "storage",
        "infrastructure",
        "research",
        "ship",
        "defense",
        "logistics-transport",
        "logistics-deploy",
        "logistics-harvest",
        "logistics-harvest-foreign",
        "colonize",
        "attack",
        "missile",
    }
    for entry in pool:
        assert entry.band == candidates.BAND_BY_FAMILY[entry.candidate.family]
        assert entry.group == candidates.GROUP_BY_FAMILY[entry.candidate.family]


def test_pool_is_ordered_by_band_then_generation_index():
    snapshot = rich_snapshot()
    pool, _ = pool_of(snapshot, make_policy(strategy=rich_strategy()), **target_kwargs(), max_candidates=200)
    assert [(e.band, e.index) for e in pool] == sorted((e.band, e.index) for e in pool)
    assert len({e.index for e in pool}) == len(pool)


def test_pool_entry_keys_are_unique():
    snapshot = rich_snapshot()
    pool, _ = pool_of(snapshot, make_policy(strategy=rich_strategy()), **target_kwargs(), max_candidates=200)
    assert len({e.key for e in pool}) == len(pool)


def test_locked_candidates_are_counted_and_never_pooled():
    snapshot = load_snapshot("planet_664.json")  # a fresh planet: nearly everything is locked
    pool, rejected = pool_of(snapshot, make_policy(strategy=rich_strategy(fleet_home_planet_id=None)))
    assert rejected.get("locked", 0) > 0
    assert all(not e.candidate.score_basis.startswith("locked:") for e in pool)


# --------------------------------------------------------------------------------------
# Non-selectable candidates: the allow_ships=false Solar Satellite leak and the capped Crawler.
# --------------------------------------------------------------------------------------


def test_allow_ships_false_satellite_is_never_pooled():
    snapshot = rich_snapshot()
    policy = make_policy(
        actions=ActionsCfg(allow_building=True, allow_research=True, allow_ships=False, allow_defense=False),
        strategy=StrategyCfg(),
    )

    # The leak is real: the energy generator still emits the satellite, tagged with the constant.
    leaked = [
        c
        for c in candidates.generate_energy_candidates(snapshot, policy, snapshot.planets[0])
        if c.action.kind is ActionKind.SHIP
    ]
    assert leaked and all(c.score_basis == candidates.ALLOW_SHIPS_FALSE_BASIS for c in leaked)

    pool, rejected = pool_of(snapshot, policy)

    assert ActionKind.SHIP not in action_kinds(pool)
    assert rejected.get("allow_flag", 0) >= 1


def test_the_satellite_is_pooled_once_ships_are_allowed():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=StrategyCfg())
    pool, _ = pool_of(snapshot, policy)
    satellites = find(pool, function="startShipProduction", entity_id=ids.Ship.SOLAR_SATELLITE, planet_id=664)
    assert len(satellites) == 1
    assert satellites[0].candidate.family == "energy"  # the lower band wins the dedup


def test_capped_crawler_is_never_pooled():
    from veydrift_agent.models import CrawlerProduction

    snapshot = rich_snapshot()
    snapshot = snapshot.model_copy(
        update={
            "planets": [
                p.model_copy(
                    update={
                        "crawler_production": CrawlerProduction(
                            total=10, effective=10, max_effective=10, boost_bps=100, capped=True
                        )
                    }
                )
                for p in snapshot.planets
            ]
        }
    )
    # Unlock the crawler so the generator reaches its cap branch.
    for planet in snapshot.planets:
        _set(planet.buildings, ids.Building.SHIPYARD, level=12)
    for tech in snapshot.technologies:
        tech.level = 12
    policy = make_policy(strategy=StrategyCfg(enable_crawler=True))

    generated = candidates.generate_crawler_candidates(snapshot, policy, snapshot.planets[0])
    assert generated and generated[0].score_basis.startswith(candidates.CRAWLER_AT_CAP_BASIS_PREFIX)

    pool, rejected = pool_of(snapshot, policy)

    assert not find(pool, entity_id=ids.Ship.CRAWLER)
    assert rejected.get("non_selectable", 0) >= 1


# --------------------------------------------------------------------------------------
# Queues.
# --------------------------------------------------------------------------------------


def _busy(kind: QueueKind, entity_id: int, name: str) -> QueueEntry:
    return QueueEntry(kind=kind, entity_id=entity_id, entity_name=name)


def test_busy_building_queue_excludes_build_for_that_planet_only():
    snapshot = rich_snapshot()
    busy = snapshot.planets[0].model_copy(
        update={
            "queues": {
                **snapshot.planets[0].queues,
                QueueKind.BUILDING: _busy(QueueKind.BUILDING, ids.Building.METAL_MINE, "Metal Mine"),
            }
        }
    )
    snapshot = snapshot.model_copy(update={"planets": [busy, snapshot.planets[1]]})
    policy = make_policy(strategy=rich_strategy())

    pool, rejected = pool_of(snapshot, policy, max_candidates=200)

    builds = [e for e in pool if e.candidate.action.kind is ActionKind.BUILD]
    assert builds, "the other planet's queue is idle"
    assert {e.candidate.action.planet_id for e in builds} == {665}
    assert rejected.get("queue_busy", 0) >= 1


def test_busy_research_queue_excludes_every_research_action():
    snapshot = rich_snapshot().model_copy(
        update={"research_queue": _busy(QueueKind.RESEARCH, ids.Technology.ENERGY, "Energy Technology")}
    )
    policy = make_policy(strategy=rich_strategy())

    pool, rejected = pool_of(snapshot, policy, max_candidates=200)

    assert ActionKind.RESEARCH not in action_kinds(pool)
    assert "research" not in families(pool)
    assert rejected.get("queue_busy", 0) >= 1


def test_busy_ship_lane_excludes_ships_and_batches_but_not_defense():
    snapshot = rich_snapshot()
    planets = [
        p.model_copy(update={"queues": {**p.queues, QueueKind.SHIP: _busy(QueueKind.SHIP, 1, "Light Fighter")}})
        for p in snapshot.planets
    ]
    snapshot = snapshot.model_copy(update={"planets": planets})
    policy = make_policy(strategy=rich_strategy(production_batch=True))

    pool, _ = pool_of(snapshot, policy, max_candidates=200)

    assert ActionKind.SHIP not in action_kinds(pool)
    assert ActionKind.DEFENSE in action_kinds(pool)
    assert ActionKind.PRODUCTION_BATCH not in action_kinds(pool)  # a batch of one lane is not a batch


def test_queues_idle_treats_a_batch_as_needing_every_lane_it_orders_from():
    from veydrift_agent.models import ProductionOrder

    snapshot = rich_snapshot()
    planet = snapshot.planets[0]
    batch = Action(
        kind=ActionKind.PRODUCTION_BATCH,
        function="startProductionBatch",
        planet_id=planet.planet_id,
        orders=[
            ProductionOrder(kind="ship", item_id=ids.Ship.LIGHT_FIGHTER, quantity=1),
            ProductionOrder(kind="defense", item_id=ids.Defense.ROCKET_LAUNCHER, quantity=1),
        ],
    )
    assert candidates._queues_idle(batch, snapshot, planet)
    defense_busy = planet.model_copy(
        update={"queues": {**planet.queues, QueueKind.DEFENSE: _busy(QueueKind.DEFENSE, 0, "Rocket Launcher")}}
    )
    assert not candidates._queues_idle(batch, snapshot, defense_busy)
    ship_busy = planet.model_copy(
        update={"queues": {**planet.queues, QueueKind.SHIP: _busy(QueueKind.SHIP, 1, "Light Fighter")}}
    )
    assert not candidates._queues_idle(batch, snapshot, ship_busy)


# --------------------------------------------------------------------------------------
# Energy-first, affordability, reserves, storage caps, fields, energy data, fleet slots.
# --------------------------------------------------------------------------------------


def test_energy_unsafe_mine_is_never_pooled_but_its_energy_substitute_is():
    snapshot = rich_snapshot()
    starved = [
        p.model_copy(update={"energy": EnergyBalance(produced=0, required=0, scale_bps=10_000, solar_satellite_energy=4)})
        for p in snapshot.planets
    ]
    snapshot = snapshot.model_copy(update={"planets": starved})

    pool, _ = pool_of(snapshot, make_policy(strategy=StrategyCfg()), max_candidates=200)

    assert "mine" not in families(pool)
    assert "energy" in families(pool)


def _poor_snapshot(metal: int, crystal: int, deuterium: int) -> Snapshot:
    snapshot = rich_snapshot()
    holdings = Resources(metal=metal, crystal=crystal, deuterium=deuterium)
    planets = [p.model_copy(update={"resources_as_of_now": holdings, "resources": holdings}) for p in snapshot.planets]
    return snapshot.model_copy(update={"planets": planets})


def test_unaffordable_candidates_are_excluded_and_cheap_ones_stay():
    # Robotics Factory costs 400 metal / 120 crystal / 200 deuterium; a Metal Mine costs 60 / 15.
    snapshot = _poor_snapshot(300, 300, 300)

    pool, rejected = pool_of(snapshot, make_policy(strategy=rich_strategy(fleet_home_planet_id=None)), max_candidates=200)

    assert not find(pool, entity_id=ids.Building.ROBOTICS_FACTORY, function="startBuildingUpgrade")
    assert find(pool, entity_id=ids.Building.METAL_MINE, function="startBuildingUpgrade")
    assert rejected.get("unaffordable", 0) >= 1


def test_reserve_breaching_candidates_are_excluded():
    snapshot = _poor_snapshot(300, 300, 300)
    # 300 - 60 (Metal Mine) = 240 < 280: every metal spend breaches the floor, though each is affordable.
    policy = make_policy(strategy=StrategyCfg(), reserves=Resources(metal=280))

    pool, rejected = pool_of(snapshot, policy, max_candidates=200)

    assert not find(pool, entity_id=ids.Building.METAL_MINE, function="startBuildingUpgrade")
    assert rejected.get("reserve", 0) >= 1
    for entry in pool:
        spend = entry.candidate.action.cost
        assert 300 - spend.metal >= 280


def test_holdings_already_below_a_reserve_floor_pool_nothing():
    """`guard._gate_reserve` BLOCKs every action when holdings sit below a floor, spend or not."""
    snapshot = _poor_snapshot(100, 100, 100)
    pool, rejected = pool_of(snapshot, make_policy(strategy=StrategyCfg(), reserves=Resources(metal=500)))
    assert pool == []
    assert rejected.get("reserve", 0) >= 1


def test_storage_capped_cost_is_excluded_while_the_storage_upgrade_itself_stays():
    snapshot = rich_snapshot()
    capped = [p.model_copy(update={"storage_caps": Resources(metal=100, crystal=10_000_000, deuterium=10_000_000)}) for p in snapshot.planets]
    snapshot = snapshot.model_copy(update={"planets": capped})
    # Robotics Factory costs 400 metal (> the 100 cap): it can never be saved up to. Metal
    # Storage costs 1000 metal, also over the cap, and is the remedy, so it must stay.
    policy = make_policy(strategy=rich_strategy(fleet_home_planet_id=None))

    pool, rejected = pool_of(snapshot, policy, max_candidates=200)

    assert not find(pool, entity_id=ids.Building.ROBOTICS_FACTORY, function="startBuildingUpgrade")
    assert find(pool, entity_id=ids.Building.METAL_STORAGE, function="startBuildingUpgrade")
    assert find(pool, entity_id=ids.Building.METAL_MINE, function="startBuildingUpgrade")  # 60 metal fits
    assert rejected.get("storage_cap", 0) >= 1


def test_a_planet_without_field_data_is_excluded_and_the_other_stays():
    snapshot = rich_snapshot()
    blind = snapshot.planets[0].model_copy(update={"fields_used": None, "fields_total": None})
    snapshot = snapshot.model_copy(update={"planets": [blind, snapshot.planets[1]]})

    pool, rejected = pool_of(snapshot, make_policy(strategy=rich_strategy()), max_candidates=200)

    on_blind = [e for e in pool if e.candidate.action.planet_id == 664]
    assert not on_blind
    assert any(e.candidate.action.planet_id == 665 for e in pool)
    assert rejected.get("fields", 0) >= 1


def test_a_planet_with_every_field_used_is_excluded():
    snapshot = rich_snapshot()
    full = snapshot.planets[0].model_copy(update={"fields_used": 100, "fields_total": 100})
    snapshot = snapshot.model_copy(update={"planets": [full, snapshot.planets[1]]})
    pool, _ = pool_of(snapshot, make_policy(strategy=StrategyCfg()), max_candidates=200)
    assert not [e for e in pool if e.candidate.action.planet_id == 664]


def test_a_planet_without_an_energy_balance_is_excluded():
    snapshot = rich_snapshot()
    blind = snapshot.planets[0].model_copy(update={"energy": None})
    snapshot = snapshot.model_copy(update={"planets": [blind, snapshot.planets[1]]})

    pool, rejected = pool_of(snapshot, make_policy(strategy=StrategyCfg()), max_candidates=200)

    assert not [e for e in pool if e.candidate.action.planet_id == 664]
    assert rejected.get("energy_unknown", 0) >= 1


@pytest.mark.parametrize(
    "active,limit",
    [(None, 3), (0, None), (3, 3), (5, 3)],
    ids=["active-unknown", "limit-unknown", "no-free-slot", "over-limit"],
)
def test_fleet_missions_need_a_known_free_slot_but_missiles_do_not(active, limit):
    snapshot = rich_snapshot().model_copy(update={"fleet_slots_active": active, "fleet_slots_limit": limit})
    policy = make_policy(strategy=rich_strategy())

    pool, rejected = pool_of(snapshot, policy, **target_kwargs(), high_stakes_only_when_idle=False, max_candidates=200)

    assert ActionKind.FLEET_MISSION not in action_kinds(pool)
    assert ActionKind.MISSILE_ATTACK in action_kinds(pool)
    assert rejected.get("fleet_slots", 0) >= 1


def test_a_free_slot_keeps_fleet_missions():
    snapshot = rich_snapshot().model_copy(update={"fleet_slots_active": 2, "fleet_slots_limit": 3})
    pool, _ = pool_of(snapshot, make_policy(strategy=rich_strategy()), **target_kwargs(), max_candidates=200)
    assert ActionKind.FLEET_MISSION in action_kinds(pool)


# --------------------------------------------------------------------------------------
# The allow flag, per action kind, checked directly.
# --------------------------------------------------------------------------------------


def _fleet(mission: int) -> Action:
    return Action(kind=ActionKind.FLEET_MISSION, function="launchFleetMission", planet_id=664, mission_type=mission)


@pytest.mark.parametrize(
    "action,flag",
    [
        (Action(kind=ActionKind.BUILD, function="startBuildingUpgrade", planet_id=664), "allow_building"),
        (Action(kind=ActionKind.RESEARCH, function="startResearch", planet_id=664), "allow_research"),
        (Action(kind=ActionKind.SHIP, function="startShipProduction", planet_id=664), "allow_ships"),
        (Action(kind=ActionKind.DEFENSE, function="startDefenseProduction", planet_id=664), "allow_defense"),
        (_fleet(ids.FleetMissionType.TRANSPORT), "allow_fleet_noncombat"),
        (_fleet(ids.FleetMissionType.DEPLOY), "allow_fleet_noncombat"),
        (_fleet(ids.FleetMissionType.HARVEST), "allow_fleet_noncombat"),
        (_fleet(ids.FleetMissionType.ATTACK), "allow_combat"),
        (Action(kind=ActionKind.MISSILE_ATTACK, function="launchInterplanetaryMissileAttack", planet_id=664), "allow_combat"),
    ],
    ids=lambda v: v.function if isinstance(v, Action) else v,
)
def test_each_action_kind_needs_exactly_its_own_flag(action, flag):
    all_off = ActionsCfg(
        allow_building=False,
        allow_research=False,
        allow_defense=False,
        allow_ships=False,
        allow_fleet_noncombat=False,
        allow_combat=False,
    )
    assert not candidates._allow_flag_ok(action, make_policy(actions=all_off))
    for name in ("allow_building", "allow_research", "allow_defense", "allow_ships", "allow_fleet_noncombat", "allow_combat"):
        policy = make_policy(actions=all_off.model_copy(update={name: True}))
        assert candidates._allow_flag_ok(action, policy) is (name == flag), name


def test_colonize_needs_the_strategy_flag_and_no_actions_flag():
    action = _fleet(ids.FleetMissionType.COLONIZE)
    all_off = ActionsCfg(allow_building=False, allow_research=False, allow_fleet_noncombat=False, allow_combat=False)
    assert not candidates._allow_flag_ok(action, make_policy(actions=all_off, strategy=StrategyCfg(colonize=False)))
    assert candidates._allow_flag_ok(action, make_policy(actions=all_off, strategy=StrategyCfg(colonize=True)))


def test_a_batch_needs_the_flag_of_every_kind_it_orders_and_an_unknown_kind_is_refused():
    from veydrift_agent.models import ProductionOrder

    ships_only = ActionsCfg(allow_ships=True, allow_defense=False)
    batch = Action(
        kind=ActionKind.PRODUCTION_BATCH,
        function="startProductionBatch",
        planet_id=664,
        orders=[
            ProductionOrder(kind="ship", item_id=ids.Ship.LIGHT_FIGHTER, quantity=1),
            ProductionOrder(kind="defense", item_id=ids.Defense.ROCKET_LAUNCHER, quantity=1),
        ],
    )
    assert not candidates._allow_flag_ok(batch, make_policy(actions=ships_only))
    assert candidates._allow_flag_ok(batch, make_policy(actions=ActionsCfg(allow_ships=True, allow_defense=True)))
    empty = batch.model_copy(update={"orders": []})
    assert not candidates._allow_flag_ok(empty, make_policy())
    assert not candidates._allow_flag_ok(_fleet(ids.FleetMissionType.ACS_DEFEND), make_policy())
    assert not candidates._allow_flag_ok(Action(kind=ActionKind.NOOP), make_policy())


# --------------------------------------------------------------------------------------
# Batch vs scored single, high-stakes-only-when-idle, dedup, pre-trim.
# --------------------------------------------------------------------------------------


def test_a_batch_is_dropped_on_a_planet_that_has_a_scored_single_ship_and_kept_elsewhere(monkeypatch):
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy(production_batch=True))

    baseline, _ = pool_of(snapshot, policy, max_candidates=200)
    assert {e.candidate.action.planet_id for e in baseline if e.candidate.family == "batch"} == {664, 665}

    real = candidates.generate_ship_candidates

    def scored_on_664(snap, pol, planet):
        generated = real(snap, pol, planet)
        if planet.planet_id != 664:
            return generated
        return [candidates.Candidate(c.action, c.family, 5.0, "scored for the test") for c in generated]

    monkeypatch.setattr(candidates, "generate_ship_candidates", scored_on_664)

    pool, rejected = pool_of(snapshot, policy, max_candidates=200)

    assert {e.candidate.action.planet_id for e in pool if e.candidate.family == "batch"} == {665}
    assert rejected["batch_vs_scored_single"] == 1
    assert find(pool, function="startShipProduction", planet_id=664)  # the scored single itself stays


def test_a_batch_carries_its_production_spend_from_live_unit_costs():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy(production_batch=True))
    pool, _ = pool_of(snapshot, policy, max_candidates=200)
    batch = next(e for e in pool if e.candidate.family == "batch")
    assert len(batch.candidate.action.orders) >= 2
    assert guard.production_spend(batch.candidate.action, snapshot) is not None


def test_high_stakes_families_are_pooled_only_when_nothing_else_is():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy())

    busy, rejected = pool_of(snapshot, policy, **target_kwargs(), max_candidates=200)
    assert not (families(busy) & candidates.HIGH_STAKES_FAMILIES)
    assert rejected.get("high_stakes_not_idle", 0) >= 1

    knob_off, rejected_off = pool_of(snapshot, policy, **target_kwargs(), high_stakes_only_when_idle=False, max_candidates=200)
    assert candidates.HIGH_STAKES_FAMILIES <= families(knob_off)
    assert "high_stakes_not_idle" not in rejected_off


def test_high_stakes_families_survive_when_the_pool_is_otherwise_empty():
    snapshot = rich_snapshot()
    only_high_stakes = make_policy(
        actions=ActionsCfg(
            allow_building=False,
            allow_research=False,
            allow_ships=False,
            allow_defense=False,
            allow_fleet_noncombat=False,
            allow_combat=True,
        ),
        strategy=StrategyCfg(colonize=True),
    )
    for knob in (True, False):
        pool, rejected = pool_of(snapshot, only_high_stakes, **target_kwargs(), high_stakes_only_when_idle=knob)
        assert families(pool) == candidates.HIGH_STAKES_FAMILIES, knob
        assert "high_stakes_not_idle" not in rejected


def test_high_stakes_only_ever_needs_one_ordinary_survivor_to_be_dropped():
    snapshot = rich_snapshot()
    one_ordinary = make_policy(
        actions=ActionsCfg(
            allow_building=False,
            allow_research=True,
            allow_ships=False,
            allow_defense=False,
            allow_fleet_noncombat=False,
            allow_combat=True,
        ),
        strategy=StrategyCfg(colonize=True),
    )
    pool, rejected = pool_of(snapshot, one_ordinary, **target_kwargs())
    assert families(pool) == {"research"}
    assert rejected["high_stakes_not_idle"] >= 3


def _energy_research_action(planet_id: int, snapshot: Snapshot) -> Action:
    tech = next(t for t in snapshot.technologies if t.id == ids.Technology.ENERGY)
    return Action(
        kind=ActionKind.RESEARCH,
        function="startResearch",
        planet_id=planet_id,
        entity_id=tech.id,
        entity_name=tech.name,
        target_level=(tech.level or 0) + 1,
        cost=tech.cost,
    )


def test_research_dedups_by_technology_and_prefers_the_planet_research_goes_through(monkeypatch):
    snapshot = rich_snapshot()
    # Research is submitted through target_planets[0], which is 665 here.
    policy = make_policy(planets=[665, 664], strategy=StrategyCfg())

    def unlock_on_the_other_planet(snap, pol, planet):
        other = 664 if planet.planet_id == 665 else 665
        return [
            candidates.Candidate(_energy_research_action(other, snap), "unlock", None, "unlock step for the test")
        ]

    monkeypatch.setattr(candidates, "generate_unlock_chain_candidates", unlock_on_the_other_planet)
    monkeypatch.setattr(candidates, "generate_research_candidates", lambda *a, **k: [])

    pool, rejected = pool_of(snapshot, policy)

    energy = [e for e in pool if e.candidate.action.kind is ActionKind.RESEARCH]
    assert len(energy) == 1
    assert energy[0].candidate.action.planet_id == 665
    assert rejected["duplicate"] == 1


def test_research_keeps_the_lower_band_when_both_instances_are_on_the_research_planet(monkeypatch):
    snapshot = rich_snapshot()
    policy = make_policy(planets=[664, 665], strategy=StrategyCfg())
    monkeypatch.setattr(
        candidates,
        "generate_unlock_chain_candidates",
        lambda snap, pol, planet: (
            [candidates.Candidate(_energy_research_action(664, snap), "unlock", None, "unlock step for the test")]
            if planet.planet_id == 664
            else []
        ),
    )

    pool, _ = pool_of(snapshot, policy)

    energy = [e for e in pool if e.candidate.action.kind is ActionKind.RESEARCH and e.candidate.action.entity_id == ids.Technology.ENERGY]
    assert [e.candidate.family for e in energy] == ["research"]


def test_dedup_keeps_the_lowest_band_then_the_lowest_index(monkeypatch):
    snapshot = rich_snapshot(rich_planet())
    policy = make_policy(strategy=StrategyCfg())
    base = candidates.generate_proactive_storage_candidates(snapshot, policy, snapshot.planets[0])[0]
    twin = candidates.Candidate(base.action, "unlock", None, "same action, later band")
    monkeypatch.setattr(candidates, "generate_unlock_chain_candidates", lambda *a, **k: [twin, twin])

    pool, rejected = pool_of(snapshot, policy)

    same = [e for e in pool if e.key == candidates.pool_key(base.action)]
    assert [e.candidate.family for e in same] == ["storage"]
    assert rejected["duplicate"] == 2


def _full_pool() -> list[PoolEntry]:
    pool, _ = pool_of(
        rich_snapshot(),
        make_policy(strategy=rich_strategy(production_batch=True)),
        **target_kwargs(),
        high_stakes_only_when_idle=False,
        max_candidates=500,
    )
    return pool


def test_pre_trim_is_deterministic():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy(production_batch=True))
    runs = [
        pool_of(snapshot, policy, **target_kwargs(), high_stakes_only_when_idle=False, max_candidates=16)
        for _ in range(3)
    ]
    keys = [[e.key for e in pool] for pool, _ in runs]
    assert keys[0] == keys[1] == keys[2]
    assert runs[0][1] == runs[1][1] == runs[2][1]


@pytest.mark.parametrize("cap", [15, 16, 20, 30])
def test_every_family_present_keeps_at_least_one_slot(cap):
    full = _full_pool()
    trimmed, rejected = pool_of(
        rich_snapshot(),
        make_policy(strategy=rich_strategy(production_batch=True)),
        **target_kwargs(),
        high_stakes_only_when_idle=False,
        max_candidates=cap,
    )
    assert len(families(full)) <= cap
    assert families(trimmed) == families(full)
    assert len(trimmed) <= cap
    assert rejected.get("pre_trim", 0) == len(full) - len(trimmed)


def test_when_families_outnumber_the_cap_the_lowest_bands_win():
    full = _full_pool()
    cap = 5
    trimmed, _ = pool_of(
        rich_snapshot(),
        make_policy(strategy=rich_strategy(production_batch=True)),
        **target_kwargs(),
        high_stakes_only_when_idle=False,
        max_candidates=cap,
    )
    expected = sorted(families(full), key=lambda f: (candidates.BAND_BY_FAMILY[f], f))[:cap]
    assert len(trimmed) == cap
    assert families(trimmed) == set(expected)


def test_the_best_ranked_entry_of_a_family_takes_its_only_slot():
    full = _full_pool()
    cap = len(families(full))
    trimmed, _ = pool_of(
        rich_snapshot(),
        make_policy(strategy=rich_strategy(production_batch=True)),
        **target_kwargs(),
        high_stakes_only_when_idle=False,
        max_candidates=cap,
    )
    assert len(trimmed) == cap
    mines = [e for e in full if e.candidate.family == "mine"]
    best = candidates.rank_candidates([e.candidate for e in mines])[0]
    kept_mine = next(e for e in trimmed if e.candidate.family == "mine")
    assert kept_mine.candidate.score == best.score


def test_a_pool_under_the_cap_is_returned_untrimmed():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy())
    full, rejected = pool_of(snapshot, policy, **target_kwargs(), max_candidates=500)
    assert "pre_trim" not in rejected
    again, _ = pool_of(snapshot, policy, **target_kwargs(), max_candidates=len(full))
    assert [e.key for e in again] == [e.key for e in full]


def test_the_pool_never_mutates_the_snapshot_or_policy():
    snapshot = rich_snapshot()
    policy = make_policy(strategy=rich_strategy(production_batch=True))
    before = (snapshot.model_dump_json(), policy.model_dump_json())
    pool_of(snapshot, policy, **target_kwargs())
    assert (snapshot.model_dump_json(), policy.model_dump_json()) == before


def test_no_target_planets_is_an_empty_pool():
    snapshot = rich_snapshot()
    pool, rejected = collect_pool(snapshot, make_policy(), [])
    assert pool == []
    assert rejected == {}


# --------------------------------------------------------------------------------------
# Properties. Every fixture x a policy matrix; the pool is held to `guard.py` and to the ladder.
# --------------------------------------------------------------------------------------

_FLAG_NAMES = ("allow_building", "allow_research", "allow_ships", "allow_defense", "allow_fleet_noncombat", "allow_combat")


def _flag_combos() -> list[dict[str, bool]]:
    combos = [dict.fromkeys(_FLAG_NAMES, True), dict.fromkeys(_FLAG_NAMES, False)]
    for name in _FLAG_NAMES:
        combos.append({**dict.fromkeys(_FLAG_NAMES, True), name: False})
        combos.append({**dict.fromkeys(_FLAG_NAMES, False), name: True})
    combos.append(dict(zip(_FLAG_NAMES, itertools.cycle([True, False]), strict=False)))
    combos.append(dict(zip(_FLAG_NAMES, itertools.cycle([False, True]), strict=False)))
    return combos


def _policy_matrix() -> list[Policy]:
    """Flags on/off x strategy (empty, or every declared target plus batching) x reserves,
    always at the economy tier the guard checks below run at."""
    strategies = [StrategyCfg(), rich_strategy(production_batch=True)]
    reserves = [Resources(), Resources(metal=200, crystal=100, deuterium=50)]
    return [
        make_policy(actions=ActionsCfg(**flags), strategy=strategy, reserves=reserve, tier=Tier.ECONOMY)
        for flags, strategy, reserve in itertools.product(_flag_combos(), strategies, reserves)
    ]


def _with_planets(snapshot: Snapshot, *, only_first: bool = False, **updates) -> Snapshot:
    """`updates` applied to every planet, or (`only_first`) just the first, leaving the rest healthy."""
    planets = [p.model_copy(update=updates) if (not only_first or i == 0) else p for i, p in enumerate(snapshot.planets)]
    return snapshot.model_copy(update={"planets": planets})


def _variants() -> dict[str, tuple[Snapshot, dict]]:
    rich = rich_snapshot()
    one_planet = rich_snapshot(rich_planet())
    busy_building = rich.model_copy(
        update={
            "planets": [
                p.model_copy(
                    update={"queues": {**p.queues, QueueKind.BUILDING: _busy(QueueKind.BUILDING, 0, "Metal Mine")}}
                )
                for p in rich.planets[:1]
            ]
            + rich.planets[1:]
        }
    )
    return {
        "planet_664": (load_snapshot("planet_664.json"), {}),
        "planet_hot": (load_snapshot("planet_hot.json"), {}),
        "rich": (rich, target_kwargs()),
        "rich-one-planet": (one_planet, target_kwargs()),
        "rich-poor": (_poor_snapshot(300, 300, 300), target_kwargs()),
        "rich-busy-research": (
            rich.model_copy(update={"research_queue": _busy(QueueKind.RESEARCH, 0, "Energy Technology")}),
            target_kwargs(),
        ),
        "rich-busy-building": (busy_building, target_kwargs()),
        "rich-no-fleet-slots": (rich.model_copy(update={"fleet_slots_active": 3, "fleet_slots_limit": 3}), target_kwargs()),
        "rich-unknown-slots": (rich.model_copy(update={"fleet_slots_active": None}), target_kwargs()),
        "rich-full-fields": (_with_planets(rich, only_first=True, fields_used=100), target_kwargs()),
        "rich-no-energy-data": (_with_planets(rich, only_first=True, energy=None), target_kwargs()),
        "rich-starved-energy": (
            _with_planets(rich, energy=EnergyBalance(produced=0, required=0, scale_bps=10_000, solar_satellite_energy=4)),
            target_kwargs(),
        ),
    }


_VARIANTS = _variants()

#: The gates whose BLOCK the pool promises a pooled action never triggers -- everything a
#: snapshot alone can decide. (`value_ceiling` only BLOCKs on an unverifiable spend; its
#: ESCALATE for a large spend is a fact for the caller, deliberately not a pool filter.)
_SNAPSHOT_GATES = (
    "prerequisites",
    "energy",
    "affordability",
    "reserve",
    "fields",
    "production_batch",
    "mission_type",
    "fleet_slots",
    "missile_target",
    "value_ceiling",
)


def _generated(snapshot: Snapshot, policy: Policy, kwargs: dict) -> list[candidates.Candidate]:
    """Every candidate the pool's generators emit, called straight from the public generators."""
    targets = _target_planets(snapshot, policy)
    out: list[candidates.Candidate] = []
    for planet in targets:
        out += candidates.generate_mine_candidates(snapshot, policy, planet)
        out += candidates.generate_energy_candidates(snapshot, policy, planet)
        out += candidates.generate_proactive_storage_candidates(snapshot, policy, planet)
        out += candidates.generate_infrastructure_candidates(snapshot, policy, planet)
        out += candidates.generate_ship_candidates(snapshot, policy, planet)
        out += candidates.generate_defense_candidates(snapshot, policy, planet)
        out += candidates.generate_production_batch_candidates(snapshot, policy, planet)
        out += candidates.generate_unlock_chain_candidates(snapshot, policy, planet)
        out += candidates.generate_transport_candidates(snapshot, policy, planet, targets)
        out += candidates.generate_deploy_candidates(snapshot, policy, planet)
        out += candidates.generate_harvest_candidates(snapshot, policy, planet, own_planet_debris=kwargs.get("own_planet_debris"))
        out += candidates.generate_foreign_harvest_candidates(
            snapshot, policy, planet, foreign_debris_targets=kwargs.get("foreign_debris_targets")
        )
        out += candidates.generate_colonize_candidates(snapshot, policy, planet, colonize_targets=kwargs.get("colonize_targets"))
        out += candidates.generate_attack_candidates(snapshot, policy, planet, attack_targets=kwargs.get("attack_targets"))
        out += candidates.generate_missile_candidates(snapshot, policy, planet, missile_targets=kwargs.get("missile_targets"))
    out += candidates.generate_research_candidates(snapshot, policy, targets)
    return out


def _identity(action: Action) -> tuple:
    """`pool_key`, except that research is one technology whichever planet it is filed under."""
    key = candidates.pool_key(action)
    return (key[0], None, *key[2:]) if action.kind is ActionKind.RESEARCH else key


def _flag_allows(action: Action, policy: Policy) -> bool:
    """The allow flag for `action`, written out separately from the pool's own mapping."""
    actions = policy.actions
    if action.kind is ActionKind.BUILD:
        return actions.allow_building
    if action.kind is ActionKind.RESEARCH:
        return actions.allow_research
    if action.kind is ActionKind.SHIP:
        return actions.allow_ships
    if action.kind is ActionKind.DEFENSE:
        return actions.allow_defense
    if action.kind is ActionKind.PRODUCTION_BATCH:
        return all(actions.allow_ships if o.kind == "ship" else actions.allow_defense for o in action.orders)
    if action.kind is ActionKind.MISSILE_ATTACK:
        return actions.allow_combat
    assert action.kind is ActionKind.FLEET_MISSION
    if action.mission_type == ids.FleetMissionType.ATTACK:
        return actions.allow_combat
    if action.mission_type == ids.FleetMissionType.COLONIZE:
        return policy.strategy.colonize
    return actions.allow_fleet_noncombat


def _lanes_idle(action: Action, snapshot: Snapshot) -> bool:
    planet = snapshot.planet(action.planet_id)
    assert planet is not None
    if action.kind is ActionKind.BUILD:
        return planet.queues.get(QueueKind.BUILDING) is None
    if action.kind is ActionKind.RESEARCH:
        return snapshot.research_queue is None
    if action.kind is ActionKind.SHIP:
        return planet.queues.get(QueueKind.SHIP) is None
    if action.kind is ActionKind.DEFENSE:
        return planet.queues.get(QueueKind.DEFENSE) is None
    if action.kind is ActionKind.PRODUCTION_BATCH:
        return all(
            planet.queues.get(QueueKind.SHIP if o.kind == "ship" else QueueKind.DEFENSE) is None for o in action.orders
        )
    return True


def _guard_report(action: Action, snapshot: Snapshot, policy: Policy):
    """`guard.evaluate_guardrails` with every live-only input stubbed to a passing value, so a
    BLOCK can only come from the snapshot and the policy."""
    from veydrift_agent.models import OnchainPin, UnsignedTx

    tx = UnsignedTx(
        to=LIVE_ADDR,
        data="0x165715e3" + "00" * 64,
        gas=100_000,
        onchain_pin=OnchainPin(
            ok=True,
            dependencies_ok=True,
            applies_to=[
                "launchFleetMission",
                "launchInterplanetaryMissileAttack",
                "resolveFleetMission",
                "launchDefenseHold",
            ],
        ),
    )
    return guard.evaluate_guardrails(
        action,
        snapshot,
        policy,
        AgentState(),
        now=NOW,
        live_addresses={LIVE_ADDR},
        unsigned_tx=tx,
        gas_cost_wei=500_000,
        eth_balance_wei=5_000_000_000_000_000,
        outgoing_colonize_count=0,
        attack_protection_allowed=True,
    )


@pytest.mark.parametrize("name", list(_VARIANTS))
def test_property_every_pooled_entry_is_generated_legal_and_never_blocked_by_the_snapshot_gates(name):
    snapshot, kwargs = _VARIANTS[name]
    checked = 0
    for policy in _policy_matrix():
        pool, _ = pool_of(snapshot, policy, **kwargs, high_stakes_only_when_idle=False, max_candidates=500)
        generated = {(c.family, candidates.pool_key(c.action)) for c in _generated(snapshot, policy, kwargs)}
        for entry in pool:
            action = entry.candidate.action
            label = f"{name}: {entry.candidate.family} {action.function} planet {action.planet_id} {action.entity_name}"

            # (a) it came out of a generator, unaltered.
            assert (entry.candidate.family, entry.key) in generated, label
            assert entry.key == candidates.pool_key(action), label

            # (b) its allow flag is on and the queues it needs are idle.
            assert _flag_allows(action, policy), label
            assert _lanes_idle(action, snapshot), label
            assert candidates.is_non_selectable(entry.candidate) is None, label

            # (c) no snapshot-decidable guard gate BLOCKs it.
            report = _guard_report(action, snapshot, policy)
            blocked = {v.gate: v.detail for v in report.verdicts if v.gate in _SNAPSHOT_GATES and v.status is GuardStatus.BLOCK}
            assert not blocked, f"{label}: {blocked}"
            checked += 1
    assert checked > 0, f"{name}: no policy in the matrix pooled anything -- the property was vacuous"


#: Pool rejection reason -> the guard gate whose BLOCK it mirrors. A ladder winner the pool
#: refuses for one of these is one the guard would have BLOCKed anyway.
_MIRRORED_BY_GUARD = {
    "unaffordable": "affordability",
    "spend_unverifiable": "affordability",
    "reserve": "reserve",
    "fields": "fields",
    "energy_unknown": "energy",
    "fleet_slots": "fleet_slots",
}


@pytest.mark.parametrize("name", list(_VARIANTS))
def test_property_the_ladders_band_2_to_8_winner_is_pooled_or_refused_as_a_guard_would(name):
    snapshot, kwargs = _VARIANTS[name]
    winners = 0
    for policy in _policy_matrix():
        action = plan_next_action(snapshot, policy, **kwargs)
        if not action.is_onchain() or not action.rule.startswith(("6:", "7:", "8")):
            continue  # a veto, the storage deadline, or nothing to do: not the pool's business
        winners += 1
        label = f"{name}: rule {action.rule} {action.function} planet {action.planet_id} {action.entity_name} flags={policy.actions}"

        pool, _ = pool_of(snapshot, policy, **kwargs, high_stakes_only_when_idle=False, max_candidates=500)
        if _identity(action) in {_identity(e.candidate.action) for e in pool}:
            continue

        matching = [c for c in _generated(snapshot, policy, kwargs) if _identity(c.action) == _identity(action)]
        assert matching, f"{label}: the ladder chose something no pool generator emits"
        reasons = {candidates._rejection_reason(c, snapshot, policy) for c in matching}
        reasons.discard(None)
        assert reasons, f"{label}: generated, filtered by nothing, yet absent from the pool"
        if reasons == {"non_selectable"}:
            # A Crawler already at its boost cap: the ladder still proposes it, the pool will not.
            assert action.entity_id == ids.Ship.CRAWLER, label
            continue
        for reason in reasons:
            assert reason in _MIRRORED_BY_GUARD, f"{label}: refused as {reason!r}, which no guard gate mirrors"
            report = _guard_report(action, snapshot, policy)
            gate = next(v for v in report.verdicts if v.gate == _MIRRORED_BY_GUARD[reason])
            assert gate.status is GuardStatus.BLOCK, f"{label}: pool says {reason!r} but guard {gate.gate} is {gate.status}: {gate.detail}"
    assert winners > 0, f"{name}: the ladder never produced a band 2-8 action -- the property was vacuous"
