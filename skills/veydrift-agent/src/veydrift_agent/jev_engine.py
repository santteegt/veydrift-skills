"""`jev_engine.py` — the `jev` decision engine (`references/jev-engine.md`).

1. Vetoes (`plan.veto_action`) and the storage-overflow deadline (`plan.deadline_action`)
   decide first, exactly as in the ladder. The backend is not called.
2. `candidates.collect_pool` builds every legal, selectable candidate on every target planet.
3. State (situation + candidates, every number bucketed by code, no wallet/signer/coordinates)
   and questions (`tick_focus` Choice, `threat` Noul, per-candidate `fit_i`/`urgency_i` Scores)
   go to TypeSafe in one request through `jev.JevBackend`.
4. Code composes a weighted score per candidate (`policy.engine.jev.weights`), picks the
   highest (ties: ladder band order, then generation order) and gates it on confidence and
   margin. Any `JevError`, an empty pool, low confidence or a thin margin returns the
   ladder's own pick instead, with the reason in `EngineTrace.fallback_reason`.

Jev is not a calculator: it cannot count, judge numeric closeness or read dates. So every
number that reaches it is turned into descriptive text here (payback, cost share, build time,
hours-to-cap, defense posture), the state stays small, and each question is narrow.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veydrift_agent import calc, candidates, guard, ids
from veydrift_agent import jev as jev_mod
from veydrift_agent import plan as plan_mod
from veydrift_agent.jev import ChoiceAnswer, JevAnswers, JevError, QuestionSpec
from veydrift_agent.models import (
    Action,
    ActionKind,
    EngineJudgment,
    EngineTrace,
    PlanetSnapshot,
    Policy,
    QueueKind,
    Resources,
    Snapshot,
)

if TYPE_CHECKING:
    from veydrift_agent.candidates import PoolEntry
    from veydrift_agent.engine import EngineContext
    from veydrift_agent.jev import JevBackend

#: Off-chain rule for a confident "hold" judgment (`policy.engine.jev.allow_hold`).
HOLD_RULE = "9j:hold"

#: The sentence `decide` appends to a jev-selected action's rationale. Built in one place so
#: `base_rationale` can remove exactly what `_with_selection_sentence` adds.
_SELECTION_SUFFIX_RE = re.compile(r"(?:^| )Selected by the jev engine from \d+ legal candidates \([^()]*\)\.$")


def _with_selection_sentence(rationale: str, pool_size: int, group: str) -> str:
    sentence = f"Selected by the jev engine from {pool_size} legal candidates ({group} focus)."
    return f"{rationale} {sentence}" if rationale else sentence


def base_rationale(action: Action) -> str:
    """`action.rationale` without the jev selection sentence. The sentence carries the pool size
    and focus group, which vary between calls; the manual-override record (part of the tick's
    dedup fingerprint) must not, so a jev pick and its ladder fallback for the same candidate
    record the same text."""
    if action.engine != "jev":
        return action.rationale
    return _SELECTION_SUFFIX_RE.sub("", action.rationale)


#: `strategy_intent` when `policy.engine.jev.intent` is empty.
DEFAULT_INTENT = (
    "Grow a sound, balanced economy: keep mines energy-safe, avoid wasted production, "
    "progress research and declared targets steadily, and avoid risky military action."
)

#: The ladder's rules for the high-stakes families (8c deploy, 8d colonize, 8e attack, 8f missile):
#: a ladder pick whose `rule` is in this set is itself a high-stakes action.
HIGH_STAKES_RULES = frozenset(plan_mod.RULE_BY_FAMILY[f] for f in candidates.HIGH_STAKES_FAMILIES)

#: A high-stakes winner needs a normalized `fit` of at least this ("directly supports" on the
#: five-level fit scale) and must be the group `tick_focus` chose.
HIGH_STAKES_MIN_FIT = 0.75

#: The economy term of a candidate with no payback score (it does not raise production
#: directly: research, infrastructure, ships, defense, logistics, ...).
ECON_UNSCORED = 0.3

#: Defense-unit count -> posture bucket. Counts planetary defense units (ids 0-7, not missiles)
#: on the planet: 0 none, 1-19 light, 20-99 moderate, 100+ strong.
_POSTURE_LIGHT_MAX = 19
_POSTURE_MODERATE_MAX = 99
_DEFENSE_UNIT_IDS = frozenset(range(ids.Defense.ROCKET_LAUNCHER, ids.Defense.LARGE_SHIELD_DOME + 1))

#: Group -> option description for `tick_focus`, in canonical order. Every group present in
#: the pool gets one; `hold` is always offered.
_GROUP_ORDER = ("economy", "research", "fleet_defense", "unlock", "logistics", "expansion", "offense")
_GROUP_CRITERIA: dict[str, dict[str, str]] = {
    "economy": {
        "what": "Upgrade mines, energy supply, storage or crawlers on a planet to raise resource income or avoid waste.",
        "not_for": "Research, ship or defense production, or any military action.",
    },
    "research": {
        "what": "Start researching a technology.",
        "not_for": "Building upgrades or ship and defense production.",
    },
    "fleet_defense": {
        "what": "Produce ships or planetary defense units, including several kinds at once in one batch.",
        "not_for": "Building upgrades, research, or moving existing fleets.",
    },
    "unlock": {
        "what": "A building or research step whose purpose is to unlock a declared ship, defense or technology target that cannot be built yet.",
        "not_for": "A step that pays off by itself or is not tied to a locked declared target.",
    },
    "logistics": {
        "what": "Move resources or fleets between the player's own planets, or harvest debris fields.",
        "not_for": "Attacking anyone or developing a planet.",
    },
    "expansion": {
        "what": "Settle a new planet with a Colony Ship.",
        "not_for": "Developing planets the player already owns.",
    },
    "offense": {
        "what": "Attack another player's planet with combat ships or missiles.",
        "not_for": "Peaceful development, defense, or logistics.",
    },
    "hold": {
        "what": "None of the available kinds fits the strategy right now; waiting is better.",
        "not_for": "Any situation where at least one available kind clearly serves the strategy.",
    },
}

_FIT_LEVELS = [
    "Works against the strategy: it spends effort on something the strategy avoids or contradicts.",
    "Unrelated to the strategy: neither helps nor hurts it.",
    "Supports the strategy indirectly: general growth the strategy benefits from.",
    "Directly supports the strategy: a kind of development it names or clearly implies.",
    "Is the strategy's stated top priority or its immediate next step.",
]
_URGENCY_LEVELS = [
    "Can wait many hours with nothing lost.",
    "Doing it soon helps a little.",
    "Time-sensitive: postponing wastes production, leaves a queue idle for long, or blocks a declared target.",
    "Critical now: postponing risks losing resources or assets.",
]

_RESOURCES = ("metal", "crystal", "deuterium")


# --------------------------------------------------------------------------------------
# Buckets. Every number the model sees goes through one of these.
# --------------------------------------------------------------------------------------


def _payback_fact(hours: float | None) -> str:
    if hours is None:
        return "no direct production gain"
    if hours < 6:
        return "pays back in under 6 hours"
    if hours < 24:
        return "pays back in 6-24 hours"
    if hours < 72:
        return "pays back in 1-3 days"
    if hours < 168:
        return "pays back in 3-7 days"
    return "pays back in more than a week"


def _cost_share_bucket(share: float) -> str:
    if share < 0.10:
        return "a small share (<10%)"
    if share < 0.35:
        return "a moderate share (10-35%)"
    if share < 0.70:
        return "a large share (35-70%)"
    return "most (>70%)"


def _duration_bucket(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 15 * 60:
        return "under 15 minutes"
    if seconds < 2 * 3600:
        return "15 minutes-2 hours"
    if seconds < 8 * 3600:
        return "2-8 hours"
    if seconds < 24 * 3600:
        return "8-24 hours"
    return "over a day"


def _hours_to_cap_bucket(planet: PlanetSnapshot, resource: str) -> str:
    cap = getattr(planet.storage_caps, resource)
    current = getattr(planet.resources_as_of_now, resource)
    per_hour = getattr(planet.production_per_hour, resource)
    if cap <= 0:
        return "unknown"
    if current >= cap:
        return "under 2 hours"
    hours = calc.hours_to_cap(current, per_hour, cap)
    if hours is None:
        return "not filling"
    if hours < 2:
        return "under 2 hours"
    if hours < 6:
        return "2-6 hours"
    if hours < 24:
        return "6-24 hours"
    return "more than a day"


def _energy_label(planet: PlanetSnapshot) -> str:
    energy = planet.energy
    if energy is None:
        return "unknown"
    spare = energy.produced - energy.required
    if energy.scale_bps < 10_000 or spare < 0:
        return "deficit: production throttled"
    if spare > 0 and spare * 10 >= max(energy.required, 1):
        return "surplus"
    return "balanced"


def _defense_posture(planet: PlanetSnapshot) -> str:
    units = sum((e.count or 0) for e in planet.defenses if e.id in _DEFENSE_UNIT_IDS)
    if units <= 0:
        return "none"
    if units <= _POSTURE_LIGHT_MAX:
        return "light"
    if units <= _POSTURE_MODERATE_MAX:
        return "moderate"
    return "strong"


def _count_bucket(count: int) -> str:
    return "none" if count <= 0 else "one" if count == 1 else "several"


def _weighted(resources: Resources, weights: Resources) -> int:
    return resources.metal * weights.metal + resources.crystal * weights.crystal + resources.deuterium * weights.deuterium


# --------------------------------------------------------------------------------------
# State.
# --------------------------------------------------------------------------------------


def _labels(target_planets: list[PlanetSnapshot]) -> dict[int, str]:
    """`planet_id -> "planet A"`, in `_target_planets` order. The map stays in code."""
    return {p.planet_id: f"planet {chr(ord('A') + i)}" if i < 26 else f"planet {i + 1}" for i, p in enumerate(target_planets)}


def _threats(snapshot: Snapshot, policy: Policy, context: EngineContext | None) -> dict[str, str]:
    hostile = sum(1 for fleet in snapshot.incoming_fleets if fleet.hostile)
    threats = {"incoming_hostile_fleets": _count_bucket(hostile)}
    report = context.radar_report if context is not None else None
    if report is None:
        threats["recent_attacks_on_you"] = "unknown"
        threats["debris_on_your_planets"] = "unknown"
        return threats
    own_ids = {p.planet_id for p in snapshot.planets}
    own_wallet = policy.wallet.lower()

    def mine(finding: Any) -> bool:
        return finding.planet_id in own_ids or finding.wallet.lower() == own_wallet

    threats["recent_attacks_on_you"] = _count_bucket(
        sum(1 for f in report.findings if f.kind == "resolved_attack" and mine(f))
    )
    threats["debris_on_your_planets"] = _count_bucket(sum(1 for f in report.findings if f.kind == "debris" and mine(f)))
    return threats


def _declared_targets(policy: Policy) -> list[str]:
    strategy = policy.strategy
    out = [f"research priority {i}: {name}" for i, name in enumerate(strategy.research_priority, 1)]
    out += [f"building priority {i}: {name}" for i, name in enumerate(strategy.building_priority, 1)]
    for label, targets, name_fn in (
        ("ship target", strategy.ship_targets, ids.ship_name),
        ("defense target", strategy.defense_targets, ids.defense_name),
    ):
        for target in targets:
            name = target.name if target.name is not None else name_fn(target.id) if target.id is not None else "?"
            out.append(f"{label}: {name}, want {target.count}")
    return out


def _planet_state(planet: PlanetSnapshot, label: str, role: str) -> dict[str, Any]:
    # `role` is only where the planet sits in the target-planet order; the ladder treats the first
    # one specially for research, but nothing here knows which planet is the player's capital.
    queues = {
        kind.value: "idle" if planet.queues.get(kind) is None else "busy"
        for kind in (QueueKind.BUILDING, QueueKind.SHIP, QueueKind.DEFENSE)
    }
    return {
        "label": label,
        "role": role,
        "energy": _energy_label(planet),
        "storage_hours_to_cap": {r: _hours_to_cap_bucket(planet, r) for r in _RESOURCES},
        "queues": queues,
        "defense_posture": _defense_posture(planet),
    }


def _situation(
    snapshot: Snapshot,
    policy: Policy,
    target_planets: list[PlanetSnapshot],
    labels: dict[int, str],
    context: EngineContext | None,
) -> dict[str, Any]:
    if snapshot.fleet_slots_active is None or snapshot.fleet_slots_limit is None:
        fleet_slots = "unknown"
    else:
        fleet_slots = "free" if snapshot.fleet_slots_active < snapshot.fleet_slots_limit else "all in use"
    active = candidates.economy_on_track(snapshot, target_planets)
    return {
        "economy_active": "building or research in progress" if active else "nothing building or researching",
        "research_queue": "idle" if snapshot.research_queue is None else "busy",
        "fleet_slots": fleet_slots,
        "threats": _threats(snapshot, policy, context),
        "declared_targets": _declared_targets(policy),
        "planets": [
            _planet_state(p, labels[p.planet_id], "listed first" if i == 0 else "other") for i, p in enumerate(target_planets)
        ],
    }


def _entity_name(action: Action) -> str:
    if action.entity_name:
        return action.entity_name
    if action.entity_id is None:
        return "?"
    lookup = {
        ActionKind.BUILD: ids.building_name,
        ActionKind.RESEARCH: ids.technology_name,
        ActionKind.SHIP: ids.ship_name,
        ActionKind.DEFENSE: ids.defense_name,
    }.get(action.kind)
    return lookup(action.entity_id) if lookup else "?"


def _batch_summary(action: Action) -> str:
    parts = []
    for order in action.orders:
        name = ids.ship_name(order.item_id) if order.kind == "ship" else ids.defense_name(order.item_id)
        parts.append(f"{order.quantity} {name}")
    return ", ".join(parts)


def _owned_label(snapshot: Snapshot, labels: dict[int, str], coordinates: str | None) -> str:
    if coordinates:
        for planet in snapshot.planets:
            if planet.coordinates == coordinates and planet.planet_id in labels:
                return labels[planet.planet_id]
    return "another of your planets"


def _what(action: Action, family: str, snapshot: Snapshot, labels: dict[int, str]) -> str:
    """One plain sentence naming the action -- no numbers beyond levels and counts the
    player declared or the action carries, no coordinates, no ids."""
    kind = action.kind
    name = _entity_name(action)
    origin = labels.get(action.planet_id, "a planet") if action.planet_id is not None else "a planet"
    if kind is ActionKind.BUILD:
        return f"Upgrade {name} to level {action.target_level}" if action.target_level is not None else f"Upgrade {name}"
    if kind is ActionKind.RESEARCH:
        return f"Research {name} to level {action.target_level}" if action.target_level is not None else f"Research {name}"
    if kind is ActionKind.SHIP or kind is ActionKind.DEFENSE:
        return f"Build {action.quantity or 1} {name}"
    if kind is ActionKind.PRODUCTION_BATCH:
        return f"Produce a batch: {_batch_summary(action)}"
    if kind is ActionKind.MISSILE_ATTACK:
        return "Fire missiles at another player's planet"
    if kind is ActionKind.FLEET_MISSION:
        mission = action.mission_type
        if mission == ids.FleetMissionType.TRANSPORT:
            return f"Transport resources from {origin} to {_owned_label(snapshot, labels, action.target_coordinates)}"
        if mission == ids.FleetMissionType.DEPLOY:
            return f"Deploy the fleet from {origin} to {_owned_label(snapshot, labels, action.target_coordinates)} permanently"
        if mission == ids.FleetMissionType.HARVEST:
            if family == "logistics-harvest-foreign":
                return "Harvest a debris field near another player's planet"
            return f"Harvest the debris field at {origin}"
        if mission == ids.FleetMissionType.COLONIZE:
            return "Colonize an empty slot"
        if mission == ids.FleetMissionType.ATTACK:
            return "Attack another player's planet"
    return f"{kind.value} action"


def _priority_fact(entry: PoolEntry, snapshot: Snapshot, policy: Policy) -> str | None:
    """Where the candidate sits among the player's declared priorities."""
    cand = entry.candidate
    action = cand.action
    family = cand.family
    strategy = policy.strategy
    if family == "unlock":
        match = re.search(r"toward your (.+?) target", cand.score_basis)
        return f"next step toward unlocking {match.group(1)}" if match else "next step toward unlocking a declared target"

    def index_of(names: list[str], id_fn: Any, entity_id: int | None) -> int | None:
        for i, declared in enumerate(names, 1):
            try:
                if id_fn(declared) == entity_id:
                    return i
            except KeyError:
                continue
        return None

    if action.kind is ActionKind.RESEARCH:
        position = index_of(strategy.research_priority, ids.technology_id, action.entity_id)
        return f"named research priority #{position}" if position else "not named in any declared priority"
    if family == "infrastructure":
        position = index_of(strategy.building_priority, ids.building_id, action.entity_id)
        return f"named building priority #{position}" if position else "not named in any declared priority"
    if action.kind in (ActionKind.SHIP, ActionKind.DEFENSE) and family in ("ship", "defense"):
        is_ship = action.kind is ActionKind.SHIP
        targets = strategy.ship_targets if is_ship else strategy.defense_targets
        id_fn = ids.ship_id if is_ship else ids.defense_id
        planet = snapshot.planet(action.planet_id) if action.planet_id is not None else None
        for target in targets:
            try:
                target_id = target.id if target.id is not None else id_fn(target.name or "")
            except KeyError:
                continue
            if target_id != action.entity_id:
                continue
            entities = (planet.ships if is_ship else planet.defenses) if planet is not None else []
            have = next((e.count or 0 for e in entities if e.id == target_id), 0)
            return f"counts toward {'ship' if is_ship else 'defense'} target {_entity_name(action)} (have {have} of {target.count})"
        return "not named in any declared priority"
    if family == "batch":
        return "covers several declared ship and defense targets in one transaction"
    return None


