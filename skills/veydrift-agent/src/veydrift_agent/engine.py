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
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer
from rich.console import Console
from rich.markup import escape

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
    from veydrift_agent.models import ADAPTIVE_INTENT_MAX_HOURS, intent_text_problems
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
    # Order matters: an expired override is cleaned up (and logged) even when the flag is off.
    # Lifetime and version are enforced here, not trusted from the file: it may be hand-edited.
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if now >= override.expires_at:
        return standing("override expired", override, expired=True)
    if not cfg.adaptive_intent:
        return standing("override ignored: adaptive_intent is off", override)
    if override.version != 1:
        return standing("override rejected: unsupported version", override)
    longest = timedelta(hours=ADAPTIVE_INTENT_MAX_HOURS)
    if (
        override.expires_at <= override.set_at
        or override.expires_at - override.set_at > longest
        or override.expires_at - now > longest
        or override.set_at > now + timedelta(minutes=5)
    ):
        return standing("override rejected: lifetime", override)
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
        try:
            from veydrift_agent import jev_engine

            intent_fields = jev_engine.intent_trace_fields(policy, context)
        except Exception:  # noqa: BLE001 -- the import itself may be what failed
            intent_fields = {}
        return _ladder(), EngineTrace(
            engine="ladder",
            configured="jev",
            fallback_reason=f"engine_error:{type(exc).__name__}",
            **intent_fields,
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


# --------------------------------------------------------------------------------------
# `vd engine intent` -- the adaptive-intent override. `set` writes a standing, expiring
# override of `policy.engine.jev.intent` (only while `adaptive_intent` is on); `show` reports
# what a tick would judge against; `clear` removes it. Exit codes: 0 ok, 2 refused, 4 policy
# load error. The override file is only ever deleted by `clear` or by the tick on expiry.
# --------------------------------------------------------------------------------------

#: Hard limits mirrored from `state.IntentOverride` (text 1000, reason 280).
INTENT_TEXT_MAX = 1000
INTENT_REASON_MAX = 280

intent_app = typer.Typer(no_args_is_help=True, help="Set, show or clear the agent's standing intent override.")
app.add_typer(intent_app, name="intent")

_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d*)?|\.\d+)\s*([mhd])?\s*$", re.IGNORECASE)
_TTL_UNIT_HOURS = {"m": 1 / 60, "h": 1.0, "d": 24.0}


def parse_ttl(raw: str) -> timedelta:
    """`<number><unit>` with unit `m`, `h` or `d` (`90m`, `6h`, `1.5h`, `2d`); a bare number
    means hours. Raises `ValueError` when unparsable. Range is not checked here."""
    match = _TTL_RE.match(raw)
    if match is None:
        raise ValueError(f"cannot parse ttl {raw!r}: use <number><m|h|d>, e.g. 90m, 6h, 2d (a bare number is hours)")
    hours = float(match.group(1)) * _TTL_UNIT_HOURS[(match.group(2) or "h").lower()]
    try:
        return timedelta(hours=hours)
    except OverflowError as exc:
        raise ValueError(f"ttl {raw!r} is out of range") from exc


