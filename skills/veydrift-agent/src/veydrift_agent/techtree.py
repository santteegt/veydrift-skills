"""Veydrift's on-chain prerequisite (tech-tree) table. Pure data + pure functions — no
network, no I/O, no cost math (that stays live-API-only, per `calc.py`'s own hard
constraint; this module never scales a cost, only compares *levels*).

**Why this module exists.** Nothing in `plan.py` or `guard.py` previously checked a
prerequisite of any kind. `_next_research_action` picked the lowest-level technology
account-wide, which on a fresh planet is Energy (id 0) — but Energy requires Research Lab
level ≥ 1 (`VeydriftDependencies.sol:requireResearch` via
`VeydriftCatalog.researchLabRequirement`). On a fresh planet at tier 2 that is a
guaranteed on-chain revert, paid in real gas. The same hole let rung 8 propose a Rocket
Launcher on a planet with no Shipyard (`requireDefense`'s unconditional
`shipyardLevel == 0` revert). This module is the shared legality table both `plan.py`
(never propose a locked entity) and `guard.py` (independently re-check one) use.

**Source of truth**: the deployed contract, commit
`701bed3578cff4d134657c714c599dbdb55a4b6a`
(`/Users/santteegt/GitRepositories/clones/veydrift`; `main` has drifted — see
`docs/RESEARCH-ADDENDUM.md` §1.1). Every table below was read with
``git show 701bed35…:<path>`` against that exact commit, not inferred, not probed, and not
transcribed from `docs.md` (which does not publish this table at all).

Five things verified directly against source while transcribing (see the module test file
for the specific spot-checks pinned against each):

1. **The 9-argument `requireBuilding` overload is the one actually called.**
   `VeydriftDependencies.sol` declares two overloads (5-arg at :11-36, 9-arg at :38-89);
   `VeydriftGame.sol:799-811`'s `_requireBuildingDependencies` — the only call site reached
   from `startBuildingUpgrade` (`VeydriftGame.sol:150`) — calls the 9-arg one. The 5-arg
   overload is missing Fusion Reactor, Terraformer and Missile Silo entirely; transcribing
   it instead would silently under-constrain the table for exactly those three buildings.
2. **Every requirement in the source is a conjunction, never a disjunction.** The contract
   writes some checks as `if (ship == Reaper && (hyperspaceDriveLevel < 7 ||
   hyperspaceLevel < 6)) revert(...)` — but that `||` sits inside a *failure* condition:
   "revert if EITHER sub-check fails" is exactly "require BOTH to succeed," i.e. an AND of
   requirements. Confirmed by reading every `||` in `VeydriftDependencies.sol` (six of
   them, on Reaper, Crawler, Ion, Hyperspace, Plasma and IGRN) — all six are ORs of
   *failure* conditions inside a `revert`, none are a genuine "either path unlocks this"
   disjunction. A flat conjunctive tuple is therefore correct for the whole table.
3. **`requireDefense` opens with an unconditional `if (shipyardLevel == 0) revert`**
   (`VeydriftDependencies.sol:97-99`) — every defense id, including Rocket Launcher (which
   has no other explicit requirement), carries an implicit `Shipyard >= 1`. Modelled here
   as an explicit `Requirement` on every entry in `DEFENSE_REQUIREMENTS`, even when a
   higher explicit Shipyard requirement already dominates it (e.g. Light Laser also needs
   Shipyard >= 2) — redundant but a more faithful 1:1 transcription of the source's actual
   assertions than silently collapsing to "the higher one wins."
4. **`requireResearch` composes two things, both captured below**:
   `VeydriftCatalog.researchLabRequirement(tech)` (a per-technology base Research Lab
   level) *plus* the technology-specific extra conjuncts declared inline in
   `requireResearch` itself (e.g. Ion additionally needs Energy 4 and Laser 5). Missing
   either half would under- or over-constrain the research branch of the ladder.
5. **Graviton's only gate is a Research Lab level in this module.** Its *other* real
   requirement — `VeydriftCatalog.researchEnergyRequirement`, `300_000 * 3^currentLevel`,
   checked against the planet's live *produced* energy at
   `VeydriftPlanetManagementModule.sol:566-573` — is not a level comparison against any
   building/technology, so it cannot be a `Requirement` tuple entry. It is modelled
   separately below as :data:`GRAVITON_ENERGY_REQUIREMENT_BASE` /
   :data:`GRAVITON_ENERGY_REQUIREMENT_MULTIPLIER` and
   :func:`graviton_energy_requirement`, with a note that `unmet()` does **not** evaluate
   it — a caller checking Graviton eligibility must additionally compare
   `graviaton_energy_requirement(...)` against the planet's live produced energy itself.

**The absent-vs-zero distinction is the single highest-value invariant in this repo**
(`AGENTS.md` §5: "a guardrail must never pass vacuously on absent data"). `Mapping.get(id)`
returning `None` means the snapshot did not report a level for that building/technology —
categorically different from a level that is reported and genuinely `0`. `unmet()` treats
both as "not satisfied" (a locked entity stays locked either way), but records `have=None`
specifically for the absent case so callers — `plan.py` (skip silently, try the next
candidate) and `guard.py` (BLOCK, never PASS) — can tell the two apart in their own
response. `plan.py:75`'s existing `_level()` helper collapses absent to `0` for its
existing callers; this module does not reuse it and builds its own level vectors instead
(see `plan.py`'s `_level_vector()`), specifically to preserve that distinction.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import NamedTuple

from veydrift_agent import ids

# --------------------------------------------------------------------------------------
# Core types.
# --------------------------------------------------------------------------------------


class EntityFamily(str, Enum):
    """Which requirement table an entity id is looked up in."""

    BUILDING = "building"
    SHIP = "ship"
    DEFENSE = "defense"
    RESEARCH = "research"


class ReqSource(str, Enum):
    """What kind of level a `Requirement` compares against — a per-*planet* building
    level, or a per-*player* technology level. (Note: even though `startResearch` is a
    per-player action, its Research Lab prerequisite is read from the *specific planet*
    the transaction is submitted through — `_buildingLevels[planetId][ResearchLab]` in
    `VeydriftPlanetManagementModule.sol:558` — so `BUILDING` requirements are always
    planet-scoped, never account-wide, including inside `RESEARCH_REQUIREMENTS`.)"""

    BUILDING = "building"
    TECHNOLOGY = "technology"


class Requirement(NamedTuple):
    source: ReqSource
    entity_id: int
    min_level: int


class UnmetRequirement(NamedTuple):
    requirement: Requirement
    #: The level the snapshot reported for `requirement.entity_id`, or `None` if the
    #: snapshot did not report one at all (absent, not zero — see module docstring).
    have: int | None


# --------------------------------------------------------------------------------------
# Requirement tables. Each keyed by the contract's own enum id (`ids.Building` /
# `ids.Ship` / `ids.Defense` / `ids.Technology`). An id with no entry has no
# prerequisites in the source (e.g. Metal Mine, Solar Plant, Robotics Factory itself).
# --------------------------------------------------------------------------------------

#: `VeydriftDependencies.sol:38-89` (the 9-arg `requireBuilding` overload — see module
#: docstring point 1), called from `VeydriftGame.sol:799-811`.
BUILDING_REQUIREMENTS: dict[int, tuple[Requirement, ...]] = {
    ids.Building.SHIPYARD: (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 2),
    ),
    ids.Building.RESEARCH_LAB: (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 1),
    ),
    ids.Building.FUSION_REACTOR: (
        Requirement(ReqSource.BUILDING, ids.Building.DEUTERIUM_SYNTHESIZER, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 3),
    ),
    ids.Building.NANITE_FACTORY: (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMPUTER, 10),
    ),
    ids.Building.TERRAFORMER: (
        Requirement(ReqSource.BUILDING, ids.Building.NANITE_FACTORY, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 12),
    ),
    ids.Building.MISSILE_SILO: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
    ),
    ids.Building.RIFT_STABILIZER: (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 4),
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 1),
    ),
}

#: `VeydriftDependencies.sol:184-329` (`requireShip`), called from
#: `VeydriftShipProductionModule.sol:159` (`_validateShipProduction`).
SHIP_REQUIREMENTS: dict[int, tuple[Requirement, ...]] = {
    ids.Ship.SMALL_CARGO: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 2),
    ),
    ids.Ship.LIGHT_FIGHTER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 1),
    ),
    ids.Ship.RECYCLER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 2),
    ),
    ids.Ship.COLONY_SHIP: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 3),
    ),
    ids.Ship.LARGE_CARGO: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 6),
    ),
    ids.Ship.HEAVY_FIGHTER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 3),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ARMOR, 2),
    ),
    ids.Ship.CRUISER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ION, 2),
    ),
    ids.Ship.BATTLESHIP: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 4),
    ),
    ids.Ship.BOMBER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.PLASMA, 5),
    ),
    ids.Ship.SOLAR_SATELLITE: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
    ),
    ids.Ship.DESTROYER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 9),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 5),
    ),
    ids.Ship.DEATHSTAR: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 12),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.GRAVITON, 1),
    ),
    ids.Ship.BATTLECRUISER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 12),
    ),
    ids.Ship.REAPER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 5),
    ),
    ids.Ship.PATHFINDER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 4),
    ),
    ids.Ship.CRAWLER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ARMOR, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 4),
    ),
}

#: `VeydriftDependencies.sol:90-166` (`requireDefense`), called from
#: `VeydriftDefenseProductionModule.sol:336-350` (`_requireDefenseDependencies`). Every
#: entry starts with the unconditional `Shipyard >= 1` at :97-99 (module docstring point 3).
DEFENSE_REQUIREMENTS: dict[int, tuple[Requirement, ...]] = {
    ids.Defense.ROCKET_LAUNCHER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
    ),
    ids.Defense.LIGHT_LASER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 3),
    ),
    ids.Defense.HEAVY_LASER: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 3),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 6),
    ),
    ids.Defense.SMALL_SHIELD_DOME: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 2),
    ),
    ids.Defense.GAUSS_CANNON: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.WEAPONS, 3),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 1),
    ),
    ids.Defense.ION_CANNON: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ION, 4),
    ),
    ids.Defense.PLASMA_TURRET: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.PLASMA, 7),
    ),
    ids.Defense.LARGE_SHIELD_DOME: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 6),
    ),
    ids.Defense.ANTI_BALLISTIC_MISSILE: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.MISSILE_SILO, 2),
    ),
    ids.Defense.INTERPLANETARY_MISSILE: (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
        Requirement(ReqSource.BUILDING, ids.Building.MISSILE_SILO, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 1),
    ),
}

#: `VeydriftDependencies.sol:331-388` (`requireResearch`), called from
#: `VeydriftPlanetManagementModule.sol:550-575` (`_requireResearchDependencies`). Each
#: entry's first `Requirement` is `VeydriftCatalog.researchLabRequirement(tech)`
#: (`VeydriftCatalog.sol:442-458`); any further entries are the per-technology extra
#: conjuncts declared inline in `requireResearch` (module docstring point 4). Graviton's
#: additional energy-based gate is *not* here — see :func:`graviton_energy_requirement`.
RESEARCH_REQUIREMENTS: dict[int, tuple[Requirement, ...]] = {
    ids.Technology.ENERGY: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 1),
    ),
    ids.Technology.LASER: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 2),
    ),
    ids.Technology.ION: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 5),
    ),
    ids.Technology.COMBUSTION_DRIVE: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 1),
    ),
    ids.Technology.COMPUTER: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 1),
    ),
    ids.Technology.WEAPONS: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 4),
    ),
    ids.Technology.SHIELDING: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 3),
    ),
    ids.Technology.ARMOR: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 2),
    ),
    ids.Technology.HYPERSPACE: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 5),
    ),
    ids.Technology.IMPULSE_DRIVE: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 2),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 1),
    ),
    ids.Technology.HYPERSPACE_DRIVE: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 3),
    ),
    ids.Technology.PLASMA: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ION, 5),
    ),
    ids.Technology.ASTROPHYSICS: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 3),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.IMPULSE_DRIVE, 3),
    ),
    ids.Technology.INTERGALACTIC_RESEARCH_NETWORK: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMPUTER, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 8),
    ),
    ids.Technology.GRAVITON: (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 12),
    ),
}

_TABLES: dict[EntityFamily, dict[int, tuple[Requirement, ...]]] = {
    EntityFamily.BUILDING: BUILDING_REQUIREMENTS,
    EntityFamily.SHIP: SHIP_REQUIREMENTS,
    EntityFamily.DEFENSE: DEFENSE_REQUIREMENTS,
    EntityFamily.RESEARCH: RESEARCH_REQUIREMENTS,
}

# --------------------------------------------------------------------------------------
# Graviton's energy-based research gate. Not a `Requirement` (see module docstring
# point 5) -- `VeydriftCatalog.sol:429-441` (`researchEnergyRequirement`), enforced at
# `VeydriftPlanetManagementModule.sol:566-573` against the planet's live *produced*
# energy, not a building/technology level.
# --------------------------------------------------------------------------------------

GRAVITON_ENERGY_REQUIREMENT_BASE = 300_000
GRAVITON_ENERGY_REQUIREMENT_MULTIPLIER = 3


def graviton_energy_requirement(current_level: int) -> int:
    """`300_000 * 3^currentLevel` (`VeydriftCatalog.sol:429-441`). Zero for every other
    technology -- Graviton is the only one with a nonzero
    `researchEnergyRequirement`, confirmed by reading the whole function. Compare the
    result against the planet's live *produced* energy (`PlanetSnapshot.energy.produced`),
    never a recomputed one -- this is a levels/requirements module, not a cost/production
    one."""
    return GRAVITON_ENERGY_REQUIREMENT_BASE * (GRAVITON_ENERGY_REQUIREMENT_MULTIPLIER**current_level)


# --------------------------------------------------------------------------------------
# Hard caps -- also "the contract will revert" territory, but a *count* ceiling rather
# than a level prerequisite. `VeydriftCatalog.sol:239-241` (`maxDefensePerPlanet`),
# `:229-233` (`missileSlots`), `:235-237` (`missileSiloCapacity`); enforced at
# `VeydriftDefenseProductionModule.sol:352-380` (`_requireDefenseCapacity`).
#
# **The contract counts *queued* quantity toward both caps, not just already-built
# count** (`_queuedDefenseQuantity` / `_queuedMissileSiloSlots`, same file, :380-410) --
# a shield dome already in the production queue still blocks a second one from being
# queued, and a missile already queued still occupies its silo slots. `PlanetSnapshot`
# only carries a single `QueueEntry | None` per `QueueKind.DEFENSE` (no queue *backlog*
# is modelled -- `models.py` is frozen), so a caller checking these caps against a live
# snapshot can account for at most one queued item, not an arbitrarily deep backlog. Not
# a caller of this module's problem to solve, but worth stating plainly: a cap check built
# on `PlanetSnapshot` alone can under-count queued quantity if the real backlog is deeper
# than one entry.
# --------------------------------------------------------------------------------------

#: Defense ids hard-capped to a maximum count (built + queued) per planet. Any id absent
#: from this dict has no cap (`maxDefensePerPlanet` returns `type(uint32).max` for it).
MAX_DEFENSE_PER_PLANET: dict[int, int] = {
    ids.Defense.SMALL_SHIELD_DOME: 1,
    ids.Defense.LARGE_SHIELD_DOME: 1,
}

#: Missile silo slots consumed per unit. Ids absent from this dict consume 0 (every
#: non-missile defense).
MISSILE_SLOTS: dict[int, int] = {
    ids.Defense.ANTI_BALLISTIC_MISSILE: 1,
    ids.Defense.INTERPLANETARY_MISSILE: 2,
}


def missile_silo_capacity(level: int) -> int:
    """`missileSiloLevel * 10` (`VeydriftCatalog.sol:235-237`)."""
    return level * 10


# --------------------------------------------------------------------------------------
# The API.
# --------------------------------------------------------------------------------------


def unmet(
    family: EntityFamily,
    entity_id: int,
    *,
    building_levels: Mapping[int, int | None],
    technology_levels: Mapping[int, int | None],
) -> tuple[UnmetRequirement, ...]:
    """Every `Requirement` for `(family, entity_id)` that is not satisfied by
    `building_levels`/`technology_levels`.

    **Fails closed on absent data** — the highest-value invariant in this repo
    (`AGENTS.md` §5). `building_levels.get(id)` / `technology_levels.get(id)` returning
    `None` means the snapshot did not report a level for that id at all — categorically
    different from a level that was reported and is genuinely `0` — and is treated as
    *not satisfying* the requirement either way (an unreported level is never assumed
    high enough), while being recorded as `UnmetRequirement(have=None)` rather than
    `UnmetRequirement(have=0)` so a caller can tell the two apart. An entity with
    requirements and *any* missing input therefore never returns `()` — callers must not
    special-case "no verdicts because we don't know" as "must be fine."

    An entity id with no entry in the family's table (no prerequisites in the source, e.g.
    Metal Mine) always returns `()` — that is a genuine "nothing to check," not an absent-
    data case.

    **Only the strictest unmet requirement per target is returned.** The tables mirror the
    contract's checks verbatim, and the contract genuinely checks the same target twice in
    places — every `Defense` carries the blanket `if (shipyardLevel == 0) revert` *plus* its
    own `Shipyard >= N` (`VeydriftDependencies.sol:88-165`), so a Plasma Turret at Shipyard 0
    matches both `Shipyard >= 1` and `Shipyard >= 8`. Reporting both would render as
    `"needs Shipyard 1 (have 0); needs Shipyard 8 (have 0)"` — noise that reads like two
    separate problems. Satisfying the strictest satisfies the rest, so the weaker duplicate
    is dropped *here*, at the output, rather than by editing the tables away from their
    source. Deduplication never changes whether the result is empty, so the fail-closed
    guarantee above is untouched.
    """
    requirements = _TABLES[family].get(entity_id, ())
    #: (source, entity_id) -> the strictest unmet requirement seen for that target.
    strictest: dict[tuple[ReqSource, int], UnmetRequirement] = {}
    for requirement in requirements:
        levels = building_levels if requirement.source is ReqSource.BUILDING else technology_levels
        have = levels.get(requirement.entity_id)
        if have is None or have < requirement.min_level:
            key = (requirement.source, requirement.entity_id)
            previous = strictest.get(key)
            if previous is None or requirement.min_level > previous.requirement.min_level:
                strictest[key] = UnmetRequirement(requirement=requirement, have=have)
    return tuple(strictest.values())


def _requirement_name(requirement: Requirement) -> str:
    if requirement.source is ReqSource.BUILDING:
        return ids.building_name(requirement.entity_id)
    return ids.technology_name(requirement.entity_id)


def describe(unmet_requirement: UnmetRequirement) -> str:
    """`"needs Robotics Factory 2 (have 0)"` for a known-but-insufficient level,
    `"needs Shipyard >= 2 (level not reported)"` for an absent one.

    Takes **one** `UnmetRequirement`, not the tuple `unmet()` returns — join across a whole
    result as `"; ".join(describe(u) for u in unmet(...))`, the way `guard.py`'s
    `prerequisites` gate detail does (`guard.py:319`)."""
    name = _requirement_name(unmet_requirement.requirement)
    min_level = unmet_requirement.requirement.min_level
    if unmet_requirement.have is None:
        return f"needs {name} >= {min_level} (level not reported)"
    return f"needs {name} {min_level} (have {unmet_requirement.have})"