def _spend(action: Action, snapshot: Snapshot) -> Resources | None:
    if action.kind in (ActionKind.SHIP, ActionKind.DEFENSE, ActionKind.PRODUCTION_BATCH):
        return guard.production_spend(action, snapshot)
    if action.kind in (ActionKind.BUILD, ActionKind.RESEARCH):
        return action.cost
    return None  # fleet missions move cargo rather than spend it; missiles are already built


def _duration_seconds(action: Action, planet: PlanetSnapshot | None, snapshot: Snapshot) -> int | None:
    def per_unit(entities: list[Any], entity_id: int | None) -> int | None:
        entity = next((e for e in entities if e.id == entity_id), None)
        return None if entity is None else entity.duration_seconds

    if action.kind is ActionKind.RESEARCH:
        return per_unit(snapshot.technologies, action.entity_id)
    if planet is None:
        return None
    if action.kind is ActionKind.BUILD:
        return per_unit(planet.buildings, action.entity_id)
    if action.kind in (ActionKind.SHIP, ActionKind.DEFENSE):
        unit = per_unit(planet.ships if action.kind is ActionKind.SHIP else planet.defenses, action.entity_id)
        return None if unit is None else unit * (action.quantity or 1)
    if action.kind is ActionKind.PRODUCTION_BATCH:
        total = 0
        for order in action.orders:
            unit = per_unit(planet.ships if order.kind == "ship" else planet.defenses, order.item_id)
            if unit is None:
                return None
            total += unit * order.quantity
        return total
    return None


