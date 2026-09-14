"""Builds `Action.brief` (`models.Briefing`) — a structured explanation attached to an
on-chain `Action` *after* the planner (or a manual override) has already decided.

Two things this closes that a bare `rationale` string doesn't:

1. **Fuller recommendations.** `rationale`/`expected_effect` are free text; this adds
   the same shape every proposal explains itself in — goal, prerequisites, queue impact,
   timing, and a ranked list of risks (`risks[0]`, if any, is the "major risk").
2. **Observed vs inferred.** A `rationale` today mixes live API values with planner math
   in one sentence. `Briefing.observed` lists only values read straight off `Snapshot`
   (each fact's `source` names the field); `Briefing.inferred` lists the planner's own
   derivations (each fact's `source` names the calculation). A reader — or a promotion
   review, `references/strategy-playbook.md` §11 — can tell which is which without
   re-deriving anything.

**Purely informational, exactly like `Action.alternatives`.** `attach()` runs after
`plan.py`/`tick.py` have already produced the `Action`; nothing here feeds back into
`guard.py` or any `Decision`. `guard.py` never reads `.brief` — see
`tests/test_brief.py::test_guard_never_reads_brief`.

**Fails closed on absent data, the same way `guard.py`/`techtree.unmet` do (AGENTS.md
§5).** A missing snapshot value renders as the literal string `"unknown"`, never a
substituted `0`, and raises the `data_unavailable` risk rather than silently omitting
the fact. Deliberately reuses existing formulas rather than re-deriving them a third
time: `calc.py` for durations, `techtree.py` for prerequisites, and the `Candidate`'s own
`score_basis` (threaded in via `score_basis=`) for the ROI/deadline reasoning that
already justified the winning candidate — never a fresh cost-scaling computation (the
`calc.py` invariant: live cost always comes from the API's `cost` field).

Pure and offline: no network calls, no `walletctl`/`veydrift-wallet` dependency. Risk
rules deliberately do NOT re-derive `guard.py`'s own fleet-mission spend/energy math
(`_derive_fleet_mission_spend`, the post-upgrade energy re-check) — that's `guard.py`'s
job, checked independently at send time; a third copy here would be exactly the kind of
formula drift AGENTS.md §5 warns about (the tier-map/allowlist drift incident). Where a
risk needs a spend figure, it uses `Action.cost` (the same value the winning candidate
was scored/selected on), documented as such in each risk's `detail`.
"""

from __future__ import annotations

from veydrift_agent import calc, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    BriefFact,
    Briefing,
    BriefRisk,
    PlanetSnapshot,
    Policy,
    QueueKind,
    Snapshot,
)
from veydrift_agent.techtree import EntityFamily, describe, unmet

_UNKNOWN = "unknown"

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

#: Goal text by `Action.rule`. Only rules a planner-produced on-chain `Action` can carry
#: need an entry here -- every veto rung (0-4, 9:no-match) and 1b's escalate branch
#: produce an off-chain Action (`function is None`), which `attach()` skips before this
#: map is ever consulted. `tests/test_brief.py::test_every_planner_rule_has_a_goal`
#: greps `plan.py`'s own `rule=` literals against this map so a new rung can't ship
#: without one.
_GOAL_BY_RULE: dict[str, str] = {
    "3:mission-resolving": (
        "Resolve a fleet mission that has finished traveling, so its outcome and cargo "
        "settle on-chain. Permissionless and free."
    ),
    "5:storage-overflow-storage": "Build storage capacity before a resource hits its cap and further production is wasted.",
    "5:storage-overflow-spend": "Spend a resource that is about to hit its storage cap, before more production is wasted.",
    "6:building-queue-empty": "Grow the economy: queue the next building upgrade, ranked highest by payback.",
    "7:research-queue-empty": (
        "Advance a declared research priority (or the lowest-level undeclared technology) "
        "while the research queue is idle."
    ),
    "8:shipyard-idle": "Produce toward a declared ship/defense standing-count target while the shipyard is idle.",
    "8b:unlock-chain": "Build the next prerequisite on the path toward a currently-locked declared target.",
    "8c:logistics-transport": "Move resources between this account's own planets using idle fleet capacity.",
    "8c:logistics-deploy": "Permanently reposition ships to the declared fleet home planet.",
    "8c:logistics-harvest": "Recover this planet's own debris field.",
    "8c:logistics-harvest-foreign": "Recover a third party's debris field.",
    "8d:colonize": "Consume a built Colony Ship to claim a new planet at a free coordinate.",
    "8e:attack": "Attack another player's planet for its raidable resources. Risks the committed fleet in combat.",
    "8f:missile": "Fire Interplanetary Missiles at a target's defenses. No fleet risked; fully synchronous.",
}

