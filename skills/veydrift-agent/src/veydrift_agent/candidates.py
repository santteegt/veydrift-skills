"""`veydrift_agent.candidates` — the generate/filter/score/select pipeline behind
`plan.py`'s rungs 5-9 (docs/SPEC.md §5.4, "Phase 2 of the general-strategy-engine
program"). Before this module, each rung both decided the action *family* and hardcoded
*which entity* in one function. This module splits that in two:

* **Generate** — one pure function per family (`generate_mine_candidates`,
  `generate_energy_candidates`, `generate_storage_candidates`,
  `generate_research_candidates`, `generate_ship_candidates`,
  `generate_defense_candidates`), each `(snapshot, policy, planet-or-planets) ->
  list[Candidate]`. No network calls, no mutation.
* **Filter** — folded into generation, not a separate pass: a locked entity
  (`techtree.unmet`) or a mine whose post-upgrade energy `required` would exceed
  `produced` is never yielded as a *selectable* candidate for its own family in the first
  place (see `generate_mine_candidates`'s docstring) — same as `plan.py`'s pre-Phase-2
  behaviour, just expressed as "never generated" instead of "generated then discarded."
* **Score** — `score_payback`: weighted cost ÷ weighted marginal `calc.production_per_hour`
  delta, in payback hours. `None` when the level change doesn't move that function's
  output at all (a storage building, a locked entity, most research/ship/defense picks).
* **Select** — `select_building_candidate` / `select_storage_candidate` /
  `select_research_candidate` / `select_shipyard_candidate` each replay the *exact* rung
  order `plan.py` used before this module existed (priority-ordered mine walk with the
  energy-first hard filter, lowest-level-then-id research walk, ship-then-defense
  shipyard walk) so the winning `Action` this phase produces is byte-identical to the
  pre-Phase-2 ladder — this phase's own acceptance criterion (docs/SPEC.md §9 AC23).
  Each returns `(winner: Candidate | None, alternatives: list[Candidate])`; `plan.py`
  attaches `alternatives` to the winning `Action` (capped at
  `policy.strategy.max_alternatives`), ranked, informational only.

**The scoring rule, stated once, applies everywhere in this module**: a candidate is
scored if and only if its level change moves `calc.production_per_hour`'s output (before
vs. after, at *this planet's* other levels held fixed). Everything else — a storage
building, a locked entity, a research technology (nothing in `calc.py` models research
moving mine output), a Rocket Launcher — is `score=None` and ranked below any scored
candidate within the same band, never above one (see `rank_candidates`).

**Never recompute cost.** Every `Candidate.action.cost` here is copied straight from a
live `Entity.cost` (buildings/ships/defenses/technologies as the API reports them) — this
module scores what `calc.production_per_hour` says the *level change* is worth, never
what the *cost* of that level is (`calc.py`'s own hard "no cost-scaling function"
constraint, restated for this module: nothing here computes `base * factor ** level`).
"""

from __future__ import annotations

from dataclasses import dataclass

from veydrift_agent import calc, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    Entity,
    EntityTarget,
    PlanetSnapshot,
    Policy,
    QueueKind,
    Resources,
    Snapshot,
)
from veydrift_agent.techtree import (
    MAX_DEFENSE_PER_PLANET,
    MISSILE_SLOTS,
    EntityFamily,
    describe,
    missile_silo_capacity,
    unmet,
)

# --------------------------------------------------------------------------------------
# The candidate type.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    action: Action
    #: mine | energy | storage | infrastructure | research | ship | defense | crawler.
    #: "infrastructure" and "crawler" were reserved/unused before Phase 3 of the
    #: general-strategy-engine program (docs/SPEC.md §5.4) and are populated by
    #: `generate_infrastructure_candidates` / `generate_crawler_candidates` below.
    family: str
    #: Payback hours, or `None` if this candidate's level change doesn't move
    #: `calc.production_per_hour`'s output (see module docstring).
    score: float | None
    #: The derivation in words when scored ("cost 210 (weighted) / +14/hr (weighted) =
    #: 15.00h payback"), or why it's unscored ("locked: needs Shipyard 2 (have 0)",
    #: "no production_per_hour change at this level").
    score_basis: str


# --------------------------------------------------------------------------------------
# Small lookups shared by every generator — moved here verbatim from `plan.py` (Phase 1),
# which no longer needs them once its rung 5-9 helpers move into this module.
# --------------------------------------------------------------------------------------


def _entity(entities: list[Entity], entity_id: int) -> Entity | None:
    return next((e for e in entities if e.id == entity_id), None)


def _level(planet: PlanetSnapshot, building_id: int) -> int:
    entity = _entity(planet.buildings, building_id)
    return entity.level if entity is not None and entity.level is not None else 0


def _level_vector(entities: list[Entity]) -> dict[int, int | None]:
    """Preserves "not reported" as `None` instead of collapsing it to `0` — see
    `techtree.unmet`'s docstring and `AGENTS.md` §5. An id with an `Entity` in the list
    but `level=None` maps to `None` here, same as an id with no `Entity` at all."""
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


def _describe_unmet(family: EntityFamily, entity_id: int, *, building_levels, technology_levels) -> str:
    reqs = unmet(family, entity_id, building_levels=building_levels, technology_levels=technology_levels)
    return "locked: " + "; ".join(describe(r) for r in reqs)


def _resolve_target_id(target: EntityTarget, id_fn) -> int:
    """`EntityTarget.id` if set, else `id_fn(target.name)` -- `id_fn` is one of
    `ids.ship_id` / `ids.defense_id`, both of which already normalize case/space/hyphen
    and raise `KeyError` on an unknown name (`ids.py`'s own convention: "no silent
    fallback anywhere in this module"). Phase 3's own brief: "fail loudly on an unknown
    name -- a typo must never silently mean 'no target'" -- so this re-raises as a
    `ValueError` with the offending name in the message rather than swallowing it,
    letting the whole `vd plan`/`vd tick` call fail rather than silently skip the typo'd
    target forever."""
    if target.id is not None:
        return target.id
    if target.name is not None:
        try:
            return id_fn(target.name)
        except KeyError as exc:
            raise ValueError(
                f"policy.strategy target name {target.name!r} does not match any known entity name"
            ) from exc
    raise ValueError("EntityTarget requires either `name` or `id` to be set")


def _resolve_name_id(name: str, id_fn) -> int:
    """Same fail-loudly contract as `_resolve_target_id`, for the plain-string entries in
    `research_priority`/`building_priority` (no `EntityTarget` wrapper -- those fields are
    `list[str]`, not `list[EntityTarget]`, per docs/SPEC.md §5.6)."""
    try:
        return id_fn(name)
    except KeyError as exc:
        raise ValueError(f"policy.strategy priority name {name!r} does not match any known entity name") from exc


def _energy_technology_level(snapshot: Snapshot) -> int:
    entity = _entity(snapshot.technologies, ids.Technology.ENERGY)
    return entity.level if entity is not None and entity.level is not None else 0


def _satellite_energy_per_unit(planet: PlanetSnapshot) -> int | None:
    """Prefer the live `energyBalance.sources.solarSatelliteEnergy` value the API already
    serves (docs/SPEC.md §5.4). Falls back to `calc.solar_satellite_energy(temperature)`
    only when no live value is present (e.g. a hand-built fixture)."""
    if planet.energy is not None and planet.energy.solar_satellite_energy is not None:
        return planet.energy.solar_satellite_energy
    if planet.temperature is not None:
        return calc.solar_satellite_energy(planet.temperature)
    return None


def build_time_savings_note(entity: Entity, robotics_level: int, nanite_level: int) -> str:
    """Plain informational text: how much faster this exact build would complete at
    Robotics Factory level+1. Ported verbatim from `plan.py`'s pre-Phase-2
    `_build_time_savings_note` — see that function's original docstring for why this is
    intentionally never a recommendation."""
    current = entity.duration_seconds or calc.build_seconds(robotics_level, nanite_level, entity.cost.metal, entity.cost.crystal)
    faster = calc.build_seconds(robotics_level + 1, nanite_level, entity.cost.metal, entity.cost.crystal)
    if faster >= current:
        return ""
    pct = round((current - faster) / current * 100)
    return (
        f"At Robotics Factory {robotics_level}, this build takes {current}s; "
        f"at level {robotics_level + 1}, it would take {faster}s ({pct}% faster)."
    )