#: Families whose only economic fact is what the move is (they have no payback score).
_FLEET_FACTS = {
    "logistics-transport": "moves resources between your own planets; nothing is gained overall",
    "logistics-deploy": "permanently moves the whole fleet to another planet of yours",
    "logistics-harvest": "recovers resources from a debris field left by past battles",
    "logistics-harvest-foreign": "recovers resources from a debris field left by past battles",
    "colonize": "consumes a Colony Ship",
    "attack": "commits combat ships to battle; they can be lost",
    "missile": "destroys enemy defenses; uses interplanetary missiles",
}


def _facts(entry: PoolEntry, snapshot: Snapshot, policy: Policy, labels: dict[int, str]) -> list[str]:
    cand = entry.candidate
    action = cand.action
    family = cand.family
    planet = snapshot.planet(action.planet_id) if action.planet_id is not None else None
    facts: list[str] = [] if family in _FLEET_FACTS else [_payback_fact(cand.score)]

    spend = _spend(action, snapshot)
    if spend is not None and planet is not None:
        weights = policy.strategy.resource_weights
        cost_w = _weighted(spend, weights)
        held_w = _weighted(planet.resources_as_of_now, weights)
        if cost_w > 0 and held_w > 0:
            facts.append(f"costs {_cost_share_bucket(cost_w / held_w)} of the planet's current resources")

    if action.kind in (ActionKind.BUILD, ActionKind.RESEARCH, ActionKind.SHIP, ActionKind.DEFENSE, ActionKind.PRODUCTION_BATCH):
        facts.append(f"build time: {_duration_bucket(_duration_seconds(action, planet, snapshot))}")

    priority = _priority_fact(entry, snapshot, policy)
    if priority:
        facts.append(priority)

    if family == "energy":
        state = _energy_label(planet) if planet is not None else "unknown"
        facts.append(f"adds energy supply; the planet's energy is currently {state}")
    elif family == "storage":
        facts.append("raises the planet's storage capacity")
    elif family == "crawler":
        facts.append("boosts mine output")
    elif family == "defense" or (family == "batch" and any(o.kind == "defense" for o in action.orders)):
        posture = _defense_posture(planet) if planet is not None else "none"
        facts.append(f"adds planetary defense; the planet's defense is currently {posture}")

    if family in _FLEET_FACTS:
        facts.append(_FLEET_FACTS[family])
    return facts


