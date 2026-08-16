"""`vd plan` — the decision engine. Input: `Snapshot` + `Policy`. Output: exactly one
`Action` (its `kind` may be `noop`/`escalate`/`halt` — see `Action.is_onchain()`).

Ladder, first match wins, exactly as docs/SPEC.md §5.4 specifies:

```
0. KILLSWITCH present                -> HALT
1. /health not ok                    -> NO-OP, reason recorded
2. pending tx unreconciled           -> NO-OP, reconcile first
3. mission Resolving > 60s           -> resolveFleetMission   (permissionless, free)
4. incoming hostile fleet            -> ESCALATE, no proposal (fleet-visibility.incoming)
5. resource within N hours of cap    -> spend it, or build the matching storage
6. building queue empty              -> next build
7. research queue empty              -> next research
8. shipyard idle AND economy on track-> ships/defense per policy
9. otherwise                         -> NO-OP with an explicit reason
```

**Rung 3 is a known, documented gap, not a silent omission.** The frozen `Snapshot` model
(`models.py`) carries `incoming_fleets` (for rung 4's hostile-fleet detection) but no
list of the *player's own* fleet missions and their status, so there is nothing in the
snapshot to check "Resolving > 60s" against. `plan_next_action` accepts
`resolvable_mission_ids` as an explicit, caller-supplied parameter (default empty) so the
rung is implemented and ready the moment that data exists (e.g. from `/missions` via
`read.py`), without requiring an edit to `models.py`. Today it never fires.

**The two invariants that matter most, both derived from planet traits, never hardcoded:**

1. **Energy-first.** Before proposing any mine upgrade, `_next_building_action` computes
   `required` at the *post-upgrade* mine level via `calc.energy_balance` and compares it
   to `produced` (preferring the API's live `PlanetSnapshot.energy.produced`). If the
   upgrade would push `required > produced`, it proposes an energy source instead of the
   mine — see `_energy_candidate`.
2. **Build order is derived from planet traits.** `_mine_priority_order` ranks Metal /
   Crystal / Deuterium mines by `base_rate * live_multiplier_bps` ("value density"), so a
   planet's deuterium multiplier changes the opener without any per-planet branch.
   `_energy_candidate` ranks Solar Plant vs Solar Satellite by cost-per-energy-point using
   only *live* costs (`Entity.cost`) and the live `solarSatelliteEnergy` — never a
   hardcoded planet id. See `references/strategy-playbook.md` for the full derivation and
   a worked numeric example on both planet 664 and a hot-planet fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from veydrift_agent import calc, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    Entity,
    PlanetSnapshot,
    Policy,
    QueueKind,
    Snapshot,
)
from veydrift_agent.techtree import EntityFamily, unmet

app = typer.Typer(no_args_is_help=True, help="Decide the next action from a snapshot + policy.")


# --------------------------------------------------------------------------------------
# Small lookups shared by every rung.
# --------------------------------------------------------------------------------------


def _entity(entities: list[Entity], entity_id: int) -> Entity | None:
    return next((e for e in entities if e.id == entity_id), None)


def _level(planet: PlanetSnapshot, building_id: int) -> int:
    entity = _entity(planet.buildings, building_id)
    return entity.level if entity is not None and entity.level is not None else 0


def _level_vector(entities: list[Entity]) -> dict[int, int | None]:
    """Legality-checking counterpart to `_level()`: preserves "not reported" as `None`
    instead of collapsing it to `0`. `_level()` stays as-is for its existing callers
    (docs/SPEC.md's energy-first invariant is fine treating an unreported mine as level
    0 — that's a "nothing built yet" default, not a legality check); this builds the
    vector `techtree.unmet()` needs to tell "reported and genuinely 0" apart from
    "the snapshot didn't say" (`AGENTS.md` §5). An id with an `Entity` in the list but
    `level=None` maps to `None` here, same as an id with no `Entity` at all -- both mean
    "not reported."""
    return {entity.id: entity.level for entity in entities}


def _unlocked(
    family: EntityFamily,
    entity_id: int,
    *,
    building_levels: dict[int, int | None],
    technology_levels: dict[int, int | None],
) -> bool:
    return not unmet(
        family,
        entity_id,
        building_levels=building_levels,
        technology_levels=technology_levels,
    )


def _energy_technology_level(snapshot: Snapshot) -> int:
    entity = _entity(snapshot.technologies, ids.Technology.ENERGY)
    return entity.level if entity is not None and entity.level is not None else 0


def _satellite_energy_per_unit(planet: PlanetSnapshot) -> int | None:
    """Prefer the live `energyBalance.sources.solarSatelliteEnergy` value the API already
    serves (docs/SPEC.md §5.4: "Read ... rather than recomputing it"). Falls back to
    `calc.solar_satellite_energy(temperature)` only when no live value is present, e.g. a
    hand-built fixture that only sets `temperature`.
    """
    if planet.energy is not None and planet.energy.solar_satellite_energy is not None:
        return planet.energy.solar_satellite_energy
    if planet.temperature is not None:
        return calc.solar_satellite_energy(planet.temperature)
    return None


def _target_planets(snapshot: Snapshot, policy: Policy) -> list[PlanetSnapshot]:
    """`policy.planets == []` means "all snapshot planets" (docs/SPEC.md §5.6); otherwise
    the policy's own order is the priority order the ladder walks in.
    """
    if not policy.planets:
        return list(snapshot.planets)
    by_id = {p.planet_id: p for p in snapshot.planets}
    return [by_id[planet_id] for planet_id in policy.planets if planet_id in by_id]


# --------------------------------------------------------------------------------------
# Build-order derivation. Both helpers below take only *this planet's* live traits —
# temperature-derived multipliers, live entity costs, current levels — and produce a
# different answer for a cold planet (664: never satellites) and a hot one (satellites
# win once Solar Plant's marginal cost per energy point exceeds a satellite's flat one).
# See references/strategy-playbook.md for the worked numbers.
# --------------------------------------------------------------------------------------


def _mine_priority_order(planet: PlanetSnapshot) -> list[int]:
    """Rank Metal / Crystal / Deuterium mines by resource "value density" on this planet:
    the contract's base production rate for each mine
    (`VeydriftFormulas.sol:70-72`: metal 30, crystal 20, deuterium 10 per scaled level)
    times this planet's *live* multiplier. Metal and crystal multipliers are always
    10_000 (`VeydriftFormulas.sol:32-33`); only the deuterium multiplier varies with
    temperature, so this is exactly what lets a deuterium-rich planet's opener lean
    deuterium without a special case.

    Ranking uses `(current_level + 1) / density` (lower = higher priority) rather than a
    plain density sort, so the choice also accounts for what is already built — a mine
    that is already far ahead of its density-implied share drops in priority even if its
    resource has the highest density.
    """
    densities = {
        ids.Building.METAL_MINE: 30 * planet.metal_multiplier_bps,
        ids.Building.CRYSTAL_MINE: 20 * planet.crystal_multiplier_bps,
        ids.Building.DEUTERIUM_SYNTHESIZER: 10 * planet.deuterium_multiplier_bps,
    }

    def score(building_id: int) -> float:
        density = densities[building_id]
        if density <= 0:
            return float("inf")
        return (_level(planet, building_id) + 1) / density

    return sorted(densities, key=score)


def _energy_candidate(
    planet: PlanetSnapshot,
    satellite_energy_per_unit: int | None,
    *,
    building_levels: dict[int, int | None],
    technology_levels: dict[int, int | None],
) -> tuple[float, str, Entity] | None:
    """Choose the cheaper energy source per unit of energy gained, comparing the next
    Solar Plant level against one more Solar Satellite — using only *live* costs
    (`Entity.cost`, never a recomputed cost-scaling factor) and the live per-satellite
    energy yield.

    Solar Plant's marginal cost-per-energy *grows* with level (cost scales by its
    unpublished-but-live factor roughly x1.5/level while the energy gained per level
    grows more slowly), while a Solar Satellite's cost-per-energy is flat (ships do not
    scale by count). The two curves cross at a level that depends entirely on
    `satellite_energy_per_unit` — high on a hot planet (crosses early), so low on a cold
    one (664: satellite energy 4) that it does not cross within any level worth building.
    That crossover, not a planet id, is what makes 664 never propose a satellite and a
    hot-planet fixture do so as soon as Solar Plant's marginal cost catches up.

    Solar Satellite (`Ship.SolarSatellite`, id 9) requires Shipyard >= 1
    (`techtree.SHIP_REQUIREMENTS`) — a planet with no Shipyard (or one the snapshot didn't
    report a level for) never gets it offered as a candidate at all, so the cheaper
    *legal* option wins instead of stalling the energy-first invariant on a locked choice.
    Solar Plant carries no requirement in the source, so it is never filtered here.

    Returns `(cost_per_energy, "solar_plant" | "solar_satellite", live_entity)` for the
    cheaper *unlocked* option, or `None` if neither is buildable/unlocked from the data
    available.
    """
    options: list[tuple[float, str, Entity]] = []

    solar = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    if solar is not None and solar.level is not None:
        gained = calc.scaled_level(20, solar.level + 1) - calc.scaled_level(20, solar.level)
        if gained > 0:
            cost_total = solar.cost.metal + solar.cost.crystal + solar.cost.deuterium
            options.append((cost_total / gained, "solar_plant", solar))

    satellite = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    if (
        satellite is not None
        and satellite_energy_per_unit
        and _unlocked(
            EntityFamily.SHIP,
            ids.Ship.SOLAR_SATELLITE,
            building_levels=building_levels,
            technology_levels=technology_levels,
        )
    ):
        cost_total = satellite.cost.metal + satellite.cost.crystal + satellite.cost.deuterium
        options.append((cost_total / satellite_energy_per_unit, "solar_satellite", satellite))

    if not options:
        return None
    options.sort(key=lambda option: option[0])
    return options[0]


def _build_time_savings_note(entity: Entity, robotics_level: int, nanite_level: int) -> str:
    """Plain informational text: how much faster this exact build would complete at
    Robotics Factory level+1. Never a recommendation to build Robotics Factory instead --
    this codebase deliberately has no formula netting time saved against resources not
    spent (an unbounded-future-horizon problem with no honest single answer); this is
    strictly the computable half, for a human/agent to read alongside the proposal.
    `calc.build_seconds` is a `public pure` contract formula, not a guessed constant --
    see calc.py's own "no cost-scaling function" hard constraint, which this doesn't
    trip (every term here is a live-known contract input, not an unpublished rational).

    Returns "" when there's nothing meaningful to report -- a build cheap enough that
    both the current and Robotics+1 durations floor at `min_queue_seconds`, where
    "0% faster" would be numerically true but reads as noise."""
    current = entity.duration_seconds or calc.build_seconds(robotics_level, nanite_level, entity.cost.metal, entity.cost.crystal)
    faster = calc.build_seconds(robotics_level + 1, nanite_level, entity.cost.metal, entity.cost.crystal)
    if faster >= current:
        return ""
    pct = round((current - faster) / current * 100)
    return (
        f"At Robotics Factory {robotics_level}, this build takes {current}s; "
        f"at level {robotics_level + 1}, it would take {faster}s ({pct}% faster)."
    )


def _next_building_action(planet: PlanetSnapshot, snapshot: Snapshot, policy: Policy, rule: str) -> Action | None:
    """The energy-first opener. Walks `_mine_priority_order`; for the first mine with
    live data, computes required energy at the *post-upgrade* level
    (`calc.energy_balance`) and compares to produced (preferring the live
    `planet.energy.produced`). If the upgrade would exceed supply, proposes whichever
    energy source (`_energy_candidate`) is cheaper per energy point instead of the mine.
    If no mine has data, or energy cannot be resolved for a shortfall, returns `None`.
    """
    if not policy.actions.allow_building:
        return None

    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)

    energy_technology_level = _energy_technology_level(snapshot)
    satellite_energy = _satellite_energy_per_unit(planet)
    solar_entity = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    fusion_entity = _entity(planet.buildings, ids.Building.FUSION_REACTOR)
    satellite_entity = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    robotics_level = _level(planet, ids.Building.ROBOTICS_FACTORY)
    nanite_level = _level(planet, ids.Building.NANITE_FACTORY)

    base_levels = {
        ids.Building.METAL_MINE: _level(planet, ids.Building.METAL_MINE),
        ids.Building.CRYSTAL_MINE: _level(planet, ids.Building.CRYSTAL_MINE),
        ids.Building.DEUTERIUM_SYNTHESIZER: _level(planet, ids.Building.DEUTERIUM_SYNTHESIZER),
    }
    produced_now = (
        planet.energy.produced
        if planet.energy is not None
        else calc.energy_balance(
            base_levels[ids.Building.METAL_MINE],
            base_levels[ids.Building.CRYSTAL_MINE],
            base_levels[ids.Building.DEUTERIUM_SYNTHESIZER],
            solar_entity.level if solar_entity and solar_entity.level is not None else 0,
            fusion_entity.level if fusion_entity and fusion_entity.level is not None else 0,
            energy_technology_level,
            satellite_entity.count if satellite_entity and satellite_entity.count else 0,
            satellite_energy or 0,
        ).produced
    )

    for mine_id in _mine_priority_order(planet):
        mine_entity = _entity(planet.buildings, mine_id)
        if mine_entity is None or mine_entity.level is None:
            continue

        post_levels = dict(base_levels)
        post_levels[mine_id] = post_levels[mine_id] + 1
        required_post = calc.energy_balance(
            post_levels[ids.Building.METAL_MINE],
            post_levels[ids.Building.CRYSTAL_MINE],
            post_levels[ids.Building.DEUTERIUM_SYNTHESIZER],
            solar_entity.level if solar_entity and solar_entity.level is not None else 0,
            fusion_entity.level if fusion_entity and fusion_entity.level is not None else 0,
            energy_technology_level,
            satellite_entity.count if satellite_entity and satellite_entity.count else 0,
            satellite_energy or 0,
        ).required

        if required_post > produced_now:
            choice = _energy_candidate(
                planet,
                satellite_energy,
                building_levels=building_levels,
                technology_levels=technology_levels,
            )
            if choice is None:
                # No energy source resolvable from live data; this mine can't be helped.
                # Try the next mine in priority order rather than giving up entirely.
                continue
            _, kind, entity = choice
            if kind == "solar_satellite" and not policy.actions.allow_ships:
                # `allow_ships` is a policy knob, so it has to bind every path that can emit
                # startShipProduction -- not just rung 8. This branch is the other one: on a
                # hot planet a Solar Satellite can be the cheaper energy source, and before
                # 2026-08-12 it was returned here regardless of the flag. Found by the second
                # judge pass; the same class of defect as the original `allow_ships` bug, which
                # was a knob the code never honoured.
                #
                # Falling back to the Solar Plant (rather than returning None) keeps the
                # energy-first invariant intact: the mine still gets the energy it needs, just
                # from the source the operator permitted. Refusing outright would stall the
                # economy on a legitimate configuration.
                solar_fallback = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
                if solar_fallback is None:
                    continue
                kind, entity = "solar_plant", solar_fallback
            if kind == "solar_plant":
                return Action(
                    kind=ActionKind.BUILD,
                    function="startBuildingUpgrade",
                    planet_id=planet.planet_id,
                    entity_id=ids.Building.SOLAR_PLANT,
                    entity_name=ids.building_name(ids.Building.SOLAR_PLANT),
                    target_level=(entity.level or 0) + 1,
                    cost=entity.cost,
                    rule=rule,
                    rationale=(
                        f"{ids.building_name(mine_id)} {mine_entity.level}->"
                        f"{mine_entity.level + 1} would need {required_post} energy against "
                        f"{produced_now} produced. Energy-first invariant: Solar Plant's "
                        f"marginal cost per energy point is cheaper here than one more Solar "
                        f"Satellite (satellite energy/unit={satellite_energy})."
                    ),
                    expected_effect=(
                        f"produced energy {produced_now} -> "
                        f"{produced_now + calc.scaled_level(20, (entity.level or 0) + 1) - calc.scaled_level(20, entity.level or 0)}"
                        + (f" | {note}" if (note := _build_time_savings_note(entity, robotics_level, nanite_level)) else "")
                    ),
                )
            return Action(
                kind=ActionKind.SHIP,
                function="startShipProduction",
                planet_id=planet.planet_id,
                entity_id=ids.Ship.SOLAR_SATELLITE,
                entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                quantity=1,
                cost=entity.cost,
                rule=rule,
                rationale=(
                    f"{ids.building_name(mine_id)} {mine_entity.level}->"
                    f"{mine_entity.level + 1} would need {required_post} energy against "
                    f"{produced_now} produced. Energy-first invariant: on this planet, one "
                    f"Solar Satellite (energy/unit={satellite_energy}) is cheaper per energy "
                    f"point than the next Solar Plant level."
                ),
                expected_effect=f"produced energy {produced_now} -> {produced_now + (satellite_energy or 0)}",
            )

        # Defense in depth: mines carry no requirement in the source today
        # (`techtree.BUILDING_REQUIREMENTS` has no entry for Metal/Crystal/Deuterium
        # Mine), so this is a no-op check against live data -- but every building branch
        # here goes through the same `unmet()` filter on principle, so a future entity
        # added to this loop (or a contract change) can't silently reintroduce the bug
        # this module exists to close. A locked mine is skipped in favour of the next
        # one in priority order, never a silent stall.
        if not _unlocked(
            EntityFamily.BUILDING, mine_id, building_levels=building_levels, technology_levels=technology_levels
        ):
            continue

        return Action(
            kind=ActionKind.BUILD,
            function="startBuildingUpgrade",
            planet_id=planet.planet_id,
            entity_id=mine_id,
            entity_name=mine_entity.name or ids.building_name(mine_id),
            target_level=mine_entity.level + 1,
            cost=mine_entity.cost,
            rule=rule,
            rationale=(
                f"{ids.building_name(mine_id)} ranked highest by value density "
                f"(base rate x live multiplier) among mines with room to grow; "
                f"{mine_entity.level}->{mine_entity.level + 1} needs {required_post} energy "
                f"against {produced_now} produced -- energy-safe."
            ),
            expected_effect=_build_time_savings_note(mine_entity, robotics_level, nanite_level),
        )

    return None


