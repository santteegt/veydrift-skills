"""Shared snapshot/policy builders for the jev engine tests (copied from `test_pool.py`, which
keeps its own): a rich two-planet account on which every candidate family has something to
say, plus the caller-supplied target kwargs (`target_kwargs`) for the fleet families."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from veydrift_agent import guard, ids
from veydrift_agent.models import (
    ActionsCfg,
    EnergyBalance,
    Entity,
    EntityTarget,
    GameMaintenance,
    Limits,
    PlanetSnapshot,
    Policy,
    QueueKind,
    RandomnessReadiness,
    Resources,
    Snapshot,
    StorageCfg,
    StrategyCfg,
    Tier,
)

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