def _candidate_entry(i: int, entry: PoolEntry, snapshot: Snapshot, policy: Policy, labels: dict[int, str]) -> dict[str, Any]:
    action = entry.candidate.action
    if action.kind is ActionKind.RESEARCH:
        planet_label = "empire-wide (research is not tied to a planet)"
    else:
        planet_label = labels.get(action.planet_id, "another of your planets") if action.planet_id is not None else "n/a"
    return {
        "id": f"c{i}",
        "group": entry.group,
        "planet": planet_label,
        "what": _what(action, entry.candidate.family, snapshot, labels),
        "facts": _facts(entry, snapshot, policy, labels),
    }


def build_request(
    snapshot: Snapshot,
    policy: Policy,
    pool: list[PoolEntry],
    context: EngineContext | None = None,
) -> tuple[dict[str, Any], dict[str, QuestionSpec]]:
    """The exact `(state, questions)` the engine sends for `pool`. Pure, no network --
    `vd engine pool` prints it. Candidate ids are `c0..c{n-1}` in pool order; questions are
    `tick_focus`, `threat`, `fit_c<i>`, `urgency_c<i>`."""
    cfg = policy.engine.jev
    target_planets = plan_mod._target_planets(snapshot, policy)
    labels = _labels(target_planets)
    if context is not None and context.intent is not None:
        intent_text, intent_source = context.intent.text, context.intent.source
    else:
        policy_intent = cfg.intent.strip()
        intent_text = policy_intent or DEFAULT_INTENT
        intent_source = "policy" if policy_intent else "default"
    state: dict[str, Any] = {
        "strategy_intent": intent_text,
        "intent_source": intent_source,
        "situation": _situation(snapshot, policy, target_planets, labels, context),
        "candidates": [_candidate_entry(i, e, snapshot, policy, labels) for i, e in enumerate(pool)],
    }

    present = {e.group for e in pool}
    focus_options = {g: dict(_GROUP_CRITERIA[g]) for g in _GROUP_ORDER if g in present}
    focus_options["hold"] = dict(_GROUP_CRITERIA["hold"])
    questions: dict[str, QuestionSpec] = {
        "tick_focus": QuestionSpec(
            kind="choice",
            instructions=(
                "Which kind of activity best serves the strategy in `strategy_intent` right now, given "
                "`situation`? Consider what `candidates` offer (each has a `group`), or choose hold if "
                "none of the kinds fits."
            ),
            criteria=focus_options,
        ),
        "threat": QuestionSpec(
            kind="noul",
            instructions=(
                "Based only on `situation.threats` and each planet's `defense_posture`, is at least one "
                "of the player's planets likely to be attacked within the next few hours?"
            ),
            criteria={
                "true": "There are signs of hostile activity (incoming hostile fleets or recent attacks) and thin defenses.",
                "false": "No signs of hostile activity, or the defenses look sufficient.",
            },
        ),
    }
    # Each per-candidate question carries its own candidate inline. A positional path
    # (`candidates[i]`) proved unreliable live: the model resolved indices off by one and
    # scored the wrong candidate. The named fields (`strategy_intent`, `situation`) stay
    # by reference -- they are names, not positions.
    for i, candidate in enumerate(state["candidates"]):
        questions[f"fit_c{i}"] = QuestionSpec(
            kind="score",
            instructions={
                "question": (
                    "How well does the candidate below serve the strategy in `strategy_intent`? "
                    "Judge the kind of development, not its cost."
                ),
                "candidate": dict(candidate),
            },
            criteria=list(_FIT_LEVELS),
        )
        questions[f"urgency_c{i}"] = QuestionSpec(
            kind="score",
            instructions={
                "question": (
                    "How much is lost by postponing the candidate below to a later turn, judging from "
                    "its `facts` and `situation`?"
                ),
                "candidate": dict(candidate),
            },
            criteria=list(_URGENCY_LEVELS),
        )
    return state, questions


