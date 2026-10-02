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

import importlib.util
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer
from rich.console import Console

from veydrift_agent import candidates
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
from veydrift_agent.state import IntentOverride

if TYPE_CHECKING:
    from veydrift_agent.jev import JevBackend

app = typer.Typer(no_args_is_help=True, help="Inspect and compare decision engines (ladder vs jev).")


@dataclass(frozen=True)
class EffectiveIntent:
    """The strategy text the jev engine judges against this tick, and where it came from.

    `source` is `"agent"` only for a live override; `override` is the stored override when one
    was found (used or not). `note` says why a stored override was not used. `expired` tells the
    tick to remove the file and log the expiry once."""

    text: str
    source: Literal["policy", "default", "agent"]
    override: IntentOverride | None = None
    note: str | None = None
    expired: bool = False


@dataclass(frozen=True)
class EngineContext:
    """Tick-only inputs an engine may use as context. The ladder ignores them."""

    radar_report: RadarReport | None = None
    alliance_state: AllianceState | None = None
    #: The resolved intent (`resolve_effective_intent`). `None` uses `policy.engine.jev.intent`.
    intent: EffectiveIntent | None = None


def resolve_effective_intent(policy: Policy, *, now: datetime) -> EffectiveIntent:
    """Which intent this tick judges against: a live agent override (only while
    `policy.engine.jev.adaptive_intent` is true, unexpired, and free of identifying text), else
    the policy intent, else the built-in default. Never raises: an unreadable override file is
    reported in `note` and ignored."""
    from veydrift_agent.jev_engine import DEFAULT_INTENT
    from veydrift_agent.models import intent_text_problems
    from veydrift_agent.state import IntentOverrideError, load_intent_override

    cfg = policy.engine.jev
    policy_text = cfg.intent.strip()
    fallback_text = policy_text or DEFAULT_INTENT
    fallback_source: Literal["policy", "default"] = "policy" if policy_text else "default"

    def standing(note: str | None = None, override: IntentOverride | None = None, expired: bool = False) -> EffectiveIntent:
        return EffectiveIntent(fallback_text, fallback_source, override=override, note=note, expired=expired)

    if policy.engine.kind != "jev":
        return standing()
    try:
        override = load_intent_override()
    except IntentOverrideError:
        return standing("override file unreadable")
    if override is None:
        return standing()
    if not cfg.adaptive_intent:
        return standing("override ignored: adaptive_intent is off", override)
    if now >= override.expires_at:
        return standing("override expired", override, expired=True)
    problems = intent_text_problems(override.intent, wallet=policy.wallet, signer=policy.signer, planet_ids=policy.planets)
    if problems:
        return standing("override rejected: " + "; ".join(problems), override)
    text = override.intent.strip()
    if not text:
        return standing("override rejected: empty", override)
    return EffectiveIntent(text, "agent", override=override)

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
    kind = engine_override or policy.engine.kind

    def _ladder() -> Action:
        # Through the module attribute, never a bound import: tests monkeypatch it.
        return plan_mod.plan_next_action(
            snapshot,
            policy,
            killswitch_active=killswitch_active,
            pending_tx_unreconciled=pending_tx_unreconciled,
            resolvable_mission_ids=resolvable_mission_ids,
            own_planet_debris=own_planet_debris,
            foreign_debris_targets=foreign_debris_targets,
            colonize_targets=colonize_targets,
            attack_targets=attack_targets,
            missile_targets=missile_targets,
            last_attended_planet_id=last_attended_planet_id,
        )

    if kind != "jev":
        return _ladder(), EngineTrace(engine="ladder", configured="ladder")

    if killswitch_active:
        # The killswitch halts before any network call: no jev import, no backend.
        halt = _ladder()
        return halt, EngineTrace(engine="ladder", configured="jev", pre_empted_by=halt.rule)

    try:
        from veydrift_agent import jev_engine

        return jev_engine.decide(
            snapshot,
            policy,
            pending_tx_unreconciled=pending_tx_unreconciled,
            resolvable_mission_ids=resolvable_mission_ids,
            own_planet_debris=own_planet_debris,
            foreign_debris_targets=foreign_debris_targets,
            colonize_targets=colonize_targets,
            attack_targets=attack_targets,
            missile_targets=missile_targets,
            last_attended_planet_id=last_attended_planet_id,
            context=context,
            backend=backend,
        )
    except Exception as exc:  # noqa: BLE001 -- the engine is never the reason a tick fails
        return _ladder(), EngineTrace(
            engine="ladder", configured="jev", fallback_reason=f"engine_error:{type(exc).__name__}"
        )