_MANUAL_OVERRIDE_GOAL = "A human or scripted override chose this action directly, bypassing the planner's own ladder."

#: `Action.kind` -> `techtree.EntityFamily`, for the kinds whose `entity_id` names
#: something the techtree tracks prerequisites for. Kinds absent here (fleet missions,
#: missile, defense hold, alliance, resolve-mission) get no prerequisites section --
#: their real preconditions are live-state (ownership, queue availability, resource
#: balances), not a static unlock chain, and are already independently re-checked by
#: `guard.py` at send time.
_FAMILY_BY_KIND: dict[ActionKind, EntityFamily] = {
    ActionKind.BUILD: EntityFamily.BUILDING,
    ActionKind.RESEARCH: EntityFamily.RESEARCH,
    ActionKind.SHIP: EntityFamily.SHIP,
    ActionKind.DEFENSE: EntityFamily.DEFENSE,
}

_COMBAT_MISSION_TYPES = {
    ids.FleetMissionType.ATTACK,
    ids.FleetMissionType.ACS_DEFEND,
    ids.FleetMissionType.INTERCEPT,
}


def attach(action: Action, snapshot: Snapshot, policy: Policy, *, score_basis: str | None = None) -> Action:
    """Returns `action` with `.brief` set, or `action` unchanged for any off-chain kind
    (noop/escalate/halt -- `function is None`, `Action.is_onchain()` false). Never
    raises: a lookup failure inside becomes a `data_unavailable` risk and `"unknown"`
    facts, not an exception -- this module must never be why a tick fails."""
    if not action.is_onchain():
        return action
    briefing = _build(action, snapshot, policy, score_basis=score_basis)
    return action.model_copy(update={"brief": briefing})


# --------------------------------------------------------------------------------------
# Small, local helpers. `_level`/`_level_vector` duplicate a two-line lookup already
# written, separately, in both `candidates.py` and `guard.py` -- the same precedent
# `candidates.build_time_savings_note`'s own docstring notes for a smaller duplication;
# a plain "level of this building on this planet" lookup is not a formula worth sharing
# across a read-only, best-effort reporting module and the two enforcement modules.
# --------------------------------------------------------------------------------------


def _level(planet: PlanetSnapshot, building_id: int) -> int:
    entity = next((b for b in planet.buildings if b.id == building_id), None)
    return entity.level if entity is not None and entity.level is not None else 0


def _building_levels(planet: PlanetSnapshot) -> dict[int, int | None]:
    return {e.id: e.level for e in planet.buildings}


def _technology_levels(snapshot: Snapshot) -> dict[int, int | None]:
    return {e.id: e.level for e in snapshot.technologies}


def _fmt(value: int | None) -> str:
    return _UNKNOWN if value is None else f"{value:,}"


def _fact(label: str, value: str, source: str) -> BriefFact:
    return BriefFact(label=label, value=value, source=source)


def _sorted_risks(risks: list[BriefRisk]) -> list[BriefRisk]:
    return sorted(risks, key=lambda r: _SEVERITY_RANK[r.severity])


# --------------------------------------------------------------------------------------
# The builder.
# --------------------------------------------------------------------------------------