# --------------------------------------------------------------------------------------
# Composition and gating.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scored:
    """One pooled entry with the judgments that feed its composite score. All 0..1."""

    id: str
    entry: PoolEntry
    fit: float
    fit_confidence: float
    urgency: float
    urgency_confidence: float
    focus: float
    economy: float
    threat: float
    composite: float

    @property
    def rank_key(self) -> tuple[float, int, int]:
        return (-round(self.composite, 6), self.entry.band, self.entry.index)


def _is_defense(action: Action) -> bool:
    return action.kind is ActionKind.DEFENSE or (
        action.kind is ActionKind.PRODUCTION_BATCH and any(o.kind == "defense" for o in action.orders)
    )


def _answer(mapping: dict[str, Any], qid: str) -> Any:
    try:
        return mapping[qid]
    except KeyError:
        raise JevError("malformed", qid) from None


def _compose(pool: list[PoolEntry], answers: JevAnswers, policy: Policy) -> list[_Scored]:
    """Composite score per pooled entry, ranked best first (ties: band, then generation
    order). `C_i = (w_fit*fit + w_urgency*urgency + w_focus*focus + w_economy*economy +
    w_threat*threat) / sum(w)`; each term is 0..1."""
    cfg = policy.engine.jev
    w = cfg.weights
    total_w = w.fit + w.urgency + w.focus + w.economy + w.threat
    if not math.isfinite(total_w) or total_w <= 0:
        raise JevError("malformed", "weights")
    focus_answer: ChoiceAnswer = _answer(answers.choices, "tick_focus")
    threat = float(_answer(answers.nouls, "threat"))
    scored: list[_Scored] = []
    for i, entry in enumerate(pool):
        fit = _answer(answers.scores, f"fit_c{i}")
        urgency = _answer(answers.scores, f"urgency_c{i}")
        focus = focus_answer.probabilities.get(entry.group, 0.0)
        hours = entry.candidate.score
        h0 = cfg.payback_reference_hours
        economy = ECON_UNSCORED if hours is None else h0 / (h0 + max(hours, 0.0))
        thr = threat if _is_defense(entry.candidate.action) else 0.0
        composite = (
            w.fit * fit.normalized + w.urgency * urgency.normalized + w.focus * focus + w.economy * economy + w.threat * thr
        ) / total_w
        scored.append(
            _Scored(
                id=f"c{i}",
                entry=entry,
                fit=fit.normalized,
                fit_confidence=fit.confidence,
                urgency=urgency.normalized,
                urgency_confidence=urgency.confidence,
                focus=focus,
                economy=economy,
                threat=thr,
                composite=composite,
            )
        )
    scored.sort(key=lambda s: s.rank_key)
    return scored


def _confidence(winner: _Scored, focus_confidence: float, policy: Policy) -> float:
    """Minimum confidence over the judgments with a positive weight that fed the winner
    (fit, urgency, tick_focus). Nouls carry none. Never a vacuous 1.0: if no weighted judgment
    carries a confidence (the policy validator forbids such weights) it is `malformed`."""
    w = policy.engine.jev.weights
    confidences: list[float] = []
    if w.fit > 0:
        confidences.append(winner.fit_confidence)
    if w.urgency > 0:
        confidences.append(winner.urgency_confidence)
    if w.focus > 0:
        confidences.append(focus_confidence)
    if not confidences:
        raise JevError("malformed", "no weighted judgment carries a confidence")
    return min(confidences)


#: Functions whose actions are distinguished by origin and target as well as by entity: two
#: fleet or missile launches from different planets, or at different targets, are different
#: moves (unlike the same upgrade on two symmetric planets).
_TARGETED_FUNCTIONS = frozenset({"launchFleetMission", "launchInterplanetaryMissileAttack"})

_Kind = tuple[Any, ...]


