"""Test doubles for `veydrift_agent.jev` -- a fake `JevBackend` and answer builders.

Used by `test_jev.py` and by the engine/scenario tests, which inject a `FakeBackend` through
`default_backend` (or directly). Nothing here touches the network or the SDK.

    backend = FakeBackend()                                    # answers everything neutrally
    backend = FakeBackend(keyword_responder({"solar": 0.9}))   # rule-based fit scores
    backend = FakeBackend(error=JevError("timeout"))           # every call raises
    backend.calls                                              # [(state, questions, model, timeout_s)]
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from veydrift_agent.jev import ChoiceAnswer, JevAnswers, JevError, QuestionSpec, ScoreAnswer

Responder = Callable[[dict[str, Any], dict[str, QuestionSpec]], JevAnswers]


def _choice_answer(spec: QuestionSpec, confidence: float, focus: str | None = None) -> ChoiceAnswer:
    """A Choice answer whose winner is `focus` (if it is an option) else the first option.
    The winner gets `confidence`; the remainder is split evenly over the other options."""
    options = list(spec.criteria) if isinstance(spec.criteria, dict) else []
    if not options:
        raise ValueError("a choice question needs criteria")
    winner = focus if focus in options else options[0]
    rest = [o for o in options if o != winner]
    if not rest:
        return ChoiceAnswer(winner, {winner: 1.0}, confidence)
    probs = {winner: confidence, **{o: (1.0 - confidence) / len(rest) for o in rest}}
    return ChoiceAnswer(winner, probs, confidence)


def neutral_answers(
    questions: Mapping[str, QuestionSpec],
    *,
    choice_confidence: float = 0.9,
    score: float = 0.5,
    score_confidence: float = 0.9,
    noul: float = 0.1,
    focus: str | None = None,
) -> JevAnswers:
    """Answer every question the same neutral way.

    Choice -> the first option (or `focus`, for every choice question that offers it) at
    `choice_confidence`; Score -> `score` (already normalised, 0..1) at `score_confidence`;
    Noul -> `noul` (probability of yes)."""
    choices: dict[str, ChoiceAnswer] = {}
    scores: dict[str, ScoreAnswer] = {}
    nouls: dict[str, float] = {}
    for qid, spec in questions.items():
        if spec.kind == "choice":
            choices[qid] = _choice_answer(spec, choice_confidence, focus)
        elif spec.kind == "score":
            scores[qid] = ScoreAnswer(score, score_confidence)
        else:
            nouls[qid] = noul
    return JevAnswers(
        choices=choices,
        scores=scores,
        nouls=nouls,
        model="jev-fake",
        request_id="fake-request",
        input_tokens=0,
        latency_ms=1,
    )


def answers_from(
    questions: Mapping[str, QuestionSpec],
    *,
    choices: Mapping[str, str | ChoiceAnswer] | None = None,
    scores: Mapping[str, float | ScoreAnswer] | None = None,
    nouls: Mapping[str, float] | None = None,
    **neutral: Any,
) -> JevAnswers:
    """Neutral answers, with explicit values for the ids given.

    `choices[qid]` is an option name (answered at the neutral choice confidence) or a full
    `ChoiceAnswer`; `scores[qid]` a normalised 0..1 float (neutral confidence) or a full
    `ScoreAnswer`; `nouls[qid]` a probability. Extra keyword arguments go to `neutral_answers`.
    Raises `ValueError` if an explicit id was not asked, was asked as another kind, or a
    choice names an option the question does not offer."""
    base = neutral_answers(questions, **neutral)
    out_c, out_s, out_n = dict(base.choices), dict(base.scores), dict(base.nouls)

    def check(qid: str, kind: str) -> QuestionSpec:
        spec = questions.get(qid)
        if spec is None:
            raise ValueError(f"{qid!r} was not asked")
        if spec.kind != kind:
            raise ValueError(f"{qid!r} is a {spec.kind} question, not a {kind}")
        return spec

    conf = neutral.get("choice_confidence", 0.9)
    for qid, val in (choices or {}).items():
        spec = check(qid, "choice")
        if isinstance(val, ChoiceAnswer):
            out_c[qid] = val
        else:
            if val not in (spec.criteria or {}):
                raise ValueError(f"{val!r} is not an option of {qid!r}")
            out_c[qid] = _choice_answer(spec, conf, val)
    sconf = neutral.get("score_confidence", 0.9)
    for qid, val in (scores or {}).items():
        check(qid, "score")
        out_s[qid] = val if isinstance(val, ScoreAnswer) else ScoreAnswer(float(val), sconf)
    for qid, val in (nouls or {}).items():
        check(qid, "noul")
        out_n[qid] = float(val)
    return JevAnswers(
        choices=out_c,
        scores=out_s,
        nouls=out_n,
        model=base.model,
        request_id=base.request_id,
        input_tokens=base.input_tokens,
        latency_ms=base.latency_ms,
    )


def keyword_responder(
    fit_keywords: Mapping[str, float],
    *,
    default_fit: float = 0.3,
    urgency_by_group: Mapping[str, float] | None = None,
    default_urgency: float = 0.5,
    threat: float = 0.0,
    **neutral: Any,
) -> Responder:
    """A small rule-based responder for scenario tests.

    Assumes `state["candidates"]` is a list of dicts with `id`, `group`, `what`, `facts`, and
    questions named `fit_<id>` / `urgency_<id>` (Score), `tick_focus` (Choice) and `threat`
    (Noul). Anything else is answered neutrally (`neutral_answers` kwargs pass through).

    - `fit_<id>`: the highest weight in `fit_keywords` whose keyword occurs (case-insensitive)
      in the candidate's `what` + `group`; `default_fit` when none does.
    - `urgency_<id>`: `urgency_by_group[group]`, else `default_urgency`.
    - `tick_focus`: the group of the best-fitting candidate if that is an option, else the
      first option.
    - `threat`: the given probability.
    """
    weights = {k.lower(): v for k, v in fit_keywords.items()}
    urgency = dict(urgency_by_group or {})

    def respond(state: dict[str, Any], questions: dict[str, QuestionSpec]) -> JevAnswers:
        fits: dict[str, float] = {}
        urgencies: dict[str, float] = {}
        groups: dict[str, str] = {}
        for cand in state.get("candidates", []):
            cid, group = str(cand["id"]), str(cand.get("group", ""))
            text = f"{cand.get('what', '')} {group}".lower()
            hits = [w for kw, w in weights.items() if kw in text]
            fits[cid] = max(hits) if hits else default_fit
            urgencies[cid] = urgency.get(group, default_urgency)
            groups[cid] = group
        explicit_scores: dict[str, float] = {}
        for cid, fit in fits.items():
            if f"fit_{cid}" in questions:
                explicit_scores[f"fit_{cid}"] = fit
            if f"urgency_{cid}" in questions:
                explicit_scores[f"urgency_{cid}"] = urgencies[cid]
        explicit_choices: dict[str, str] = {}
        if "tick_focus" in questions and fits:
            best = max(fits, key=lambda c: fits[c])
            options = questions["tick_focus"].criteria or {}
            if groups[best] in options:
                explicit_choices["tick_focus"] = groups[best]
        explicit_nouls = {"threat": threat} if "threat" in questions else {}
        return answers_from(
            questions,
            choices=explicit_choices,
            scores=explicit_scores,
            nouls=explicit_nouls,
            **neutral,
        )

    return respond


class FakeBackend:
    """A `JevBackend` that never touches the network.

    `responder(state, questions) -> JevAnswers` decides each call's answers (default: every
    question answered neutrally). `error` makes every call raise that `JevError` instead. Every
    call is recorded, even a failing one, in `calls` as `(state, questions, model, timeout_s)`."""

    def __init__(self, responder: Responder | None = None, *, error: JevError | None = None):
        if responder is not None and error is not None:
            raise ValueError("pass a responder or an error, not both")
        self.responder: Responder = responder or (lambda _s, q: neutral_answers(q))
        self.error = error
        self.calls: list[tuple[dict[str, Any], dict[str, QuestionSpec], str, float]] = []

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, QuestionSpec],
        *,
        model: str,
        timeout_s: float,
    ) -> JevAnswers:
        self.calls.append((state, questions, model, timeout_s))
        if self.error is not None:
            raise self.error
        return self.responder(state, questions)
