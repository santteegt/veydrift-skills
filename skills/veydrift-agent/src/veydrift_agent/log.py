"""`vd log` — the append-only sinks, pretty tick report, and `--digest` (docs/SPEC.md §5.9).

Four sinks, all under `$VEYDRIFT_HOME` (never the skill tree — see `state.py`):

| Sink | Written by | Contents |
| --- | --- | --- |
| `logs/proposals.jsonl` | every tick | one JSON line per proposal: full `GuardReport.verdicts`, calldata, `executed` bool |
| `logs/actions.jsonl` | only when a tx is actually sent | tx hash, gas, block, before/after resources, indexed-at |
| `logs/ticks/<iso>.md` | every tick | the pretty block below, one file per tick |
| `logs/strategy.md` | ticks with something worth narrating | rationale, plan revisions, escalations, human decisions |

**Secret scrubbing is unconditional and applied to every line before it touches disk**:
`scrub_text` strips any `0x[0-9a-fA-F]{64}` that is not a known tx hash for *this* record,
and separately redacts the current value of any configured secret env var
(`DEFAULT_SECRET_ENV_VARS`, extendable via `VEYDRIFT_SECRET_ENV_VARS`, a comma-separated
list) wherever it appears verbatim. `tests/test_log.py` exercises this directly with a
fake private key and a fake tx hash in the same record, and asserts the key never reaches
disk while the legitimate hash survives.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from veydrift_agent.state import logs_dir, ticks_dir, veydrift_home

app = typer.Typer(no_args_is_help=False, help="Read and summarise the action and strategy logs.")

_console = Console()

# --------------------------------------------------------------------------------------
# Secret scrubbing
# --------------------------------------------------------------------------------------

_HEX64_RE = re.compile(r"0x[0-9a-fA-F]{64}")

#: Names checked by default, beyond whatever `VEYDRIFT_SECRET_ENV_VARS` (comma-separated)
#: adds. Covers both wallet-engine credentials (docs/SPEC.md §6.3) even though this
#: package never reads them itself -- a shared environment means they could still leak
#: into a log via an interpolated shell command's output, so the scrub checks for them
#: defensively regardless of which process actually uses them.
DEFAULT_SECRET_ENV_VARS: tuple[str, ...] = (
    "VEYDRIFT_PRIVATE_KEY",
    "VEYDRIFT_KEYSTORE_PASSWORD",
)


def configured_secret_env_vars() -> tuple[str, ...]:
    extra = os.environ.get("VEYDRIFT_SECRET_ENV_VARS", "")
    extra_names = tuple(name.strip() for name in extra.split(",") if name.strip())
    return DEFAULT_SECRET_ENV_VARS + extra_names


def scrub_text(
    text: str,
    *,
    known_tx_hashes: Iterable[str] = (),
    secret_env_vars: Iterable[str] | None = None,
) -> str:
    """Strip anything that looks like a private key from `text`.

    1. Any `0x` + 64 hex chars that is not in `known_tx_hashes` (case-insensitive) is
       replaced with a masked placeholder of the same shape. A private key and a tx hash
       are the same byte length and indistinguishable by pattern alone, so the caller must
       tell this function which 32-byte hex values are *legitimately* meant to be logged
       (the actual tx hash for *this* record) -- everything else matching the pattern is
       treated as a potential secret and never written.
    2. The literal current value of every configured secret env var (with/without a `0x`
       prefix) is redacted wherever it appears verbatim, independent of the regex above --
       this also catches secrets that are not 32 bytes (e.g. a keystore password).
    """
    known = {h.lower() for h in known_tx_hashes if h}

    def _mask_hex(match: re.Match[str]) -> str:
        value = match.group(0)
        return value if value.lower() in known else "0x" + "*" * 64

    scrubbed = _HEX64_RE.sub(_mask_hex, text)

    for var in secret_env_vars if secret_env_vars is not None else configured_secret_env_vars():
        value = os.environ.get(var)
        if not value:
            continue
        candidates = {value}
        if value.startswith(("0x", "0X")):
            candidates.add(value[2:])
        else:
            candidates.add("0x" + value)
        for candidate in candidates:
            if candidate and candidate in scrubbed:
                scrubbed = scrubbed.replace(candidate, f"[REDACTED:{var}]")
    return scrubbed


def _extract_known_tx_hashes(record: Mapping[str, Any]) -> list[str]:
    hashes: list[str] = []
    tx_hash = record.get("tx_hash")
    if isinstance(tx_hash, str):
        hashes.append(tx_hash)
    tx = record.get("tx")
    if isinstance(tx, Mapping):
        data = tx.get("data")
        # calldata legitimately contains arbitrary hex that is NOT a secret (it's public,
        # about-to-be-broadcast transaction data) but is also not "a known tx hash" in the
        # narrow sense -- excluding it from `known` would corrupt it via `_mask_hex` if it
        # happens to contain a 32-byte-aligned run of hex digits equal to 64 chars (e.g. a
        # uint256 arg). Calldata is passed through scrub_text separately, unscrubbed, by
        # `_json_line` below -- see its docstring.
        del data
    # tick.py's human-activity reconciliation (_maybe_check_human_activity) embeds real,
    # public, already-indexed tx hashes from /wallet/{addr}/activity -- exactly as "known
    # legitimate" as the top-level tx_hash field above, not a secret. Without this, every
    # activity tx hash would come out masked as `0x****...`, defeating the point of
    # recording it for a human to cross-reference.
    activity = record.get("human_activity_check")
    if isinstance(activity, Mapping):
        for item in activity.get("items") or []:
            if isinstance(item, Mapping) and isinstance(item.get("transactionHash"), str):
                hashes.append(item["transactionHash"])
    return hashes


def _json_line(record: Mapping[str, Any]) -> str:
    """Serialise `record` to one scrubbed JSON line.

    `tx.data` (calldata) is deliberately exempted from the hex-64 mask: it is public,
    about-to-be-broadcast transaction data, not a secret, and masking a `uint256` argument
    that happens to be 32 bytes would silently corrupt the audit record. Every other field
    goes through the full scrub.
    """
    tx_data_placeholder = None
    marker = "@@VD_TX_DATA_PLACEHOLDER@@"
    record = dict(record)
    tx = record.get("tx")
    if isinstance(tx, Mapping) and isinstance(tx.get("data"), str):
        tx_data_placeholder = tx["data"]
        record = {**record, "tx": {**tx, "data": marker}}

    known = _extract_known_tx_hashes(record)
    raw = json.dumps(record, sort_keys=False, default=str)
    scrubbed = scrub_text(raw, known_tx_hashes=known)
    if tx_data_placeholder is not None:
        scrubbed = scrubbed.replace(marker, tx_data_placeholder)
    return scrubbed


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_json_line(record))
        fh.write("\n")


# --------------------------------------------------------------------------------------
# The four sinks
# --------------------------------------------------------------------------------------


def proposals_path() -> Path:
    return logs_dir() / "proposals.jsonl"


def actions_path() -> Path:
    return logs_dir() / "actions.jsonl"


def strategy_path() -> Path:
    return logs_dir() / "strategy.md"


def log_proposal(record: Mapping[str, Any]) -> None:
    """Every proposal, every tick, regardless of guard decision or whether it executed --
    the audit artifact docs/SPEC.md §5.9 calls for. `record` is expected to carry at least
    `ts`, `tick`, `guard_decision`, `guard_verdicts`, `tx` (or `null`), `executed`."""
    _append_jsonl(proposals_path(), record)


def read_proposals() -> list[dict[str, Any]]:
    """Every logged proposal, oldest first. Used by `--digest` and `vd tick --readiness`."""
    return _read_jsonl(proposals_path())


def read_actions() -> list[dict[str, Any]]:
    """Every logged **executed** action, oldest first."""
    return _read_jsonl(actions_path())


def log_action(record: Mapping[str, Any]) -> None:
    """**Executed only.** Never called for a tier-1 dry run or a blocked/escalated
    proposal -- `tests/test_tick.py` asserts `actions.jsonl` stays untouched for exactly
    those cases."""
    _append_jsonl(actions_path(), record)


def append_strategy(text: str, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    path = strategy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"\n## {now.isoformat()}\n\n{scrub_text(text)}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry)


# --------------------------------------------------------------------------------------
# The pretty tick block (docs/SPEC.md §5.9's worked example) — one function builds the
# plain-text block, reused for both the coloured stdout Panel and the plain .md file.
# --------------------------------------------------------------------------------------


def format_tick_block(
    *,
    tick_number: int,
    taken_at: datetime,
    tier: str,
    planet_line: str,
    state_line: str,
    queues_line: str,
    incoming_line: str,
    proposal_lines: list[str],
    next_hint: str | None = None,
    duplicate_of: str | None = None,
    human_activity_line: str | None = None,
    alliance_line: str | None = None,
) -> str:
    lines = [
        f"[{taken_at.strftime('%Y-%m-%dT%H:%M:%SZ')}] TICK #{tick_number}  tier={tier}  {planet_line}",
        f"  state:    {state_line}",
        f"  queues:   {queues_line}",
        f"  incoming: {incoming_line}",
        *([f"  activity: {human_activity_line}"] if human_activity_line else []),
        *([f"  alliance: {alliance_line}"] if alliance_line else []),
        *[f"  {line}" for line in proposal_lines],
    ]
    if duplicate_of:
        lines.append(f"  note:     {duplicate_of}")
    if next_hint:
        lines.append(f"  next:     {next_hint}")
    return "\n".join(lines)


def print_tick_report(block_text: str, *, tick_number: int) -> None:
    _console.print(Panel(block_text, title=f"vd tick #{tick_number}", border_style="cyan", expand=False))


def write_tick_markdown(block_text: str, *, taken_at: datetime) -> Path:
    path = ticks_dir() / f"{taken_at.strftime('%Y-%m-%dT%H-%M-%SZ')}.md"
    path.write_text(f"# Tick {taken_at.isoformat()}\n\n```text\n{scrub_text(block_text)}\n```\n")
    return path


# --------------------------------------------------------------------------------------
# --digest — docs/SPEC.md §5.9: "builds, research, resources produced, gas spent, and
# everything refused, with reasons."
# --------------------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)([hd])$")


def parse_window(spec: str):
    from datetime import timedelta

    match = _WINDOW_RE.match(spec.strip())
    if not match:
        raise ValueError(f"invalid --digest window {spec!r}; expected e.g. '24h' or '7d'")
    n, unit = match.groups()
    return timedelta(hours=int(n)) if unit == "h" else timedelta(days=int(n))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def build_digest(window_spec: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    window = parse_window(window_spec)
    cutoff = now - window

    proposals = [
        p for p in _read_jsonl(proposals_path()) if (ts := _parse_ts(p.get("ts"))) is not None and ts >= cutoff
    ]
    actions = [a for a in _read_jsonl(actions_path()) if (ts := _parse_ts(a.get("ts"))) is not None and ts >= cutoff]

    executed = [p for p in proposals if p.get("executed")]
    refused = [p for p in proposals if p.get("guard_decision") in ("block", "escalate")]

    by_function: dict[str, int] = {}
    for p in executed:
        fn = p.get("function") or "unknown"
        by_function[fn] = by_function.get(fn, 0) + 1

    total_gas = sum(int(a.get("gas_wei") or 0) for a in actions)

    lines = [
        f"# Digest — trailing {window_spec} (since {cutoff.isoformat()})",
        "",
        f"- proposals: {len(proposals)}",
        f"- executed: {len(executed)}",
        f"- refused (blocked/escalated): {len(refused)}",
        f"- gas spent: {total_gas} wei",
        "",
        "## Executed, by function",
    ]
    if by_function:
        for fn, count in sorted(by_function.items()):
            lines.append(f"- {fn}: {count}")
    else:
        lines.append("- (none)")

    lines += ["", "## Refused, with reasons"]
    if refused:
        for p in refused:
            reasons = [
                f"{v.get('gate')}: {v.get('detail')}"
                for v in (p.get("guard_verdicts") or [])
                if v.get("status") in ("block", "escalate")
            ]
            lines.append(f"- tick {p.get('tick')} {p.get('function') or p.get('kind')}: {'; '.join(reasons) or p.get('guard_decision')}")
    else:
        lines.append("- (none)")

    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def log_main(
    ctx: typer.Context,
    digest: str | None = typer.Option(None, "--digest", help="Print a digest for the trailing window, e.g. 24h, 7d."),
) -> None:
    """`vd log --digest 24h` — the daily/weekly summary (docs/SPEC.md §5.9)."""
    if digest is not None:
        typer.echo(build_digest(digest))
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(f"logs live under: {veydrift_home() / 'logs'}")
        typer.echo("use --digest 24h, or a subcommand: tail-proposals, tail-actions, strategy")


@app.command(name="tail-proposals")
def tail_proposals(n: int = typer.Option(10, "-n", help="How many of the most recent proposals to print.")) -> None:
    for record in _read_jsonl(proposals_path())[-n:]:
        typer.echo(json.dumps(record, indent=2))


@app.command(name="tail-actions")
def tail_actions(n: int = typer.Option(10, "-n", help="How many of the most recent executed actions to print.")) -> None:
    for record in _read_jsonl(actions_path())[-n:]:
        typer.echo(json.dumps(record, indent=2))


@app.command()
def strategy() -> None:
    """Print `logs/strategy.md` verbatim."""
    path = strategy_path()
    typer.echo(path.read_text() if path.exists() else f"(no strategy log yet at {path})")


if __name__ == "__main__":
    app()
