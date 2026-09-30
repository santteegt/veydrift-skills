"""Tests for `veydrift_agent.jev` and `tests/jev_fakes.py`.

The real `typesafe-sdk` runs end to end here: `typesafe_sdk.TypeSafeClient` is wrapped so every
client the backend builds gets an `httpx2.MockTransport`, which exercises the SDK's request
encoding, response parsing and status-to-exception mapping. No test reaches the network, except
the opt-in live test (`VEYDRIFT_JEV_LIVE_TESTS=1`, which also needs a real `TYPESAFE_API_KEY`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
import typesafe_sdk
from jev_fakes import FakeBackend, answers_from, keyword_responder, neutral_answers

from veydrift_agent import jev
from veydrift_agent.jev import (
    BASE_URL,
    MAX_ESTIMATED_TOKENS,
    ChoiceAnswer,
    JevAnswers,
    JevError,
    QuestionSpec,
    ScoreAnswer,
    TypeSafeBackend,
    estimate_tokens,
)
from veydrift_agent.models import JevCfg

KEY = "test-key-0123456789abcdef"
SENTINEL = "SENTINEL-STATE-VALUE-7f3a"
REQUEST_ID = "req_abc123"

QUESTIONS: dict[str, QuestionSpec] = {
    "fit_a": QuestionSpec("score", "How well does A fit?", ["poor", "ok", "good", "great"]),
    "urgency_a": QuestionSpec("score", "How urgent?", ["l0", "l1", "l2", "l3", "l4"]),
    "tick_focus": QuestionSpec(
        "choice", "Which group?", {"economy": "grow", "defense": "protect", "science": None}
    ),
    "threat": QuestionSpec("noul", "Is a threat imminent?", {"true": "yes", "false": "no"}),
}


def _payload(
    *,
    fit: float = 2.4,
    urgency: float = 3.0,
    choice: str = "economy",
    noul: float = 0.25,
) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "usage": {"input_tokens": 321, "output_tokens": 7},
        "answers": {
            "fit_a": {
                "type": "score",
                "score": fit,
                "confidence": 0.8,
                "legend": {"0": "poor", "1": "ok", "2": "good", "3": "great"},
                "probabilities": {"0": 0.1, "1": 0.1, "2": 0.5, "3": 0.3},
            },
            "urgency_a": {
                "type": "score",
                "score": urgency,
                "confidence": 0.7,
                "legend": {"0": "l0", "1": "l1", "2": "l2", "3": "l3", "4": "l4"},
                "probabilities": {"0": 0.0, "1": 0.1, "2": 0.1, "3": 0.7, "4": 0.1},
            },
            "tick_focus": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.6,
                "probabilities": {"economy": 0.6, "defense": 0.3, "science": 0.1},
            },
            "threat": {"type": "noul", "noul": noul},
        },
    }


class Wire:
    """Installs a `MockTransport` under every `TypeSafeClient` the backend creates."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []
        self.clients: list[dict[str, Any]] = []
        self._handler = handler

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def calls(self) -> int:
        return len(self.requests)


def _ok(payload: dict[str, Any] | None = None) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _r: httpx2.Response(
        200, json=payload or _payload(), headers={"x-typesafe-request-id": REQUEST_ID}
    )


def _status(code: int) -> Callable[[httpx2.Request], httpx2.Response]:
    # The body echoes the state sentinel on purpose: nothing of it may reach a JevError.
    return lambda _r: httpx2.Response(
        code,
        json={"error": f"boom {SENTINEL}"},
        headers={"x-typesafe-request-id": REQUEST_ID},
    )


@pytest.fixture
def wire(monkeypatch):
    """`wire(handler)` -> a `Wire` recording every request; the SDK's env base-URL override
    is set to a hostile value, to prove the backend ignores it."""
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://evil.example.invalid")
    real = typesafe_sdk.TypeSafeClient

    def install(handler: Callable[[httpx2.Request], httpx2.Response]) -> Wire:
        w = Wire(handler)

        class Client(real):  # type: ignore[misc, valid-type]
            def __init__(self, **kw: Any) -> None:
                w.clients.append(dict(kw))
                super().__init__(transport=httpx2.MockTransport(w.handle), **kw)

        monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", Client)
        return w

    return install


