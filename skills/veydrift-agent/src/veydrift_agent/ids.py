"""Canonical enum <-> name maps for Veydrift's six on-chain enums.

**Source of truth**: the deployed contract, not `docs.md`, not the OGame convention.
Repo `/Users/santteegt/GitRepositories/clones/veydrift`, commit
`701bed3578cff4d134657c714c599dbdb55a4b6a` (the *deployed* commit; `main` has drifted —
see `docs/RESEARCH-ADDENDUM.md` §1.1). Every enum below was read with
``git show 701bed35…:<path>`` against that exact commit, not inferred or probed.

Two things prior docs (`docs/NOTES.md`, `veydrift-agent-resources.md`) get wrong that this
module must not reproduce:

1. **`Defense` is not in OGame order.** ``SmallShieldDome`` is id 3 and ``GaussCannon`` is
   id 4 — the shield dome sorts *before* the cannon. ``IonCannon`` is id 5.
2. **Ship id 11 is `Deathstar` in the enum**, but every rapidfire table and prior doc calls
   it `Dreadstar`. Both names resolve to id 11 here; the canonical display name is
   `"Deathstar"` because that is what the contract calls it.

No network calls, no cost math — this module is pure data.
"""

from __future__ import annotations

from enum import IntEnum

# --------------------------------------------------------------------------------------
# Enums — member order is the contract's declaration order, which IS the on-chain id.
# --------------------------------------------------------------------------------------


class Building(IntEnum):
    """packages/contracts/src/libraries/VeydriftTypes.sol:4-21 (commit 701bed35)."""

    METAL_MINE = 0
    CRYSTAL_MINE = 1
    DEUTERIUM_SYNTHESIZER = 2
    SOLAR_PLANT = 3
    ROBOTICS_FACTORY = 4
    SHIPYARD = 5
    RESEARCH_LAB = 6
    METAL_STORAGE = 7
    CRYSTAL_STORAGE = 8
    DEUTERIUM_TANK = 9
    FUSION_REACTOR = 10
    NANITE_FACTORY = 11
    TERRAFORMER = 12
    ALLIANCE_DEPOT = 13
    MISSILE_SILO = 14
    #: Contract name is `InterdimensionalRiftStabilizer`; docs/NOTES.md call it
    #: "Rift Stabilizer". Hard-capped at level 1 (NOTES.md §13.5). Mechanics unpublished.
    RIFT_STABILIZER = 15


class Technology(IntEnum):
    """packages/contracts/src/libraries/VeydriftTypes.sol:62-78 (commit 701bed35).

    Not the docs' table order — Impulse Drive is id 9, after the combat techs
    (docs/NOTES.md §2).
    """

    ENERGY = 0
    LASER = 1
    ION = 2
    COMBUSTION_DRIVE = 3
    COMPUTER = 4
    WEAPONS = 5
    SHIELDING = 6
    ARMOR = 7
    HYPERSPACE = 8
    IMPULSE_DRIVE = 9
    HYPERSPACE_DRIVE = 10
    PLASMA = 11
    ASTROPHYSICS = 12
    INTERGALACTIC_RESEARCH_NETWORK = 13
    GRAVITON = 14


class Ship(IntEnum):
    """packages/contracts/src/libraries/VeydriftTypes.sol:43-60 (commit 701bed35).

    Id 11 is `Deathstar` in the enum. Rapidfire tables in `docs.md` and prior notes call
    the same unit "Dreadstar" — both names resolve to 11 via :data:`SHIP_IDS`.

    This is **not** the 14-slot fleet-mission tuple order. SolarSatellite (9) and Crawler
    (15) cannot fly (`VeydriftFleetFuel.sol:73-87`) and are omitted from that tuple, which
    shifts ids 10-15 down by one slot. See `references/entity-ids.md` §4 for the tuple
    table; the conversion function itself is `veydrift-wallet`'s
    `shipCountsToFleetTuple()` (TypeScript, WP4a) — not reimplemented here.
    """

    SMALL_CARGO = 0
    LIGHT_FIGHTER = 1
    RECYCLER = 2
    COLONY_SHIP = 3
    LARGE_CARGO = 4
    HEAVY_FIGHTER = 5
    CRUISER = 6
    BATTLESHIP = 7
    BOMBER = 8
    SOLAR_SATELLITE = 9
    DESTROYER = 10
    DEATHSTAR = 11
    BATTLECRUISER = 12
    REAPER = 13
    PATHFINDER = 14
    CRAWLER = 15