def _build(action: Action, snapshot: Snapshot, policy: Policy, *, score_basis: str | None) -> Briefing:
    goal = _GOAL_BY_RULE.get(action.rule, "")
    if not goal and action.source == "manual_override":
        goal = _MANUAL_OVERRIDE_GOAL
    if not goal:
        goal = f"Planner-selected action (no goal text documented yet for rule {action.rule!r})."

    observed: list[BriefFact] = []
    inferred: list[BriefFact] = []
    prerequisites: list[str] = []
    queue_impact = ""
    timing = ""
    risks: list[BriefRisk] = []

    planet = snapshot.planet(action.planet_id) if action.planet_id is not None else None
    if action.planet_id is not None and planet is None:
        risks.append(
            BriefRisk(
                code="data_unavailable",
                severity="medium",
                detail=f"planet {action.planet_id} is not present in this snapshot; every planet-scoped fact below is unavailable.",
            )
        )

    if planet is not None:
        observed.extend(_observed_planet_facts(action, planet))
        risks.extend(_planet_risks(action, planet, policy))

    if score_basis:
        inferred.append(_fact("Why this candidate won", score_basis, "Candidate.score_basis (the winning candidate's own ROI/deadline reasoning)"))

    if planet is not None:
        family = _FAMILY_BY_KIND.get(action.kind)
        if family is not None and action.entity_id is not None:
            prerequisites = _prerequisites(family, action.entity_id, planet, snapshot)
        obs_qi, inf_qi, queue_impact, timing = _queue_and_timing(action, planet, snapshot)
        observed.extend(obs_qi)
        inferred.extend(inf_qi)
    else:
        queue_impact, timing = _fleet_or_account_queue_and_timing(action, snapshot)

    risks.extend(_kind_risks(action))
    risks.extend(_snapshot_risks(snapshot))

    return Briefing(
        goal=goal,
        prerequisites=prerequisites,
        queue_impact=queue_impact,
        timing=timing,
        observed=observed,
        inferred=inferred,
        risks=_sorted_risks(risks),
        snapshot_taken_at=snapshot.taken_at,
        indexed_block=snapshot.latest_indexed_block,
    )


def _observed_planet_facts(action: Action, planet: PlanetSnapshot) -> list[BriefFact]:
    facts = [
        _fact(
            f"Cost vs. holdings on planet {planet.planet_id}",
            f"cost M{action.cost.metal:,} C{action.cost.crystal:,} D{action.cost.deuterium:,} vs. holdings "
            f"M{planet.resources_as_of_now.metal:,} C{planet.resources_as_of_now.crystal:,} D{planet.resources_as_of_now.deuterium:,}",
            f"planets[{planet.planet_id}].resources_as_of_now",
        )
    ]
    if planet.energy is not None:
        facts.append(
            _fact(
                f"Energy on planet {planet.planet_id}",
                f"produced {planet.energy.produced:,} / required {planet.energy.required:,} (scaleBps {planet.energy.scale_bps})",
                f"planets[{planet.planet_id}].energy",
            )
        )
    else:
        facts.append(_fact(f"Energy on planet {planet.planet_id}", _UNKNOWN, f"planets[{planet.planet_id}].energy (absent from snapshot)"))
    if planet.raidable_resources is not None:
        r = planet.raidable_resources
        if r.metal or r.crystal or r.deuterium:
            facts.append(
                _fact(
                    f"Raidable resources on planet {planet.planet_id}",
                    f"M{r.metal:,} C{r.crystal:,} D{r.deuterium:,}",
                    f"planets[{planet.planet_id}].raidable_resources",
                )
            )
    return facts


def _prerequisites(family: EntityFamily, entity_id: int, planet: PlanetSnapshot, snapshot: Snapshot) -> list[str]:
    reqs = unmet(
        family,
        entity_id,
        building_levels=_building_levels(planet),
        technology_levels=_technology_levels(snapshot),
    )
    if not reqs:
        return ["all prerequisites met"]
    return [describe(r) for r in reqs]