def _ask(**over: Any) -> JevAnswers:
    kw: dict[str, Any] = {"model": "jev-latest", "timeout_s": 5.0}
    kw.update(over)
    state = kw.pop("state", {"candidates": [{"id": "a"}]})
    questions = kw.pop("questions", QUESTIONS)
    return TypeSafeBackend().ask(state, questions, **kw)


# --- success path through the real SDK ---------------------------------------------------


def test_success_parses_mixed_choice_score_noul(wire):
    w = wire(_ok())
    out = _ask()
    assert w.calls == 1
    assert out.model == "jev-1.13.0"
    assert out.request_id == REQUEST_ID
    assert out.input_tokens == 321
    assert isinstance(out.latency_ms, int) and out.latency_ms >= 0
    # 4-level score, legend 0..3: 2.4 / 3
    assert out.scores["fit_a"] == ScoreAnswer(pytest.approx(0.8), 0.8)
    # 5-level score, legend 0..4: 3.0 / 4
    assert out.scores["urgency_a"].normalized == pytest.approx(0.75)
    assert out.choices["tick_focus"] == ChoiceAnswer(
        "economy", {"economy": 0.6, "defense": 0.3, "science": 0.1}, 0.6
    )
    assert out.nouls == {"threat": 0.25}


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.0, 0.0), (3.0, 1.0), (1.5, 0.5), (3.0000004, 1.0), (-0.0000004, 0.0)],
)
def test_score_normalisation_is_zero_based_and_clamped(wire, score, expected):
    wire(_ok(_payload(fit=score)))
    assert _ask().scores["fit_a"].normalized == pytest.approx(expected)


def test_request_goes_to_the_constant_base_url_despite_env_override(wire):
    w = wire(_ok())
    _ask()
    request = w.requests[0]
    assert str(request.url).startswith(BASE_URL + "/")
    assert request.url.host == "api.typesafe.ai"
    assert request.method == "POST"
    assert request.url.path == "/v1/systemone"
    assert w.clients[0]["base_url"] == BASE_URL


def test_request_body_carries_model_state_and_questions(wire):
    w = wire(_ok())
    _ask(model="jev-test-model", state={"candidates": [{"id": "a"}], "note": "hello"})
    body = json.loads(w.requests[0].content)
    assert body["model"] == "jev-test-model"
    assert body["state"] == {"candidates": [{"id": "a"}], "note": "hello"}
    assert set(body["questions"]) == set(QUESTIONS)
    assert body["questions"]["fit_a"] == {
        "type": "score",
        "instructions": "How well does A fit?",
        "criteria": ["poor", "ok", "good", "great"],
    }
    assert body["questions"]["tick_focus"]["criteria"] == {
        "economy": "grow",
        "defense": "protect",
        "science": None,
    }
    assert body["questions"]["threat"]["criteria"] == {"true": "yes", "false": "no"}
    assert w.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_noul_without_criteria_omits_them(wire):
    w = wire(
        _ok(
            {
                "model": "m",
                "usage": {},
                "answers": {"q": {"type": "noul", "noul": 0.5}},
            }
        )
    )
    out = _ask(questions={"q": QuestionSpec("noul", "Is it?")})
    assert out.nouls == {"q": 0.5}
    assert "criteria" not in json.loads(w.requests[0].content)["questions"]["q"]
    assert out.input_tokens is None


def test_client_is_configured_with_one_retry_and_the_timeout_budget(wire):
    w = wire(_ok())
    _ask(timeout_s=3.5)
    kw = w.clients[0]
    assert kw["retry"].max_retries == 1
    assert kw["retry"].timeout == 3.5
    # Each attempt gets half the budget per network phase, so a failed attempt plus its
    # retry stays near the budget; it is an `httpx2.Timeout`, not a bare float.
    timeout = kw["timeout"]
    assert isinstance(timeout, httpx2.Timeout)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (1.75,) * 4
    assert kw["model"] == "jev-latest"


