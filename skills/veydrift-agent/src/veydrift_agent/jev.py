"""`jev.py` — the thin client between the `jev` decision engine and TypeSafe's System One
API (model `jev-latest`). Everything the engine needs from TypeSafe goes through
`JevBackend.ask`; nothing else in this package imports `typesafe_sdk`.

- The SDK is imported lazily inside `TypeSafeBackend`, so a ladder-engine user never loads it.
- The API key comes from `TYPESAFE_API_KEY` only. The base URL is `BASE_URL` below and is
  always passed explicitly -- the SDK would otherwise honour an environment override, which
  would let the environment redirect game state to another host.
- Every failure surfaces as `JevError` with a fixed `reason` code. An SDK error's message is
  never kept (it can echo request content); only its class name, HTTP status and request id.
- Score answers are normalised to 0..1 here (`ScoreAnswer.normalized`), so the engine never
  deals with level indices. The API's score legend is 0-based: a 4-level Score spans 0..3.
- Answer *values* are validated, not just their types: a score outside its legend's span, a
  confidence/probability outside [0, 1], an empty or wrong-sized legend, or choice
  probabilities that do not sum to 1 are `JevError("malformed", <qid>)`.
- Time budget (`timeout_s`): each network attempt gets `timeout_s / 2` as an `httpx2.Timeout`
  (applied to each of connect, read, write and pool), and the SDK's retry budget is
  `timeout_s` with at most one retry, so a failed attempt plus its retry is about `timeout_s`
  in the normal case. This is *not* a hard wall-clock deadline: the timeouts are per network
  phase, the SDK only refuses to *start* a retry past the budget, and a peer that trickles
  bytes is bounded per chunk, not overall.

Tests replace the backend through `default_backend` (the SDK's `httpx2` transport is not
something `respx` intercepts).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from veydrift_agent.models import JevCfg

#: TypeSafe API base URL. A constant on purpose -- never read from policy or environment.
BASE_URL = "https://api.typesafe.ai"
#: Environment variable holding the API key (also scrubbed from logs, see `log.py`).
API_KEY_ENV = "TYPESAFE_API_KEY"
#: A request whose estimated size exceeds this is refused before sending (the API caps a
#: request at 64k tokens, and state plus the longest question at 32k).
MAX_ESTIMATED_TOKENS = 48_000

JevErrorReason = Literal[
    "missing_key",
    "sdk_missing",
    "timeout",
    "connection",
    "rate_limited",
    "auth",
    "bad_request",
    "server",
    "malformed",
    "request_too_large",
]


class JevError(Exception):
    """A TypeSafe call that produced no usable answers. `reason` is one of `JevErrorReason`;
    `detail` is safe to log (class name / status / request id only)."""

    def __init__(self, reason: JevErrorReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: JevErrorReason = reason
        self.detail = detail


@dataclass(frozen=True)
class QuestionSpec:
    """One System One question. `criteria`: a dict of option -> description for a Choice,
    an ordered list of level descriptions (low to high) for a Score, an optional
    `{"true": ..., "false": ...}` dict for a Noul."""

    kind: Literal["choice", "score", "noul"]
    instructions: str | dict[str, Any] | list[Any]
    criteria: dict[str, Any] | list[Any] | None = None


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class ScoreAnswer:
    #: Expected level scaled to 0..1 (0 = lowest level, 1 = highest).
    normalized: float
    confidence: float


@dataclass(frozen=True)
class JevAnswers:
    choices: dict[str, ChoiceAnswer] = field(default_factory=dict)
    scores: dict[str, ScoreAnswer] = field(default_factory=dict)
    #: Probability of yes, 0..1.
    nouls: dict[str, float] = field(default_factory=dict)
    model: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    latency_ms: int | None = None


class JevBackend(Protocol):
    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, QuestionSpec],
        *,
        model: str,
        timeout_s: float,
    ) -> JevAnswers:
        """Send one System One request. Raises `JevError` on any failure, including an
        answer missing for any question asked."""
        ...


def estimate_tokens(state: dict[str, Any], questions: dict[str, QuestionSpec]) -> int:
    """Rough request size (JSON characters / 4). Used for the pre-flight size check and
    `vd engine pool`'s report."""
    payload = {"state": state, "questions": {qid: asdict(q) for qid, q in questions.items()}}
    return len(json.dumps(payload, separators=(",", ":"))) // 4


_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,80}")


