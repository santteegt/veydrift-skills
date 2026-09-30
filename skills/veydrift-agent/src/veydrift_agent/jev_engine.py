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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veydrift_agent.models import Action, EngineTrace, Policy, Resources, Snapshot

if TYPE_CHECKING:
    from veydrift_agent.candidates import PoolEntry
    from veydrift_agent.engine import EngineContext
    from veydrift_agent.jev import JevBackend, QuestionSpec

#: Off-chain rule for a confident "hold" judgment (`policy.engine.jev.allow_hold`).
HOLD_RULE = "9j:hold"


def build_request(
    snapshot: Snapshot,
    policy: Policy,
    pool: list[PoolEntry],
    context: EngineContext | None = None,
) -> tuple[dict[str, Any], dict[str, QuestionSpec]]:
    """The exact `(state, questions)` the engine sends for `pool`. Pure, no network --
    `vd engine pool` prints it. Candidate ids are `c0..c{n-1}` in pool order; questions are
    `tick_focus`, `threat`, `fit_c<i>`, `urgency_c<i>`."""
    raise NotImplementedError


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
    raise NotImplementedError