# --- error mapping ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "calls"),
    [
        (400, "bad_request", 1),
        (401, "auth", 1),
        (403, "auth", 1),
        (404, "bad_request", 1),
        (422, "bad_request", 1),
        (429, "rate_limited", 2),
        (500, "server", 2),
        (503, "server", 2),
    ],
)
def test_http_status_maps_to_reason_and_retries_at_most_once(wire, status, reason, calls):
    w = wire(_status(status))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == reason
    assert w.calls == calls  # 1 initial attempt, plus at most one retry
    assert f"status={status}" in exc.value.detail
    assert f"request_id={REQUEST_ID}" in exc.value.detail


def test_an_unlisted_status_is_a_server_error(wire):
    wire(_status(418))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "server"


def test_transport_timeout_maps_to_timeout(wire):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    w = wire(handler)
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "timeout"
    assert w.calls == 2


def test_transport_connection_error_maps_to_connection(wire):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"no route {SENTINEL}", request=request)

    w = wire(handler)
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "connection"
    assert w.calls == 2
    assert SENTINEL not in str(exc.value)


def test_a_retry_that_succeeds_returns_the_answers(wire):
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _status(503)(request)
        return _ok()(request)

    wire(handler)
    assert _ask().nouls == {"threat": 0.25}
    assert calls["n"] == 2


def test_unparseable_success_body_is_malformed(wire):
    wire(lambda _r: httpx2.Response(200, json={"nope": True}))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "malformed"


@pytest.mark.parametrize("dropped", ["fit_a", "urgency_a", "tick_focus", "threat"])
def test_an_asked_question_missing_from_the_answers_is_malformed(wire, dropped):
    payload = _payload()
    del payload["answers"][dropped]
    wire(_ok(payload))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "malformed"
    assert exc.value.detail == dropped


def test_a_wrong_typed_answer_is_malformed(wire):
    payload = _payload()
    payload["answers"]["threat"] = {
        "type": "score",
        "score": 1.0,
        "confidence": 0.5,
        "legend": {"0": "a", "1": "b"},
        "probabilities": {"0": 0.5, "1": 0.5},
    }
    wire(_ok(payload))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "malformed"
    assert exc.value.detail == "threat"


def test_a_choice_outside_the_offered_options_is_malformed(wire):
    wire(_ok(_payload(choice="conquest")))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "malformed"
    assert exc.value.detail == "tick_focus"