def _next_research_action(snapshot: Snapshot, target_planets: list[PlanetSnapshot], policy: Policy, rule: str) -> Action | None:
    """Research is per-player, not per-planet (`Snapshot.research_queue`). This walks
    technologies ordered by lowest current level account-wide (ties broken by ascending
    contract id — a simple, generalizable default) and returns the **first one whose
    on-chain prerequisites are met** (`techtree.unmet`, `EntityFamily.RESEARCH`), skipping
    any locked candidate rather than proposing it or falling straight through to a NOOP.

    **This is the fix for the bug this work package exists to close.** Before this
    change, the lowest-level tie-break alone picked Energy Technology (id 0) on a fresh
    account — but Energy requires Research Lab >= 1 (`VeydriftDependencies.sol:
    requireResearch` via `VeydriftCatalog.researchLabRequirement`). On a Research-Lab-less
    planet that was a guaranteed on-chain revert, paid in real gas, the very first time
    tier >= 2 ever tried it. The contract's Research Lab check is planet-scoped even
    though the research *queue* itself is per-player
    (`VeydriftPlanetManagementModule.sol:558`: `_buildingLevels[planetId][ResearchLab]`),
    so the building-level vector used here comes from `target_planets[0]` — the planet
    `startResearch` would actually be submitted through — not from any other target
    planet.

    If every candidate technology is locked (e.g. Research Lab is genuinely 0, or its
    level wasn't reported at all), this returns `None` rather than proposing anything —
    rung 7 simply doesn't fire, and the ladder falls through to rung 8/9 exactly as if the
    research queue were merely unavailable. It never falls back to a locked first choice.
    """
    if not policy.actions.allow_research or not snapshot.technologies or not target_planets:
        return None

    planet = target_planets[0]
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)

    candidates = sorted(snapshot.technologies, key=lambda t: ((t.level or 0), t.id))
    for candidate in candidates:
        unmet_reqs = unmet(
            EntityFamily.RESEARCH,
            candidate.id,
            building_levels=building_levels,
            technology_levels=technology_levels,
        )
        if unmet_reqs:
            continue
        return Action(
            kind=ActionKind.RESEARCH,
            function="startResearch",
            planet_id=planet.planet_id,
            entity_id=candidate.id,
            entity_name=candidate.name or ids.technology_name(candidate.id),
            target_level=(candidate.level or 0) + 1,
            cost=candidate.cost,
            rule=rule,
            rationale=(
                f"{candidate.name or ids.technology_name(candidate.id)} is the lowest-level "
                f"unlocked technology account-wide (level {candidate.level or 0}); research "
                f"queue is idle."
            ),
        )
    return None