def _safe_id(value: object) -> str | None:
    """A request id is server-supplied text; keep it only if it looks like an id."""
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _api_detail(exc: BaseException) -> str:
    """Class name, HTTP status and request id -- never `str(exc)`, which can echo state."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        parts.append(f"status={status}")
    headers = getattr(exc, "headers", None)
    if headers is not None:
        request_id = _safe_id(headers.get("x-typesafe-request-id"))
        if request_id:
            parts.append(f"request_id={request_id}")
    return " ".join(parts)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


#: Slack for float noise on a value that should lie in [0, 1] / inside a legend's span.
_EPS = 1e-6
#: How far a choice's probabilities may stray from summing to 1.
_PROB_SUM_TOLERANCE = 0.05


def _unit(value: object) -> bool:
    """A finite number in [0, 1] (within `_EPS`)."""
    return _finite(value) and -_EPS <= float(value) <= 1.0 + _EPS  # type: ignore[arg-type]


def _sdk_question(sdk: Any, spec: QuestionSpec) -> Any:
    crit = spec.criteria
    if spec.kind == "choice":
        if not isinstance(crit, dict) or not crit:
            raise ValueError("choice needs a non-empty criteria dict")
        return sdk.Choice(instructions=spec.instructions, criteria=dict(crit))
    if spec.kind == "score":
        if not isinstance(crit, (list, tuple)) or not crit:
            raise ValueError("score needs a non-empty criteria list")
        return sdk.Score(instructions=spec.instructions, criteria=list(crit))
    if spec.kind == "noul":
        if crit is None:
            return sdk.Noul(instructions=spec.instructions)
        if not isinstance(crit, dict):
            raise ValueError("noul criteria must be a dict")
        kept = {k: crit[k] for k in ("true", "false") if k in crit}
        return sdk.Noul(instructions=spec.instructions, criteria=kept)
    raise ValueError(f"unknown question kind {spec.kind!r}")


class TypeSafeBackend:
    """`JevBackend` over the `typesafe-sdk` synchronous client."""

    def __init__(self) -> None:
        try:
            import typesafe_sdk  # noqa: F401  (lazy: a ladder-engine user never loads it)
        except ImportError:
            raise JevError("sdk_missing", "typesafe-sdk is not installed") from None
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            raise JevError("missing_key", f"{API_KEY_ENV} is not set")
        self._api_key = key

    def __repr__(self) -> str:
        return "TypeSafeBackend()"

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, QuestionSpec],
        *,
        model: str,
        timeout_s: float,
    ) -> JevAnswers:
        try:
            import typesafe_sdk as sdk
        except ImportError:
            raise JevError("sdk_missing", "typesafe-sdk is not installed") from None

        if not questions:
            raise JevError("bad_request", "no questions")
        if not _finite(timeout_s) or timeout_s <= 0:
            raise JevError("bad_request", "timeout_s must be positive")
        try:
            estimated = estimate_tokens(state, questions)
        except (TypeError, ValueError):
            raise JevError("bad_request", "state is not JSON-serialisable") from None
        if estimated > MAX_ESTIMATED_TOKENS:
            raise JevError("request_too_large", f"~{estimated} tokens")
        try:
            sdk_questions = {qid: _sdk_question(sdk, q) for qid, q in questions.items()}
        except Exception as exc:  # noqa: BLE001 -- pydantic/SDK validation; class name only
            raise JevError("bad_request", f"question spec invalid ({type(exc).__name__})") from None

        # At most one retry, and no retry is *started* past the caller's budget; short backoff
        # so the retry fits inside a few-second budget. Each attempt gets half the budget per
        # network phase, so a failed attempt plus its retry stays near `timeout_s` (see the
        # module docstring: per-phase timeouts, not a hard wall-clock deadline).
        import httpx2  # the SDK's own HTTP layer; present whenever the SDK is

        retry = sdk.RetryPolicy(
            max_retries=1, backoff_initial=0.25, backoff_max=1.0, timeout=timeout_s
        )
        started = time.monotonic()
        try:
            client = sdk.TypeSafeClient(
                api_key=self._api_key,
                model=model,
                retry=retry,
                timeout=httpx2.Timeout(timeout_s / 2),
                base_url=BASE_URL,
            )
        except sdk.TypeSafeError:
            raise JevError("auth", "client rejected the API key or timeout") from None
        try:
            with client:
                response = client.system_one(state, sdk_questions, model=model)
        except sdk.TypeSafeAPIResponseValidationError as exc:
            raise JevError("malformed", _api_detail(exc)) from None
        except sdk.TypeSafeAPITimeoutError as exc:
            raise JevError("timeout", _api_detail(exc)) from None
        except sdk.TypeSafeAPIConnectionError as exc:
            raise JevError("connection", _api_detail(exc)) from None
        except sdk.TypeSafeRateLimitError as exc:
            raise JevError("rate_limited", _api_detail(exc)) from None
        except (sdk.TypeSafeAuthenticationError, sdk.TypeSafePermissionDeniedError) as exc:
            raise JevError("auth", _api_detail(exc)) from None
        except (
            sdk.TypeSafeBadRequestError,
            sdk.TypeSafeNotFoundError,
            sdk.TypeSafeUnprocessableEntityError,
        ) as exc:
            raise JevError("bad_request", _api_detail(exc)) from None
        except sdk.TypeSafeAPIError as exc:  # 5xx and any other status
            raise JevError("server", _api_detail(exc)) from None
        except sdk.TypeSafeError as exc:
            raise JevError("server", type(exc).__name__) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        return _parse(response, questions, model, latency_ms)


def _parse(
    response: Any, questions: dict[str, QuestionSpec], model: str, latency_ms: int
) -> JevAnswers:
    """Turn an SDK `SystemOneResponse` into `JevAnswers`; a missing, wrong-typed or non-finite
    answer for any asked question -- or one whose values are out of range -- is
    `JevError("malformed", <qid>)`."""
    choices: dict[str, ChoiceAnswer] = {}
    scores: dict[str, ScoreAnswer] = {}
    nouls: dict[str, float] = {}
    current = ""
    try:
        r_choices, r_scores, r_nouls = response.choices, response.scores, response.nouls
        for qid, spec in questions.items():
            current = qid
            if spec.kind == "choice":
                a = r_choices.get(qid)
                options = spec.criteria if isinstance(spec.criteria, dict) else {}
                if a is None or not _unit(a.confidence) or a.choice not in options:
                    raise ValueError
                probs = {str(k): float(v) for k, v in a.probabilities.items()}
                if (
                    not probs
                    or not all(_unit(v) for v in probs.values())
                    or not set(probs) <= {str(o) for o in options}
                    or abs(sum(probs.values()) - 1.0) > _PROB_SUM_TOLERANCE
                ):
                    raise ValueError
                choices[qid] = ChoiceAnswer(a.choice, probs, float(a.confidence))
            elif spec.kind == "score":
                s = r_scores.get(qid)
                if s is None or not _finite(s.score) or not _unit(s.confidence):
                    raise ValueError
                # The API's legend is 0-based (a 4-level Score spans 0..3); read the span from
                # the legend rather than assuming it, and require it to match the levels asked.
                levels = sorted(int(k) for k in s.legend)
                asked = len(spec.criteria) if isinstance(spec.criteria, (list, tuple)) else 0
                if not levels or len(levels) != asked:
                    raise ValueError
                lo, hi = levels[0], levels[-1]
                if not lo - _EPS <= float(s.score) <= hi + _EPS:
                    raise ValueError
                norm = 0.0 if hi <= lo else (float(s.score) - lo) / (hi - lo)
                scores[qid] = ScoreAnswer(min(1.0, max(0.0, norm)), float(s.confidence))
            else:
                n = r_nouls.get(qid)
                if n is None or not _unit(n.noul):
                    raise ValueError
                nouls[qid] = min(1.0, max(0.0, float(n.noul)))
        current = ""
        try:
            request_id = _safe_id(response.request_id)
        except Exception:  # noqa: BLE001 -- the SDK raises when the header is absent
            request_id = None
        tokens = getattr(getattr(response, "usage", None), "input_tokens", None)
        return JevAnswers(
            choices=choices,
            scores=scores,
            nouls=nouls,
            model=str(getattr(response, "model", None) or model),
            request_id=request_id,
            input_tokens=tokens if isinstance(tokens, int) else None,
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001 -- anything unexpected in a response is malformed
        raise JevError("malformed", current) from None


def default_backend(cfg: JevCfg) -> JevBackend:
    """The backend the engine uses when none is injected. Tests monkeypatch this.
    May raise `JevError` (no key, no SDK), which the engine treats as a fallback."""
    return TypeSafeBackend()