def _mutated(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    payload = _payload()
    mutate(payload["answers"])
    return payload


def _set(qid: str, **fields: Any) -> Callable[[dict[str, Any]], None]:
    return lambda answers: answers[qid].update(fields)


@pytest.mark.parametrize(
    ("qid", "mutate"),
    [
        ("fit_a", _set("fit_a", legend={})),
        ("fit_a", _set("fit_a", legend={"0": "poor", "1": "ok"})),  # 4 levels asked, 2 returned
        ("fit_a", _set("fit_a", score=3.4)),
        ("fit_a", _set("fit_a", score=-0.2)),
        ("fit_a", _set("fit_a", confidence=5.0)),
        ("fit_a", _set("fit_a", confidence=-0.1)),
        ("tick_focus", _set("tick_focus", confidence=1.5)),
        ("tick_focus", _set("tick_focus", probabilities={"economy": 7.0, "defense": -3.0})),
        ("tick_focus", _set("tick_focus", probabilities={"economy": 0.5, "defense": 0.2})),
        ("tick_focus", _set("tick_focus", probabilities={"economy": 0.9, "defense": 0.9})),
        ("tick_focus", _set("tick_focus", probabilities={"economy": 0.6, "conquest": 0.4})),
        ("tick_focus", _set("tick_focus", probabilities={})),
        ("threat", _set("threat", noul=1.5)),
        ("threat", _set("threat", noul=-0.5)),
    ],
)
def test_out_of_range_answer_values_are_malformed(wire, qid, mutate):
    wire(_ok(_mutated(mutate)))
    with pytest.raises(JevError) as exc:
        _ask()
    assert exc.value.reason == "malformed"
    assert exc.value.detail == qid


def test_probabilities_summing_to_one_within_tolerance_are_accepted(wire):
    payload = _mutated(
        _set("tick_focus", probabilities={"economy": 0.62, "defense": 0.3, "science": 0.1})
    )
    wire(_ok(payload))
    assert _ask().choices["tick_focus"].choice == "economy"


def test_a_probability_subset_of_the_options_is_accepted(wire):
    payload = _mutated(_set("tick_focus", probabilities={"economy": 0.7, "defense": 0.3}))
    wire(_ok(payload))
    assert set(_ask().choices["tick_focus"].probabilities) == {"economy", "defense"}


def test_an_unrequested_extra_answer_is_ignored(wire):
    payload = _payload()
    payload["answers"]["extra"] = {"type": "noul", "noul": 0.9}
    wire(_ok(payload))
    assert "extra" not in _ask().nouls


def test_errors_never_carry_the_key_or_state(wire):
    for handler in (
        _status(400),
        _status(500),
        lambda r: (_ for _ in ()).throw(httpx2.ConnectError(f"x {SENTINEL} {KEY}", request=r)),
    ):
        wire(handler)
        with pytest.raises(JevError) as exc:
            _ask(state={"candidates": [{"id": "a", "what": SENTINEL}]})
        err = exc.value
        rendered = "\n".join([str(err), repr(err), err.detail, *traceback.format_exception(err)])
        assert SENTINEL not in rendered
        assert KEY not in rendered


def test_backend_repr_hides_the_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    b = TypeSafeBackend()
    assert KEY not in repr(b)
    assert KEY not in str(b)


# --- construction and pre-flight ---------------------------------------------------------


def test_missing_key_is_missing_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError) as exc:
        TypeSafeBackend()
    assert exc.value.reason == "missing_key"


def test_blank_key_is_missing_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    with pytest.raises(JevError) as exc:
        TypeSafeBackend()
    assert exc.value.reason == "missing_key"


def test_sdk_missing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    with pytest.raises(JevError) as exc:
        TypeSafeBackend()
    assert exc.value.reason == "sdk_missing"