def describe_trace(trace: EngineTrace) -> str:
    """One line describing what a *configured-jev* engine did, for the tick report and
    `vd plan run`: `jev (jev-1.13.0, 140ms, confidence 0.71, agrees with ladder)`,
    `jev -> ladder fallback (timeout)` or `jev pre-empted by 1b:game-paused`."""
    if trace.pre_empted_by:
        return f"jev pre-empted by {trace.pre_empted_by}"
    if trace.engine != "jev":
        return f"jev -> ladder fallback ({trace.fallback_reason or 'unknown'})"
    parts: list[str] = []
    if trace.model:
        parts.append(trace.model)
    if trace.latency_ms is not None:
        parts.append(f"{trace.latency_ms}ms")
    if trace.winner_confidence is not None:
        parts.append(f"confidence {trace.winner_confidence:.2f}")
    if trace.agrees_with_ladder is not None:
        parts.append("agrees with ladder" if trace.agrees_with_ladder else "differs from ladder")
    return f"jev ({', '.join(parts)})" if parts else "jev"


def doctor_lines() -> list[str]:
    """Lines `vd doctor` prints about the decision engine: the configured kind (from
    `$VEYDRIFT_HOME/policy.json` if present), whether `TYPESAFE_API_KEY` is set (never its
    value), and whether `typesafe_sdk` is importable. Read-only: never creates a file and
    never imports the SDK."""
    from veydrift_agent.jev import API_KEY_ENV
    from veydrift_agent.state import veydrift_home

    path = Path(veydrift_home()) / "policy.json"
    if not path.exists():
        kind = "ladder (no policy.json)"
    else:
        try:
            kind = Policy.model_validate(json.loads(path.read_text())).engine.kind
        except (OSError, ValueError):
            kind = "unknown (policy.json invalid)"
    key = "set" if os.environ.get(API_KEY_ENV, "").strip() else "unset"
    try:
        sdk = "importable" if importlib.util.find_spec("typesafe_sdk") is not None else "missing"
    except (ImportError, ValueError):
        sdk = "missing"
    return [f"engine: {kind}", f"{API_KEY_ENV}: {key}", f"typesafe-sdk: {sdk}"]


# --------------------------------------------------------------------------------------
# CLI -- `pool` is offline (no network, no key); `compare` runs the jev engine, so a real
# jev answer needs the key and the network (without them jev falls back to the ladder).
# --------------------------------------------------------------------------------------


def _load(snapshot: Path, policy: Path, console: Console) -> tuple[Snapshot, Policy]:
    try:
        return (
            Snapshot.model_validate(json.loads(snapshot.read_text())),
            Policy.model_validate(json.loads(policy.read_text())),
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]failed to load snapshot/policy: {exc}[/red]")
        raise typer.Exit(code=4) from exc


def _same_pick(a: Action, b: Action) -> bool:
    """Do two actions name the same call? Off-chain actions compare by kind and rule."""
    if a.is_onchain() and b.is_onchain():
        return candidates.pool_key(a) == candidates.pool_key(b)
    return a.kind == b.kind and a.rule == b.rule


def _describe(action: Action) -> str:
    if action.function:
        return f"{action.rule}  {action.function}(planet={action.planet_id}, entity={action.entity_id})"
    return f"{action.rule}  {action.kind.value}"