class Defense(IntEnum):
    """packages/contracts/src/libraries/VeydriftTypes.sol:30-41 (commit 701bed35).

    **Not OGame order.** ``SmallShieldDome`` (3) sorts before ``GaussCannon`` (4), and
    ``IonCannon`` is 5 — docs/RESEARCH-ADDENDUM.md §3 flags this explicitly as a place
    prior docs got it wrong.
    """

    ROCKET_LAUNCHER = 0
    LIGHT_LASER = 1
    HEAVY_LASER = 2
    SMALL_SHIELD_DOME = 3
    GAUSS_CANNON = 4
    ION_CANNON = 5
    PLASMA_TURRET = 6
    LARGE_SHIELD_DOME = 7
    ANTI_BALLISTIC_MISSILE = 8
    INTERPLANETARY_MISSILE = 9


class FleetMissionType(IntEnum):
    """packages/contracts/src/VeydriftGameStorage.sol:166-177 (commit 701bed35).

    `Intercept` and `DefenseHold` appear in neither `docs.md` nor the prior notes
    (docs/RESEARCH-ADDENDUM.md §3). Combat mission types (Attack=3, AcsAttack=8,
    MissileAttack=7, Intercept=6) are unreachable from this codebase at every tier
    (docs/SPEC.md §4) — they are listed here only so the enum is complete and auditable.
    """

    TRANSPORT = 0
    DEPLOY = 1
    COLONIZE = 2
    ATTACK = 3
    HARVEST = 4
    ACS_DEFEND = 5
    INTERCEPT = 6
    MISSILE_ATTACK = 7
    ACS_ATTACK = 8
    DEFENSE_HOLD = 9


class Resource(IntEnum):
    """packages/contracts/src/libraries/VeydriftTypes.sol:80-85 (commit 701bed35).

    The `uint8` used by `depositMarketResource`, `requestMarketResourceWithdrawal`,
    `finishMarketResourceWithdrawal` (docs/RESEARCH-ADDENDUM.md §3).
    """

    METAL = 0
    CRYSTAL = 1
    DEUTERIUM = 2
    ENERGY = 3


# --------------------------------------------------------------------------------------
# Display names. These are what `read.py` should use to populate `Entity.name` — the
# live API never sends entity names, only bare ids (verified by probing /infrastructure,
# /research, /shipyard, /defenses on 2026-08-12; see docs/RESEARCH-ADDENDUM.md §2).
# --------------------------------------------------------------------------------------

BUILDING_NAMES: dict[int, str] = {
    Building.METAL_MINE: "Metal Mine",
    Building.CRYSTAL_MINE: "Crystal Mine",
    Building.DEUTERIUM_SYNTHESIZER: "Deuterium Synthesizer",
    Building.SOLAR_PLANT: "Solar Plant",
    Building.ROBOTICS_FACTORY: "Robotics Factory",
    Building.SHIPYARD: "Shipyard",
    Building.RESEARCH_LAB: "Research Lab",
    Building.METAL_STORAGE: "Metal Storage",
    Building.CRYSTAL_STORAGE: "Crystal Storage",
    Building.DEUTERIUM_TANK: "Deuterium Tank",
    Building.FUSION_REACTOR: "Fusion Reactor",
    Building.NANITE_FACTORY: "Nanite Factory",
    Building.TERRAFORMER: "Terraformer",
    Building.ALLIANCE_DEPOT: "Alliance Depot",
    Building.MISSILE_SILO: "Missile Silo",
    Building.RIFT_STABILIZER: "Rift Stabilizer",
}