def _queue_and_timing(
    action: Action, planet: PlanetSnapshot, snapshot: Snapshot
) -> tuple[list[BriefFact], list[BriefFact], str, str]:
    """Returns `(observed_facts, inferred_facts, queue_impact, timing)` for a
    planet-scoped, queue-occupying action (BUILD/RESEARCH/SHIP/DEFENSE) or a resolve
    (no queue). Every other kind is handled by `_fleet_or_account_queue_and_timing`."""
    observed: list[BriefFact] = []
    inferred: list[BriefFact] = []

    if action.kind is ActionKind.RESOLVE_MISSION:
        return observed, inferred, "no queue -- resolveFleetMission is permissionless and free.", "resolves immediately once mined."

    queue_kind_by_action = {
        ActionKind.BUILD: (QueueKind.BUILDING, "building"),
        ActionKind.SHIP: (QueueKind.SHIP, "ship"),
        ActionKind.DEFENSE: (QueueKind.DEFENSE, "defense"),
    }
    if action.kind is ActionKind.RESEARCH:
        current = snapshot.research_queue
        queue_label = "research"
    elif action.kind in queue_kind_by_action:
        queue_kind, queue_label = queue_kind_by_action[action.kind]
        current = planet.queues.get(queue_kind)
    else:
        return observed, inferred, "", ""

    if current is not None:
        queue_impact = (
            f"occupies planet {planet.planet_id}'s {queue_label} queue, currently busy with "
            f"{current.entity_name} (ready in {current.seconds_remaining if current.seconds_remaining is not None else _UNKNOWN}s)."
        )
        observed.append(
            _fact(
                f"Current {queue_label} queue on planet {planet.planet_id}",
                f"{current.entity_name}, {current.seconds_remaining if current.seconds_remaining is not None else _UNKNOWN}s remaining",
                "research_queue" if action.kind is ActionKind.RESEARCH else f"planets[{planet.planet_id}].queues[{queue_label}]",
            )
        )
    else:
        queue_impact = f"occupies planet {planet.planet_id}'s {queue_label} queue (currently idle)."

    duration, duration_source = _duration_seconds(action, planet, snapshot)
    if duration is None:
        timing = "duration unknown -- neither the API nor calc.py could produce one for this action."
    else:
        timing = f"takes {duration:,}s once queued."
        fact = _fact(f"Duration for this {queue_label} action", f"{duration:,}s", duration_source)
        (observed if duration_source.startswith("Entity.duration_seconds") else inferred).append(fact)

    return observed, inferred, queue_impact, timing


def _duration_seconds(action: Action, planet: PlanetSnapshot, snapshot: Snapshot) -> tuple[int | None, str]:
    """`(seconds, source)` -- `source` starting with `"Entity.duration_seconds"` marks
    this as observed (the API reported it directly); anything else is inferred via
    `calc.py`, mirroring `candidates.build_time_savings_note`'s own
    `entity.duration_seconds or calc.build_seconds(...)` fallback."""
    entities_by_kind = {
        ActionKind.BUILD: planet.buildings,
        ActionKind.SHIP: planet.ships,
        ActionKind.DEFENSE: planet.defenses,
    }
    if action.kind in entities_by_kind and action.entity_id is not None:
        entity = next((e for e in entities_by_kind[action.kind] if e.id == action.entity_id), None)
        if entity is not None and entity.duration_seconds is not None:
            return entity.duration_seconds, "Entity.duration_seconds (the API's own reported duration for this upgrade)"
        cost = entity.cost if entity is not None else action.cost
        robotics = _level(planet, ids.Building.ROBOTICS_FACTORY)
        nanite = _level(planet, ids.Building.NANITE_FACTORY)
        if action.kind is ActionKind.BUILD:
            return calc.build_seconds(robotics, nanite, cost.metal, cost.crystal), "calc.build_seconds(robotics, nanite, cost)"
        shipyard = _level(planet, ids.Building.SHIPYARD)
        quantity = action.quantity or 1
        return (
            calc.ship_seconds(shipyard, nanite, cost.metal, cost.crystal, quantity),
            "calc.ship_seconds(shipyard, nanite, cost, quantity)",
        )
    if action.kind is ActionKind.RESEARCH:
        return (
            calc.research_seconds(snapshot.research_lab_level, action.cost.metal, action.cost.crystal),
            "calc.research_seconds(research_lab_level, cost)",
        )
    return None, ""