# --------------------------------------------------------------------------------------
# Scoring.
# --------------------------------------------------------------------------------------


def score_payback(
    cost: Resources,
    weights: Resources,
    production_before: Resources,
    production_after: Resources,
) -> tuple[float | None, str]:
    """Weighted cost ÷ weighted marginal `calc.production_per_hour` delta, in payback
    hours. `production_before`/`production_after` are two calls to
    `calc.production_per_hour` (`calc.py:226`) — once at the planet's live level vector,
    once with the candidate's one level bumped, everything else held fixed — differenced
    here. `cost` is always a live `Entity.cost`, never recomputed (module docstring).

    Returns `(None, reason)` when the level change moves none of the three resources'
    hourly output at all — a storage building (not a `calc.production_per_hour` input),
    or a mine/energy candidate that doesn't change anything because the planet is not
    currently energy-throttled at the relevant levels.
    """
    delta_metal = production_after.metal - production_before.metal
    delta_crystal = production_after.crystal - production_before.crystal
    delta_deuterium = production_after.deuterium - production_before.deuterium
    if delta_metal == 0 and delta_crystal == 0 and delta_deuterium == 0:
        return None, "no production_per_hour change at this level increment (not economically comparable)"

    weighted_marginal = delta_metal * weights.metal + delta_crystal * weights.crystal + delta_deuterium * weights.deuterium
    if weighted_marginal <= 0:
        # Only reachable with a non-default (e.g. negative) weight -- production deltas
        # from a level *increase* are never negative on their own. Guards the division
        # below rather than assuming positive.
        return None, f"weighted marginal production_per_hour did not increase (weighted delta {weighted_marginal})"

    weighted_cost = cost.metal * weights.metal + cost.crystal * weights.crystal + cost.deuterium * weights.deuterium
    payback_hours = weighted_cost / weighted_marginal
    return (
        payback_hours,
        f"cost {weighted_cost} (weighted) / +{weighted_marginal} (weighted)/hr = {payback_hours:.2f}h payback",
    )


def _planet_production_context(planet: PlanetSnapshot, snapshot: Snapshot) -> dict[str, int]:
    """Every flat scalar `calc.production_per_hour` needs, read live from `planet` /
    `snapshot` -- the "current" state `score_payback`'s `production_before` is computed
    from. Never a recomputed cost-scaling factor; only *levels* and live multipliers."""
    satellite_entity = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    crawler_entity = _entity(planet.ships, ids.Ship.CRAWLER)
    solar_entity = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    fusion_entity = _entity(planet.buildings, ids.Building.FUSION_REACTOR)
    return dict(
        metal_level=_level(planet, ids.Building.METAL_MINE),
        crystal_level=_level(planet, ids.Building.CRYSTAL_MINE),
        deuterium_level=_level(planet, ids.Building.DEUTERIUM_SYNTHESIZER),
        solar_level=solar_entity.level if solar_entity and solar_entity.level is not None else 0,
        fusion_level=fusion_entity.level if fusion_entity and fusion_entity.level is not None else 0,
        solar_satellite_count=satellite_entity.count if satellite_entity and satellite_entity.count else 0,
        crawler_count=crawler_entity.count if crawler_entity and crawler_entity.count else 0,
        energy_technology_level=_energy_technology_level(snapshot),
        metal_multiplier_bps=planet.metal_multiplier_bps,
        crystal_multiplier_bps=planet.crystal_multiplier_bps,
        deuterium_multiplier_bps_=planet.deuterium_multiplier_bps,
        solar_satellite_energy_per_unit=_satellite_energy_per_unit(planet) or 0,
    )


def _score_level_delta(
    planet: PlanetSnapshot,
    snapshot: Snapshot,
    weights: Resources,
    cost: Resources,
    **level_delta: int,
) -> tuple[float | None, str]:
    """`score_payback` convenience wrapper: computes `calc.production_per_hour` at the
    planet's current live levels, then again with `level_delta` (e.g.
    `metal_level=1`, meaning "+1 to the current metal_level") applied on top, and scores
    the difference. `level_delta` keys must be `calc.production_per_hour` parameter
    names."""
    base = _planet_production_context(planet, snapshot)
    before = calc.production_per_hour(**base)
    after_kwargs = dict(base)
    for key, delta in level_delta.items():
        after_kwargs[key] = after_kwargs[key] + delta
    after = calc.production_per_hour(**after_kwargs)
    return score_payback(cost, weights, before, after)


# --------------------------------------------------------------------------------------
# Mine family (rung 6's mine half). Filter is fused into generation: a mine whose
# post-upgrade `required` energy would exceed `produced` is never yielded here at all
# (docs/SPEC.md §5.4's energy-first invariant, "a hard filter, not a score") — the
# `energy` family generator below is what fills that gap.
# --------------------------------------------------------------------------------------

_MINE_BUILDING_IDS = (ids.Building.METAL_MINE, ids.Building.CRYSTAL_MINE, ids.Building.DEUTERIUM_SYNTHESIZER)


def _mine_priority_order(planet: PlanetSnapshot) -> list[int]:
    """Ranks Metal / Crystal / Deuterium mines by resource "value density" on this
    planet: contract base production rate (`VeydriftFormulas.sol:70-72`: metal 30,
    crystal 20, deuterium 10 per scaled level) times this planet's live multiplier,
    ordered by `(current_level + 1) / density` (lower = higher priority). Ported
    verbatim from `plan.py`'s pre-Phase-2 `_mine_priority_order` — see
    `references/strategy-playbook.md` for the full derivation."""
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


def _mine_energy_safe(
    planet: PlanetSnapshot,
    snapshot: Snapshot,
    mine_id: int,
    *,
    produced_now: int,
) -> tuple[bool, int]:
    """`(is_safe, required_post)` -- would upgrading *only* `mine_id` by one level push
    required energy past `produced_now`? Same `calc.energy_balance` call
    `plan.py`'s pre-Phase-2 `_next_building_action` made, isolated so both
    `generate_mine_candidates` and `select_building_candidate` can call it without
    duplicating the arithmetic (only the *use* of the result differs between them)."""
    solar_entity = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    fusion_entity = _entity(planet.buildings, ids.Building.FUSION_REACTOR)
    satellite_entity = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    energy_technology_level = _energy_technology_level(snapshot)
    satellite_energy = _satellite_energy_per_unit(planet)

    post_levels = {b: _level(planet, b) for b in _MINE_BUILDING_IDS}
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
    return required_post <= produced_now, required_post


def _produced_now(planet: PlanetSnapshot, snapshot: Snapshot) -> int:
    if planet.energy is not None:
        return planet.energy.produced
    solar_entity = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    fusion_entity = _entity(planet.buildings, ids.Building.FUSION_REACTOR)
    satellite_entity = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    energy_technology_level = _energy_technology_level(snapshot)
    satellite_energy = _satellite_energy_per_unit(planet)
    return calc.energy_balance(
        _level(planet, ids.Building.METAL_MINE),
        _level(planet, ids.Building.CRYSTAL_MINE),
        _level(planet, ids.Building.DEUTERIUM_SYNTHESIZER),
        solar_entity.level if solar_entity and solar_entity.level is not None else 0,
        fusion_entity.level if fusion_entity and fusion_entity.level is not None else 0,
        energy_technology_level,
        satellite_entity.count if satellite_entity and satellite_entity.count else 0,
        satellite_energy or 0,
    ).produced


_MINE_PRODUCTION_LEVER = {
    ids.Building.METAL_MINE: "metal_level",
    ids.Building.CRYSTAL_MINE: "crystal_level",
    ids.Building.DEUTERIUM_SYNTHESIZER: "deuterium_level",
}


