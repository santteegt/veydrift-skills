"""Tests for `veydrift_agent.jev_engine` -- the jev decision engine: request building (state and
questions), composition, gating, fallback, and the scenario suite.

Everything runs against `jev_fakes.FakeBackend`; nothing touches the network. The live scenario
variant at the bottom is skipped unless `VEYDRIFT_JEV_LIVE_TESTS=1`.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, get_args

import pytest
from jev_fakes import FakeBackend, answers_from, keyword_responder
from pool_fixtures import (
    FIXTURES,
    WALLET,
    _set,
    load_snapshot,
    make_policy,
    rich_planet,
    rich_snapshot,
    rich_strategy,
    target_kwargs,
)

from veydrift_agent import candidates, ids, jev, jev_engine, plan
from veydrift_agent import engine as engine_mod
from veydrift_agent.candidates import PoolEntry
from veydrift_agent.engine import EngineContext
from veydrift_agent.jev import ChoiceAnswer, JevAnswers, JevError, QuestionSpec, ScoreAnswer
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    Decision,
    EngineCfg,
    EngineTrace,
    EntityTarget,
    IncomingFleet,
    JevCfg,
    JevWeights,
    Policy,
    RadarFinding,
    RadarReport,
    Resources,
    Snapshot,
    StrategyCfg,
)

SIGNER = "0x1111111111111111111111111111111111111111"
SCENARIOS = Path(FIXTURES) / "jev_scenarios"


# --------------------------------------------------------------------------------------
# Builders.
# --------------------------------------------------------------------------------------


def jev_policy(*, strategy: StrategyCfg | None = None, weights: JevWeights | None = None, **jev_kw: Any) -> Policy:
    """A policy with the jev engine configured. `min_margin` defaults to 0 here so a test that
    is not about the margin gate never trips over it."""
    jev_kw.setdefault("min_margin", 0.0)
    cfg = JevCfg(weights=weights or JevWeights(), **jev_kw)
    return make_policy(strategy=strategy or rich_strategy(), engine=EngineCfg(kind="jev", jev=cfg))


def pool_of(snapshot: Snapshot, policy: Policy, **kwargs: Any) -> list[PoolEntry]:
    cfg = policy.engine.jev
    pool, _ = candidates.collect_pool(
        snapshot,
        policy,
        plan._target_planets(snapshot, policy),
        high_stakes_only_when_idle=cfg.high_stakes_only_when_idle,
        max_candidates=cfg.max_candidates,
        **kwargs,
    )
    return pool


def scripted(
    *,
    fits: dict[int, float] | None = None,
    urgs: dict[int, float] | None = None,
    fit_confs: dict[int, float] | None = None,
    urg_confs: dict[int, float] | None = None,
    default_fit: float = 0.5,
    default_urg: float = 0.5,
    fit_conf: float = 0.9,
    urg_conf: float = 0.9,
    focus: str | None = None,
    focus_p: float = 0.9,
    focus_conf: float = 0.9,
    threat: float = 0.0,
    focus_probabilities: dict[str, float] | None = None,
) -> Callable[[dict[str, Any], dict[str, QuestionSpec]], JevAnswers]:
    """A responder answering per candidate index. `focus` is the group `tick_focus` chooses
    (first option when None) with probability `focus_p`, the rest shared evenly."""

    def respond(state: dict[str, Any], questions: dict[str, QuestionSpec]) -> JevAnswers:
        scores: dict[str, ScoreAnswer] = {}
        for qid in questions:
            if qid.startswith("fit_c"):
                i = int(qid[5:])
                scores[qid] = ScoreAnswer((fits or {}).get(i, default_fit), (fit_confs or {}).get(i, fit_conf))
            elif qid.startswith("urgency_c"):
                i = int(qid[9:])
                scores[qid] = ScoreAnswer((urgs or {}).get(i, default_urg), (urg_confs or {}).get(i, urg_conf))
        options = list(questions["tick_focus"].criteria)  # type: ignore[arg-type]
        chosen = focus if focus in options else options[0]
        rest = [o for o in options if o != chosen]
        probs = focus_probabilities or {chosen: focus_p, **{o: (1 - focus_p) / len(rest) for o in rest}}
        choice = ChoiceAnswer(chosen, probs, focus_conf)
        return answers_from(questions, choices={"tick_focus": choice}, scores=scores, nouls={"threat": threat})

    return respond


def run(
    snapshot: Snapshot,
    policy: Policy,
    backend: FakeBackend | None = None,
    **kwargs: Any,
) -> tuple[Action, EngineTrace]:
    return jev_engine.decide(snapshot, policy, backend=backend or FakeBackend(), **kwargs)


def ladder(snapshot: Snapshot, policy: Policy, **kwargs: Any) -> Action:
    allowed = {
        "pending_tx_unreconciled",
        "resolvable_mission_ids",
        "own_planet_debris",
        "foreign_debris_targets",
        "colonize_targets",
        "attack_targets",
        "missile_targets",
        "last_attended_planet_id",
    }
    return plan.plan_next_action(snapshot, policy, **{k: v for k, v in kwargs.items() if k in allowed})


def two_planet() -> Snapshot:
    return rich_snapshot()


def one_planet() -> Snapshot:
    return rich_snapshot(rich_planet())


# --------------------------------------------------------------------------------------
# build_request: state and questions.
# --------------------------------------------------------------------------------------


def test_questions_have_exactly_the_documented_ids_and_shapes():
    snapshot, policy = two_planet(), jev_policy()
    pool = pool_of(snapshot, policy)
    state, questions = jev_engine.build_request(snapshot, policy, pool)

    n = len(pool)
    assert set(questions) == {"tick_focus", "threat"} | {f"fit_c{i}" for i in range(n)} | {f"urgency_c{i}" for i in range(n)}
    assert len(questions) == 2 * n + 2
    assert [c["id"] for c in state["candidates"]] == [f"c{i}" for i in range(n)]

    focus = questions["tick_focus"]
    assert focus.kind == "choice"
    groups = {e.group for e in pool}
    assert set(focus.criteria) == groups | {"hold"}
    for option in focus.criteria.values():
        assert set(option) == {"what", "not_for"}
    assert "`strategy_intent`" in focus.instructions and "`situation`" in focus.instructions

    threat = questions["threat"]
    assert threat.kind == "noul"
    assert "`situation.threats`" in threat.instructions and "`defense_posture`" in threat.instructions
    assert set(threat.criteria) == {"true", "false"}

    for i in (0, n - 1):
        fit, urg = questions[f"fit_c{i}"], questions[f"urgency_c{i}"]
        assert fit.kind == urg.kind == "score"
        assert len(fit.criteria) == 5 and len(urg.criteria) == 4
        assert f"`candidates[{i}]`" in fit.instructions
        assert f"`candidates[{i}]`" in urg.instructions and f"`candidates[{i}].facts`" in urg.instructions


def test_the_state_has_the_documented_shape():
    snapshot, policy = two_planet(), jev_policy()
    state, _ = jev_engine.build_request(snapshot, policy, pool_of(snapshot, policy))

    assert set(state) == {"strategy_intent", "intent_source", "situation", "candidates"}
    situation = state["situation"]
    assert set(situation) == {
        "economy_active",
        "research_queue",
        "fleet_slots",
        "threats",
        "declared_targets",
        "planets",
    }
    assert situation["economy_active"] == "nothing building or researching"
    assert situation["research_queue"] == "idle"
    assert situation["fleet_slots"] == "free"
    assert situation["threats"] == {
        "incoming_hostile_fleets": "none",
        "recent_attacks_on_you": "unknown",
        "debris_on_your_planets": "unknown",
    }
    assert "building priority 1: Robotics Factory" in situation["declared_targets"]
    assert "ship target: Light Fighter, want 3" in situation["declared_targets"]
    assert "defense target: Rocket Launcher, want 3" in situation["declared_targets"]
    planets = situation["planets"]
    assert [p["label"] for p in planets] == ["planet A", "planet B"]
    assert [p["role"] for p in planets] == ["listed first", "other"]
    a = planets[0]
    assert a["energy"] == "surplus"
    assert a["storage_hours_to_cap"] == {"metal": "more than a day", "crystal": "more than a day", "deuterium": "more than a day"}
    assert a["queues"] == {"building": "idle", "ship": "idle", "defense": "idle"}
    assert a["defense_posture"] == "none"
    for cand in state["candidates"]:
        assert set(cand) == {"id", "group", "planet", "what", "facts"}
        assert cand["facts"] and all(isinstance(f, str) for f in cand["facts"])


def test_labels_follow_the_target_planet_order():
    snapshot = two_planet()
    policy = make_policy(planets=[665, 664], strategy=rich_strategy(), engine=EngineCfg(kind="jev"))
    pool = pool_of(snapshot, policy)
    state, _ = jev_engine.build_request(snapshot, policy, pool)
    planets = state["situation"]["planets"]
    assert [(p["label"], p["role"]) for p in planets] == [("planet A", "listed first"), ("planet B", "other")]
    by_id = {e.candidate.action.planet_id: c["planet"] for e, c in zip(pool, state["candidates"], strict=True) if c["planet"].startswith("planet")}
    assert by_id[665] == "planet A" and by_id[664] == "planet B"


def test_candidate_sentences_and_facts():
    snapshot = two_planet()
    policy = jev_policy(high_stakes_only_when_idle=False, max_candidates=60)
    pool = pool_of(snapshot, policy, **target_kwargs())
    state, _ = jev_engine.build_request(snapshot, policy, pool)
    what = {c["what"]: c for c in state["candidates"]}

    assert "Upgrade Metal Mine to level 4" in what
    assert "Research Energy Technology to level 3" in what
    assert "Build 1 Rocket Launcher" in what
    assert "Transport resources from planet A to planet B" in what
    assert "Deploy the fleet from planet A to planet B permanently" in what
    assert "Colonize an empty slot" in what
    assert "Attack another player's planet" in what
    assert "Fire missiles at another player's planet" in what

    mine = what["Upgrade Metal Mine to level 4"]["facts"]
    assert "pays back in under 6 hours" in mine
    assert any(f.startswith("costs a small share") for f in mine)
    assert any(f.startswith("build time: ") for f in mine)
    assert what["Research Energy Technology to level 3"]["planet"].startswith("empire-wide")
    assert "not named in any declared priority" in what["Research Energy Technology to level 3"]["facts"]
    assert "named building priority #1" in what["Upgrade Robotics Factory to level 4"]["facts"]
    assert "counts toward defense target Rocket Launcher (have 0 of 3)" in what["Build 1 Rocket Launcher"]["facts"]
    assert "consumes a Colony Ship" in what["Colonize an empty slot"]["facts"]
    assert "commits combat ships to battle; they can be lost" in what["Attack another player's planet"]["facts"]
    assert "destroys enemy defenses; uses interplanetary missiles" in what["Fire missiles at another player's planet"]["facts"]
    assert "moves resources between your own planets; nothing is gained overall" in what["Transport resources from planet A to planet B"]["facts"]


def test_an_unlock_step_names_the_target_it_unlocks():
    planet = rich_planet()
    _set(planet.buildings, ids.Building.SHIPYARD, level=0)
    _set(planet.buildings, ids.Building.ROBOTICS_FACTORY, level=2)
    snapshot = rich_snapshot(planet)
    policy = make_policy(
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=50)]),
        engine=EngineCfg(kind="jev"),
    )
    pool = pool_of(snapshot, policy)
    state, _ = jev_engine.build_request(snapshot, policy, pool)
    unlock = [c for c in state["candidates"] if c["group"] == "unlock"]
    assert unlock, "the fixture should produce an unlock step"
    assert "next step toward unlocking Small Cargo" in unlock[0]["facts"]


def test_the_default_intent_is_used_when_the_policy_gives_none():
    snapshot = one_planet()
    for intent in ("", "   \n"):
        policy = jev_policy(intent=intent)
        state, _ = jev_engine.build_request(snapshot, policy, pool_of(snapshot, policy))
        assert state["intent_source"] == "default"
        assert state["strategy_intent"] == jev_engine.DEFAULT_INTENT
    policy = jev_policy(intent="  Turtle up and research.  ")
    state, _ = jev_engine.build_request(snapshot, policy, pool_of(snapshot, policy))
    assert state["intent_source"] == "policy"
    assert state["strategy_intent"] == "Turtle up and research."


def test_situation_threats_and_buckets():
    snapshot = one_planet()
    fleets = [IncomingFleet(mission_id="1", hostile=True), IncomingFleet(mission_id="2", hostile=True), IncomingFleet(mission_id="3", hostile=False)]
    snapshot = snapshot.model_copy(update={"incoming_fleets": fleets})
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    assert jev_engine.build_request(snapshot, policy, pool)[0]["situation"]["threats"]["incoming_hostile_fleets"] == "several"

    report = RadarReport(
        findings=[
            RadarFinding(kind="resolved_attack", wallet=WALLET, planet_id=664, detail="battleReport 1 at 7:181:14"),
            RadarFinding(kind="debris", wallet=WALLET, planet_id=664, detail="debris at 7:181:14"),
            RadarFinding(kind="debris", wallet=WALLET, planet_id=664, detail="more debris"),
            RadarFinding(kind="resolved_attack", wallet="0x" + "9" * 40, planet_id=12345, detail="someone else"),
        ]
    )
    state, _ = jev_engine.build_request(snapshot, policy, pool, EngineContext(radar_report=report))
    threats = state["situation"]["threats"]
    assert threats["recent_attacks_on_you"] == "one"
    assert threats["debris_on_your_planets"] == "several"
    assert "battleReport" not in json.dumps(state)


def test_planet_buckets_for_energy_storage_queues_and_defense():
    from veydrift_agent.models import EnergyBalance, QueueEntry, QueueKind, Resources

    planet = rich_planet().model_copy(
        update={
            "energy": EnergyBalance(produced=80, required=100, scale_bps=8000),
            "resources_as_of_now": Resources(metal=9_000_000, crystal=100, deuterium=0),
            "production_per_hour": Resources(metal=1_000_000, crystal=1, deuterium=0),
        }
    )
    planet.queues[QueueKind.BUILDING] = QueueEntry(kind=QueueKind.BUILDING, entity_id=0, entity_name="Metal Mine")
    for defense in planet.defenses:
        if defense.id == 0:
            defense.count = 150
    snapshot = rich_snapshot(planet)
    policy = jev_policy()
    state, _ = jev_engine.build_request(snapshot, policy, pool_of(snapshot, policy))
    p = state["situation"]["planets"][0]
    assert p["energy"] == "deficit: production throttled"
    assert p["storage_hours_to_cap"] == {"metal": "under 2 hours", "crystal": "more than a day", "deuterium": "not filling"}
    assert p["queues"]["building"] == "busy" and p["queues"]["ship"] == "idle"
    assert p["defense_posture"] == "strong"
    assert state["situation"]["economy_active"] == "building or research in progress"


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (None, "no direct production gain"),
        (5.9, "pays back in under 6 hours"),
        (6, "pays back in 6-24 hours"),
        (24, "pays back in 1-3 days"),
        (72, "pays back in 3-7 days"),
        (168, "pays back in more than a week"),
    ],
)
def test_payback_buckets(hours, expected):
    assert jev_engine._payback_fact(hours) == expected


@pytest.mark.parametrize(
    ("share", "expected"),
    [(0.0, "a small share (<10%)"), (0.1, "a moderate share (10-35%)"), (0.35, "a large share (35-70%)"), (0.7, "most (>70%)")],
)
def test_cost_share_buckets(share, expected):
    assert jev_engine._cost_share_bucket(share) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "unknown"), (899, "under 15 minutes"), (900, "15 minutes-2 hours"), (7200, "2-8 hours"), (8 * 3600, "8-24 hours"), (86400, "over a day")],
)
def test_duration_buckets(seconds, expected):
    assert jev_engine._duration_bucket(seconds) == expected


def test_a_24_candidate_request_stays_well_under_the_token_limits():
    snapshot = two_planet()
    policy = jev_policy(max_candidates=24)
    pool = pool_of(snapshot, policy, **target_kwargs())
    assert len(pool) == 24
    state, questions = jev_engine.build_request(snapshot, policy, pool)
    estimate = jev.estimate_tokens(state, questions)
    assert estimate < 20_000
    assert estimate < 32_000


def test_the_payload_carries_no_wallet_signer_address_coordinate_or_raw_planet_id():
    snapshot = two_planet().model_copy(update={"wallet": WALLET})
    policy = make_policy(
        signer=SIGNER,
        strategy=rich_strategy(),
        engine=EngineCfg(kind="jev", jev=JevCfg(high_stakes_only_when_idle=False, max_candidates=60)),
    )
    pool = pool_of(snapshot, policy, **target_kwargs())
    assert {"colonize", "attack", "missile"} <= {e.candidate.family for e in pool}
    report = RadarReport(findings=[RadarFinding(kind="resolved_attack", wallet=WALLET, planet_id=664, detail=f"hit from 7:185:2 by {SIGNER}")])
    state, questions = jev_engine.build_request(snapshot, policy, pool, EngineContext(radar_report=report))
    blob = json.dumps(state) + json.dumps({qid: asdict(q) for qid, q in questions.items()})

    assert WALLET.lower() not in blob.lower()
    assert SIGNER.lower() not in blob.lower()
    assert not re.search(r"0x[0-9a-fA-F]{40}", blob)
    assert not re.search(r"\d+:\d+:\d+", blob)
    for planet in snapshot.planets:
        assert not re.search(rf"\b{planet.planet_id}\b", blob)
    for cand in state["candidates"]:
        assert not cand["planet"].isdigit()


# --------------------------------------------------------------------------------------
# Composition.
# --------------------------------------------------------------------------------------


def test_composition_matches_the_formula_by_hand():
    snapshot = two_planet()
    weights = JevWeights(fit=0.30, urgency=0.25, focus=0.15, economy=0.25, threat=0.05)
    policy = jev_policy(weights=weights, payback_reference_hours=24.0)
    pool = pool_of(snapshot, policy)
    n = len(pool)
    fits = {i: (i % 5) / 4 for i in range(n)}
    urgs = {i: ((i * 3) % 4) / 3 for i in range(n)}
    backend = FakeBackend(scripted(fits=fits, urgs=urgs, focus="research", focus_p=0.6, threat=0.5))

    action, trace = run(snapshot, policy, backend)

    others = [g for g in {e.group for e in pool} if g != "research"]
    focus_p = {"research": 0.6, **{g: 0.4 / (len(others) + 1) for g in others}}  # +1: the hold option

    def expected(i: int, entry: PoolEntry) -> float:
        hours = entry.candidate.score
        econ = 0.3 if hours is None else 24 / (24 + hours)
        family_is_defense = entry.candidate.action.kind is ActionKind.DEFENSE
        thr = 0.5 if family_is_defense else 0.0
        return (0.30 * fits[i] + 0.25 * urgs[i] + 0.15 * focus_p.get(entry.group, 0.0) + 0.25 * econ + 0.05 * thr) / 1.0

    ranked = sorted(range(n), key=lambda i: (-round(expected(i, pool[i]), 6), pool[i].band, pool[i].index))
    assert trace.engine == "jev"
    assert [j.id for j in trace.top] == [f"c{i}" for i in ranked[:5]]
    for j, i in zip(trace.top, ranked[:5], strict=True):
        assert j.composite == pytest.approx(expected(i, pool[i]), abs=1e-4)
        assert j.fit == pytest.approx(fits[i], abs=1e-4)
        assert j.urgency == pytest.approx(urgs[i], abs=1e-4)
    assert action.entity_id == pool[ranked[0]].candidate.action.entity_id
    assert trace.margin == pytest.approx(expected(ranked[0], pool[ranked[0]]) - expected(ranked[1], pool[ranked[1]]), abs=1e-4)


def test_weights_are_normalised_so_only_ratios_matter():
    snapshot = two_planet()
    pool = pool_of(snapshot, jev_policy())
    fits = {i: (i % 7) / 6 for i in range(len(pool))}
    small = jev_policy(weights=JevWeights(fit=0.3, urgency=0.25, focus=0.15, economy=0.25, threat=0.05))
    big = jev_policy(weights=JevWeights(fit=3, urgency=2.5, focus=1.5, economy=2.5, threat=0.5))
    _, t_small = run(snapshot, small, FakeBackend(scripted(fits=fits)))
    _, t_big = run(snapshot, big, FakeBackend(scripted(fits=fits)))
    assert [j.composite for j in t_small.top] == [j.composite for j in t_big.top]


def test_ties_break_by_band_then_generation_index():
    snapshot = two_planet()
    policy = jev_policy(weights=JevWeights(fit=1, urgency=0, focus=0, economy=0, threat=0))
    pool = pool_of(snapshot, policy)
    band3 = next(i for i, e in enumerate(pool) if e.band == 3)
    band4 = next(i for i, e in enumerate(pool) if e.band == 4)
    assert pool[band3].band < pool[band4].band

    # A higher band number loses the tie even when it comes first in nothing but its own order.
    action, trace = run(snapshot, policy, FakeBackend(scripted(fits={band3: 0.8, band4: 0.8})))
    assert trace.top[0].id == f"c{band3}" and trace.top[1].id == f"c{band4}"
    assert action.entity_id == pool[band3].candidate.action.entity_id

    # Same band, same score: the lower generation index wins.
    same_band = [i for i, e in enumerate(pool) if e.band == pool[0].band]
    a, b = same_band[1], same_band[2]
    _, trace = run(snapshot, policy, FakeBackend(scripted(fits={a: 0.9, b: 0.9})))
    assert trace.top[0].id == f"c{a}"

    # Nothing separates any two entries: the very first pool entry wins.
    _, trace = run(snapshot, policy, FakeBackend(scripted()))
    assert trace.top[0].id == "c0"


def test_a_zero_weight_drops_its_term_and_its_confidence():
    snapshot = two_planet()
    pool = pool_of(snapshot, jev_policy())
    every_fit_unsure = {i: 0.1 for i in range(len(pool))}
    responder = scripted(fits={0: 0.9}, fit_confs=every_fit_unsure)

    # fit weighted: the winner's fit confidence 0.1 is below min_confidence -> fallback.
    weighted = jev_policy(weights=JevWeights(fit=1, urgency=1, focus=0, economy=0, threat=0))
    _action, trace = run(snapshot, weighted, FakeBackend(responder))
    assert trace.fallback_reason == "low_confidence" and trace.engine == "ladder"

    # fit weight zero: that judgment feeds nothing, so its confidence is ignored.
    unweighted = jev_policy(weights=JevWeights(fit=0, urgency=1, focus=0, economy=0, threat=0))
    _action, trace = run(snapshot, unweighted, FakeBackend(responder))
    assert trace.fallback_reason is None and trace.engine == "jev"
    assert trace.winner_confidence == 0.9

    # the tick_focus confidence only counts while focus is weighted.
    low_focus = scripted(focus_conf=0.2)
    focus_weighted = jev_policy(weights=JevWeights(fit=1, urgency=0, focus=1, economy=0, threat=0))
    focus_off = jev_policy(weights=JevWeights(fit=1, urgency=0, focus=0, economy=1, threat=0))
    assert run(snapshot, focus_weighted, FakeBackend(low_focus))[1].fallback_reason == "low_confidence"
    assert run(snapshot, focus_off, FakeBackend(low_focus))[1].engine == "jev"


def test_the_winners_confidence_is_the_minimum_over_its_weighted_judgments():
    snapshot = one_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    responder = scripted(fits={0: 0.95}, fit_confs={0: 0.7}, urg_confs={0: 0.8}, focus_conf=0.9)
    _, trace = run(snapshot, policy, FakeBackend(responder))
    assert trace.top[0].id == "c0" and len(pool) > 1
    assert trace.winner_confidence == 0.7


def test_a_threat_lifts_defense_candidates_only():
    snapshot = one_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    defense = [i for i, e in enumerate(pool) if e.candidate.action.kind is ActionKind.DEFENSE]
    assert defense
    _, calm = run(snapshot, policy, FakeBackend(scripted(threat=0.0)))
    _, hot = run(snapshot, policy, FakeBackend(scripted(threat=1.0)))
    assert hot.threat == 1.0 and calm.threat == 0.0
    top_hot = {j.id: j for j in hot.top}
    for j in hot.top:
        expected_thr = 1.0 if int(j.id[1:]) in defense else 0.0
        assert j.threat == expected_thr
    assert all(j.threat == 0.0 for j in calm.top)
    assert top_hot  # the ranking still exists


def test_a_missing_answer_is_a_malformed_fallback():
    snapshot = one_planet()
    policy = jev_policy()

    def drop_a_score(state, questions):
        answers = scripted()(state, questions)
        scores = dict(answers.scores)
        scores.pop("fit_c0")
        return JevAnswers(choices=answers.choices, scores=scores, nouls=answers.nouls)

    action, trace = run(snapshot, policy, FakeBackend(drop_a_score))
    assert trace.fallback_reason == "malformed"
    assert action.model_dump() == ladder(snapshot, policy).model_dump()


# --------------------------------------------------------------------------------------
# The chosen action.
# --------------------------------------------------------------------------------------


def test_a_jev_pick_is_a_finalized_action_with_the_rule_of_its_family():
    snapshot = two_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    research = next(i for i, e in enumerate(pool) if e.candidate.family == "research")
    action, trace = run(snapshot, policy, FakeBackend(scripted(fits={research: 1.0}, urgs={research: 1.0}, focus="research")))

    assert action.engine == "jev"
    assert action.kind is ActionKind.RESEARCH
    assert action.rule == plan.RULE_BY_FAMILY["research"] == "7:research-queue-empty"
    assert action.brief is not None
    assert action.source == "planner"
    tail = action.rationale.rsplit(" Selected by the jev engine", 1)[1]
    assert tail == f" from {len(pool)} legal candidates (research focus)."
    assert not re.search(r"0\.\d|confiden|probab|score", action.rationale.lower().split("selected by")[1])
    # alternatives: pool order, winner excluded, capped
    others = [e.candidate for j, e in enumerate(pool) if j != research]
    assert [a.entity_name for a in action.alternatives] == [c.action.entity_name for c in others][: policy.strategy.max_alternatives]
    assert trace.engine == "jev" and trace.configured == "jev" and trace.pool_size == len(pool)
    assert trace.model == "jev-fake" and trace.request_id == "fake-request" and trace.latency_ms == 1


def test_alternatives_never_depend_on_the_composite_order():
    snapshot = two_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    research = next(i for i, e in enumerate(pool) if e.candidate.family == "research")
    shuffled = {i: ((i * 7) % 10) / 20 for i in range(len(pool))}
    a1, _ = run(snapshot, policy, FakeBackend(scripted(fits={**shuffled, research: 1.0}, urgs={research: 1.0}, focus="research")))
    reversed_ = {i: ((len(pool) - i) % 10) / 20 for i in range(len(pool))}
    a2, _ = run(snapshot, policy, FakeBackend(scripted(fits={**reversed_, research: 1.0}, urgs={research: 1.0}, focus="research")))
    assert a1.model_dump() == a2.model_dump()


def test_jittered_probabilities_with_the_same_argmax_give_an_identical_action():
    snapshot = two_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    winner = next(i for i, e in enumerate(pool) if e.candidate.action.entity_name == "Laser Technology")

    def jittered(seed: float):
        fits = {i: 0.3 + seed * ((i * 13) % 5) / 500 for i in range(len(pool))}
        urgs = {i: 0.5 + seed * ((i * 3) % 7) / 700 for i in range(len(pool))}
        fits[winner], urgs[winner] = 0.97 + seed / 1000, 0.9
        return scripted(fits=fits, urgs=urgs, focus="research", focus_p=0.7 + seed / 100, threat=0.1 + seed / 50)

    first, t1 = run(snapshot, policy, FakeBackend(jittered(0.1)))
    second, t2 = run(snapshot, policy, FakeBackend(jittered(0.9)))
    assert first.entity_name == "Laser Technology" == second.entity_name
    assert first.model_dump() == second.model_dump()
    assert t1.top[0].composite != t2.top[0].composite  # the trace moves, the action does not


def test_the_trace_records_the_ladder_pick_and_agreement():
    snapshot = two_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    expected = ladder(snapshot, policy)
    assert expected.is_onchain()
    same = next(i for i, e in enumerate(pool) if candidates.pool_key(e.candidate.action) == candidates.pool_key(expected))
    action, trace = run(snapshot, policy, FakeBackend(scripted(fits={same: 1.0}, urgs={same: 1.0})))
    assert trace.ladder_pick == {
        "rule": expected.rule,
        "function": expected.function,
        "planet_id": expected.planet_id,
        "entity_id": expected.entity_id,
        "entity_name": expected.entity_name,
    }
    assert trace.agrees_with_ladder is True
    assert candidates.pool_key(action) == candidates.pool_key(expected)

    other = next(i for i, e in enumerate(pool) if e.candidate.family == "research")
    action, trace = run(snapshot, policy, FakeBackend(scripted(fits={other: 1.0}, urgs={other: 1.0}, focus="research")))
    assert trace.agrees_with_ladder is False


def test_the_trace_is_bounded_and_rounded():
    snapshot = two_planet()
    policy = jev_policy()
    pool = pool_of(snapshot, policy)
    fits = {i: 1 / 3 + i / 97 for i in range(len(pool))}
    _, trace = run(snapshot, policy, FakeBackend(scripted(fits=fits, focus_p=2 / 3)))
    assert len(trace.top) == 5
    assert [j.composite for j in trace.top] == sorted((j.composite for j in trace.top), reverse=True)
    for value in (trace.margin, trace.winner_confidence, trace.threat, *trace.focus_probabilities.values()):
        assert round(value, 4) == value
    for j in trace.top:
        for value in (j.fit, j.urgency, j.focus, j.economy, j.composite):
            assert round(value, 4) == value
    assert trace.input_tokens == 0
    assert set(trace.focus_probabilities) == {e.group for e in pool} | {"hold"}


# --------------------------------------------------------------------------------------
# Gating and fallback.
# --------------------------------------------------------------------------------------


def assert_fell_back(snapshot: Snapshot, policy: Policy, result: tuple[Action, EngineTrace], reason: str, **kwargs: Any) -> None:
    action, trace = result
    assert trace.fallback_reason == reason
    assert trace.engine == "ladder" and trace.configured == "jev"
    assert action.engine == "ladder"
    assert action.model_dump() == ladder(snapshot, policy, **kwargs).model_dump()


def test_low_confidence_falls_back():
    snapshot, policy = one_planet(), jev_policy()
    result = run(snapshot, policy, FakeBackend(scripted(fit_conf=0.4)))
    assert_fell_back(snapshot, policy, result, "low_confidence")
    assert result[1].winner_confidence == 0.4
    assert result[1].top, "diagnostics are kept on a fallback"


def test_the_confidence_floor_is_inclusive():
    snapshot, policy = one_planet(), jev_policy(min_confidence=0.6)
    assert run(snapshot, policy, FakeBackend(scripted(fit_conf=0.6)))[1].engine == "jev"


def test_high_stakes_winners_need_the_higher_floor(monkeypatch):
    snapshot = two_planet()
    policy = jev_policy(high_stakes_only_when_idle=False, max_candidates=60)
    full = pool_of(snapshot, policy, **target_kwargs())
    colonize = next(e for e in full if e.candidate.family == "colonize")
    economy = next(e for e in full if e.candidate.family == "mine")
    pool = sorted([colonize, economy], key=lambda e: (e.band, e.index))
    monkeypatch.setattr(candidates, "collect_pool", lambda *a, **k: (pool, {}))
    at = pool.index(colonize)

    responder = scripted(fits={at: 1.0}, urgs={at: 1.0}, fit_conf=0.6, urg_conf=0.6, default_fit=0.0, default_urg=0.0, focus="expansion")
    result = run(snapshot, policy, FakeBackend(responder), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "low_confidence_high_stakes", **target_kwargs())

    strong = scripted(fits={at: 1.0}, urgs={at: 1.0}, fit_conf=0.8, urg_conf=0.8, default_fit=0.0, default_urg=0.0, focus="expansion")
    action, trace = run(snapshot, policy, FakeBackend(strong), **target_kwargs())
    assert trace.engine == "jev" and action.rule == "8d:colonize" and action.engine == "jev"

    # the same 0.6 confidence is fine for a non-high-stakes winner
    other = 1 - at
    ok = scripted(fits={other: 1.0}, urgs={other: 1.0}, fit_conf=0.6, urg_conf=0.6, default_fit=0.0, default_urg=0.0)
    assert run(snapshot, policy, FakeBackend(ok), **target_kwargs())[1].engine == "jev"


def test_a_thin_margin_falls_back():
    snapshot, policy = one_planet(), jev_policy(min_margin=0.03)
    # Every judgment ties; only the code-computed economy term separates the entries.
    result = run(snapshot, policy, FakeBackend(scripted()))
    assert_fell_back(snapshot, policy, result, "low_margin")
    assert result[1].margin is not None and result[1].margin < 0.03


def test_a_clear_margin_passes():
    snapshot, policy = one_planet(), jev_policy(min_margin=0.03)
    _action, trace = run(snapshot, policy, FakeBackend(scripted(fits={3: 1.0}, urgs={3: 1.0})))
    assert trace.engine == "jev" and trace.margin >= 0.03


def test_a_single_entry_pool_skips_the_margin_gate(monkeypatch):
    snapshot, policy = one_planet(), jev_policy(min_margin=0.5)
    only = pool_of(snapshot, policy)[:1]
    monkeypatch.setattr(candidates, "collect_pool", lambda *a, **k: (only, {}))
    action, trace = run(snapshot, policy, FakeBackend(scripted()))
    assert trace.engine == "jev" and trace.margin is None
    assert action.rationale.endswith("Selected by the jev engine from 1 legal candidates (economy focus).")


@pytest.mark.parametrize("reason", get_args(jev.JevErrorReason))
def test_every_jev_error_falls_back_to_the_ladder_action(reason):
    snapshot, policy = two_planet(), jev_policy()
    backend = FakeBackend(error=JevError(reason))
    result = run(snapshot, policy, backend)
    assert_fell_back(snapshot, policy, result, reason)
    assert len(backend.calls) == 1
    assert result[1].pool_size and result[1].pool_size > 0


def test_a_missing_key_from_default_backend_falls_back(monkeypatch):
    snapshot, policy = two_planet(), jev_policy()

    def no_key(cfg):
        raise JevError("missing_key")

    monkeypatch.setattr(jev, "default_backend", no_key)
    result = jev_engine.decide(snapshot, policy)
    assert_fell_back(snapshot, policy, result, "missing_key")


def test_the_configured_model_and_timeout_reach_the_backend():
    snapshot, policy = one_planet(), jev_policy(model="jev-1.13.0", timeout_s=2.5)
    backend = FakeBackend(scripted())
    run(snapshot, policy, backend)
    (_, _, model, timeout_s), = backend.calls
    assert (model, timeout_s) == ("jev-1.13.0", 2.5)


def test_the_fallback_keeps_the_ladders_own_kwargs():
    snapshot, policy = two_planet(), jev_policy()
    kwargs = {"last_attended_planet_id": 664, **target_kwargs()}
    result = run(snapshot, policy, FakeBackend(error=JevError("timeout")), **kwargs)
    assert_fell_back(snapshot, policy, result, "timeout", **kwargs)


def test_an_empty_pool_falls_back_without_a_call():
    snapshot = one_planet()
    policy = make_policy(
        actions=ActionsCfg(
            allow_building=False,
            allow_research=False,
            allow_defense=False,
            allow_ships=False,
            allow_fleet_noncombat=False,
            allow_combat=False,
        ),
        engine=EngineCfg(kind="jev"),
    )
    backend = FakeBackend()
    result = run(snapshot, policy, backend)
    assert_fell_back(snapshot, policy, result, "empty_pool")
    assert backend.calls == [] and result[1].pool_size == 0


# --------------------------------------------------------------------------------------
# High-stakes winners must be endorsed, and must respect the ladder's meaning of "idle".
# --------------------------------------------------------------------------------------


def only_high_stakes_setup(**jev_kw: Any) -> tuple[Snapshot, Policy]:
    """Everything ordinary is switched off, so the ladder itself reaches 8d:colonize and the pool
    holds colonize/attack/missile (two planets each): c0/c1 colonize, c2/c3 attack, c4/c5 missile."""
    jev_kw.setdefault("min_margin", 0.0)
    policy = make_policy(
        actions=ActionsCfg(
            allow_building=False,
            allow_research=False,
            allow_ships=False,
            allow_defense=False,
            allow_fleet_noncombat=False,
            allow_combat=True,
        ),
        strategy=StrategyCfg(colonize=True),
        engine=EngineCfg(kind="jev", jev=JevCfg(**jev_kw)),
    )
    return two_planet(), policy


def endorsing(at: int = 0, **kwargs: Any) -> Callable[[dict[str, Any], dict[str, QuestionSpec]], JevAnswers]:
    """Candidate `at` is fit 1.0 / urgency 1.0 and `tick_focus` picks its group; all else scores 0."""
    kwargs.setdefault("focus", "expansion")
    kwargs.setdefault("default_fit", 0.0)
    kwargs.setdefault("default_urg", 0.0)
    return scripted(fits={at: kwargs.pop("fit", 1.0)}, urgs={at: 1.0}, **kwargs)


def test_an_unaffordable_ladder_pick_does_not_open_the_door_to_an_unendorsed_attack():
    """The judge's repro: nothing affordable but an attack. The ladder proposes (unaffordable)
    research; the pool drops the unaffordable items and holds only the attack; a model that finds
    the attack unrelated to the strategy (fit 0, urgency 0) used to win it by default."""
    planet = rich_planet().model_copy(
        update={
            "resources_as_of_now": Resources(metal=0, crystal=0, deuterium=50_000),
            "resources": Resources(metal=0, crystal=0, deuterium=50_000),
        }
    )
    snapshot = rich_snapshot(planet)
    policy = make_policy(
        actions=ActionsCfg(allow_building=True, allow_research=True, allow_combat=True, allow_defense=True, allow_ships=True),
        strategy=StrategyCfg(),
        engine=EngineCfg(kind="jev", jev=JevCfg(min_margin=0.0)),
    )
    kwargs = target_kwargs()
    kwargs.pop("missile_targets")

    ladder_action = ladder(snapshot, policy, **kwargs)
    assert ladder_action.rule == "7:research-queue-empty" and ladder_action.is_onchain()
    pool = pool_of(snapshot, policy, **kwargs)
    assert [e.candidate.family for e in pool] == ["attack"]

    def indifferent(state, questions):
        scores = {qid: 0.0 for qid in questions if qid.startswith(("fit_", "urgency_"))}
        return answers_from(questions, scores=scores, score_confidence=0.95, choice_confidence=0.95)

    action, trace = run(snapshot, policy, FakeBackend(indifferent), **kwargs)

    assert_fell_back(snapshot, policy, (action, trace), "high_stakes_not_idle", **kwargs)
    assert action.rule == "7:research-queue-empty"
    # ... and the attack really was something the guard would have let through.
    from test_pool import _guard_report

    attack = pool[0].candidate.action
    assert _guard_report(attack, snapshot, policy).decision is Decision.ALLOW


def test_a_high_stakes_winner_is_refused_while_the_ladders_pick_is_ordinary(monkeypatch):
    snapshot = two_planet()
    policy = jev_policy(max_candidates=60)  # high_stakes_only_when_idle stays on
    full = pool_of(snapshot, jev_policy(high_stakes_only_when_idle=False, max_candidates=60), **target_kwargs())
    colonize = next(e for e in full if e.candidate.family == "colonize")
    economy = next(e for e in full if e.candidate.family == "mine")
    pool = sorted([colonize, economy], key=lambda e: (e.band, e.index))
    monkeypatch.setattr(candidates, "collect_pool", lambda *a, **k: (pool, {}))
    at = pool.index(colonize)
    assert not ladder(snapshot, policy, **target_kwargs()).rule.startswith("8")

    result = run(snapshot, policy, FakeBackend(endorsing(at)), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "high_stakes_not_idle", **target_kwargs())

    off = jev_policy(high_stakes_only_when_idle=False, max_candidates=60)
    action, trace = run(snapshot, off, FakeBackend(endorsing(at)), **target_kwargs())
    assert trace.engine == "jev" and action.rule == "8d:colonize"


def test_a_weakly_fitting_high_stakes_winner_falls_back():
    snapshot, policy = only_high_stakes_setup()
    assert ladder(snapshot, policy, **target_kwargs()).rule == "8d:colonize"
    for fit in (0.0, 0.5, 0.7):  # 0.75 ("directly supports") is the least that endorses
        result = run(snapshot, policy, FakeBackend(endorsing(fit=fit)), **target_kwargs())
        assert result[1].fallback_reason == "high_stakes_not_endorsed", fit
        assert result[1].winner_confidence == 0.9
    assert run(snapshot, policy, FakeBackend(endorsing(fit=0.75)), **target_kwargs())[1].engine == "jev"


def test_a_high_stakes_winner_of_another_group_than_the_focus_falls_back():
    snapshot, policy = only_high_stakes_setup()
    result = run(snapshot, policy, FakeBackend(endorsing(focus="offense")), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "high_stakes_not_endorsed", **target_kwargs())
    assert result[1].top[0].family == "colonize", "colonize still won the composite; the focus vetoed it"


def test_a_high_stakes_winner_falls_back_when_the_focus_is_hold_even_if_hold_is_not_allowed():
    snapshot, policy = only_high_stakes_setup(allow_hold=False)
    result = run(snapshot, policy, FakeBackend(endorsing(focus="hold")), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "high_stakes_hold", **target_kwargs())
    assert result[1].top[0].family == "colonize"

    # a hold too weak to be taken as a hold still vetoes a high-stakes move
    weak = run(snapshot, policy, FakeBackend(endorsing(focus="hold", focus_p=0.4)), **target_kwargs())
    assert weak[1].fallback_reason == "high_stakes_hold"


def test_an_endorsed_high_stakes_winner_is_taken_when_the_ladder_pick_is_high_stakes_too():
    snapshot, policy = only_high_stakes_setup()
    action, trace = run(snapshot, policy, FakeBackend(endorsing()), **target_kwargs())
    assert trace.engine == "jev" and trace.fallback_reason is None
    assert action.rule == "8d:colonize" and action.engine == "jev"
    assert trace.winner_confidence == 0.9 and trace.margin > 0

    # the attack rung (also high-stakes) is just as reachable when it is the endorsed one
    attack = next(i for i, e in enumerate(pool_of(snapshot, policy, **target_kwargs())) if e.candidate.family == "attack")
    action, trace = run(snapshot, policy, FakeBackend(endorsing(attack, focus="offense")), **target_kwargs())
    assert trace.engine == "jev" and action.rule == "8e:attack"


def test_an_endorsed_high_stakes_winner_is_taken_when_the_ladder_has_nothing_to_do(monkeypatch):
    snapshot, policy = only_high_stakes_setup()
    idle = Action(kind=ActionKind.NOOP, rule="9:noop", rationale="nothing to do")
    monkeypatch.setattr(plan, "plan_next_action", lambda *a, **k: idle)
    action, trace = run(snapshot, policy, FakeBackend(endorsing()), **target_kwargs())
    assert trace.engine == "jev" and action.rule == "8d:colonize"
    assert trace.ladder_pick["rule"] == "9:noop"


def test_deploy_is_high_stakes_at_the_engine_too(monkeypatch):
    snapshot = two_planet()
    policy = jev_policy(max_candidates=60)
    full = pool_of(snapshot, jev_policy(high_stakes_only_when_idle=False, max_candidates=60), **target_kwargs())
    deploy = next(e for e in full if e.candidate.family == "logistics-deploy")
    mine = next(e for e in full if e.candidate.family == "mine")
    pool = sorted([deploy, mine], key=lambda e: (e.band, e.index))
    monkeypatch.setattr(candidates, "collect_pool", lambda *a, **k: (pool, {}))
    at = pool.index(deploy)
    assert "logistics-deploy" in candidates.HIGH_STAKES_FAMILIES

    result = run(snapshot, policy, FakeBackend(endorsing(at, focus="logistics")), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "high_stakes_not_idle", **target_kwargs())

    weak = run(snapshot, policy, FakeBackend(endorsing(at, focus="logistics", fit_conf=0.6, urg_conf=0.6)), **target_kwargs())
    assert weak[1].fallback_reason == "low_confidence_high_stakes"


def test_the_ladder_rules_that_count_as_high_stakes_are_exactly_the_high_stakes_families():
    assert jev_engine.HIGH_STAKES_RULES == {"8c:logistics-deploy", "8d:colonize", "8e:attack", "8f:missile"}


# --------------------------------------------------------------------------------------
# Degenerate composition, and the margin against a different kind of action.
# --------------------------------------------------------------------------------------


def with_weights(policy: Policy, weights: JevWeights) -> Policy:
    jev_cfg = policy.engine.jev.model_copy(update={"weights": weights})
    return policy.model_copy(update={"engine": policy.engine.model_copy(update={"jev": jev_cfg})})


@pytest.mark.parametrize("bad", [float("inf"), float("nan")], ids=["inf", "nan"])
def test_a_non_finite_weight_is_a_malformed_fallback(bad):
    snapshot, base = two_planet(), jev_policy()
    weights = JevWeights.model_construct(fit=bad, urgency=0.25, focus=0.15, economy=0.25, threat=0.05)
    policy = with_weights(base, weights)
    result = run(snapshot, policy, FakeBackend(scripted()))
    assert result[1].fallback_reason == "malformed" and result[1].engine == "ladder"
    assert result[0].model_dump() == ladder(snapshot, policy).model_dump()


def test_a_non_finite_composite_is_a_malformed_fallback():
    snapshot, policy = two_planet(), jev_policy(payback_reference_hours=1.0)
    policy = policy.model_copy(update={"engine": policy.engine.model_copy(update={"jev": policy.engine.jev.model_copy(update={"payback_reference_hours": float("nan")})})})
    result = run(snapshot, policy, FakeBackend(scripted()))
    assert result[1].fallback_reason == "malformed"
    assert result[0].model_dump() == ladder(snapshot, policy).model_dump()


def test_an_all_zero_weight_vector_is_malformed_not_a_division_by_zero():
    snapshot = two_planet()
    weights = JevWeights.model_construct(fit=0.0, urgency=0.0, focus=0.0, economy=0.0, threat=0.0)
    result = run(snapshot, with_weights(jev_policy(), weights), FakeBackend(scripted()))
    assert result[1].fallback_reason == "malformed"


def test_confidence_is_never_vacuously_certain():
    snapshot = two_planet()
    # only the (code-computed) economy term is weighted: no judgment carries a confidence.
    weights = JevWeights.model_construct(fit=0.0, urgency=0.0, focus=0.0, economy=1.0, threat=0.0)
    policy = with_weights(jev_policy(), weights)
    result = run(snapshot, policy, FakeBackend(scripted(fit_conf=0.01, urg_conf=0.01, focus_conf=0.01)))
    assert result[1].fallback_reason == "malformed" and result[1].engine == "ladder"

    winner = jev_engine._Scored(
        id="c0", entry=pool_of(snapshot, policy)[0], fit=1.0, fit_confidence=0.9, urgency=1.0, urgency_confidence=0.9,
        focus=1.0, economy=1.0, threat=0.0, composite=1.0,
    )
    with pytest.raises(JevError) as err:
        jev_engine._confidence(winner, 0.9, policy)
    assert err.value.reason == "malformed"


def test_identical_candidates_on_symmetric_planets_do_not_starve_the_margin_gate():
    snapshot, policy = two_planet(), jev_policy(min_margin=0.03)
    pool = pool_of(snapshot, policy)
    twins = [i for i, e in enumerate(pool) if e.candidate.family == "mine" and e.candidate.action.entity_id == ids.Building.METAL_MINE]
    assert len(twins) == 2 and pool[twins[0]].candidate.action.planet_id != pool[twins[1]].candidate.action.planet_id

    action, trace = run(snapshot, policy, FakeBackend(scripted(fits={i: 1.0 for i in twins}, default_fit=0.3)))

    assert trace.fallback_reason is None and trace.engine == "jev"
    first, second = trace.top[0], trace.top[1]
    assert first.composite == second.composite, "the twins tie"
    assert {first.id, second.id} == {f"c{i}" for i in twins}
    assert action.planet_id == pool[twins[0]].candidate.action.planet_id, "the tie goes to the lower generation index"
    rival = max(j.composite for j in trace.top if j.id not in {first.id, second.id})
    assert trace.margin == pytest.approx(first.composite - rival, abs=1e-4)
    assert trace.margin >= 0.03


def test_the_margin_is_still_measured_against_a_different_kind_of_action():
    snapshot, policy = two_planet(), jev_policy(min_margin=0.03)
    pool = pool_of(snapshot, policy)
    metal = next(i for i, e in enumerate(pool) if e.candidate.action.entity_id == ids.Building.METAL_MINE)
    crystal = next(i for i, e in enumerate(pool) if e.candidate.action.entity_id == ids.Building.CRYSTAL_MINE)
    # Two different upgrades that score alike: that is a genuinely thin margin.
    result = run(snapshot, policy, FakeBackend(scripted(fits={metal: 1.0, crystal: 1.0}, urgs={metal: 1.0, crystal: 1.0}, default_fit=0.0, default_urg=0.0)))
    assert result[1].fallback_reason == "low_margin"


def test_a_pool_of_one_kind_only_has_no_rival_to_be_confused_with(monkeypatch):
    snapshot, policy = two_planet(), jev_policy(min_margin=0.5)
    pool = pool_of(snapshot, policy)
    twins = [e for e in pool if e.candidate.family == "mine" and e.candidate.action.entity_id == ids.Building.METAL_MINE]
    monkeypatch.setattr(candidates, "collect_pool", lambda *a, **k: (twins, {}))
    _action, trace = run(snapshot, policy, FakeBackend(scripted()))
    assert trace.engine == "jev" and trace.margin is None
    assert engine_mod.describe_trace(trace).startswith("jev (")  # a None margin is rendered without a number



# --------------------------------------------------------------------------------------
# Vetoes and the deadline decide first; the backend is never called.
# --------------------------------------------------------------------------------------


def near_cap_snapshot() -> Snapshot:
    from veydrift_agent.models import Resources

    planet = rich_planet().model_copy(
        update={
            "resources_as_of_now": Resources(metal=9_500_000, crystal=5_000_000, deuterium=5_000_000),
            "resources": Resources(metal=9_500_000, crystal=5_000_000, deuterium=5_000_000),
            "production_per_hour": Resources(metal=1_000_000, crystal=500, deuterium=200),
        }
    )
    return rich_snapshot(planet)


def _veto_cases() -> list[tuple[str, Callable[[], tuple[Snapshot, dict[str, Any]]], str]]:
    def health():
        return one_planet().model_copy(update={"health_ok": False}), {}

    def paused():
        return one_planet().model_copy(update={"game_paused": True}), {}

    def pending():
        return one_planet(), {"pending_tx_unreconciled": True}

    def mission():
        return one_planet(), {"resolvable_mission_ids": [77]}

    def hostile():
        return one_planet().model_copy(update={"incoming_fleets": [IncomingFleet(mission_id="1", hostile=True)]}), {}

    def cap():
        return near_cap_snapshot(), {}

    return [
        ("health", health, "1:"),
        ("paused", paused, "1b:"),
        ("pending", pending, "2:"),
        ("mission", mission, "3:"),
        ("hostile", hostile, "4:"),
        ("deadline", cap, "5:"),
    ]


@pytest.mark.parametrize(("name", "make", "prefix"), _veto_cases(), ids=[c[0] for c in _veto_cases()])
def test_vetoes_and_the_deadline_never_call_the_backend(name, make, prefix):
    snapshot, kwargs = make()
    policy = jev_policy()
    backend = FakeBackend()
    action, trace = run(snapshot, policy, backend, **kwargs)

    assert backend.calls == []
    assert action.rule.startswith(prefix)
    assert trace.pre_empted_by == action.rule
    assert trace.engine == "ladder" and trace.configured == "jev" and trace.fallback_reason is None
    assert action.model_dump() == ladder(snapshot, policy, **kwargs).model_dump()


def test_a_hostile_fleet_with_escalation_off_reaches_the_backend():
    snapshot = one_planet().model_copy(update={"incoming_fleets": [IncomingFleet(mission_id="1", hostile=True)]})
    policy = make_policy(
        strategy=rich_strategy(),
        escalation=make_policy().escalation.model_copy(update={"on_incoming_fleet": False}),
        engine=EngineCfg(kind="jev", jev=JevCfg(min_margin=0.0)),
    )
    backend = FakeBackend(scripted())
    _, trace = run(snapshot, policy, backend)
    assert len(backend.calls) == 1 and trace.pre_empted_by is None
    assert backend.calls[0][0]["situation"]["threats"]["incoming_hostile_fleets"] == "one"


# --------------------------------------------------------------------------------------
# Hold.
# --------------------------------------------------------------------------------------


def hold_answers(p_hold: float, confidence: float = 0.9, choice: str = "hold"):
    def respond(state, questions):
        base = scripted()(state, questions)
        options = list(questions["tick_focus"].criteria)
        rest = [o for o in options if o != "hold"]
        probs = {"hold": p_hold, **{o: (1 - p_hold) / len(rest) for o in rest}}
        return JevAnswers(
            choices={"tick_focus": ChoiceAnswer(choice, probs, confidence)},
            scores=base.scores,
            nouls=base.nouls,
            model="jev-fake",
            request_id="fake-request",
        )

    return respond


def test_a_confident_hold_returns_a_noop_when_allowed():
    snapshot, policy = one_planet(), jev_policy(allow_hold=True)
    action, trace = run(snapshot, policy, FakeBackend(hold_answers(0.8)))
    assert action.kind is ActionKind.NOOP and action.rule == jev_engine.HOLD_RULE == "9j:hold"
    assert action.engine == "jev" and action.function is None
    assert action.rationale == "The jev engine judged that waiting fits the strategy better than any available action."
    assert trace.engine == "jev" and trace.fallback_reason is None
    assert trace.focus_probabilities["hold"] == 0.8


def test_hold_is_ignored_when_not_allowed():
    snapshot, policy = one_planet(), jev_policy(allow_hold=False)
    action, trace = run(snapshot, policy, FakeBackend(hold_answers(0.8)))
    assert action.rule != jev_engine.HOLD_RULE and action.function is not None
    assert trace.engine == "jev"


@pytest.mark.parametrize(
    ("p_hold", "confidence"),
    [(0.45, 0.9), (0.8, 0.3)],
    ids=["hold-not-a-majority", "hold-unsure"],
)
def test_a_weak_hold_is_not_taken(p_hold, confidence):
    snapshot, policy = one_planet(), jev_policy(allow_hold=True)
    action, _ = run(snapshot, policy, FakeBackend(hold_answers(p_hold, confidence)))
    assert action.rule != jev_engine.HOLD_RULE


def test_choosing_another_group_is_never_a_hold():
    snapshot, policy = one_planet(), jev_policy(allow_hold=True)
    action, _ = run(snapshot, policy, FakeBackend(hold_answers(0.8, choice="economy")))
    assert action.rule != jev_engine.HOLD_RULE


# --------------------------------------------------------------------------------------
# Scenario suite.
# --------------------------------------------------------------------------------------


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for key, value in patch.items():
            out[key] = _merge(base.get(key), value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(patch)


def load_scenario(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def scenario_inputs(spec: dict[str, Any]) -> tuple[Snapshot, Policy]:
    source = spec["snapshot"]
    if isinstance(source, str):
        snapshot = load_snapshot(source)
    elif source["builder"] == "rich_one":
        snapshot = one_planet()
    elif source["builder"] == "rich_two":
        snapshot = two_planet()
    else:
        raise AssertionError(f"unknown snapshot builder {source['builder']!r}")
    dumped = snapshot.model_dump(mode="json")
    dumped = _merge(dumped, spec.get("snapshot_patch", {}))
    for planet_id, patch in spec.get("planet_patches", {}).items():
        for i, planet in enumerate(dumped["planets"]):
            if str(planet["planet_id"]) == planet_id:
                dumped["planets"][i] = _merge(planet, patch)
    snapshot = Snapshot.model_validate(dumped)

    policy_json = _merge(make_policy().model_dump(mode="json"), spec.get("policy_overrides", {}))
    policy_json["engine"] = {"kind": "jev", "jev": {"intent": spec["intent"], "min_margin": 0.0}}
    return snapshot, Policy.model_validate(policy_json)


def acceptable(action: Action, entries: list[dict[str, str]]) -> bool:
    for entry in entries:
        if "rule_prefix" in entry:
            if action.rule.startswith(entry["rule_prefix"]):
                return True
            continue
        if all(getattr(action, key) == value for key, value in entry.items()):
            return True
    return False


SCENARIO_FILES = sorted(SCENARIOS.glob("*.json"))


def test_the_scenario_folder_covers_the_documented_cases():
    assert {p.stem for p in SCENARIO_FILES} >= {
        "planet_664_opener",
        "planet_hot_opener",
        "storage_near_cap",
        "band2_starvation",
        "incoming_threat",
        "multi_planet_research",
    }


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=[p.stem for p in SCENARIO_FILES])
def test_scenario_with_a_rule_based_backend(path):
    spec = load_scenario(path)
    snapshot, policy = scenario_inputs(spec)
    responder = keyword_responder(**spec["responder"])
    backend = FakeBackend(responder)

    action, trace = jev_engine.decide(snapshot, policy, backend=backend)

    if spec.get("backend_not_called"):
        assert backend.calls == []
        assert trace.pre_empted_by == action.rule
    else:
        assert len(backend.calls) == 1
        assert trace.fallback_reason is None, trace
        assert trace.engine == "jev" and action.engine == "jev"
    assert acceptable(action, spec["acceptable"]), (action.rule, action.function, action.entity_name, trace.top)


def test_the_starvation_scenario_really_differs_from_the_ladder():
    spec = load_scenario(SCENARIOS / "band2_starvation.json")
    snapshot, policy = scenario_inputs(spec)
    assert ladder(snapshot, policy).rule == "6:building-queue-empty"
    action, trace = jev_engine.decide(snapshot, policy, backend=FakeBackend(keyword_responder(**spec["responder"])))
    assert action.function == "startResearch"
    assert trace.agrees_with_ladder is False


# --------------------------------------------------------------------------------------
# Live variant: real Jev, only on request.
# --------------------------------------------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("VEYDRIFT_JEV_LIVE_TESTS") != "1",
    reason="set VEYDRIFT_JEV_LIVE_TESTS=1 (and TYPESAFE_API_KEY) to run against the real TypeSafe API",
)


@live
def test_scenarios_against_the_live_backend(capsys):
    fallbacks: list[str] = []
    wrong: list[str] = []
    for path in SCENARIO_FILES:
        spec = load_scenario(path)
        snapshot, policy = scenario_inputs(spec)
        action, trace = jev_engine.decide(snapshot, policy, backend=jev.TypeSafeBackend())
        if trace.fallback_reason is not None:
            fallbacks.append(f"{path.stem}: {trace.fallback_reason}")
        elif not acceptable(action, spec["acceptable"]):
            wrong.append(f"{path.stem}: {action.rule} {action.function} {action.entity_name}")
    with capsys.disabled():
        print(f"\njev live scenarios: {len(SCENARIO_FILES)} run, {len(fallbacks)} fell back {fallbacks}, {len(wrong)} unacceptable {wrong}")
    assert not wrong
    assert len(fallbacks) <= len(SCENARIO_FILES) / 2


def test_two_attacks_from_different_origins_are_different_kinds_and_must_clear_the_margin():
    snapshot, policy = only_high_stakes_setup(min_margin=0.2)
    pool = pool_of(snapshot, policy, **target_kwargs())
    attacks = [i for i, e in enumerate(pool) if e.candidate.family == "attack"]
    origins = {pool[i].candidate.action.planet_id for i in attacks}
    assert len(attacks) == 2 and len(origins) == 2
    assert {pool[i].candidate.action.target_planet_id for i in attacks} == {7001}, "same target, different origin"

    both = scripted(fits={i: 1.0 for i in attacks}, urgs={i: 1.0 for i in attacks}, default_fit=0.0, default_urg=0.0, focus="offense")
    result = run(snapshot, policy, FakeBackend(both), **target_kwargs())
    assert_fell_back(snapshot, policy, result, "low_margin", **target_kwargs())
    assert result[1].margin is not None and result[1].margin < 0.2

    # with one origin clearly ahead the margin is real and the attack is taken
    action, trace = run(snapshot, policy, FakeBackend(endorsing(attacks[0], focus="offense")), **target_kwargs())
    assert trace.engine == "jev" and action.rule == "8e:attack" and trace.margin >= 0.2


def test_the_kind_of_a_launch_carries_mission_origin_and_target_but_an_upgrade_does_not():
    snapshot, policy = only_high_stakes_setup()
    pool = pool_of(snapshot, policy, **target_kwargs())

    def scored(entry: PoolEntry) -> jev_engine._Scored:
        return jev_engine._Scored(
            id="c", entry=entry, fit=0.5, fit_confidence=0.9, urgency=0.5, urgency_confidence=0.9,
            focus=0.5, economy=0.5, threat=0.0, composite=0.5,
        )

    a, b = (scored(e) for e in pool if e.candidate.family == "attack")
    assert jev_engine._kind(a) != jev_engine._kind(b)
    assert jev_engine._kind(a) == jev_engine._kind(a)
    mines = [scored(e) for e in pool_of(two_planet(), jev_policy()) if e.candidate.family == "mine" and e.candidate.action.entity_id == ids.Building.METAL_MINE]
    assert jev_engine._kind(mines[0]) == jev_engine._kind(mines[1])