def _load_policy_or_exit(path: Path | None, console: Console) -> Policy:
    """Load `path` (default `$VEYDRIFT_HOME/policy.json`); any failure prints and exits 4."""
    from veydrift_agent import state

    path = path or state.policy_path()
    try:
        return Policy.model_validate(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        console.print(f"[red]failed to load policy {escape(str(path))}: {escape(str(exc))}[/red]")
        raise typer.Exit(code=4) from exc


def _refuse(console: Console, problems: list[str]) -> None:
    console.print("[red]refused: intent override not written[/red]")
    for problem in problems:
        console.print(f"[red]  - {escape(problem)}[/red]")
    raise typer.Exit(code=2)


def _override_json(override: IntentOverride) -> dict[str, Any]:
    return json.loads(override.model_dump_json())


@intent_app.command("set")
def intent_set(
    text: str = typer.Argument(..., help="The new strategy intent: plain prose, no addresses/coordinates/planet ids."),
    reason: str = typer.Option(..., "--reason", help="Why the intent is changing (logged to strategy.md)."),
    ttl: str = typer.Option(
        None, "--ttl", help="Lifetime: <number><m|h|d> (90m, 6h, 2d); bare number = hours. Default from policy."
    ),
    policy: Path = typer.Option(None, "--policy", help="Path to policy.json (default: $VEYDRIFT_HOME/policy.json)."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Print the stored override as JSON."),
) -> None:
    """Set a standing intent override, honoured by every tick until it expires or is cleared.
    Refused (exit 2) unless policy.engine.jev.adaptive_intent is true."""
    from veydrift_agent import log, state
    from veydrift_agent.models import ADAPTIVE_INTENT_MAX_HOURS, intent_text_problems

    console = Console()
    policy_model = _load_policy_or_exit(policy, console)
    cfg = policy_model.engine.jev
    text, reason = text.strip(), reason.strip()

    problems: list[str] = []
    if not cfg.adaptive_intent:
        problems.append("policy.engine.jev.adaptive_intent is false: set it to true in policy.json to allow overrides")
    if not text:
        problems.append("intent text is empty")
    elif len(text) > INTENT_TEXT_MAX:
        problems.append(f"intent text is {len(text)} characters (maximum {INTENT_TEXT_MAX})")
    else:
        problems += [
            f"intent {p}"
            for p in intent_text_problems(
                text, wallet=policy_model.wallet, signer=policy_model.signer, planet_ids=policy_model.planets
            )
        ]
    if not reason:
        problems.append("--reason is required and must not be empty")
    elif len(reason) > INTENT_REASON_MAX:
        problems.append(f"reason is {len(reason)} characters (maximum {INTENT_REASON_MAX})")
    delta: timedelta | None = None
    try:
        delta = parse_ttl(ttl) if ttl is not None else timedelta(hours=cfg.adaptive_intent_default_hours)
    except ValueError as exc:
        problems.append(str(exc))
    if delta is not None:
        if delta <= timedelta(0):
            problems.append("ttl must be greater than zero")
        elif delta > timedelta(hours=ADAPTIVE_INTENT_MAX_HOURS):
            problems.append(f"ttl exceeds the maximum of {ADAPTIVE_INTENT_MAX_HOURS}h")
    if problems:
        _refuse(console, problems)
    assert delta is not None

    now = datetime.now(UTC)
    override = IntentOverride(intent=text, reason=reason, set_at=now, expires_at=now + delta)
    try:
        replaced = state.load_intent_override() is not None
    except state.IntentOverrideError:
        replaced = True  # an unreadable file is overwritten too
    state.save_intent_override(override)
    log.append_strategy(f'intent override set until {override.expires_at.isoformat()}: "{text}" -- {reason}', now=now)

    warnings: list[str] = []
    if policy is not None and policy.resolve() != state.policy_path().resolve():
        warnings.append(
            f"ticks read {state.policy_path()}, not {policy}: the override was validated against {policy.name} "
            "but is judged against the policy a tick loads"
        )
    if policy_model.engine.kind != "jev":
        warnings.append('policy.engine.kind is not "jev": this override has no effect until the jev engine is enabled')
    if json_output:
        typer.echo(json.dumps({**_override_json(override), "replaced": replaced, "warnings": warnings}, indent=2))
        return
    for warning in warnings:
        console.print(f"[yellow]warning: {escape(warning)}[/yellow]", highlight=False, soft_wrap=True)
    verb = "replaced" if replaced else "set"
    console.print(
        f"[green]intent override {verb}[/green] until {override.expires_at.isoformat()}\n"
        f"  intent: {escape(text)}\n  reason: {escape(reason)}",
        highlight=False,
        soft_wrap=True,
    )


@intent_app.command("show")
def intent_show(
    policy: Path = typer.Option(None, "--policy", help="Path to policy.json (default: $VEYDRIFT_HOME/policy.json)."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show the intent a tick would judge against now, and where it comes from. Read-only: an
    expired override file is reported, never deleted (the tick does that)."""
    console = Console()
    policy_model = _load_policy_or_exit(policy, console)
    effective = resolve_effective_intent(policy_model, now=datetime.now(UTC))
    override = effective.override
    if override is None and policy_model.engine.kind != "jev":
        # The resolver does not look for a file under the ladder; show it anyway, marked unused.
        from veydrift_agent import state

        try:
            override = state.load_intent_override()
        except state.IntentOverrideError:
            override = None

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "engine_kind": policy_model.engine.kind,
                    "adaptive_intent": policy_model.engine.jev.adaptive_intent,
                    "intent": effective.text,
                    "source": effective.source,
                    "note": effective.note,
                    "expired": effective.expired,
                    "override": _override_json(override) if override is not None else None,
                },
                indent=2,
            )
        )
        return

    typer.echo(f"intent: {effective.text}")
    typer.echo(f"source: {effective.source}")
    if policy_model.engine.kind != "jev":
        typer.echo('engine: ladder (the intent is not used until policy.engine.kind is "jev")')
    if override is not None:
        typer.echo(f"override reason: {override.reason}")
        typer.echo(f"override set at: {override.set_at.isoformat()}")
        typer.echo(f"override expires at: {override.expires_at.isoformat()}")
        if effective.source != "agent":
            typer.echo(f"override text (not in use): {override.intent}")
    if effective.note:
        typer.echo(f"note: {effective.note}")


@intent_app.command("clear")
def intent_clear(
    reason: str = typer.Option(None, "--reason", help="Why the override is being removed (logged to strategy.md)."),
    policy: Path = typer.Option(None, "--policy", help="Accepted for symmetry; clearing never reads the policy."),  # noqa: B008
) -> None:
    """Remove the stored override, if any. Always allowed, and exits 0 whether or not one existed."""
    from veydrift_agent import log, state

    if not state.clear_intent_override():
        typer.echo("no intent override stored")
        return
    why = (reason or "").strip() or "no reason given"
    log.append_strategy(f"intent override cleared -- {why}")
    typer.echo("intent override cleared")