def _fleet_or_account_queue_and_timing(action: Action, snapshot: Snapshot) -> tuple[str, str]:
    """For fleet-mission/missile/defense-hold/alliance kinds, or a planet-scoped kind
    whose planet isn't in this snapshot. Deliberately does not recompute travel time
    (`calc.travel_seconds`) -- that needs live ship-speed/universe-speed inputs this
    module has no independent source for, and `guard.py` already re-derives it at send
    time; recomputing it a third time here is exactly the drift risk this module's own
    docstring warns against."""
    if action.kind is ActionKind.RESOLVE_MISSION:
        return "no queue -- resolveFleetMission is permissionless and free.", "resolves immediately once mined."
    if action.kind is ActionKind.ALLIANCE:
        return "no queue -- alliance membership actions are account-level, not queued.", "synchronous once mined."
    if action.kind is ActionKind.MISSILE_ATTACK:
        return "no fleet slot, no queue -- fully synchronous.", "resolves in the same transaction that sends it."
    if action.kind in (ActionKind.FLEET_MISSION, ActionKind.DEFENSE_HOLD):
        slots = (
            f"{snapshot.fleet_slots_active if snapshot.fleet_slots_active is not None else _UNKNOWN}/"
            f"{snapshot.fleet_slots_limit if snapshot.fleet_slots_limit is not None else _UNKNOWN}"
        )
        return (
            f"occupies one fleet slot ({slots} active/limit).",
            "travel time not computed by this brief -- see the action's own fuel/cargo fields.",
        )
    return "", ""