def _kind(s: _Scored) -> _Kind:
    """What sort of move an entry is, for the margin: `(family, function, entity)`. The same
    upgrade on two planets is one kind; a different entity or family is a different one. A fleet
    or missile launch additionally carries its mission type, origin planet and target, so two
    attacks from different origins or on different targets are different kinds."""
    action = s.entry.candidate.action
    base: _Kind = (s.entry.candidate.family, action.function, action.entity_id)
    if action.function in _TARGETED_FUNCTIONS:
        return (*base, action.mission_type, action.planet_id, action.target_planet_id, action.target_coordinates)
    return base


def _margin(scored: list[_Scored], winner: _Scored) -> float | None:
    """`winner`'s composite minus the best composite of a *different kind* of action; `None` when
    every entry is the winner's kind (nothing to be confused with). Identical candidates on
    symmetric planets tie by construction and say nothing about the decision. Used for
    high-stakes winners, which must beat every other move outright."""
    kind = _kind(winner)
    rivals = [s.composite for s in scored if s is not winner and _kind(s) != kind]
    return winner.composite - max(rivals) if rivals else None


def _group_confidence(best: _Scored, focus_confidence: float, policy: Policy) -> float:
    """Confidence of the judgments that decide *which kind of development* wins: `tick_focus`
    and the group's best candidate's fit, each only when weighted. Urgency is a per-item
    preference and counts only when neither fit nor focus is weighted (the policy validator
    guarantees one of the three is)."""
    w = policy.engine.jev.weights
    confidences: list[float] = []
    if w.focus > 0:
        confidences.append(focus_confidence)
    if w.fit > 0:
        confidences.append(best.fit_confidence)
    if not confidences and w.urgency > 0:
        confidences.append(best.urgency_confidence)
    if not confidences:
        raise JevError("malformed", "no weighted judgment carries a confidence")
    return min(confidences)


def _group_margin(scored: list[_Scored]) -> float | None:
    """Best composite of the winning group minus the best composite of any other group; `None`
    when only one group is on offer."""
    group = scored[0].entry.group
    rivals = [s.composite for s in scored if s.entry.group != group]
    return scored[0].composite - max(rivals) if rivals else None


def _item_pick(scored: list[_Scored], min_margin: float) -> _Scored:
    """Within the winning group, the candidates the model rated as a near-tie (composite within
    `min_margin` of the group's best) are an acceptable-alternatives set, not a judgment worth
    gating: take the first of them in pool order (band, then generation index), the ladder's own
    deterministic priority order, which already honours declared priorities. Only entries with
    the same high-stakes status as the top one are eligible, so pool order never promotes a
    high-stakes move the model did not rank first."""
    top = scored[0]
    high = top.entry.candidate.family in candidates.HIGH_STAKES_FAMILIES
    near = [
        s
        for s in scored
        if s.entry.group == top.entry.group
        and (s.entry.candidate.family in candidates.HIGH_STAKES_FAMILIES) == high
        and s.composite >= top.composite - min_margin
    ]
    return min(near, key=lambda s: (s.entry.band, s.entry.index))


def _high_stakes_reason(
    winner: _Scored, ladder_action: Action, focus_answer: ChoiceAnswer, cfg: Any
) -> str | None:
    """Why a high-stakes winner must not be taken, or `None` when it is endorsed. A high-stakes
    move (colonize, attack, missile, deploy) needs more than a top composite:

    - `high_stakes_not_idle`: the ladder reaches 8c-deploy/8d/8e/8f only when every earlier band
      proposed nothing at all, affordable or not. If the ladder's own pick is an on-chain action
      whose rule is not in `HIGH_STAKES_RULES`, the account is not idle -- something ordinary is
      pending (even if merely unaffordable) -- so the ladder's pick stands.
    - `high_stakes_hold`: `tick_focus` chose hold.
    - `high_stakes_not_endorsed`: the winner's fit is below `HIGH_STAKES_MIN_FIT`, or `tick_focus`
      chose a group other than the winner's."""
    if cfg.high_stakes_only_when_idle and ladder_action.is_onchain() and ladder_action.rule not in HIGH_STAKES_RULES:
        return "high_stakes_not_idle"
    if focus_answer.choice == "hold":
        return "high_stakes_hold"
    if winner.fit < HIGH_STAKES_MIN_FIT or focus_answer.choice != winner.entry.group:
        return "high_stakes_not_endorsed"
    return None


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _judgment(s: _Scored) -> EngineJudgment:
    action = s.entry.candidate.action
    return EngineJudgment(
        id=s.id,
        family=s.entry.candidate.family,
        entity_name=action.entity_name,
        planet_id=action.planet_id,
        fit=_r(s.fit),
        fit_confidence=_r(s.fit_confidence),
        urgency=_r(s.urgency),
        urgency_confidence=_r(s.urgency_confidence),
        focus=_r(s.focus),
        economy=_r(s.economy),
        threat=_r(s.threat),
        composite=_r(s.composite),
    )


def _ladder_pick(action: Action) -> dict[str, Any]:
    return {
        "rule": action.rule,
        "function": action.function,
        "planet_id": action.planet_id,
        "entity_id": action.entity_id,
        "entity_name": action.entity_name,
    }


def intent_trace_fields(policy: Policy, context: EngineContext | None) -> dict[str, Any]:
    """The `EngineTrace` intent fields for this tick: the effective intent (`context.intent`,
    else the policy intent or the default), plus the override's metadata when one is attached
    and why a stored override was not used. Mirrors `build_request`'s choice of text/source."""
    effective = context.intent if context is not None else None
    if effective is None:
        policy_intent = policy.engine.jev.intent.strip()
        return {
            "intent": policy_intent or DEFAULT_INTENT,
            "intent_source": "policy" if policy_intent else "default",
        }
    fields: dict[str, Any] = {"intent": effective.text, "intent_source": effective.source}
    override = effective.override
    if override is not None:
        fields["intent_reason"] = override.reason
        fields["intent_set_at"] = override.set_at
        fields["intent_expires_at"] = override.expires_at
    if effective.note is not None:
        fields["intent_note"] = effective.note
    return fields


