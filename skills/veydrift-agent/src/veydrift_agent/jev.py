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

Tests replace the backend through `default_backend` (the SDK's `httpx2` transport is not
something `respx` intercepts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    raise NotImplementedError


class TypeSafeBackend:
    """`JevBackend` over the `typesafe-sdk` synchronous client."""

    def __init__(self) -> None:
        raise NotImplementedError

    def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, QuestionSpec],
        *,
        model: str,
        timeout_s: float,
    ) -> JevAnswers:
        raise NotImplementedError


def default_backend(cfg: JevCfg) -> JevBackend:
    """The backend the engine uses when none is injected. Tests monkeypatch this."""
    raise NotImplementedError