def test_default_backend_builds_a_typesafe_backend_or_raises_jev_error(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    assert isinstance(jev.default_backend(JevCfg()), TypeSafeBackend)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(JevError) as exc:
        jev.default_backend(JevCfg())
    assert exc.value.reason == "missing_key"


def test_importing_jev_does_not_import_the_sdk():
    code = "import sys, veydrift_agent.jev; assert 'typesafe_sdk' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_too_large_request_is_refused_before_any_http_call(wire):
    w = wire(_ok())
    big_state = {"blob": "x" * (MAX_ESTIMATED_TOKENS * 4 + 400)}
    with pytest.raises(JevError) as exc:
        _ask(state=big_state)
    assert exc.value.reason == "request_too_large"
    assert exc.value.detail.startswith("~")
    assert w.calls == 0
    assert w.clients == []


def test_estimate_tokens_is_compact_json_chars_over_four():
    state = {"a": [1, 2, 3]}
    qs = {"q": QuestionSpec("noul", "Is it?")}
    payload = {
        "state": state,
        "questions": {"q": {"kind": "noul", "instructions": "Is it?", "criteria": None}},
    }
    assert estimate_tokens(state, qs) == len(json.dumps(payload, separators=(",", ":"))) // 4


def test_unserialisable_state_is_bad_request_without_a_call(wire):
    w = wire(_ok())
    with pytest.raises(JevError) as exc:
        _ask(state={"x": object()})
    assert exc.value.reason == "bad_request"
    assert w.calls == 0


@pytest.mark.parametrize(
    "spec",
    [
        QuestionSpec("choice", "q", None),
        QuestionSpec("score", "q", []),
        QuestionSpec("score", "q", {"a": "b"}),
    ],
)
def test_malformed_question_specs_are_bad_request(wire, spec):
    w = wire(_ok())
    with pytest.raises(JevError) as exc:
        _ask(questions={"q": spec})
    assert exc.value.reason == "bad_request"
    assert w.calls == 0


def test_no_questions_and_bad_timeout_are_bad_request(wire):
    wire(_ok())
    with pytest.raises(JevError) as exc:
        _ask(questions={})
    assert exc.value.reason == "bad_request"
    with pytest.raises(JevError) as exc:
        _ask(timeout_s=0)
    assert exc.value.reason == "bad_request"


# --- jev_fakes ---------------------------------------------------------------------------


def test_neutral_answers_cover_every_question_with_the_documented_defaults():
    out = neutral_answers(QUESTIONS)
    assert set(out.scores) == {"fit_a", "urgency_a"}
    assert set(out.choices) == {"tick_focus"}
    assert set(out.nouls) == {"threat"}
    assert out.scores["fit_a"] == ScoreAnswer(0.5, 0.9)
    assert out.nouls["threat"] == 0.1
    c = out.choices["tick_focus"]
    assert c.choice == "economy"  # first option
    assert c.confidence == 0.9
    assert sum(c.probabilities.values()) == pytest.approx(1.0)
    assert max(c.probabilities, key=c.probabilities.get) == "economy"


def test_neutral_answers_focus_and_overrides():
    out = neutral_answers(
        QUESTIONS, focus="defense", choice_confidence=0.7, score=0.2, score_confidence=0.4, noul=0.6
    )
    assert out.choices["tick_focus"].choice == "defense"
    assert out.choices["tick_focus"].probabilities["defense"] == pytest.approx(0.7)
    assert out.scores["urgency_a"] == ScoreAnswer(0.2, 0.4)
    assert out.nouls["threat"] == 0.6
    # a focus that is not an option falls back to the first option
    assert neutral_answers(QUESTIONS, focus="nope").choices["tick_focus"].choice == "economy"


def test_neutral_single_option_choice():
    qs = {"c": QuestionSpec("choice", "q", {"only": None})}
    assert neutral_answers(qs).choices["c"].probabilities == {"only": 1.0}


def test_answers_from_fills_explicit_values_and_neutral_defaults():
    out = answers_from(
        QUESTIONS,
        choices={"tick_focus": "science"},
        scores={"fit_a": 0.9, "urgency_a": ScoreAnswer(0.1, 0.3)},
        nouls={"threat": 0.8},
    )
    assert out.choices["tick_focus"].choice == "science"
    assert out.scores["fit_a"] == ScoreAnswer(0.9, 0.9)
    assert out.scores["urgency_a"] == ScoreAnswer(0.1, 0.3)
    assert out.nouls["threat"] == 0.8
    partial = answers_from(QUESTIONS, scores={"fit_a": 1.0})
    assert partial.scores["urgency_a"] == ScoreAnswer(0.5, 0.9)
    assert partial.nouls["threat"] == 0.1


def test_answers_from_rejects_an_id_that_was_not_asked_or_the_wrong_kind():
    with pytest.raises(ValueError, match="not asked"):
        answers_from(QUESTIONS, scores={"fit_zzz": 0.5})
    with pytest.raises(ValueError, match="not a score"):
        answers_from(QUESTIONS, scores={"threat": 0.5})
    with pytest.raises(ValueError, match="not an option"):
        answers_from(QUESTIONS, choices={"tick_focus": "conquest"})


def _pool_state() -> dict[str, Any]:
    return {
        "candidates": [
            {"id": "c1", "group": "economy", "what": "Upgrade Metal Mine", "facts": {}},
            {"id": "c2", "group": "defense", "what": "Build Rocket Launcher", "facts": {}},
        ]
    }


POOL_QUESTIONS: dict[str, QuestionSpec] = {
    "fit_c1": QuestionSpec("score", "fit", ["a", "b", "c"]),
    "fit_c2": QuestionSpec("score", "fit", ["a", "b", "c"]),
    "urgency_c1": QuestionSpec("score", "urgency", ["a", "b", "c"]),
    "urgency_c2": QuestionSpec("score", "urgency", ["a", "b", "c"]),
    "tick_focus": QuestionSpec("choice", "focus", {"economy": None, "defense": None}),
    "threat": QuestionSpec("noul", "threat"),
}


def test_keyword_responder_scores_fit_by_keyword_and_group():
    respond = keyword_responder(
        {"rocket": 0.9, "metal": 0.6}, urgency_by_group={"defense": 0.8}, threat=0.4
    )
    out = respond(_pool_state(), POOL_QUESTIONS)
    assert out.scores["fit_c1"].normalized == 0.6
    assert out.scores["fit_c2"].normalized == 0.9
    assert out.scores["urgency_c1"].normalized == 0.5  # default urgency
    assert out.scores["urgency_c2"].normalized == 0.8
    assert out.choices["tick_focus"].choice == "defense"  # group of the best fit
    assert out.nouls["threat"] == 0.4


def test_keyword_responder_default_fit_and_partial_questions():
    respond = keyword_responder({"zzz": 1.0}, default_fit=0.2)
    qs = {"fit_c1": POOL_QUESTIONS["fit_c1"], "threat": POOL_QUESTIONS["threat"]}
    out = respond(_pool_state(), qs)
    assert set(out.scores) == {"fit_c1"} and out.scores["fit_c1"].normalized == 0.2
    assert out.nouls["threat"] == 0.0


def test_fake_backend_records_calls_and_uses_the_responder():
    backend = FakeBackend(keyword_responder({"rocket": 0.9}))
    state = _pool_state()
    out = backend.ask(state, POOL_QUESTIONS, model="jev-latest", timeout_s=4.0)
    assert out.scores["fit_c2"].normalized == 0.9
    assert backend.calls == [(state, POOL_QUESTIONS, "jev-latest", 4.0)]


def test_fake_backend_default_is_neutral_and_error_raises_and_still_records():
    ok = FakeBackend()
    assert ok.ask({}, QUESTIONS, model="m", timeout_s=1.0).nouls == {"threat": 0.1}
    bad = FakeBackend(error=JevError("timeout", "x"))
    with pytest.raises(JevError) as exc:
        bad.ask({"s": 1}, QUESTIONS, model="m", timeout_s=2.0)
    assert exc.value.reason == "timeout"
    assert bad.calls == [({"s": 1}, QUESTIONS, "m", 2.0)]
    with pytest.raises(ValueError):
        FakeBackend(lambda _s, q: neutral_answers(q), error=JevError("timeout"))


# --- opt-in live test --------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("VEYDRIFT_JEV_LIVE_TESTS") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
    reason="set VEYDRIFT_JEV_LIVE_TESTS=1 and TYPESAFE_API_KEY to run against the real API",
)
def test_live_tiny_request_round_trip():
    questions = {
        "urgency": QuestionSpec(
            "score",
            "How urgent is this?",
            ["can wait", "this week", "today", "right now"],
        ),
        "kind": QuestionSpec(
            "choice",
            "What kind of task is this?",
            {"economy": "growing income", "defense": "protecting assets", "other": "anything else"},
        ),
    }
    state = {"task": "The reactor is overheating and an alarm is sounding."}
    out = TypeSafeBackend().ask(state, questions, model="jev-latest", timeout_s=15.0)
    assert isinstance(out.scores["urgency"].normalized, float)
    assert 0.0 <= out.scores["urgency"].normalized <= 1.0
    assert 0.0 <= out.scores["urgency"].confidence <= 1.0
    assert out.choices["kind"].choice in {"economy", "defense", "other"}
    assert set(out.choices["kind"].probabilities) == {"economy", "defense", "other"}
    assert out.model and out.latency_ms is not None
    # legend 0-basedness, straight from the SDK: a 4-level Score has keys 0..3
    with typesafe_sdk.TypeSafeClient(base_url=BASE_URL, timeout=15.0) as client:
        raw = client.system_one(
            state,
            {
                "urgency": typesafe_sdk.Score(
                    instructions="How urgent is this?",
                    criteria=["can wait", "this week", "today", "right now"],
                )
            },
            model="jev-latest",
        )
    assert sorted(raw.scores["urgency"].legend) == [0, 1, 2, 3]