def generate_mine_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """One `Candidate` per Metal/Crystal/Deuterium mine that has live data on `planet`,
    in `_mine_priority_order`. A mine whose post-upgrade `required` energy would exceed
    `produced` is **never yielded** (the energy-first hard filter) — `generate_energy_
    candidates` is what fills that gap. A locked mine (no entry in
    `techtree.BUILDING_REQUIREMENTS` today, so unreachable in practice, but checked on
    principle -- see `plan.py`'s original "defense in depth" comment) is yielded with
    `score=None` so it can appear as a locked alternative, but is never selectable."""
    if not policy.actions.allow_building:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    produced_now = _produced_now(planet, snapshot)
    weights = policy.strategy.resource_weights

    out: list[Candidate] = []
    for mine_id in _mine_priority_order(planet):
        mine_entity = _entity(planet.buildings, mine_id)
        if mine_entity is None or mine_entity.level is None:
            continue
        if not _unlocked(EntityFamily.BUILDING, mine_id, building_levels=building_levels, technology_levels=technology_levels):
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.BUILD,
                        function="startBuildingUpgrade",
                        planet_id=planet.planet_id,
                        entity_id=mine_id,
                        entity_name=mine_entity.name or ids.building_name(mine_id),
                        target_level=mine_entity.level + 1,
                        cost=mine_entity.cost,
                    ),
                    family="mine",
                    score=None,
                    score_basis=_describe_unmet(
                        EntityFamily.BUILDING, mine_id, building_levels=building_levels, technology_levels=technology_levels
                    ),
                )
            )
            continue

        safe, required_post = _mine_energy_safe(planet, snapshot, mine_id, produced_now=produced_now)
        if not safe:
            # Energy-first hard filter: never generated as a "mine" candidate at all.
            continue

        score, basis = _score_level_delta(
            planet, snapshot, weights, mine_entity.cost, **{_MINE_PRODUCTION_LEVER[mine_id]: 1}
        )
        out.append(
            Candidate(
                action=Action(
                    kind=ActionKind.BUILD,
                    function="startBuildingUpgrade",
                    planet_id=planet.planet_id,
                    entity_id=mine_id,
                    entity_name=mine_entity.name or ids.building_name(mine_id),
                    target_level=mine_entity.level + 1,
                    cost=mine_entity.cost,
                    rationale=(
                        f"{ids.building_name(mine_id)} ranked highest by value density "
                        f"(base rate x live multiplier) among mines with room to grow; "
                        f"{mine_entity.level}->{mine_entity.level + 1} needs {required_post} energy "
                        f"against {produced_now} produced -- energy-safe."
                    ),
                    expected_effect=build_time_savings_note(
                        mine_entity, _level(planet, ids.Building.ROBOTICS_FACTORY), _level(planet, ids.Building.NANITE_FACTORY)
                    ),
                ),
                family="mine",
                score=score,
                score_basis=basis,
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Energy family (rung 6's energy-substitute half): Solar Plant vs. Solar Satellite.
# --------------------------------------------------------------------------------------


def generate_energy_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """Solar Plant, Fusion Reactor (Phase 3) and (if unlocked) Solar Satellite as energy
    candidates, cheapest cost-per-energy-point first. Ported from `plan.py`'s pre-Phase-2
    `_energy_candidate`. Scored via `score_payback` against the planet's *current* levels
    -- typically `None` (raising future energy supply doesn't move
    `calc.production_per_hour`'s output until a mine level actually consumes it), except
    when the planet is already energy-throttled today (`scale_bps < 10000` at current
    levels), in which case more energy supply genuinely raises current output and is
    scored.

    **Fusion Reactor is deliberately NOT wired into `_cheapest_energy_choice`** (the
    function `select_building_candidate` uses to pick the energy-first *substitute* when
    a mine upgrade would be energy-unsafe) -- that comparison is pinned by
    `tests/test_plan.py::test_planet_hot_inverts_and_proposes_satellite` to exactly
    Solar Plant vs. Solar Satellite, and Fusion Reactor happens to be unlocked on that
    exact fixture, so adding it there would risk silently changing which source wins.
    Fusion Reactor is instead generated here as an ordinary scored/locked "energy" family
    candidate -- visible in `alternatives`, and reachable as a *winner* wherever a caller
    scores the full energy-candidate pool directly, without touching the pinned
    substitution branch. See docs/SPEC.md §5.4 Phase 3 and the WP report for the full
    reasoning."""
    if not policy.actions.allow_building:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    satellite_energy = _satellite_energy_per_unit(planet)
    weights = policy.strategy.resource_weights
    energy_technology_level = _energy_technology_level(snapshot)

    out: list[Candidate] = []

    fusion = _entity(planet.buildings, ids.Building.FUSION_REACTOR)
    if fusion is not None and fusion.level is not None:
        if not _unlocked(EntityFamily.BUILDING, ids.Building.FUSION_REACTOR, building_levels=building_levels, technology_levels=technology_levels):
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.BUILD,
                        function="startBuildingUpgrade",
                        planet_id=planet.planet_id,
                        entity_id=ids.Building.FUSION_REACTOR,
                        entity_name=fusion.name or ids.building_name(ids.Building.FUSION_REACTOR),
                        target_level=fusion.level + 1,
                        cost=fusion.cost,
                    ),
                    family="energy",
                    score=None,
                    score_basis=_describe_unmet(
                        EntityFamily.BUILDING, ids.Building.FUSION_REACTOR, building_levels=building_levels, technology_levels=technology_levels
                    ),
                )
            )
        else:
            gained = calc.fusion_energy(fusion.level + 1, energy_technology_level) - calc.fusion_energy(
                fusion.level, energy_technology_level
            )
            if gained > 0:
                score, basis = _score_level_delta(planet, snapshot, weights, fusion.cost, fusion_level=1)
                out.append(
                    Candidate(
                        action=Action(
                            kind=ActionKind.BUILD,
                            function="startBuildingUpgrade",
                            planet_id=planet.planet_id,
                            entity_id=ids.Building.FUSION_REACTOR,
                            entity_name=fusion.name or ids.building_name(ids.Building.FUSION_REACTOR),
                            target_level=fusion.level + 1,
                            cost=fusion.cost,
                            expected_effect=f"produced energy -> +{gained} (also raises deuterium upkeep -- calc.fusion_deuterium_upkeep)",
                        ),
                        family="energy",
                        score=score,
                        score_basis=basis,
                    )
                )

    solar = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
    if solar is not None and solar.level is not None:
        gained = calc.scaled_level(20, solar.level + 1) - calc.scaled_level(20, solar.level)
        if gained > 0:
            score, basis = _score_level_delta(planet, snapshot, weights, solar.cost, solar_level=1)
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.BUILD,
                        function="startBuildingUpgrade",
                        planet_id=planet.planet_id,
                        entity_id=ids.Building.SOLAR_PLANT,
                        entity_name=ids.building_name(ids.Building.SOLAR_PLANT),
                        target_level=solar.level + 1,
                        cost=solar.cost,
                        expected_effect=(
                            f"produced energy -> +{calc.scaled_level(20, solar.level + 1) - calc.scaled_level(20, solar.level)}"
                        ),
                    ),
                    family="energy",
                    score=score,
                    score_basis=basis,
                )
            )

    satellite = _entity(planet.ships, ids.Ship.SOLAR_SATELLITE)
    if satellite is not None and satellite_energy:
        if not _unlocked(EntityFamily.SHIP, ids.Ship.SOLAR_SATELLITE, building_levels=building_levels, technology_levels=technology_levels):
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.SHIP,
                        function="startShipProduction",
                        planet_id=planet.planet_id,
                        entity_id=ids.Ship.SOLAR_SATELLITE,
                        entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                        quantity=1,
                        cost=satellite.cost,
                    ),
                    family="energy",
                    score=None,
                    score_basis=_describe_unmet(
                        EntityFamily.SHIP, ids.Ship.SOLAR_SATELLITE, building_levels=building_levels, technology_levels=technology_levels
                    ),
                )
            )
        elif not policy.actions.allow_ships:
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.SHIP,
                        function="startShipProduction",
                        planet_id=planet.planet_id,
                        entity_id=ids.Ship.SOLAR_SATELLITE,
                        entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                        quantity=1,
                        cost=satellite.cost,
                    ),
                    family="energy",
                    score=None,
                    score_basis="policy.actions.allow_ships=false",
                )
            )
        else:
            score, basis = _score_level_delta(planet, snapshot, weights, satellite.cost, solar_satellite_count=1)
            out.append(
                Candidate(
                    action=Action(
                        kind=ActionKind.SHIP,
                        function="startShipProduction",
                        planet_id=planet.planet_id,
                        entity_id=ids.Ship.SOLAR_SATELLITE,
                        entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                        quantity=1,
                        cost=satellite.cost,
                        expected_effect=f"produced energy -> +{satellite_energy}",
                    ),
                    family="energy",
                    score=score,
                    score_basis=basis,
                )
            )
    return out