@app.command()
def pool(
    snapshot: Path = typer.Option(..., "--snapshot", help="Path to a Snapshot JSON file."),  # noqa: B008
    policy: Path = typer.Option(..., "--policy", help="Path to a Policy JSON file."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show the candidate pool the jev engine would choose from, and the exact request it would
    send. Offline: no network, no API key."""
    console = Console()
    snapshot_model, policy_model = _load(snapshot, policy, console)
    jev_cfg = policy_model.engine.jev
    entries, rejected = candidates.collect_pool(
        snapshot_model,
        policy_model,
        plan_mod._target_planets(snapshot_model, policy_model),
        high_stakes_only_when_idle=jev_cfg.high_stakes_only_when_idle,
        max_candidates=jev_cfg.max_candidates,
        proactive_storage_hours=jev_cfg.proactive_storage_hours,
    )
    rows = [
        {
            "id": f"c{i}",
            "band": e.band,
            "group": e.group,
            "family": e.candidate.family,
            "planet_id": e.candidate.action.planet_id,
            "entity": e.candidate.action.entity_name or e.candidate.action.function,
            "score_basis": e.candidate.score_basis,
        }
        for i, e in enumerate(entries)
    ]

    from veydrift_agent import jev, jev_engine

    state, questions = jev_engine.build_request(snapshot_model, policy_model, entries, None)
    request: dict[str, Any] = {
        "state": state,
        "questions": {qid: asdict(q) for qid, q in questions.items()},
        "estimated_tokens": jev.estimate_tokens(state, questions),
    }

    if json_output:
        typer.echo(json.dumps({"pool": rows, "rejected": rejected, "request": request}, indent=2, default=str))
        return

    typer.echo(f"pool: {len(rows)} candidate(s)")
    for row in rows:
        typer.echo(
            f"  {row['id']:>4}  band {row['band']}  {row['group']:<13} {row['family']:<15} "
            f"planet={row['planet_id']}  {row['entity']}  -- {row['score_basis']}"
        )
    typer.echo("rejected: " + (", ".join(f"{k}={v}" for k, v in sorted(rejected.items())) or "none"))
    typer.echo(f"estimated request size: ~{request['estimated_tokens']} tokens")
    typer.echo("state:")
    typer.echo(json.dumps(request["state"], indent=2, default=str))
    typer.echo("questions:")
    typer.echo(json.dumps(request["questions"], indent=2, default=str))


@app.command()
def compare(
    snapshot: Path = typer.Option(..., "--snapshot", help="Path to a Snapshot JSON file."),  # noqa: B008
    policy: Path = typer.Option(..., "--policy", help="Path to a Policy JSON file."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Run the jev engine and the ladder on the same input and compare their picks. Needs
    TYPESAFE_API_KEY and the network for a real jev answer; without them jev falls back to the
    ladder. Exit codes: 0 agree, 1 disagree, 3 jev fell back to the ladder, 4 load error."""
    console = Console()
    snapshot_model, policy_model = _load(snapshot, policy, console)
    ladder_action = plan_mod.plan_next_action(snapshot_model, policy_model)
    jev_action, trace = decide(snapshot_model, policy_model, engine_override="jev")

    fell_back = trace.fallback_reason is not None
    agree = _same_pick(ladder_action, jev_action)
    code = 3 if fell_back else (0 if agree else 1)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "ladder": json.loads(ladder_action.model_dump_json()),
                    "jev": json.loads(jev_action.model_dump_json()),
                    "trace": trace.model_dump(mode="json"),
                    "agree": agree,
                    "fallback": fell_back,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=code)

    typer.echo(f"ladder: {_describe(ladder_action)}")
    typer.echo(f"jev:    {_describe(jev_action)}  (engine={jev_action.engine})")
    verdict = "FALLBACK" if fell_back else ("agree" if agree else "DISAGREE")
    typer.echo(f"result: {verdict}")
    if trace.fallback_reason:
        typer.echo(f"fallback_reason: {trace.fallback_reason}")
    if trace.pre_empted_by:
        typer.echo(f"pre_empted_by: {trace.pre_empted_by}")
    if trace.winner_confidence is not None:
        typer.echo(f"confidence: {trace.winner_confidence:.2f}")
    if trace.margin is not None:
        typer.echo(f"margin: {trace.margin:.3f}")
    for j in trace.top:
        composite = f"{j.composite:.3f}" if j.composite is not None else "n/a"
        typer.echo(f"  {j.id:>4} {j.family:<15} {j.entity_name or '-'}  composite={composite}")
    raise typer.Exit(code=code)
