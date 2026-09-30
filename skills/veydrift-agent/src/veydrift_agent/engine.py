"""`engine.py` — picks which decision engine decides a tick's action, and hosts `vd engine`.

`decide` is the single entry point `tick.py` calls in place of `plan.plan_next_action`:

- `policy.engine.kind == "ladder"` (default): returns `plan_mod.plan_next_action(...)`
  unchanged, called through the module attribute so a test that monkeypatches
  `plan.plan_next_action` still controls it.
- `"jev"`: `jev_engine.decide(...)`. Any exception escaping it becomes a ladder decision
  with `fallback_reason="engine_error:<ExceptionClass>"`. The engine is never the reason a
  tick fails.

`plan_next_action` itself stays pure and offline; the only network call an engine makes
(TypeSafe, via `jev.py`) happens under this module, never inside `plan.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import typer

from veydrift_agent import plan as plan_mod
from veydrift_agent.models import (
    Action,
    AllianceState,
    EngineTrace,
    Policy,
    RadarReport,
    Resources,
    Snapshot,
)

if TYPE_CHECKING:
    from veydrift_agent.jev import JevBackend

app = typer.Typer(no_args_is_help=True, help="Inspect and compare decision engines (ladder vs jev).")


@dataclass(frozen=True)
class EngineContext:
    """Tick-only inputs an engine may use as context. The ladder ignores them."""

    radar_report: RadarReport | None = None
    alliance_state: AllianceState | None = None


def decide(
    snapshot: Snapshot,
    policy: Policy,
    *,
    killswitch_active: bool = False,
    pending_tx_unreconciled: bool = False,
    resolvable_mission_ids: list[int] | None = None,
    own_planet_debris: dict[int, Resources] | None = None,
    foreign_debris_targets: dict[int, tuple[str, Resources]] | None = None,
    colonize_targets: list[tuple[str, int]] | None = None,
    attack_targets: dict[int, tuple[str, Resources, bool | None]] | None = None,
    missile_targets: dict[int, tuple[str, dict[int, int], bool | None]] | None = None,
    last_attended_planet_id: int | None = None,
    context: EngineContext | None = None,
    engine_override: Literal["ladder", "jev"] | None = None,
    backend: JevBackend | None = None,
) -> tuple[Action, EngineTrace]:
    """Decide this tick's single `Action` with the configured engine (or `engine_override`).
    Keyword arguments mirror `plan.plan_next_action` exactly."""
    raise NotImplementedError


def doctor_lines() -> list[str]:
    """Lines `vd doctor` prints about the decision engine: the configured kind (from
    `$VEYDRIFT_HOME/policy.json` if present), whether `TYPESAFE_API_KEY` is set (never its
    value), and whether `typesafe_sdk` imports."""
    return []