def _known_identifiers(
    snapshot: Snapshot, target_kwargs: dict[str, Any], policy: Policy | None = None
) -> tuple[list[int], list[int], list[str]]:
    """`(own_planet_ids, foreign_planet_ids, own_coordinates)` this tick knows of. Own: every
    planet the account owns (the snapshot's detailed planets and the full list from
    `/wallet/{addr}/planets`, so a planet outside `policy.planets` counts too), `policy.planets`,
    and the own-planet debris targets. Foreign: the ids of the attack, missile and foreign-debris
    targets, which are other players' planets, so only a planet-introduced mention of them
    (`planet 10`) is a leak. Coordinates are the account's own: a foreign target's coordinates
    are only ever caught in `g:s:p` colon form, by the generic rule."""
    own_ids: list[int] = [p.planet_id for p in snapshot.planets]
    own_ids.extend(snapshot.owned_planet_ids)
    if policy is not None:
        own_ids.extend(policy.planets)
    own_ids.extend(target_kwargs.get("own_planet_debris") or {})
    coordinates: list[str] = [p.coordinates for p in snapshot.planets if p.coordinates]
    coordinates.extend(snapshot.owned_planet_coordinates)
    foreign_ids: list[int] = []
    for key in ("foreign_debris_targets", "attack_targets", "missile_targets"):
        foreign_ids.extend(target_kwargs.get(key) or {})
    return list(dict.fromkeys(own_ids)), list(dict.fromkeys(foreign_ids)), list(dict.fromkeys(coordinates))


def sendable_context(
    snapshot: Snapshot,
    policy: Policy,
    context: EngineContext | None,
    target_kwargs: dict[str, Any],
) -> tuple[EngineContext | None, dict[str, Any]]:
    """The single choke point before any request leaves the machine: re-check the effective
    intent text against the real account (`policy.wallet`/`signer`, every owned planet id and
    coordinate, and the ids of the targets), which the tick-time resolver could not see.

    Returns `(context, trace_fields)`. When the text is clean that is `context` itself and no
    fields. Otherwise the text is replaced -- an agent override by the policy intent (when that
    passes the same check), else the built-in default; a policy intent by the default -- and
    `trace_fields` carries the text actually sent plus a note saying why. Never raises."""
    from veydrift_agent.engine import EffectiveIntent, EngineContext
    from veydrift_agent.models import intent_text_problems

    cfg = policy.engine.jev
    effective = context.intent if context is not None else None
    if effective is None:
        policy_text = cfg.intent.strip()
        effective = EffectiveIntent(policy_text or DEFAULT_INTENT, "policy" if policy_text else "default")
    if effective.source == "default":
        return context, {}
    own_ids, foreign_ids, coordinates = _known_identifiers(snapshot, target_kwargs, policy)

    def check(text: str) -> list[str]:
        return intent_text_problems(
            text,
            wallet=policy.wallet,
            signer=policy.signer,
            planet_ids=own_ids,
            foreign_planet_ids=foreign_ids,
            coordinates=coordinates,
        )

    problems = check(effective.text)
    if not problems:
        return context, {}
    what = "override" if effective.source == "agent" else "policy intent"
    note = f"{what} rejected at send time: " + "; ".join(problems)
    if effective.note:
        note += f"; {effective.note}"
    policy_text = cfg.intent.strip()
    if effective.source == "agent" and policy_text and not check(policy_text):
        replacement = EffectiveIntent(policy_text, "policy", override=effective.override, note=note)
    else:
        replacement = EffectiveIntent(DEFAULT_INTENT, "default", override=effective.override, note=note)
    if context is None:
        new_context = EngineContext(intent=replacement)
    else:
        new_context = dataclasses.replace(context, intent=replacement)
    return new_context, intent_trace_fields(policy, new_context)