TECHNOLOGY_NAMES: dict[int, str] = {
    Technology.ENERGY: "Energy Technology",
    Technology.LASER: "Laser Technology",
    Technology.ION: "Ion Technology",
    Technology.COMBUSTION_DRIVE: "Combustion Drive",
    Technology.COMPUTER: "Computer Technology",
    Technology.WEAPONS: "Weapons Technology",
    Technology.SHIELDING: "Shielding Technology",
    Technology.ARMOR: "Armor Technology",
    Technology.HYPERSPACE: "Hyperspace Technology",
    Technology.IMPULSE_DRIVE: "Impulse Drive",
    Technology.HYPERSPACE_DRIVE: "Hyperspace Drive",
    Technology.PLASMA: "Plasma Technology",
    Technology.ASTROPHYSICS: "Astrophysics",
    Technology.INTERGALACTIC_RESEARCH_NETWORK: "Intergalactic Research Network",
    Technology.GRAVITON: "Graviton Technology",
}

#: Canonical display name is "Deathstar" — that is the contract's enum member name.
#: "Dreadstar" is accepted as an alias everywhere names are looked up (see SHIP_IDS).
SHIP_NAMES: dict[int, str] = {
    Ship.SMALL_CARGO: "Small Cargo",
    Ship.LIGHT_FIGHTER: "Light Fighter",
    Ship.RECYCLER: "Recycler",
    Ship.COLONY_SHIP: "Colony Ship",
    Ship.LARGE_CARGO: "Large Cargo",
    Ship.HEAVY_FIGHTER: "Heavy Fighter",
    Ship.CRUISER: "Cruiser",
    Ship.BATTLESHIP: "Battleship",
    Ship.BOMBER: "Bomber",
    Ship.SOLAR_SATELLITE: "Solar Satellite",
    Ship.DESTROYER: "Destroyer",
    Ship.DEATHSTAR: "Deathstar",
    Ship.BATTLECRUISER: "Battlecruiser",
    Ship.REAPER: "Reaper",
    Ship.PATHFINDER: "Pathfinder",
    Ship.CRAWLER: "Crawler",
}

DEFENSE_NAMES: dict[int, str] = {
    Defense.ROCKET_LAUNCHER: "Rocket Launcher",
    Defense.LIGHT_LASER: "Light Laser",
    Defense.HEAVY_LASER: "Heavy Laser",
    Defense.SMALL_SHIELD_DOME: "Small Shield Dome",
    Defense.GAUSS_CANNON: "Gauss Cannon",
    Defense.ION_CANNON: "Ion Cannon",
    Defense.PLASMA_TURRET: "Plasma Turret",
    Defense.LARGE_SHIELD_DOME: "Large Shield Dome",
    Defense.ANTI_BALLISTIC_MISSILE: "Anti-Ballistic Missile",
    Defense.INTERPLANETARY_MISSILE: "Interplanetary Missile",
}

FLEET_MISSION_TYPE_NAMES: dict[int, str] = {
    FleetMissionType.TRANSPORT: "Transport",
    FleetMissionType.DEPLOY: "Deploy",
    FleetMissionType.COLONIZE: "Colonize",
    FleetMissionType.ATTACK: "Attack",
    FleetMissionType.HARVEST: "Harvest",
    FleetMissionType.ACS_DEFEND: "ACS Defend",
    FleetMissionType.INTERCEPT: "Intercept",
    FleetMissionType.MISSILE_ATTACK: "Missile Attack",
    FleetMissionType.ACS_ATTACK: "ACS Attack",
    FleetMissionType.DEFENSE_HOLD: "Defense Hold",
}

RESOURCE_NAMES: dict[int, str] = {
    Resource.METAL: "Metal",
    Resource.CRYSTAL: "Crystal",
    Resource.DEUTERIUM: "Deuterium",
    Resource.ENERGY: "Energy",
}

#: Aliases accepted in *lookup* (name -> id) beyond the canonical display name above.
#: Only Ship needs one today (Deathstar/Dreadstar); the dict stays generic for future use.
_SHIP_ALIASES: dict[str, int] = {"dreadstar": Ship.DEATHSTAR}


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().replace("_", " ").replace("-", " ").split())