def _cheapest_energy_choice(
    planet: PlanetSnapshot,
    satellite_energy_per_unit: int | None,
    *,
    building_levels: dict[int, int | None],
    technology_levels: dict[int, int | None],
) -> tuple[float, str, Entity] | None:
    """`(cost_per_energy_point, "solar_plant" | "solar_satellite", live_entity)` for the
    cheaper *unlocked* option — ported verbatim from `plan.py`'s pre-Phase-2
    `_energy_candidate` (`allow_ships` gating alone is left to the caller,
    `select_building_candidate`, same division of labour the original function had
    between itself and its caller)."""
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
        and _unlocked(EntityFamily.SHIP, ids.Ship.SOLAR_SATELLITE, building_levels=building_levels, technology_levels=technology_levels)
    ):
        cost_total = satellite.cost.metal + satellite.cost.crystal + satellite.cost.deuterium
        options.append((cost_total / satellite_energy_per_unit, "solar_satellite", satellite))
    if not options:
        return None
    options.sort(key=lambda option: option[0])
    return options[0]


# --------------------------------------------------------------------------------------
# Infrastructure family (new in Phase 3, docs/SPEC.md §5.4 "Phase 3 of the general-
# strategy-engine program") -- Robotics Factory, Nanite Factory, Shipyard, Research Lab,
# Terraformer, Missile Silo as first-class candidates. Always `score=None` (none of these
# move `calc.production_per_hour` directly -- Fusion Reactor does, which is exactly why
# it lives in `generate_energy_candidates` instead, not here). Ordered by
# `policy.strategy.building_priority`; empty means this family never fires at all --
# these six buildings stay reachable only through whatever path already touched them
# pre-Phase-3 (none), which is what makes `building_priority` load-bearing rather than
# cosmetic.
# --------------------------------------------------------------------------------------

_INFRASTRUCTURE_BUILDING_IDS: tuple[int, ...] = (
    ids.Building.ROBOTICS_FACTORY,
    ids.Building.NANITE_FACTORY,
    ids.Building.SHIPYARD,
    ids.Building.RESEARCH_LAB,
    ids.Building.TERRAFORMER,
    ids.Building.MISSILE_SILO,
)


def _infrastructure_priority_order(policy: Policy) -> list[int]:
    """`policy.strategy.building_priority`'s named buildings first (in declared order,
    resolved via `ids.building_id` -- case-insensitive, raises `ValueError` on an unknown
    name per `_resolve_name_id`), then any of the six infrastructure ids not mentioned, in
    their `_INFRASTRUCTURE_BUILDING_IDS` default order. A `building_priority` name outside
    this family's six ids (e.g. a mine) is simply not relevant here and is dropped --
    `generate_infrastructure_candidates` only ever proposes the six infrastructure
    buildings, never a mine/storage/energy building through this path."""
    if not policy.strategy.building_priority:
        return list(_INFRASTRUCTURE_BUILDING_IDS)
    named = [_resolve_name_id(name, ids.building_id) for name in policy.strategy.building_priority]
    named = [building_id for building_id in named if building_id in _INFRASTRUCTURE_BUILDING_IDS]
    seen = set(named)
    remaining = [building_id for building_id in _INFRASTRUCTURE_BUILDING_IDS if building_id not in seen]
    return named + remaining