def _planet_risks(action: Action, planet: PlanetSnapshot, policy: Policy) -> list[BriefRisk]:
    risks: list[BriefRisk] = []
    total_cost = action.cost.metal + action.cost.crystal + action.cost.deuterium
    holdings = planet.resources_as_of_now
    total_holdings = holdings.metal + holdings.crystal + holdings.deuterium
    if total_cost > 0 and total_holdings > 0:
        pct = total_cost / total_holdings * 100
        if pct > policy.limits.escalate_above_pct_of_resources:
            risks.append(
                BriefRisk(
                    code="spend_share",
                    severity="medium",
                    detail=(
                        f"this action's cost is {pct:.0f}% of planet {planet.planet_id}'s current holdings "
                        f"(threshold {policy.limits.escalate_above_pct_of_resources}%), based on Action.cost."
                    ),
                )
            )
    breaches = []
    for label, current, spend, floor in (
        ("metal", holdings.metal, action.cost.metal, policy.reserves.metal),
        ("crystal", holdings.crystal, action.cost.crystal, policy.reserves.crystal),
        ("deuterium", holdings.deuterium, action.cost.deuterium, policy.reserves.deuterium),
    ):
        if current - spend < floor:
            breaches.append(label)
    if breaches:
        risks.append(
            BriefRisk(
                code="below_reserve",
                severity="medium",
                detail=(
                    f"spending this action's Action.cost would put {', '.join(breaches)} below "
                    f"policy.reserves on planet {planet.planet_id}."
                ),
            )
        )
    if planet.raidable_resources is not None:
        r = planet.raidable_resources
        if r.metal or r.crystal or r.deuterium:
            risks.append(
                BriefRisk(
                    code="raidable_after_spend",
                    severity="low",
                    detail=f"planet {planet.planet_id} already has raidable resources exposed, independent of this action.",
                )
            )
    if planet.storage_caps.metal or planet.storage_caps.crystal or planet.storage_caps.deuterium:
        for label, current, per_hour, cap in (
            ("metal", holdings.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            ("crystal", holdings.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            ("deuterium", holdings.deuterium, planet.production_per_hour.deuterium, planet.storage_caps.deuterium),
        ):
            if per_hour > 0 and cap > 0 and current < cap:
                hours_to_cap = (cap - current) / per_hour
                if hours_to_cap < policy.storage.hours_to_cap_trigger:
                    risks.append(
                        BriefRisk(
                            code="storage_overflow_while_busy",
                            severity="low",
                            detail=(
                                f"{label} on planet {planet.planet_id} reaches its storage cap in "
                                f"~{hours_to_cap:.1f}h, independent of this action."
                            ),
                        )
                    )
    return risks


def _kind_risks(action: Action) -> list[BriefRisk]:
    if action.kind is ActionKind.MISSILE_ATTACK:
        return [BriefRisk(code="combat_loss", severity="high", detail="fires a weapon at another player; no fleet risked, but this is an act of combat.")]
    if action.kind in (ActionKind.FLEET_MISSION, ActionKind.DEFENSE_HOLD) and action.mission_type in _COMBAT_MISSION_TYPES:
        return [
            BriefRisk(
                code="combat_loss",
                severity="high",
                detail="commits a fleet to combat (Attack/AcsDefend/Intercept); the committed ships can be lost.",
            )
        ]
    return []


def _snapshot_risks(snapshot: Snapshot) -> list[BriefRisk]:
    risks: list[BriefRisk] = []
    if snapshot.incoming_fleets and any(f.hostile for f in snapshot.incoming_fleets):
        risks.append(
            BriefRisk(
                code="hostile_fleet_incoming",
                severity="high",
                detail=f"{sum(1 for f in snapshot.incoming_fleets if f.hostile)} hostile fleet(s) incoming, independent of this action.",
            )
        )
    if snapshot.safe_to_serve_indexed_state is False or (
        snapshot.indexed_state is not None and snapshot.indexed_state != "healthy"
    ):
        risks.append(
            BriefRisk(
                code="snapshot_stale",
                severity="medium",
                detail=f"indexed_state={snapshot.indexed_state!r}, safe_to_serve_indexed_state={snapshot.safe_to_serve_indexed_state!r} -- this brief's observed facts may be stale.",
            )
        )
    return risks


# --------------------------------------------------------------------------------------
# Rendering. One function shared by the compact panel line (`tick._proposal_lines`), the
# full block (`vd plan run`'s panel, `ticks/<ts>.md`'s extra section) so the three
# outputs can't drift.
# --------------------------------------------------------------------------------------


def render_lines(brief: Briefing, *, full: bool) -> list[str]:
    if not full:
        lines = [f"goal:   {brief.goal}"] if brief.goal else []
        if brief.queue_impact:
            lines.append(f"queue:  {brief.queue_impact}")
        if brief.timing:
            lines.append(f"time:   {brief.timing}")
        if brief.risks:
            top = brief.risks[0]
            more = f" (+{len(brief.risks) - 1} more)" if len(brief.risks) > 1 else ""
            lines.append(f"risk:   [{top.severity}] {top.code}: {top.detail}{more}")
        return lines

    lines = [f"goal:        {brief.goal}"]
    if brief.prerequisites:
        lines.append(f"prereqs:     {'; '.join(brief.prerequisites)}")
    if brief.queue_impact:
        lines.append(f"queue:       {brief.queue_impact}")
    if brief.timing:
        lines.append(f"timing:      {brief.timing}")
    freshness = f"snapshot {brief.snapshot_taken_at.isoformat() if brief.snapshot_taken_at else _UNKNOWN}, block {brief.indexed_block if brief.indexed_block is not None else _UNKNOWN}"
    lines.append(f"observed (API, {freshness}):")
    if brief.observed:
        for fact in brief.observed:
            lines.append(f"  - {fact.label}: {fact.value}  [{fact.source}]")
    else:
        lines.append("  (none)")
    lines.append("inferred (planner):")
    if brief.inferred:
        for fact in brief.inferred:
            lines.append(f"  - {fact.label}: {fact.value}  [{fact.source}]")
    else:
        lines.append("  (none)")
    if brief.risks:
        lines.append("risks:")
        for risk in brief.risks:
            lines.append(f"  - [{risk.severity}] {risk.code}: {risk.detail}")
    return lines