def _name_to_id_map(names: dict[int, str], aliases: dict[str, int] | None = None) -> dict[str, int]:
    out = {_normalize(name): id_ for id_, name in names.items()}
    if aliases:
        for alias, id_ in aliases.items():
            out[_normalize(alias)] = id_
    return out


BUILDING_IDS: dict[str, int] = _name_to_id_map(BUILDING_NAMES)
TECHNOLOGY_IDS: dict[str, int] = _name_to_id_map(TECHNOLOGY_NAMES)
SHIP_IDS: dict[str, int] = _name_to_id_map(SHIP_NAMES, _SHIP_ALIASES)
DEFENSE_IDS: dict[str, int] = _name_to_id_map(DEFENSE_NAMES)
FLEET_MISSION_TYPE_IDS: dict[str, int] = _name_to_id_map(FLEET_MISSION_TYPE_NAMES)
RESOURCE_IDS: dict[str, int] = _name_to_id_map(RESOURCE_NAMES)


def building_name(id_: int) -> str:
    return BUILDING_NAMES.get(id_, f"Building#{id_}")


def technology_name(id_: int) -> str:
    return TECHNOLOGY_NAMES.get(id_, f"Technology#{id_}")


def ship_name(id_: int) -> str:
    return SHIP_NAMES.get(id_, f"Ship#{id_}")


def defense_name(id_: int) -> str:
    return DEFENSE_NAMES.get(id_, f"Defense#{id_}")


def mission_type_name(id_: int) -> str:
    return FLEET_MISSION_TYPE_NAMES.get(id_, f"FleetMissionType#{id_}")


def resource_name(id_: int) -> str:
    return RESOURCE_NAMES.get(id_, f"Resource#{id_}")


def building_id(name: str) -> int:
    return BUILDING_IDS[_normalize(name)]


def technology_id(name: str) -> int:
    return TECHNOLOGY_IDS[_normalize(name)]


def ship_id(name: str) -> int:
    """Accepts either "Deathstar" (canonical) or "Dreadstar" (alias used in rapidfire
    tables and prior docs) — both resolve to id 11."""
    return SHIP_IDS[_normalize(name)]


def defense_id(name: str) -> int:
    return DEFENSE_IDS[_normalize(name)]


def mission_type_id(name: str) -> int:
    return FLEET_MISSION_TYPE_IDS[_normalize(name)]


def resource_id(name: str) -> int:
    return RESOURCE_IDS[_normalize(name)]


# --------------------------------------------------------------------------------------
# The 14-slot fleet-mission tuple order — documentation only (docs/RESEARCH-ADDENDUM.md
# §3). Every fleet entrypoint (`launchFleetMission`, etc.) takes `uint32[14]`, not the
# 16-member Ship enum: SolarSatellite (9) and Crawler (15) cannot fly
# (`VeydriftFleetFuel.sol:73-87`, `_missionShipQuantity`) and are omitted, so tuple slots
# 9-13 hold Ship ids 10-14 — a silent off-by-one if you index by Ship id directly.
# The conversion itself belongs to veydrift-wallet (TypeScript, WP4a); this tuple is
# recorded here because it is enum-adjacent and this module owns the enums.
# --------------------------------------------------------------------------------------

FLEET_TUPLE_ORDER: tuple[Ship, ...] = (
    Ship.SMALL_CARGO,
    Ship.LIGHT_FIGHTER,
    Ship.RECYCLER,
    Ship.COLONY_SHIP,
    Ship.LARGE_CARGO,
    Ship.HEAVY_FIGHTER,
    Ship.CRUISER,
    Ship.BATTLESHIP,
    Ship.BOMBER,
    Ship.DESTROYER,
    Ship.DEATHSTAR,
    Ship.BATTLECRUISER,
    Ship.REAPER,
    Ship.PATHFINDER,
)

#: Ships that cannot fly and therefore have no fleet-tuple slot.
NON_FLYABLE_SHIPS: frozenset[Ship] = frozenset({Ship.SOLAR_SATELLITE, Ship.CRAWLER})