def decide(
    snapshot: Snapshot,
    policy: Policy,
    *,
    pending_tx_unreconciled: bool = False,
    resolvable_mission_ids: list[int] | None = None,
    own_planet_debris: dict[int, Resources] | None = None,
    foreign_debris_targets: dict[int, tuple[str, Resources]] | None = None,
    colonize_targets: list[tuple[str, int]] | None = None,
    attack_targets: dict[int, tuple[str, Resources, bool | None]] | None = None,
    missile_targets: dict[int, tuple[str, dict[int, int], bool | None]] | None = None,
    last_attended_planet_id: int | None = None,
    context: EngineContext | None = None,
    backend: JevBackend | None = None,
) -> tuple[Action, EngineTrace]:
    """Decide one `Action` with the jev engine. Never raises for a TypeSafe failure (that is
    a ladder fallback); `engine.decide` additionally catches anything unexpected."""
    cfg = policy.engine.jev
    intent_fields = intent_trace_fields(policy, context)

    # Vetoes and the storage deadline decide first, with no model call. `plan_next_action`
    # would return the very same action (it runs the same two helpers first).
    vetoed = plan_mod.veto_action(
        snapshot,
        policy,
        killswitch_active=False,
        pending_tx_unreconciled=pending_tx_unreconciled,
        resolvable_mission_ids=resolvable_mission_ids,
    )
    if vetoed is not None:
        return vetoed, EngineTrace(engine="ladder", configured="jev", pre_empted_by=vetoed.rule, **intent_fields)
    target_planets = plan_mod._target_planets(snapshot, policy)
    deadline = plan_mod.deadline_action(snapshot, policy, target_planets)
    if deadline is not None:
        return deadline, EngineTrace(engine="ladder", configured="jev", pre_empted_by=deadline.rule, **intent_fields)

    target_kwargs: dict[str, Any] = {
        "own_planet_debris": own_planet_debris,
        "foreign_debris_targets": foreign_debris_targets,
        "colonize_targets": colonize_targets,
        "attack_targets": attack_targets,
        "missile_targets": missile_targets,
    }
    # The ladder's pick is the fallback and the agreement reference. Pure and cheap.
    ladder_action = plan_mod.plan_next_action(
        snapshot,
        policy,
        killswitch_active=False,
        pending_tx_unreconciled=pending_tx_unreconciled,
        resolvable_mission_ids=resolvable_mission_ids,
        last_attended_planet_id=last_attended_planet_id,
        **target_kwargs,
    )

    pool, rejected = candidates.collect_pool(
        snapshot,
        policy,
        target_planets,
        high_stakes_only_when_idle=cfg.high_stakes_only_when_idle,
        max_candidates=cfg.max_candidates,
        proactive_storage_hours=cfg.proactive_storage_hours,
        **target_kwargs,
    )
    fields: dict[str, Any] = {
        "configured": "jev",
        "pool_size": len(pool),
        "rejected": dict(rejected),
        "ladder_pick": _ladder_pick(ladder_action),
        **intent_fields,
    }

    def fall_back(reason: str, **extra: Any) -> tuple[Action, EngineTrace]:
        return ladder_action, EngineTrace(engine="ladder", fallback_reason=reason, **{**fields, **extra})

    if not pool:
        return fall_back("empty_pool")

    # Everything below this point may leave the machine: hold the intent against the real account.
    try:
        context, intent_override_fields = sendable_context(snapshot, policy, context, target_kwargs)
    except Exception:  # noqa: BLE001 -- a check that cannot run must not send an unchecked override
        from veydrift_agent.engine import EffectiveIntent, EngineContext

        failed = EffectiveIntent(DEFAULT_INTENT, "default", note="intent check failed: built-in default used")
        context = dataclasses.replace(context, intent=failed) if context is not None else EngineContext(intent=failed)
        intent_override_fields = intent_trace_fields(policy, context)
    fields.update(intent_override_fields)

    # The intent text sent is now fixed and recorded in `fields`. Anything unexpected from here on
    # (the backend, scoring, finalising) is a ladder fallback whose trace describes what was sent,
    # not the text that was rejected; `engine.decide` stays the last resort.
    try:
        try:
            backend = backend or jev_mod.default_backend(cfg)
            state, questions = build_request(snapshot, policy, pool, context)
            answers = backend.ask(state, questions, model=cfg.model, timeout_s=cfg.timeout_s)
        except JevError as err:
            return fall_back(err.reason)
        fields.update(model=answers.model, request_id=answers.request_id, latency_ms=answers.latency_ms, input_tokens=answers.input_tokens)

        try:
            scored = _compose(pool, answers, policy)
        except JevError as err:
            return fall_back(err.reason)
        focus_answer = answers.choices["tick_focus"]
        threat = answers.nouls["threat"]
        if not all(math.isfinite(s.composite) for s in scored):
            return fall_back("malformed")
        # Two stages. The group stage is the real judgment -- which kind of development this tick
        # serves -- and is gated on the group-deciding judgments and the lead over other groups.
        # The item stage picks among same-group near-ties in the ladder's own order, ungated: several
        # equally rated legal options are a harmless preference, not uncertainty worth a fallback.
        best = scored[0]
        winner = _item_pick(scored, cfg.min_margin)
        high_stakes = winner.entry.candidate.family in candidates.HIGH_STAKES_FAMILIES
        try:
            conf = (
                _confidence(winner, focus_answer.confidence, policy)
                if high_stakes
                else _group_confidence(best, focus_answer.confidence, policy)
            )
        except JevError as err:
            return fall_back(err.reason)
        group_margin = _group_margin(scored)
        kind_margin = _margin(scored, winner) if high_stakes else None
        for value in (group_margin, kind_margin):
            if value is not None and not math.isfinite(value):
                return fall_back("malformed")
        diagnostics: dict[str, Any] = {
            "winner_confidence": _r(conf),
            "margin": None if group_margin is None else _r(group_margin),
            "focus_probabilities": {k: round(v, 4) for k, v in focus_answer.probabilities.items()},
            "threat": _r(threat),
            "top": [_judgment(s) for s in scored[:5]],
        }
        winner_action = winner.entry.candidate.action
        agrees = candidates.pool_key(winner_action) == candidates.pool_key(ladder_action) if ladder_action.is_onchain() else False
        fields.update(diagnostics)

        if (
            cfg.allow_hold
            and focus_answer.choice == "hold"
            and focus_answer.probabilities.get("hold", 0.0) >= 0.5
            and focus_answer.confidence >= cfg.min_confidence
        ):
            hold = Action(
                kind=ActionKind.NOOP,
                rule=HOLD_RULE,
                rationale="The jev engine judged that waiting fits the strategy better than any available action.",
                engine="jev",
            )
            return hold, EngineTrace(engine="jev", agrees_with_ladder=False, **fields)

        fields["agrees_with_ladder"] = agrees
        if conf < cfg.min_confidence:
            return fall_back("low_confidence")
        if group_margin is not None and group_margin < cfg.min_margin:
            return fall_back("low_margin")
        if high_stakes:
            # The strict path: every weighted judgment's confidence (urgency included), a lead over
            # every other kind of move, then ladder idleness and model endorsement.
            if conf < cfg.min_confidence_high_stakes:
                return fall_back("low_confidence_high_stakes")
            if kind_margin is not None and kind_margin < cfg.min_margin:
                return fall_back("low_margin")
            reason = _high_stakes_reason(winner, ladder_action, focus_answer, cfg)
            if reason is not None:
                return fall_back(reason)

        # Alternatives in pool order (band, then generation index), never composite order, so the
        # action -- and its dedup fingerprint -- does not move when probabilities jitter.
        alternatives = [e.candidate for e in pool if e is not winner.entry]
        action = plan_mod.finalize_candidate(
            winner.entry.candidate,
            alternatives,
            plan_mod.RULE_BY_FAMILY[winner.entry.candidate.family],
            policy,
            snapshot,
        )
        action = action.model_copy(
            update={
                "engine": "jev",
                "rationale": _with_selection_sentence(action.rationale, len(pool), winner.entry.group),
            }
        )
        return action, EngineTrace(engine="jev", **fields)
    except Exception as exc:  # noqa: BLE001 -- an engine problem never fails a tick
        return fall_back(f"engine_error:{type(exc).__name__}")