# --------------------------------------------------------------------------------------
# Storage overflow (rung 5).
# --------------------------------------------------------------------------------------

_RESOURCE_LABELS = ("metal", "crystal", "deuterium")
_STORAGE_BUILDING_FOR_RESOURCE = {
    0: ids.Building.METAL_STORAGE,
    1: ids.Building.CRYSTAL_STORAGE,
    2: ids.Building.DEUTERIUM_TANK,
}


def _most_urgent_overflow(
    target_planets: list[PlanetSnapshot], trigger_hours: float
) -> tuple[PlanetSnapshot, int, float] | None:
    """The `(planet, resource_index, hours_to_cap)` closest to overflow across every
    target planet, if any is within `trigger_hours`. `resource_index` is 0/1/2 for
    metal/crystal/deuterium, matching `_RESOURCE_LABELS`.
    """
    worst: tuple[PlanetSnapshot, int, float] | None = None
    for planet in target_planets:
        triples = (
            (planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            (planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            (
                planet.resources_as_of_now.deuterium,
                planet.production_per_hour.deuterium,
                planet.storage_caps.deuterium,
            ),
        )
        for index, (current, per_hour, cap) in enumerate(triples):
            hours = calc.hours_to_cap(current, per_hour, cap)
            if hours is None or hours > trigger_hours:
                continue
            if worst is None or hours < worst[2]:
                worst = (planet, index, hours)
    return worst


def _storage_overflow_action(
    snapshot: Snapshot, target_planets: list[PlanetSnapshot], policy: Policy
) -> Action | None:
    overflow = _most_urgent_overflow(target_planets, policy.storage.hours_to_cap_trigger)
    if overflow is None:
        return None
    planet, resource_index, hours = overflow
    label = _RESOURCE_LABELS[resource_index]
    queue_busy = planet.queues.get(QueueKind.BUILDING) is not None

    if queue_busy:
        # The contract only allows one active BuildingConstruction per planet
        # (`buildingConstructions[planetId].active` -> `ConstructionActive` revert,
        # `VeydriftGame.sol:117-138`). A second startBuildingUpgrade -- whether "spend
        # it" or the matching storage building -- would be a guaranteed-revert proposal
        # while a construction is already in flight. Nothing safe to propose from this
        # rung; fall through and let a later rung (or an honest rung-9 NOOP) take over.
        return None

    candidate = _next_building_action(planet, snapshot, policy, rule="5:storage-overflow-spend")
    if candidate is not None:
        candidate.rationale = (
            f"Planet {planet.planet_id} {label} is {hours:.1f}h from its storage cap "
            f"(trigger {policy.storage.hours_to_cap_trigger}h) -- spending it via the "
            f"normal next-building pick. {candidate.rationale}"
        )
        return candidate

    if policy.actions.allow_building:
        storage_building = _STORAGE_BUILDING_FOR_RESOURCE[resource_index]
        entity = _entity(planet.buildings, storage_building)
        if entity is not None and entity.level is not None:
            return Action(
                kind=ActionKind.BUILD,
                function="startBuildingUpgrade",
                planet_id=planet.planet_id,
                entity_id=storage_building,
                entity_name=entity.name or ids.building_name(storage_building),
                target_level=entity.level + 1,
                cost=entity.cost,
                rule="5:storage-overflow-storage",
                rationale=(
                    # Fix 6a: this branch is only reachable when the building queue is
                    # IDLE (`_storage_overflow_action` already returned early, above,
                    # whenever `queue_busy` was True) -- the previous wording claimed the
                    # opposite, a false statement written straight into the audit log.
                    # The real reason nothing could be spent here is that the ordinary
                    # next-building pick (`_next_building_action`) came back empty even
                    # though the queue was free to take a new order.
                    f"Planet {planet.planet_id} {label} is {hours:.1f}h from its storage "
                    f"cap and no ordinary next-building spend was available right now "
                    f"(queue is idle) -- upgrading {ids.building_name(storage_building)} instead."
                ),
            )
    return None


# --------------------------------------------------------------------------------------
# Rung 8 — shipyard idle AND economy on track. Deliberately minimal: `policy.json`'s
# defaults disable both `allow_ships` and `allow_defense` (docs/SPEC.md §5.6), so this
# rung rarely fires in practice, and the ladder does not ask for a ship/defense strategy
# the way it asks for an energy-first opener.
# --------------------------------------------------------------------------------------


def _economy_on_track(snapshot: Snapshot, target_planets: list[PlanetSnapshot]) -> bool:
    """"On track" = something is already actively building or researching. If every
    queue is idle everywhere, spending shipyard capacity is not obviously safe (it may
    mean policy disabled building/research entirely, not that the economy is healthy).
    """
    if snapshot.research_queue is not None:
        return True
    return any(planet.queues.get(QueueKind.BUILDING) is not None for planet in target_planets)


def _shipyard_action(snapshot: Snapshot, target_planets: list[PlanetSnapshot], policy: Policy) -> Action | None:
    if not target_planets or not _economy_on_track(snapshot, target_planets):
        return None

    for planet in target_planets:
        ship_idle = planet.queues.get(QueueKind.SHIP) is None
        defense_idle = planet.queues.get(QueueKind.DEFENSE) is None
        building_levels = _level_vector(planet.buildings)
        technology_levels = _level_vector(snapshot.technologies)

        if policy.actions.allow_ships and ship_idle:
            satellite_energy = _satellite_energy_per_unit(planet)
            choice = _energy_candidate(
                planet,
                satellite_energy,
                building_levels=building_levels,
                technology_levels=technology_levels,
            )
            if choice is not None and choice[1] == "solar_satellite":
                _, _, entity = choice
                return Action(
                    kind=ActionKind.SHIP,
                    function="startShipProduction",
                    planet_id=planet.planet_id,
                    entity_id=ids.Ship.SOLAR_SATELLITE,
                    entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                    quantity=1,
                    cost=entity.cost,
                    rule="8:shipyard-idle",
                    rationale=(
                        "Shipyard idle, economy on track, and a Solar Satellite is "
                        "currently the cheaper energy source per point on this planet."
                    ),
                )

        if (
            policy.actions.allow_defense
            and defense_idle
            and _unlocked(
                EntityFamily.DEFENSE,
                ids.Defense.ROCKET_LAUNCHER,
                building_levels=building_levels,
                technology_levels=technology_levels,
            )
        ):
            entity = _entity(planet.defenses, ids.Defense.ROCKET_LAUNCHER)
            if entity is not None and entity.count is not None:
                return Action(
                    kind=ActionKind.DEFENSE,
                    function="startDefenseProduction",
                    planet_id=planet.planet_id,
                    entity_id=ids.Defense.ROCKET_LAUNCHER,
                    entity_name=entity.name or ids.defense_name(ids.Defense.ROCKET_LAUNCHER),
                    quantity=1,
                    cost=entity.cost,
                    rule="8:shipyard-idle",
                    rationale=(
                        "Defense queue idle, economy on track, allow_defense=true; "
                        "Rocket Launcher is the cheapest defense entry and a reasonable "
                        "policy-driven default in the absence of a threat model."
                    ),
                )
    return None


# --------------------------------------------------------------------------------------
# The ladder.
# --------------------------------------------------------------------------------------


def plan_next_action(
    snapshot: Snapshot,
    policy: Policy,
    *,
    killswitch_active: bool = False,
    pending_tx_unreconciled: bool = False,
    resolvable_mission_ids: list[int] | None = None,
) -> Action:
    """Decide exactly one `Action` from `snapshot` + `policy`. First matching rung wins;
    `Action.rule` records which one fired (e.g. `"5:storage-overflow-spend"`) so the log
    is auditable without re-running the planner (docs/SPEC.md §5.4).

    `killswitch_active`, `pending_tx_unreconciled` and `resolvable_mission_ids` are not on
    `Snapshot` (that model is frozen and owned by WP1) — they are `tick.py`'s
    responsibility to discover (killswitch file, `agent-state.json`, `/missions`) and pass
    in. Defaults are the safe "nothing pending" state, so calling this with just a
    snapshot and policy is a legitimate offline planning call.
    """
    if killswitch_active:
        return Action(kind=ActionKind.HALT, rule="0:killswitch", rationale="KILLSWITCH file present; halting before any further action.")

    if not snapshot.health_ok:
        return Action(
            kind=ActionKind.NOOP,
            rule="1:health-not-ok",
            rationale="/health reported not ok / not ready; refusing to plan against a possibly-stale snapshot.",
        )

    if pending_tx_unreconciled:
        return Action(
            kind=ActionKind.NOOP,
            rule="2:pending-tx-unreconciled",
            rationale="A previous transaction has not been reconciled yet; resolving that takes priority over a new proposal.",
        )

    if resolvable_mission_ids:
        return Action(
            kind=ActionKind.RESOLVE_MISSION,
            function="resolveFleetMission",
            mission_id=resolvable_mission_ids[0],
            rule="3:mission-resolving",
            rationale=f"Mission {resolvable_mission_ids[0]} has been Resolving for >60s; resolveFleetMission is permissionless and free.",
        )

    if policy.escalation.on_incoming_fleet:
        hostiles = [fleet for fleet in snapshot.incoming_fleets if fleet.hostile]
        if hostiles:
            return Action(
                kind=ActionKind.ESCALATE,
                rule="4:incoming-hostile-fleet",
                rationale=(
                    f"{len(hostiles)} incoming hostile fleet(s) detected via "
                    "fleet-visibility.incoming; escalating to a human rather than proposing."
                ),
            )

    target_planets = _target_planets(snapshot, policy)

    overflow_action = _storage_overflow_action(snapshot, target_planets, policy)
    if overflow_action is not None:
        return overflow_action

    for planet in target_planets:
        if planet.queues.get(QueueKind.BUILDING) is None:
            candidate = _next_building_action(planet, snapshot, policy, rule="6:building-queue-empty")
            if candidate is not None:
                return candidate

    if snapshot.research_queue is None:
        candidate = _next_research_action(snapshot, target_planets, policy, rule="7:research-queue-empty")
        if candidate is not None:
            return candidate

    shipyard_candidate = _shipyard_action(snapshot, target_planets, policy)
    if shipyard_candidate is not None:
        return shipyard_candidate

    return Action(
        kind=ActionKind.NOOP,
        rule="9:no-match",
        rationale=(
            "No ladder rung produced a proposal: queues busy, or policy disallows the "
            "available actions, or no entity data was present for any target planet."
        ),
    )


# --------------------------------------------------------------------------------------
# CLI — offline: reads a Snapshot and a Policy from files. `tick.py` (WP3) is the wired,
# online caller; this is a debugging / dry-run entrypoint that does not depend on it.
# --------------------------------------------------------------------------------------


@app.command()
def run(
    snapshot: Path = typer.Option(..., "--snapshot", help="Path to a Snapshot JSON file."),  # noqa: B008
    policy: Path = typer.Option(..., "--policy", help="Path to a Policy JSON file."),  # noqa: B008
    killswitch: bool = typer.Option(False, help="Simulate a present KILLSWITCH file."),
    pending_tx: bool = typer.Option(False, "--pending-tx", help="Simulate an unreconciled pending tx."),
    json_output: bool = typer.Option(False, "--json", help="Print the Action as JSON instead of a panel."),
) -> None:
    """Decide the next action from a snapshot + policy file. Offline; no network calls."""
    console = Console()
    try:
        snapshot_model = Snapshot.model_validate(json.loads(snapshot.read_text()))
        policy_model = Policy.model_validate(json.loads(policy.read_text()))
    except (OSError, ValueError) as exc:
        console.print(f"[red]failed to load snapshot/policy: {exc}[/red]")
        raise typer.Exit(code=4) from exc

    action = plan_next_action(
        snapshot_model,
        policy_model,
        killswitch_active=killswitch,
        pending_tx_unreconciled=pending_tx,
    )

    if json_output:
        typer.echo(action.model_dump_json(indent=2))
        return

    body = [f"rule:      {action.rule}", f"kind:      {action.kind.value}"]
    if action.function:
        body.append(f"function:  {action.function}(planet={action.planet_id}, entity={action.entity_id})")
    if action.target_level is not None:
        body.append(f"target:    level {action.target_level}")
    if action.quantity is not None:
        body.append(f"quantity:  {action.quantity}")
    if action.cost.metal or action.cost.crystal or action.cost.deuterium:
        body.append(f"cost:      M {action.cost.metal}  C {action.cost.crystal}  D {action.cost.deuterium}")
    body.append(f"why:       {action.rationale}")
    console.print(Panel("\n".join(body), title=f"vd plan run -- {action.kind.value}"))


if __name__ == "__main__":
    app()