def generate_infrastructure_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """One `Candidate` per infrastructure building with live data on `planet`, in
    `_infrastructure_priority_order`. A locked building is still yielded (`score=None`,
    `"locked: ..."` basis) so it appears as an alternative with `techtree.describe()` in
    the reason, same convention `generate_research_candidates` already uses.

    Gated on `policy.strategy.building_priority` being non-empty, same as
    `generate_ship_target_candidates`/`generate_defense_target_candidates` gate on their
    own target lists -- this is the family's sole reachability switch (see
    `StrategyCfg.building_priority`'s docstring), so an empty list must return `[]`
    here directly, not merely be skipped by a caller that happens to check first."""
    if not policy.actions.allow_building or not policy.strategy.building_priority:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)

    out: list[Candidate] = []
    for building_id in _infrastructure_priority_order(policy):
        entity = _entity(planet.buildings, building_id)
        if entity is None or entity.level is None:
            continue
        action = Action(
            kind=ActionKind.BUILD,
            function="startBuildingUpgrade",
            planet_id=planet.planet_id,
            entity_id=building_id,
            entity_name=entity.name or ids.building_name(building_id),
            target_level=entity.level + 1,
            cost=entity.cost,
            rationale=(
                f"{ids.building_name(building_id)} is next in policy.strategy.building_priority "
                "order (or the default infrastructure order, if unset)."
            ),
        )
        unmet_reqs = unmet(EntityFamily.BUILDING, building_id, building_levels=building_levels, technology_levels=technology_levels)
        if unmet_reqs:
            out.append(
                Candidate(action=action, family="infrastructure", score=None, score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs))
            )
            continue
        out.append(
            Candidate(
                action=action,
                family="infrastructure",
                score=None,
                score_basis="policy-declared infrastructure order (score=None: not a calc.production_per_hour input)",
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Proactive storage (Phase 3): storage as a Band-2 (this economically-scored-building
# band's) candidate, not only the Band-1 deadline-driven one (`generate_storage_candidates`
# below). Activates `calc.storage_cap` (previously dead per docs/COVERAGE.md Part 3) so
# storage headroom is visible in `alternatives` well before the reactive overflow trigger
# fires. Always `score=None` -- same rule `generate_storage_candidates` documents, a
# storage-cap raise never moves `calc.production_per_hour`'s output -- this only changes
# *when* storage becomes visible as a candidate, never whether it can outrank a scored
# mine/energy pick.
# --------------------------------------------------------------------------------------


def generate_proactive_storage_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    if not policy.actions.allow_building:
        return []
    out: list[Candidate] = []
    for index, (building_id, current, per_hour, cap) in enumerate(
        (
            (ids.Building.METAL_STORAGE, planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            (ids.Building.CRYSTAL_STORAGE, planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            (ids.Building.DEUTERIUM_TANK, planet.resources_as_of_now.deuterium, planet.production_per_hour.deuterium, planet.storage_caps.deuterium),
        )
    ):
        entity = _entity(planet.buildings, building_id)
        if entity is None or entity.level is None:
            continue
        try:
            next_cap = calc.storage_cap(entity.level + 1)
        except ValueError:
            continue  # already at MAX_LEVEL (50) -- nothing more to propose
        hours = calc.hours_to_cap(current, per_hour, cap)
        if per_hour <= 0:
            headroom = "not currently producing this resource"
        elif hours is None:
            headroom = "never at current production"
        else:
            headroom = f"{hours:.1f}h to cap at current production"
        out.append(
            Candidate(
                action=Action(
                    kind=ActionKind.BUILD,
                    function="startBuildingUpgrade",
                    planet_id=planet.planet_id,
                    entity_id=building_id,
                    entity_name=entity.name or ids.building_name(building_id),
                    target_level=entity.level + 1,
                    cost=entity.cost,
                    expected_effect=f"storage cap {cap} -> {next_cap}",
                ),
                family="storage",
                score=None,
                score_basis=f"proactive: {_RESOURCE_LABELS[index]} {headroom} (cap {cap} -> {next_cap} at next level)",
            )
        )
    return out


# --------------------------------------------------------------------------------------
# select_building_candidate — rung 6's select step. Replays the exact pre-Phase-2 walk:
# priority-ordered mine, energy-first hard filter, fall through to the next mine only
# when no energy substitute is resolvable at all. See module docstring's AC23 note.
#
# Phase 3 adds one new precedence step ahead of the mine walk, gated entirely behind
# `policy.strategy.building_priority` being non-empty: an explicit `building_priority`
# is a declared human intent (the governing principle stated in the Phase 3 brief -- "the
# policy declares intent for everything else"), so it wins outright. Left unset (the
# default), this new step never fires and the function is behaviourally identical to
# Phase 2 (AC pinned in tests/test_candidates.py).
# --------------------------------------------------------------------------------------


def select_building_candidate(
    snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot
) -> tuple[Candidate | None, list[Candidate]]:
    if not policy.actions.allow_building:
        return None, []

    if policy.strategy.building_priority:
        infra_candidates = generate_infrastructure_candidates(snapshot, policy, planet)
        selectable_infra = [c for c in infra_candidates if not c.score_basis.startswith("locked:")]
        if selectable_infra:
            winner = selectable_infra[0]
            remaining_infra = [c for c in infra_candidates if c is not winner]
            mine_pool = generate_mine_candidates(snapshot, policy, planet)
            energy_pool = generate_energy_candidates(snapshot, policy, planet)
            storage_pool = generate_proactive_storage_candidates(snapshot, policy, planet)
            return winner, rank_candidates(remaining_infra + mine_pool + energy_pool + storage_pool)

    mine_candidates = {c.action.entity_id: c for c in generate_mine_candidates(snapshot, policy, planet)}
    energy_candidates = generate_energy_candidates(snapshot, policy, planet)
    proactive_storage_candidates = generate_proactive_storage_candidates(snapshot, policy, planet)
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    satellite_energy = _satellite_energy_per_unit(planet)
    produced_now = _produced_now(planet, snapshot)

    alternatives: list[Candidate] = []
    winner: Candidate | None = None

    for mine_id in _mine_priority_order(planet):
        mine_entity = _entity(planet.buildings, mine_id)
        if mine_entity is None or mine_entity.level is None:
            continue

        candidate = mine_candidates.get(mine_id)
        if candidate is not None and candidate.score_basis.startswith("locked:"):
            alternatives.append(candidate)
            continue
        if candidate is not None:
            winner = candidate
            break

        # No mine candidate at all -- either "no data" (already handled above) or the
        # energy-first hard filter excluded it. Distinguish by re-running the same safety
        # check `generate_mine_candidates` used (cheap, pure -- see `_mine_energy_safe`).
        safe, required_post = _mine_energy_safe(planet, snapshot, mine_id, produced_now=produced_now)
        if safe:
            # Shouldn't happen (generate_mine_candidates would have yielded it) -- but
            # never silently stall the ladder over an internal inconsistency.
            continue

        choice = _cheapest_energy_choice(
            planet, satellite_energy, building_levels=building_levels, technology_levels=technology_levels
        )
        if choice is None:
            continue
        _, kind, entity = choice
        if kind == "solar_satellite" and not policy.actions.allow_ships:
            solar_fallback = _entity(planet.buildings, ids.Building.SOLAR_PLANT)
            if solar_fallback is None:
                continue
            kind, entity = "solar_plant", solar_fallback

        chosen_energy = next((c for c in energy_candidates if c.action.entity_id == entity.id and c.action.function == ("startBuildingUpgrade" if kind == "solar_plant" else "startShipProduction")), None)
        if chosen_energy is None:
            continue
        rationale = (
            f"{ids.building_name(mine_id)} {mine_entity.level}->{mine_entity.level + 1} would need "
            f"{required_post} energy against {produced_now} produced. Energy-first invariant: "
            f"{'Solar Plant' if kind == 'solar_plant' else 'a Solar Satellite'} is currently the "
            f"cheaper energy source per point on this planet (satellite energy/unit={satellite_energy})."
        )
        winner = Candidate(
            action=chosen_energy.action.model_copy(update={"rationale": rationale}),
            family="energy",
            score=chosen_energy.score,
            score_basis=chosen_energy.score_basis,
        )
        alternatives.extend(c for c in energy_candidates if c is not chosen_energy)
        break

    if winner is None:
        return None, []

    # Everything else this planet's mine/energy generators produced, minus the winner,
    # becomes an informational alternative -- ranked, deduped by (family, entity_id).
    # `seen` starts pre-populated with the winner AND anything already queued into
    # `alternatives` above (the energy-substitution branch adds the non-chosen energy
    # option there directly) so this pass never re-adds the same candidate twice.
    seen = {(winner.family, winner.action.entity_id)}
    seen.update((c.family, c.action.entity_id) for c in alternatives)
    for pool in (mine_candidates.values(), energy_candidates, proactive_storage_candidates):
        for candidate in pool:
            key = (candidate.family, candidate.action.entity_id)
            if key in seen:
                continue
            seen.add(key)
            alternatives.append(candidate)

    return winner, rank_candidates(alternatives)


# --------------------------------------------------------------------------------------
# Storage family (rung 5) — deadline-driven, not economically scored.
# --------------------------------------------------------------------------------------

_RESOURCE_LABELS = ("metal", "crystal", "deuterium")
_STORAGE_BUILDING_FOR_RESOURCE = {
    0: ids.Building.METAL_STORAGE,
    1: ids.Building.CRYSTAL_STORAGE,
    2: ids.Building.DEUTERIUM_TANK,
}


def _most_urgent_overflow(target_planets: list[PlanetSnapshot], trigger_hours: float) -> tuple[PlanetSnapshot, int, float] | None:
    worst: tuple[PlanetSnapshot, int, float] | None = None
    for planet in target_planets:
        triples = (
            (planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            (planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            (planet.resources_as_of_now.deuterium, planet.production_per_hour.deuterium, planet.storage_caps.deuterium),
        )
        for index, (current, per_hour, cap) in enumerate(triples):
            hours = calc.hours_to_cap(current, per_hour, cap)
            if hours is None or hours > trigger_hours:
                continue
            if worst is None or hours < worst[2]:
                worst = (planet, index, hours)
    return worst


def generate_storage_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """The matching storage building for every resource within
    `policy.storage.hours_to_cap_trigger` of its cap on `planet` -- deadline-driven, so
    always `score=None` (a storage-cap raise never moves `calc.production_per_hour`'s
    output). Building-queue-busy is a filter here, same as the pre-Phase-2 code: the
    contract allows only one active `BuildingConstruction` per planet."""
    out: list[Candidate] = []
    if planet.queues.get(QueueKind.BUILDING) is not None:
        return out
    for index, (current, per_hour, cap) in enumerate(
        (
            (planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            (planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            (planet.resources_as_of_now.deuterium, planet.production_per_hour.deuterium, planet.storage_caps.deuterium),
        )
    ):
        hours = calc.hours_to_cap(current, per_hour, cap)
        if hours is None or hours > policy.storage.hours_to_cap_trigger:
            continue
        building_id = _STORAGE_BUILDING_FOR_RESOURCE[index]
        entity = _entity(planet.buildings, building_id)
        if entity is None or entity.level is None:
            continue
        out.append(
            Candidate(
                action=Action(
                    kind=ActionKind.BUILD,
                    function="startBuildingUpgrade",
                    planet_id=planet.planet_id,
                    entity_id=building_id,
                    entity_name=entity.name or ids.building_name(building_id),
                    target_level=entity.level + 1,
                    cost=entity.cost,
                ),
                family="storage",
                score=None,
                score_basis=f"deadline: {_RESOURCE_LABELS[index]} {hours:.1f}h from cap (trigger {policy.storage.hours_to_cap_trigger}h)",
            )
        )
    return out


def select_storage_candidate(
    snapshot: Snapshot, policy: Policy, target_planets: list[PlanetSnapshot]
) -> tuple[Candidate | None, list[Candidate]]:
    """Ported from `plan.py`'s pre-Phase-2 `_storage_overflow_action`: find the single
    most urgent overflow across `target_planets`; try to "spend it" via the ordinary
    building pick first (`select_building_candidate`); only reach for the matching
    storage building if that comes back empty (queue idle but nothing else to spend on).
    """
    overflow = _most_urgent_overflow(target_planets, policy.storage.hours_to_cap_trigger)
    if overflow is None:
        return None, []
    planet, resource_index, hours = overflow
    label = _RESOURCE_LABELS[resource_index]
    if planet.queues.get(QueueKind.BUILDING) is not None:
        return None, []

    spend_winner, spend_alternatives = select_building_candidate(snapshot, policy, planet)
    if spend_winner is not None:
        rationale = (
            f"Planet {planet.planet_id} {label} is {hours:.1f}h from its storage cap "
            f"(trigger {policy.storage.hours_to_cap_trigger}h) -- spending it via the "
            f"normal next-building pick. {spend_winner.action.rationale}"
        )
        winner = Candidate(
            action=spend_winner.action.model_copy(update={"rationale": rationale}),
            family=spend_winner.family,
            score=spend_winner.score,
            score_basis=spend_winner.score_basis,
        )
        return winner, spend_alternatives

    storage_candidates = generate_storage_candidates(snapshot, policy, planet)
    chosen = next((c for c in storage_candidates if c.action.entity_id == _STORAGE_BUILDING_FOR_RESOURCE[resource_index]), None)
    if chosen is None or not policy.actions.allow_building:
        return None, []
    rationale = (
        f"Planet {planet.planet_id} {label} is {hours:.1f}h from its storage cap and no "
        f"ordinary next-building spend was available right now (queue is idle) -- "
        f"upgrading {chosen.action.entity_name} instead."
    )
    winner = Candidate(
        action=chosen.action.model_copy(update={"rationale": rationale}),
        family=chosen.family,
        score=chosen.score,
        score_basis=chosen.score_basis,
    )
    return winner, [c for c in storage_candidates if c is not chosen]


# --------------------------------------------------------------------------------------
# Research family (rung 7) — policy-declared, always unscored (nothing in `calc.py`
# models a technology moving `production_per_hour`).
# --------------------------------------------------------------------------------------


def _research_priority_order(snapshot: Snapshot, policy: Policy) -> tuple[list[int], set[int]]:
    """`(order, declared_ids)` -- `order` is every technology id known to the snapshot,
    `policy.strategy.research_priority`'s named technologies first (resolved via
    `ids.technology_id`, case-insensitive, raises loudly on an unknown name per
    `_resolve_name_id`), then the remaining ids in the pre-Phase-3 lowest-level-then-id
    fallback order. `declared_ids` is which of those ids came from `research_priority`,
    so the caller can label a pick as policy-declared-by-name vs. the default fallback.
    Empty `research_priority` returns the fallback order unchanged and an empty
    `declared_ids` set -- exactly Phase 2's ordering."""
    fallback_order = [t.id for t in sorted(snapshot.technologies, key=lambda t: ((t.level or 0), t.id))]
    if not policy.strategy.research_priority:
        return fallback_order, set()
    known_ids = {t.id for t in snapshot.technologies}
    declared = [_resolve_name_id(name, ids.technology_id) for name in policy.strategy.research_priority]
    declared = [tid for tid in declared if tid in known_ids]
    seen = set(declared)
    remainder = [tid for tid in fallback_order if tid not in seen]
    return declared + remainder, seen


def generate_research_candidates(snapshot: Snapshot, policy: Policy, target_planets: list[PlanetSnapshot]) -> list[Candidate]:
    """Every technology, ordered by `policy.strategy.research_priority` first (Phase 3),
    then the pre-Phase-2 lowest-current-level-account-wide fallback (ties by ascending
    id) -- `_research_priority_order` computes the combined order; empty
    `research_priority` reproduces the pre-Phase-3 order exactly. The Research Lab
    prerequisite is planet-scoped even though the queue itself is per-player, so it's
    read from `target_planets[0]` -- the planet `startResearch` would actually be
    submitted through (see the original `_next_research_action`'s docstring for why)."""
    if not policy.actions.allow_research or not snapshot.technologies or not target_planets:
        return []
    planet = target_planets[0]
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    order, declared_ids = _research_priority_order(snapshot, policy)
    by_id = {t.id: t for t in snapshot.technologies}
    declared_positions = {tid: position + 1 for position, tid in enumerate(tid for tid in order if tid in declared_ids)}

    out: list[Candidate] = []
    for tech_id in order:
        candidate = by_id.get(tech_id)
        if candidate is None:
            continue
        unmet_reqs = unmet(EntityFamily.RESEARCH, candidate.id, building_levels=building_levels, technology_levels=technology_levels)
        if candidate.id in declared_ids:
            rationale = (
                f"{candidate.name or ids.technology_name(candidate.id)} is next in "
                "policy.strategy.research_priority order."
            )
            basis = f"policy-declared via research_priority (position {declared_positions[candidate.id]})"
        else:
            rationale = (
                f"{candidate.name or ids.technology_name(candidate.id)} is the lowest-level "
                f"unlocked technology account-wide (level {candidate.level or 0}); research queue is idle."
            )
            basis = "default: lowest unlocked level/id (no policy.strategy.research_priority match)"
        action = Action(
            kind=ActionKind.RESEARCH,
            function="startResearch",
            planet_id=planet.planet_id,
            entity_id=candidate.id,
            entity_name=candidate.name or ids.technology_name(candidate.id),
            target_level=(candidate.level or 0) + 1,
            cost=candidate.cost,
            rationale=rationale,
        )
        if unmet_reqs:
            out.append(
                Candidate(
                    action=action,
                    family="research",
                    score=None,
                    score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs),
                )
            )
            continue
        out.append(Candidate(action=action, family="research", score=None, score_basis=basis))
    return out


def select_research_candidate(
    snapshot: Snapshot, policy: Policy, target_planets: list[PlanetSnapshot]
) -> tuple[Candidate | None, list[Candidate]]:
    candidates = generate_research_candidates(snapshot, policy, target_planets)
    winner = next((c for c in candidates if not c.score_basis.startswith("locked:")), None)
    if winner is None:
        return None, []
    alternatives = [c for c in candidates if c is not winner]
    return winner, rank_candidates(alternatives)


# --------------------------------------------------------------------------------------
# Ship / defense families (rung 8) — policy-declared, gated on `_economy_on_track`.
# --------------------------------------------------------------------------------------


def economy_on_track(snapshot: Snapshot, target_planets: list[PlanetSnapshot]) -> bool:
    """"On track" = something is already actively building or researching. Ported
    verbatim from `plan.py`'s pre-Phase-2 `_economy_on_track`."""
    if snapshot.research_queue is not None:
        return True
    return any(planet.queues.get(QueueKind.BUILDING) is not None for planet in target_planets)


def _generate_satellite_ship_candidate(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """A Solar Satellite when it's currently the cheaper energy source per point on this
    planet -- ported from `plan.py`'s pre-Phase-2 `_shipyard_action` ship branch. Scored
    the same way `generate_energy_candidates` scores a satellite (usually `None` -- see
    that function's docstring). **This is Solar Satellite's separate energy-driven path
    -- Phase 3's `ship_targets` stock-keeping (`generate_ship_target_candidates` below)
    never touches it and never substitutes for it.** Caller (`generate_ship_candidates`)
    already applied the `allow_ships`/ship-queue-idle guard."""
    satellite_energy = _satellite_energy_per_unit(planet)
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    choice = _cheapest_energy_choice(
        planet, satellite_energy, building_levels=building_levels, technology_levels=technology_levels
    )
    if choice is None or choice[1] != "solar_satellite":
        return []
    _, _, entity = choice
    weights = policy.strategy.resource_weights
    score, basis = _score_level_delta(planet, snapshot, weights, entity.cost, solar_satellite_count=1)
    return [
        Candidate(
            action=Action(
                kind=ActionKind.SHIP,
                function="startShipProduction",
                planet_id=planet.planet_id,
                entity_id=ids.Ship.SOLAR_SATELLITE,
                entity_name=ids.ship_name(ids.Ship.SOLAR_SATELLITE),
                quantity=1,
                cost=entity.cost,
                rationale=(
                    "Shipyard idle, economy on track, and a Solar Satellite is currently "
                    "the cheaper energy source per point on this planet."
                ),
            ),
            family="ship",
            score=score,
            score_basis=basis,
        )
    ]


def generate_crawler_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """Crawler (Ship id 15, non-flyable -- produced via `startShipProduction`, never
    flown; Phase 3) scored via the marginal `calc.crawler_boost_bps` effect on
    `calc.production_per_hour`, the same `score_payback` mechanism every other scored
    family uses. `calc.production_per_hour` already enforces both contract caps
    internally (`crawler_boost_bps`'s own `effective_cap = combined_mine_level * 8` and
    the flat 5,000 bps ceiling), so a crawler count already at either cap simply produces
    a zero-delta score (`score_payback` returns `None`, "no production_per_hour change")
    without any extra cap logic here -- **except** when the live
    `PlanetSnapshot.crawler_production.capped` is already `True`, in which case this
    short-circuits before doing the delta computation at all and says so plainly (Phase
    3's own "prefer the API's own numbers over recomputing" instruction, same posture
    `energy.solar_satellite_energy` already takes)."""
    crawler = _entity(planet.ships, ids.Ship.CRAWLER)
    if crawler is None or crawler.count is None:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    entity_name = crawler.name or ids.ship_name(ids.Ship.CRAWLER)

    action = Action(
        kind=ActionKind.SHIP,
        function="startShipProduction",
        planet_id=planet.planet_id,
        entity_id=ids.Ship.CRAWLER,
        entity_name=entity_name,
        quantity=1,
        cost=crawler.cost,
        rationale=(
            "Crawler adds a mine-production boost (calc.crawler_boost_bps), capped at 8 "
            "per combined mine level and 5,000 bps total."
        ),
    )

    unmet_reqs = unmet(EntityFamily.SHIP, ids.Ship.CRAWLER, building_levels=building_levels, technology_levels=technology_levels)
    if unmet_reqs:
        return [Candidate(action=action, family="crawler", score=None, score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs))]

    if planet.crawler_production is not None and planet.crawler_production.capped:
        live = planet.crawler_production
        return [
            Candidate(
                action=action,
                family="crawler",
                score=None,
                score_basis=(
                    f"at boost cap -- live crawlerProduction.capped=true (effective {live.effective}"
                    f"/{live.max_effective}, boostBps={live.boost_bps})"
                ),
            )
        ]

    weights = policy.strategy.resource_weights
    score, basis = _score_level_delta(planet, snapshot, weights, crawler.cost, crawler_count=1)
    if planet.crawler_production is not None:
        live = planet.crawler_production
        basis = f"{basis} (live crawlerProduction: effective {live.effective}/{live.max_effective}, boostBps={live.boost_bps})"
    return [Candidate(action=action, family="crawler", score=score, score_basis=basis)]


def generate_ship_target_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """Stock-keeping toward `policy.strategy.ship_targets` (Phase 3): for every declared
    target below its live `Entity.count`, propose one more unit, filtered through
    `techtree.unmet()`. **Never touches Solar Satellite's separate energy-driven path**
    (`_generate_satellite_ship_candidate`) -- a `ship_targets` entry naming Solar
    Satellite would stock-keep it as an ordinary policy-declared target alongside every
    other ship, entirely independent of the energy-driven mechanism. Empty
    `ship_targets` (the default) returns `[]`, reproducing pre-Phase-3 behaviour exactly.
    A target already at or above its declared count yields nothing for that target
    (not even a locked/informational entry) -- there is nothing left to propose. A
    target whose live `Entity` is missing/uncounted (`count is None`) is skipped, not
    treated as "0 built" -- fails closed on absent data, same rule as everywhere else in
    this module."""
    if not policy.strategy.ship_targets:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)

    out: list[Candidate] = []
    for target in policy.strategy.ship_targets:
        entity_id = _resolve_target_id(target, ids.ship_id)
        entity = _entity(planet.ships, entity_id)
        if entity is None or entity.count is None:
            continue
        if entity.count >= target.count:
            continue
        entity_name = entity.name or ids.ship_name(entity_id)
        action = Action(
            kind=ActionKind.SHIP,
            function="startShipProduction",
            planet_id=planet.planet_id,
            entity_id=entity_id,
            entity_name=entity_name,
            quantity=1,
            cost=entity.cost,
            rationale=(
                f"policy.strategy.ship_targets declares {target.count}x {entity_name}; "
                f"currently {entity.count} built/queued."
            ),
        )
        unmet_reqs = unmet(EntityFamily.SHIP, entity_id, building_levels=building_levels, technology_levels=technology_levels)
        if unmet_reqs:
            out.append(Candidate(action=action, family="ship", score=None, score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs)))
            continue
        out.append(
            Candidate(
                action=action,
                family="ship",
                score=None,
                score_basis=f"policy-declared stock target ({entity.count}/{target.count})",
            )
        )
    return out


def generate_ship_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """The "ships" family: Solar Satellite's separate energy-driven path first (unchanged
    priority from pre-Phase-3), then Crawler (scored), then `ship_targets` stock-keeping
    (policy-declared) -- all gated once, here, on `allow_ships`/ship-queue-idle. With
    `ship_targets` empty and Crawler locked/absent, this returns exactly what pre-Phase-3
    `generate_ship_candidates` returned (AC: docs/SPEC.md §9, "empty strategy targets
    reproduce Phase 2 behaviour exactly")."""
    if not policy.actions.allow_ships or planet.queues.get(QueueKind.SHIP) is not None:
        return []
    return (
        _generate_satellite_ship_candidate(snapshot, policy, planet)
        + generate_crawler_candidates(snapshot, policy, planet)
        + generate_ship_target_candidates(snapshot, policy, planet)
    )


# --------------------------------------------------------------------------------------
# Defense targets + caps (Phase 3). `_queued_defense_quantity`/`_defense_capacity_reason`
# independently re-derive `_requireDefenseCapacity`
# (`VeydriftDefenseProductionModule.sol:352-380`) from `candidates.py`'s own side --
# deliberately NOT shared code with `guard.py`'s `_defense_cap_violation`, the same
# defense-in-depth posture `guard.py`'s `_gate_energy` already takes toward `plan.py`'s
# energy invariant (two independent implementations of the same contract rule, so a bug
# in one is unlikely to also be in the other). `PlanetSnapshot` carries only a single
# `QueueEntry | None` per `QueueKind.DEFENSE` (no backlog list -- `models.py`'s frozen
# shape), so -- like `guard.py` -- this can account for at most one queued item, not an
# arbitrarily deep backlog; that under-counts a deeper real backlog, which is the
# safe-to-under-restrict-yourself direction for a cap check, never the safe-to-
# vacuously-pass one.
# --------------------------------------------------------------------------------------


def _queued_defense_quantity(planet: PlanetSnapshot, defense_id: int) -> int:
    entry = planet.queues.get(QueueKind.DEFENSE)
    if entry is None or entry.entity_id != defense_id:
        return 0
    return entry.quantity or 0


def _defense_capacity_reason(planet: PlanetSnapshot, defense_id: int, quantity: int) -> str | None:
    """`None` when nothing is violated; otherwise a detail string suitable for a
    `"locked: ..."` `score_basis` (this module's convention for "never selectable as a
    winner, but still visible as an alternative"). Fails closed: a built/queued count or
    a missile-silo level the snapshot didn't report BLOCKs (via the "locked:" prefix)
    rather than being treated as zero -- `missile_silo_level is None` must never read as
    0, per AGENTS.md §5 and this phase's own brief."""
    cap = MAX_DEFENSE_PER_PLANET.get(defense_id)
    if cap is not None:
        built_entity = _entity(planet.defenses, defense_id)
        if built_entity is None or built_entity.count is None:
            return f"{ids.defense_name(defense_id)} count not reported for planet {planet.planet_id}; cannot verify the {cap}-per-planet cap"
        queued = _queued_defense_quantity(planet, defense_id)
        projected = built_entity.count + queued + quantity
        if projected > cap:
            return (
                f"{ids.defense_name(defense_id)} is capped at {cap} per planet "
                f"(built {built_entity.count} + queued {queued} + this action {quantity} = {projected})"
            )

    slots_per_unit = MISSILE_SLOTS.get(defense_id, 0)
    if slots_per_unit:
        if planet.missile_silo_level is None:
            return f"Missile Silo level not reported for planet {planet.planet_id}; cannot verify missile slot capacity"
        capacity = missile_silo_capacity(planet.missile_silo_level)
        used = 0
        for missile_id, slots in MISSILE_SLOTS.items():
            count_entity = _entity(planet.defenses, missile_id)
            if count_entity is None or count_entity.count is None:
                return f"{ids.defense_name(missile_id)} count not reported for planet {planet.planet_id}; cannot verify missile slot capacity"
            used += slots * (count_entity.count + _queued_defense_quantity(planet, missile_id))
        requested = slots_per_unit * quantity
        if used + requested > capacity:
            return (
                f"{ids.defense_name(defense_id)} would use {requested} missile silo slot(s); "
                f"{used} already used/queued against a capacity of {capacity} "
                f"(Missile Silo level {planet.missile_silo_level})"
            )
    return None


def generate_defense_target_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """Stock-keeping toward `policy.strategy.defense_targets`, the defense-family
    counterpart to `generate_ship_target_candidates` -- same below-count/locked/absent-
    data rules, plus `_defense_capacity_reason`'s shield-dome and missile-silo caps."""
    if not policy.strategy.defense_targets:
        return []
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)

    out: list[Candidate] = []
    for target in policy.strategy.defense_targets:
        entity_id = _resolve_target_id(target, ids.defense_id)
        entity = _entity(planet.defenses, entity_id)
        if entity is None or entity.count is None:
            continue
        if entity.count >= target.count:
            continue
        entity_name = entity.name or ids.defense_name(entity_id)
        action = Action(
            kind=ActionKind.DEFENSE,
            function="startDefenseProduction",
            planet_id=planet.planet_id,
            entity_id=entity_id,
            entity_name=entity_name,
            quantity=1,
            cost=entity.cost,
            rationale=(
                f"policy.strategy.defense_targets declares {target.count}x {entity_name}; "
                f"currently {entity.count} built/queued."
            ),
        )
        unmet_reqs = unmet(EntityFamily.DEFENSE, entity_id, building_levels=building_levels, technology_levels=technology_levels)
        if unmet_reqs:
            out.append(Candidate(action=action, family="defense", score=None, score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs)))
            continue
        cap_reason = _defense_capacity_reason(planet, entity_id, 1)
        if cap_reason is not None:
            out.append(Candidate(action=action, family="defense", score=None, score_basis=f"locked: {cap_reason}"))
            continue
        out.append(
            Candidate(
                action=action,
                family="defense",
                score=None,
                score_basis=f"policy-declared stock target ({entity.count}/{target.count})",
            )
        )
    return out


def _generate_default_rocket_launcher_candidate(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """The pre-Phase-3 hardcoded default: a single Rocket Launcher, unconditionally --
    ported verbatim from `plan.py`'s pre-Phase-2 `_shipyard_action` defense branch. Fires
    only when `policy.strategy.defense_targets` is empty (`generate_defense_candidates`
    below), preserving Phase 2 behaviour exactly for the AC in docs/SPEC.md §9."""
    building_levels = _level_vector(planet.buildings)
    technology_levels = _level_vector(snapshot.technologies)
    unmet_reqs = unmet(EntityFamily.DEFENSE, ids.Defense.ROCKET_LAUNCHER, building_levels=building_levels, technology_levels=technology_levels)
    entity = _entity(planet.defenses, ids.Defense.ROCKET_LAUNCHER)
    if entity is None or entity.count is None:
        return []
    action = Action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        planet_id=planet.planet_id,
        entity_id=ids.Defense.ROCKET_LAUNCHER,
        entity_name=entity.name or ids.defense_name(ids.Defense.ROCKET_LAUNCHER),
        quantity=1,
        cost=entity.cost,
        rationale=(
            "Defense queue idle, economy on track, allow_defense=true; Rocket Launcher is "
            "the cheapest defense entry and a reasonable policy-driven default in the "
            "absence of a threat model."
        ),
    )
    if unmet_reqs:
        return [Candidate(action=action, family="defense", score=None, score_basis="locked: " + "; ".join(describe(r) for r in unmet_reqs))]
    return [Candidate(action=action, family="defense", score=None, score_basis="policy-declared; cheapest defense entry")]


def generate_defense_candidates(snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot) -> list[Candidate]:
    """`defense_targets` (Phase 3), when declared, entirely replaces the pre-Phase-3
    hardcoded Rocket-Launcher-only default -- a human who declares explicit defense
    intent has superseded the "reasonable policy-driven default in the absence of a
    threat model" the old comment describes. Empty `defense_targets` (the default)
    reproduces the old hardcoded behaviour exactly."""
    if not policy.actions.allow_defense or planet.queues.get(QueueKind.DEFENSE) is not None:
        return []
    if policy.strategy.defense_targets:
        return generate_defense_target_candidates(snapshot, policy, planet)
    return _generate_default_rocket_launcher_candidate(snapshot, policy, planet)


def select_shipyard_candidate(
    snapshot: Snapshot, policy: Policy, target_planets: list[PlanetSnapshot]
) -> tuple[Candidate | None, list[Candidate]]:
    """Ported from `plan.py`'s pre-Phase-2 `_shipyard_action`: per target planet, ship
    branch before defense branch, first hit wins. Phase 3: both branches now filter out
    `"locked:"` candidates before picking a winner (pre-Phase-3, `generate_ship_candidates`
    could never yield a locked entry, so no filter was needed there; Crawler/`ship_targets`
    now can). Among selectable ships, the best-scored one wins (ties/all-unscored fall
    back to generation order, i.e. Solar Satellite's priority is unchanged when nothing
    new is configured) -- `rank_candidates` already implements exactly that ordering."""
    if not target_planets or not economy_on_track(snapshot, target_planets):
        return None, []
    alternatives: list[Candidate] = []
    for planet in target_planets:
        ships = generate_ship_candidates(snapshot, policy, planet)
        selectable_ships = [c for c in ships if not c.score_basis.startswith("locked:")]
        if selectable_ships:
            winner = rank_candidates(selectable_ships)[0]
            defenses = generate_defense_candidates(snapshot, policy, planet)
            remaining_ships = [c for c in ships if c is not winner]
            return winner, rank_candidates(alternatives + remaining_ships + defenses)
        if ships:
            alternatives.extend(ships)
        defenses = generate_defense_candidates(snapshot, policy, planet)
        selectable = [c for c in defenses if not c.score_basis.startswith("locked:")]
        if selectable:
            return selectable[0], rank_candidates(alternatives + [c for c in defenses if c is not selectable[0]])
        alternatives.extend(defenses)
    return None, []


# --------------------------------------------------------------------------------------
# Ranking — shared by every `select_*` function's alternatives list. Scored candidates
# sort ascending by payback hours (cheapest first); unscored candidates are always
# ranked after every scored one, in generation order among themselves. This is purely
# for the human-facing alternatives list -- see module docstring for why the actual
# *selection* (the winning Action) never uses this function.
# --------------------------------------------------------------------------------------


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    scored = [c for c in candidates if c.score is not None]
    unscored = [c for c in candidates if c.score is None]
    scored.sort(key=lambda c: c.score)
    return [*scored, *unscored]
