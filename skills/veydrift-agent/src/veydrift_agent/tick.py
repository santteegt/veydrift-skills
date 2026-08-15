"""`vd tick` — the loop entrypoint (docs/SPEC.md §5.7). Idempotent, lockfile-protected.

```
1. load + validate policy         6. guard
2. killswitch check                7. if ALLOW and tier>=2 and not --dry-run:
3. reconcile pending txs                 walletctl build (already done at step 6)
4. snapshot                              if require_confirmation: print the command, don't send
5. plan                                  else: send -> await receipt status -> await INDEXED
                                    8. log: proposal, unless content-identical to the
                                       immediately-previous logged proposal (dedup);
                                       action only on a real send attempt
                                    9. pretty report -> stdout + logs/ticks/
```

**Repeated identical proposals are deduped, not re-logged** (step 8): `_finish_tick`
fingerprints the record it's about to write (`_fingerprint_proposal`, sha256 over every
field except `ts`/`tick`) and compares it against `AgentState.last_proposal_fingerprint`.
A match means this tick produced no new evidence — most commonly a human or agent
re-running `vd tick` seconds later just to re-inspect output in a different `--format` —
so `tick_count`/`proposals_count` don't advance and nothing is appended to
`proposals.jsonl`/`strategy.md`. `last_tick_at` still updates regardless (`AgentState.touch`),
and the printed/`--format json` report is always the full, accurate current state, with a
`duplicate`/`note` marker so the caller isn't confused about why the tick number didn't
move. This is deliberately content-based, not time-window-based: live guard-evaluation
figures (resources, energy, gas price) drift over any real elapsed time even when the
top recommendation is unchanged, so a genuine re-evaluation hours later naturally
produces a different fingerprint and is logged normally.

**Never signs, never imports any chain-signing JS library** (acceptance criterion 15 —
grep-verifiable per docs/SPEC.md; this package's `src/` must not contain those library
names as an import). Tier >= 2 reaches the wallet engine *only* via a subprocess call to
`walletctl`
(`skills/veydrift-wallet`'s CLI) — see `_walletctl_argv` below for how that path is
resolved, and its docstring for the sibling-directory assumption this makes.

**`--dry-run` is the default at tier 1 and cannot be disabled there** — enforced in
`_effective_dry_run`, independent of whatever the caller passes.

**`policy.wallet_engine.require_confirmation` (default `true`) gates step 7's send, not
just a human's own discipline:** when true, `_run_tick` builds and guard-evaluates the
action (unconditionally, same as always) but stops there — it prints the exact
`walletctl send --tx <path> --confirm` command a human should run, and ends the tick
cleanly rather than sending automatically. Only `false` reaches `_send_and_await`.

**A sent tx's outcome is read from the receipt's `status`, never assumed** (Fix 2):
`_send_and_await`/`_await_receipt` poll for a receipt carrying `"success"` or
`"reverted"`; a reverted tx is still written to `logs/actions.jsonl` (hiding a revert is
worse than recording it) but is never counted as an execution, and calls
`AgentState.record_revert` so `guard.py`'s `revert_streak` gate can actually fire. A
receipt that never resolves in time is `"unknown"` — also never treated as success —
and `_reconcile_pending` gets another chance at it on a later tick.

**The indexed-wait is mandatory** (step 7's last part, success path only): a confirmed
receipt is not indexed state. `_await_indexed` polls a fresh snapshot's
`latest_indexed_block` until it covers the receipt's block, or
`policy.limits.max_index_wait_s` elapses — in which case the tick logs the timeout and
does **not** treat the action as reconciled (the next tick's `guard.index_lag` gate will
BLOCK further action on this planet until it catches up).

**`vd init`:** `cli.py` (frozen) does not mount a bare top-level `vd init` — its
`_SUBAPPS` list only ever mounts this module under the name `tick`. So the closest
achievable command is `vd tick init`, implemented here by delegating straight to
`state.init_policy()`. Documented as a known deviation from the spec's literal `vd init`
wording in the WP3 report; `references/scheduling.md` and `SKILL.md` should point at
`vd tick init` as the actual command.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from veydrift_agent import guard as guard_mod
from veydrift_agent import http, log, read
from veydrift_agent import plan as plan_mod
from veydrift_agent.models import (
    Action,
    ActionKind,
    Decision,
    GuardReport,
    GuardStatus,
    GuardVerdict,
    Policy,
    Snapshot,
    Tier,
    UnsignedTx,
)
from veydrift_agent.state import (
    AgentState,
    PendingTx,
    TickLockedError,
    UnresolvedProposal,
    load_agent_state,
    policy_path,
    save_agent_state,
    tick_lock,
)
from veydrift_agent.state import (
    killswitch_active as _killswitch_active,
)

app = typer.Typer(no_args_is_help=False, help="Run one loop iteration.")
_console = Console()
_stderr_console = Console(stderr=True)

_WALLETCTL_TIMEOUT_S = 60
_INDEX_POLL_INTERVAL_S = 5
_NPM_INSTALL_TIMEOUT_S = 300


# --------------------------------------------------------------------------------------
# vd tick init
# --------------------------------------------------------------------------------------


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite an existing policy.json.")) -> None:
    """Copy `assets/policy.example.json` to `$VEYDRIFT_HOME/policy.json`. See this
    module's docstring for why this is `vd tick init` rather than a bare `vd init`."""
    from veydrift_agent.state import PolicyInitError, init_policy

    try:
        dest = init_policy(force=force)
    except PolicyInitError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    _console.print(f"[green]wrote[/green] {dest}")


# --------------------------------------------------------------------------------------
# Policy loading — invalid policy is a hard stop, never a silent fallback (docs/SPEC.md
# §5.6). `Policy.model_config` sets `extra="forbid"`, so an unknown key already raises.
# --------------------------------------------------------------------------------------


def _warn_dead_policy_keys(policy: Policy) -> None:
    """Fix 4: `actions.allow_fleet_noncombat` is read by no code path -- the planner has
    no fleet rung (deliberately: building one is out of scope for this fix, per the
    brief). Rather than let the key keep misleading a human the way `allow_ships` used to
    before it grew a rung, surface its dead-ness explicitly on every load. `cli.py`'s
    `doctor` command is frozen and cannot be extended to report this instead, so policy
    load time is the next best place a human will actually see it."""
    if policy.actions.allow_fleet_noncombat:
        _console.print(
            "[yellow]warning:[/yellow] policy.actions.allow_fleet_noncombat=true has no effect -- "
            "no planner rung proposes non-combat fleet missions yet. This key is currently dead "
            "config; do not rely on it."
        )


def _load_policy(path: Path) -> Policy:
    if not path.exists():
        raise typer.BadParameter(f"no policy at {path} -- run `vd tick init` first, or pass --policy PATH")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON: {exc}") from exc
    try:
        policy = Policy.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise typer.BadParameter(f"{path} failed Policy validation: {exc}") from exc
    _warn_dead_policy_keys(policy)
    return policy


def _effective_dry_run(policy: Policy, requested_dry_run: bool) -> bool:
    """Tier 1 cannot disable --dry-run, full stop (docs/SPEC.md §5.7)."""
    if policy.tier is Tier.ADVISOR:
        return True
    return requested_dry_run


# --------------------------------------------------------------------------------------
# walletctl subprocess bridge. This is the ONLY place tick.py touches the wallet engine,
# and it is always a subprocess call -- never an import.
# --------------------------------------------------------------------------------------


def _wallet_skill_dir() -> Path | None:
    """Resolve `skills/veydrift-wallet` as a sibling of this installed skill.

    Both skills are installed together (`npx skills add . -a claude-code -a
    hermes-agent`, docs/SPEC.md §2.2) from the same repository root into the same target
    agent's skills directory, so the sibling relationship this assumes should survive
    install -- but if it doesn't (a harness that installs skills into per-skill isolated
    roots), `VEYDRIFT_WALLET_DIR` is the escape hatch, and the final fallback is a plain
    `walletctl` resolved from `PATH` (e.g. after `npm link` in that project).
    """
    env = os.environ.get("VEYDRIFT_WALLET_DIR")
    if env:
        candidate = Path(env).expanduser()
        if (candidate / "src" / "cli.ts").exists():
            return candidate
    sibling = Path(__file__).resolve().parents[3] / "veydrift-wallet"
    if (sibling / "src" / "cli.ts").exists():
        return sibling
    return None


def _walletctl_argv(*args: str) -> tuple[list[str], Path | None]:
    wallet_dir = _wallet_skill_dir()
    if wallet_dir is not None:
        return ["npx", "--yes", "tsx", str(wallet_dir / "src" / "cli.ts"), *args], wallet_dir
    return ["walletctl", *args], None


def _ensure_wallet_deps_installed(wallet_dir: Path) -> str | None:
    """`npx skills add` copies `veydrift-wallet`'s source, `package.json` and
    `package-lock.json` but never runs `npm install` at the destination -- there's no
    `uv run`-style auto-venv equivalent for npm. Left alone, the first `npx tsx cli.ts`
    invocation fails with a raw `ERR_MODULE_NOT_FOUND` on `commander`, which then surfaces
    as an opaque `walletctl_build` ESCALATE detail. This self-heals it once, from the
    already-committed, pinned `package-lock.json` (`npm install`, never a floating
    resolution) -- visibly, via `_stderr_console`, never silently. Returns `None` on
    success (or if already installed), or a human-readable error to use as the
    `CompletedProcess.stderr` in place of the eventual cryptic import failure."""
    if (wallet_dir / "node_modules").is_dir():
        return None
    _stderr_console.print(
        f"[dim]veydrift-wallet: no node_modules yet in {wallet_dir} -- running `npm install` "
        "(first run only, from the pinned package-lock.json)...[/dim]"
    )
    try:
        install = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=wallet_dir,
            capture_output=True,
            text=True,
            timeout=_NPM_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            f"veydrift-wallet dependencies are not installed and `npm install` could not be "
            f"run automatically ({exc}). Run `npm install` in {wallet_dir} yourself, then retry."
        )
    if install.returncode != 0:
        detail = (install.stderr or install.stdout).strip()[:500]
        return (
            "veydrift-wallet dependencies are not installed and automatic `npm install` "
            f"failed: {detail}. Run `npm install` in {wallet_dir} yourself, then retry."
        )
    _stderr_console.print("[dim]veydrift-wallet: dependencies installed.[/dim]")
    return None


def _run_walletctl(*args: str, timeout: int = _WALLETCTL_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    argv, cwd = _walletctl_argv(*args)
    if cwd is not None:
        install_error = _ensure_wallet_deps_installed(cwd)
        if install_error is not None:
            return subprocess.CompletedProcess(argv, 1, "", install_error)
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _action_to_walletctl_json(action: Action) -> dict[str, Any]:
    """`Action` (this package's pydantic model) -> the `{function, args, purpose}` shape
    `walletctl build --action` expects (`veydrift-wallet/src/tx.ts`'s `Action` interface).
    Positional `args` match the ABI's declared input order exactly."""
    fn = action.function
    if fn == "startBuildingUpgrade" or fn == "startResearch":
        args = [action.planet_id, action.entity_id]
    elif fn == "startShipProduction" or fn == "startDefenseProduction":
        args = [action.planet_id, action.entity_id, action.quantity or 0]
    elif fn == "resolveFleetMission":
        args = [action.mission_id]
    elif fn == "settlePlanet":
        args = [action.planet_id]
    else:
        raise ValueError(f"tick.py does not know how to build calldata for function {fn!r}")
    return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200]}


def _walletctl_build(action: Action, *, provider: str) -> tuple[UnsignedTx | None, int | None, str | None, Path | None]:
    """Returns `(unsigned_tx, gas_cost_wei, error, built_tx_path)`. Never raises -- a
    build failure (e.g. `walletctl` unreachable, or a live /runtime-config fetch failing
    inside it) is reported back as `error` so the tick can still produce a report with
    the honest "could not build" state rather than crashing the whole loop.

    **Unit contract (Fix 1):** `walletctl build`'s JSON output carries `gas` (gas
    *units*, ~1e5 on Base) and `estimatedCostWei` (gas units * gas price, the actual wei
    cost -- ~1e12-1e15). `gas_cost_wei` here is **always** parsed from `estimatedCostWei`,
    never from `gas` -- feeding gas units into a wei-scale ceiling (`guard.py`'s `gas`
    gate) or into `AgentState.record_gas_spent` would make both permanently inert (the
    confirmed defect this fix addresses). `estimatedCostWei` may legitimately be `null`
    (e.g. no provider configured to estimate from) -- that is passed through as `None`,
    never silently substituted with `0`, so the gas gate's existing ESCALATE-on-`None`
    path does its job. `unsigned_tx.gas` (the units field) is kept as-is on the model --
    it is a legitimate gas-limit hint for the eventual `walletctl send`, not something
    compared against a wei ceiling.

    `built_tx_path` is the `--out` file `walletctl build` wrote -- the same file
    `walletctl send --tx <path> --confirm` consumes. Returned so the `require_confirmation`
    path (Fix 3) can print the exact command a human should run, without re-building."""
    if not action.is_onchain():
        return None, None, None, None
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd-tick-"))
    action_file = tmp_dir / "action.json"
    out_file = tmp_dir / "tx.json"
    try:
        action_file.write_text(json.dumps(_action_to_walletctl_json(action)))
    except ValueError as exc:
        return None, None, str(exc), None
    try:
        result = _run_walletctl("build", "--action", str(action_file), "--out", str(out_file), "--provider", provider)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, f"walletctl build could not be run: {exc}", None
    if result.returncode != 0 or not out_file.exists():
        return None, None, f"walletctl build failed: {(result.stderr or result.stdout).strip()[:500]}", None
    built = json.loads(out_file.read_text())
    tx = UnsignedTx(
        to=built["to"],
        data=built["data"],
        value=int(built.get("value") or 0),
        chain_id=int(built.get("chainId") or 8453),
        gas=int(built["gas"]) if built.get("gas") else None,
    )
    cost_raw = built.get("estimatedCostWei")
    gas_cost_wei = int(cost_raw) if cost_raw not in (None, "") else None
    return tx, gas_cost_wei, None, out_file


def _walletctl_eth_balance_wei(*, provider: str) -> int | None:
    """Best-effort. `walletctl status` prints plain text (`balance: X ETH`), not JSON;
    parsed defensively. Returns `None` (never raises) on any failure -- the caller
    (`guard.py`'s `eth_floor` gate) already treats `None` as "cannot verify", never as
    "the wallet has enough ETH"."""
    try:
        result = _run_walletctl("status", "--provider", provider, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.strip().startswith("balance:"):
            try:
                eth_str = line.split(":", 1)[1].strip().split(" ")[0]
                return int(round(float(eth_str) * 10**18))
            except (ValueError, IndexError):
                return None
    return None


def _walletctl_receipt(tx_hash: str) -> dict[str, Any] | None:
    """`walletctl receipt --hash <tx_hash>` -> the parsed receipt JSON, or `None` on any
    failure (unreachable, non-zero exit, unparseable stdout). Callers must treat `None`
    -- and a present-but-`status`-less dict -- as **unknown outcome**, never as success
    (Fix 2): `{"status": "success" | "reverted", "blockNumber", "gasUsed",
    "effectiveGasPrice", "actualCostWei"}` is the contract; `status` is what
    `_send_and_await`/`_reconcile_pending` gate a revert/success decision on, and
    `actualCostWei` is what gets charged to the daily gas ledger (the real amount burned,
    not the pre-send estimate)."""
    try:
        result = _run_walletctl("receipt", "--hash", tx_hash, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _walletctl_send(tx_path: Path, *, tier: Tier, provider: str) -> tuple[str | None, str | None]:
    """Returns `(tx_hash, error)`. Only reachable when guard ALLOWed, tier>=2,
    `policy.wallet_engine.require_confirmation` is false, and --dry-run is false -- see
    `run()` below. Never called during this WP's own verification pass (no tier>=2 policy,
    no wallet credentials were configured)."""
    try:
        result = _run_walletctl("send", "--tx", str(tx_path), "--confirm", "--tier", tier.value, "--provider", provider, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"walletctl send could not be run: {exc}"
    for line in result.stdout.splitlines():
        if line.strip().startswith("SUBMITTED:"):
            return line.split(":", 1)[1].strip(), None
    return None, f"walletctl send did not report SUBMITTED: {(result.stderr or result.stdout).strip()[:500]}"


def _parse_hex_or_int(raw: Any) -> int | None:
    """`walletctl receipt` fields (`blockNumber`) may arrive as a `0x`-prefixed hex
    string or a plain int; `estimatedCostWei`/`actualCostWei` are decimal strings.
    Shared by `_reconcile_pending` and `_send_and_await` so the two receipt-parsing
    paths can't drift."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.startswith("0x"):
        return int(raw, 16)
    return int(raw)


def _parse_wei_str(raw: Any) -> int | None:
    """Decimal-string wei field (`estimatedCostWei`/`actualCostWei`) -> int, or `None`
    when absent/blank. Never coerces a missing measurement to `0` (Fix 1's rule applies
    here too: a zero you did not measure must never be substituted for one you didn't
    get)."""
    if raw in (None, ""):
        return None
    return int(raw)


def _live_addresses() -> set[str] | None:
    try:
        config = http.fetch("/runtime-config")
    except http.VeydriftAPIError:
        return None
    addresses = {a for a in (config.get("gameContractAddress"), config.get("contractAddress")) if a}
    return addresses or None


# --------------------------------------------------------------------------------------
# Snapshot acquisition — calls straight into `read.snapshot`, WP1's own composed-snapshot
# command, rather than duplicating its HTTP/parsing logic here. `out=` is used (rather
# than `--json` to stdout) so the health-not-ok path -- which still writes the file before
# raising `typer.Exit(2)` -- is captured too; plan.py's own rung 1 is what actually acts
# on `Snapshot.health_ok`, not an exception here.
# --------------------------------------------------------------------------------------


def _fetch_snapshot(wallet: str, policy_planets: list[int]) -> Snapshot | None:
    planet_id = policy_planets[0] if len(policy_planets) == 1 else None
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd-tick-snapshot-"))
    out_file = tmp_dir / "snapshot.json"
    try:
        read.snapshot(wallet=wallet, planet_id=planet_id, json_output=False, out=out_file, max_age=None)
    except typer.Exit:
        pass  # health-not-ok / bad-args paths still write `out_file` first when they can
    if not out_file.exists():
        return None
    return Snapshot.model_validate(json.loads(out_file.read_text()))


def _fetch_health_only() -> bool:
    """Used only on the killswitch path (step 2): the ONE network call allowed before a
    halt (acceptance criterion: "halts before any network call beyond health")."""
    try:
        data = http.fetch("/health")
    except http.VeydriftAPIError:
        return False
    readiness = data.get("readiness") or {}
    return data.get("ok") is True and readiness.get("ready") is True


# --------------------------------------------------------------------------------------
# Reconciliation (step 3) — see this module's docstring for the ordering note: the actual
# "is it indexed yet" answer needs a snapshot's indexed block, so this function is called
# once before the snapshot (to poll a receipt if one is missing) and once after (to
# compare against the freshly-fetched indexed block).
# --------------------------------------------------------------------------------------


def _reconcile_pending(agent_state: AgentState, *, indexed_block: int | None, now: datetime) -> bool:
    """Returns True if a pending tx remains unreconciled (plan.py rung 2).

    Also covers the case where a tx's outcome wasn't known yet when `_send_and_await`
    returned in an earlier tick (its own receipt poll timed out before the tx was mined):
    if a receipt shows up here with a `status`, it is recorded exactly the same way
    `_send_and_await` would have if it had seen it immediately (Fix 2) -- a revert (or a
    success) must never go unrecorded just because it took more than one tick's poll
    window to be mined. `pending.gas_wei` (set the first time *either* function charges
    the ledger for this pending tx -- from `actualCostWei` if known, else the pre-send
    estimate) guards against ever double-charging the same tx twice across the two call
    sites.
    """
    pending = agent_state.pending
    if pending is None:
        return False
    if pending.indexed_at is not None:
        agent_state.pending = None
        return False
    if pending.reverted:
        # Already recorded (by _send_and_await or a prior reconcile pass) -- nothing left
        # to wait on for this entry.
        agent_state.pending = None
        return False
    if pending.block is None and pending.tx_hash:
        receipt = _walletctl_receipt(pending.tx_hash)
        if receipt is not None:
            status = receipt.get("status")
            if status == "reverted":
                agent_state.record_revert(pending.key)
                if pending.gas_wei is None:
                    cost_wei = _parse_wei_str(receipt.get("actualCostWei"))
                    if cost_wei is not None:
                        agent_state.record_gas_spent(cost_wei, now=now)
                        pending.gas_wei = cost_wei
                log.append_strategy(
                    f"REVERTED (confirmed on a later tick): {pending.key} (tx {pending.tx_hash}) -- "
                    f"revert recorded, gas charged, pending cleared.",
                    now=now,
                )
                pending.reverted = True
                agent_state.pending = None
                return False
            if status == "success":
                # A success `_send_and_await` didn't stick around long enough to see --
                # its own poll window (`_RECEIPT_WAIT_S`) timed out first. Record it now,
                # the only place this ever happens for this pending entry (guarded by the
                # enclosing `pending.block is None` check, which flips false the moment
                # `block` is set just below -- so this branch runs at most once).
                agent_state.executions_count += 1
                if pending.gas_wei is None:
                    cost_wei = _parse_wei_str(receipt.get("actualCostWei"))
                    if cost_wei is not None:
                        agent_state.record_gas_spent(cost_wei, now=now)
                        pending.gas_wei = cost_wei
                log.append_strategy(
                    f"CONFIRMED SUCCESS (confirmed on a later tick): {pending.key} (tx {pending.tx_hash}).",
                    now=now,
                )
            block = _parse_hex_or_int(receipt.get("blockNumber"))
            if block is not None:
                pending.block = block
                pending.receipt_at = now
    if pending.block is not None and indexed_block is not None and indexed_block >= pending.block:
        pending.indexed_at = now
        agent_state.pending = None
        return False
    return True


def _maybe_check_human_activity(
    policy_model: Policy, previous: UnresolvedProposal | None, *, now: datetime
) -> tuple[dict[str, Any] | None, str | None]:
    """If `previous` (the previous tick's unresolved on-chain proposal -- tier 1, or
    `require_confirmation` stopped the send) is set, fetch
    `/wallet/{addr}/activity?since=<previous.ts>` and surface whatever raw items come
    back, verbatim -- title/kind/tx hash, no match/diverge verdict. Returns `(None,
    None)` when `previous` is `None`, which is the common case (nothing unresolved to
    check this tick).

    **Deliberately does not classify.** The only `/activity` item ever actually observed
    against this codebase (fixtures or live) is a one-time `"planet-started"` milestone
    (`references/api-routes.md` §3.15) -- nobody has confirmed the shape of a
    queue-completion item. Asserting a confident match/no-match against an unconfirmed
    `kind` taxonomy would be exactly the vacuous-confidence trap AGENTS.md §5 warns
    guard.py's gates against; a human reads the raw titles instead. A structured
    classifier is a deferred follow-up once a real completion-shaped item has actually
    been observed -- see CHANGELOG.md's `[Unreleased]` entry for this feature.

    Never raises and never influences `Decision`/guard.py -- reporting-only, exactly like
    `_live_addresses`'s existing best-effort posture. A fetch failure degrades to a
    `fetch_error` field and an honest "could not fetch" line, never a crash.

    `/activity` has no server-side planet filter (confirmed, `references/api-routes.md`
    §3.15) -- filtering to `previous.planet_id` happens client-side against each item's
    `metadata.planetId`. An item with no `planetId` in its metadata is KEPT rather than
    dropped: the metadata shape beyond the one observed sample is unconfirmed, and
    hiding a true positive is worse than showing an unrelated item.
    """
    if previous is None:
        return None, None

    since = str(int(previous.ts.timestamp()))
    prior_desc = f"{previous.function or previous.entity_name or 'action'} (planet {previous.planet_id}, entity {previous.entity_id})"

    try:
        data = read.fetch_activity(policy_model.wallet, since=since)
    except http.VeydriftAPIError as exc:
        record = {
            "checked": True,
            "since_ts": previous.ts.isoformat(),
            "prior_function": previous.function,
            "prior_planet_id": previous.planet_id,
            "prior_entity_id": previous.entity_id,
            "items_found": None,
            "items": [],
            "fetch_error": str(exc)[:300],
        }
        return record, f"could not fetch /activity since unresolved {prior_desc} -- see fetch_error in proposals.jsonl"

    raw_items = data.get("items") or []
    kept: list[dict[str, Any]] = []
    for item in raw_items:
        meta_planet = (item.get("metadata") or {}).get("planetId")
        if previous.planet_id is not None and meta_planet is not None and str(meta_planet) != str(previous.planet_id):
            continue
        kept.append(
            {
                "kind": item.get("kind"),
                "title": item.get("title"),
                "detail": item.get("detail"),
                "transactionHash": item.get("transactionHash"),
                "occurredAt": item.get("occurredAt"),
            }
        )

    record = {
        "checked": True,
        "since_ts": previous.ts.isoformat(),
        "prior_function": previous.function,
        "prior_planet_id": previous.planet_id,
        "prior_entity_id": previous.entity_id,
        "items_found": len(kept),
        "items": kept,
        "fetch_error": None,
    }

    if kept:
        titles = "; ".join(f"{i.get('kind')}: {i.get('title')}" for i in kept)
        log.append_strategy(
            f"human activity check: {len(kept)} /activity item(s) since the unresolved proposal "
            f"{prior_desc} at {previous.ts.isoformat()} -- {titles}. NOT a confirmed match to that "
            "proposal (see tick.py's _maybe_check_human_activity docstring); a human should read "
            "these titles directly.",
            now=now,
        )
        line = f"{len(kept)} activity item(s) since unresolved {prior_desc} -- see logs/strategy.md"
    else:
        line = f"no activity items found since unresolved {prior_desc} (does not confirm nothing happened)"

    return record, line


def _await_indexed(*, wallet: str, policy_planets: list[int], target_block: int, max_wait_s: int) -> bool:
    """The mandatory post-receipt wait (docs/SPEC.md §5.7): polls a fresh snapshot's
    `latest_indexed_block` until it covers `target_block`, or `max_wait_s` elapses.
    Returns whether the index caught up in time."""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        snap = _fetch_snapshot(wallet, policy_planets)
        if snap is not None and snap.latest_indexed_block is not None and snap.latest_indexed_block >= target_block:
            return True
        time.sleep(_INDEX_POLL_INTERVAL_S)
    return False


# --------------------------------------------------------------------------------------
# The tick.
# --------------------------------------------------------------------------------------


def _resources_summary(snapshot: Snapshot, planet_id: int | None) -> str:
    planet = snapshot.planet(planet_id) if planet_id is not None else (snapshot.planets[0] if snapshot.planets else None)
    if planet is None:
        return "(no planet data)"
    r = planet.resources_as_of_now
    energy = f"{planet.energy.produced}/{planet.energy.required} (scale {planet.energy.scale_bps})" if planet.energy else "unknown"
    fields = f"{planet.fields_used}/{planet.fields_total}" if planet.fields_used is not None and planet.fields_total is not None else "unknown"
    return f"M {r.metal:,}  C {r.crystal:,}  D {r.deuterium:,}   | energy {energy} | fields {fields}"


def _queues_summary(snapshot: Snapshot, planet_id: int | None) -> str:
    planet = snapshot.planet(planet_id) if planet_id is not None else (snapshot.planets[0] if snapshot.planets else None)
    if planet is None:
        return "(no planet data)"
    from veydrift_agent.models import QueueKind

    parts = []
    for kind in (QueueKind.BUILDING, QueueKind.RESEARCH, QueueKind.SHIP, QueueKind.DEFENSE):
        entry = snapshot.research_queue if kind is QueueKind.RESEARCH else planet.queues.get(kind)
        parts.append(f"{kind.value} {'busy' if entry is not None else 'idle'}")
    return " · ".join(parts)


def _planet_line(snapshot: Snapshot, planet_id: int | None) -> str:
    planet = snapshot.planet(planet_id) if planet_id is not None else (snapshot.planets[0] if snapshot.planets else None)
    if planet is None:
        return "planet (unknown)"
    coords = f" ({planet.coordinates})" if planet.coordinates else ""
    return f"planet {planet.planet_id}{coords}"


def _proposal_lines(
    action: Action,
    guard_report: GuardReport,
    unsigned_tx: UnsignedTx | None,
    executed: bool,
    tier: Tier,
    *,
    send_outcome: str | None = None,
    confirm_hint: str | None = None,
) -> list[str]:
    verb = "EXECUTE" if executed else "PROPOSE"
    if action.kind in (ActionKind.NOOP, ActionKind.ESCALATE, ActionKind.HALT):
        return [f"{action.kind.value.upper():9s} {action.rationale}"]
    header = f"{verb:9s} {action.function}(planet={action.planet_id}, entity={action.entity_id})"
    lines = [header]
    if action.cost.metal or action.cost.crystal or action.cost.deuterium:
        lines.append(f"  cost:   M {action.cost.metal}  C {action.cost.crystal}  D {action.cost.deuterium}")
    lines.append(f"  why:    {action.rationale}")
    lines.append(f"  guards: {guard_report.passed}/{guard_report.total} pass ({guard_report.decision.value})")
    if unsigned_tx is not None:
        submitted = "" if executed else f" (NOT SUBMITTED -- tier {tier.value})"
        lines.append(f"  tx:     to {unsigned_tx.to}  data {unsigned_tx.data[:10]}...{submitted}")
    # Fix 2: a revert or an unresolved outcome must be surfaced prominently in the tick
    # report itself, not just buried in strategy.md/actions.jsonl.
    if send_outcome == "reverted":
        lines.append("  !! REVERTED on-chain -- gas was spent, no effect. See logs/actions.jsonl + logs/strategy.md.")
    elif send_outcome == "unknown":
        lines.append("  ?? outcome UNKNOWN -- could not confirm success/revert in time; NOT counted as executed.")
    elif send_outcome == "send_failed":
        lines.append("  !! walletctl send failed -- nothing was submitted. See logs/strategy.md.")
    # Fix 3: require_confirmation's printed hand-off command.
    if confirm_hint:
        lines.append(f"  {confirm_hint}")
    return lines


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="Path to policy.json (default: $VEYDRIFT_HOME/policy.json)."),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Build/plan/guard but never send. Always true at tier 1."),
    readiness: bool = typer.Option(False, "--readiness", help="Print promotion evidence instead of running a tick."),
    format: str = typer.Option("md", "--format", help="Report format: md or json."),
) -> None:
    """`vd tick [--policy PATH] [--dry-run] [--readiness] [--format md|json]` — run one
    tick (see this module's docstring for the 9-step contract), unless a subcommand
    (`init`) was given, in which case that runs instead and this callback is a no-op."""
    if ctx.invoked_subcommand is not None:
        return
    if readiness:
        _print_readiness()
        return

    policy_file = policy or policy_path()
    policy_model = _load_policy(policy_file)
    effective_dry_run = _effective_dry_run(policy_model, dry_run)

    try:
        with tick_lock():
            _run_tick(policy_model, effective_dry_run, format)
    except TickLockedError as exc:
        _console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0)


def _run_tick(policy_model: Policy, effective_dry_run: bool, format: str) -> None:
    now = datetime.now(UTC)
    agent_state = load_agent_state()
    # Captured BEFORE this tick's own state mutations -- describes what this tick should
    # check for human activity on, not what it's about to propose itself. Never consulted
    # on the killswitch path below (must never add a network call beyond /health there).
    previous_unresolved = agent_state.last_unresolved_onchain_proposal
    agent_state.touch(now=now)  # tick_count decision is deferred to _finish_tick's dedup check

    # Step 2: killswitch check -- ONE health call, nothing else, if active.
    if _killswitch_active():
        health_ok = _fetch_health_only()
        halted_snapshot = Snapshot(taken_at=now, wallet=policy_model.wallet, health_ok=health_ok)
        action = plan_mod.plan_next_action(halted_snapshot, policy_model, killswitch_active=True)
        guard_report = guard_mod.evaluate_guardrails(action, halted_snapshot, policy_model, agent_state, killswitch_active=True, now=now)
        _finish_tick(policy_model, agent_state, halted_snapshot, action, guard_report, None, executed=False, format=format, now=now)
        return

    # Step 3 (part 1) + Step 4: reconcile what we can before the snapshot, fetch it, then
    # finish reconciling against its indexed block (see _reconcile_pending's docstring).
    pending_before = agent_state.pending is not None
    if pending_before:
        _reconcile_pending(agent_state, indexed_block=None, now=now)

    snapshot = _fetch_snapshot(policy_model.wallet, policy_model.planets)
    if snapshot is None:
        _console.print("[red]tick aborted: could not fetch a snapshot (network/API failure before any usable response).[/red]")
        save_agent_state(agent_state)
        raise typer.Exit(code=3)

    pending_unreconciled = _reconcile_pending(agent_state, indexed_block=snapshot.latest_indexed_block, now=now)

    # Step 5: plan.
    action = plan_mod.plan_next_action(snapshot, policy_model, killswitch_active=False, pending_tx_unreconciled=pending_unreconciled)

    # Step 6: guard. Gather live-only facts ONLY when the action is on-chain -- an
    # off-chain action (noop/escalate/halt) needs none of this and triggers no extra
    # network calls, matching the same "no unnecessary network calls" posture the
    # killswitch path takes.
    unsigned_tx: UnsignedTx | None = None
    build_error: str | None = None
    live_addresses: set[str] | None = None
    eth_balance_wei: int | None = None
    built_tx_path: Path | None = None
    gas_cost_wei: int | None = None
    if action.is_onchain():
        unsigned_tx, gas_cost_wei, build_error, built_tx_path = _walletctl_build(action, provider=policy_model.wallet_engine.provider)
        live_addresses = _live_addresses()
        if policy_model.tier is not Tier.ADVISOR:
            eth_balance_wei = _walletctl_eth_balance_wei(provider=policy_model.wallet_engine.provider)

    guard_report = guard_mod.evaluate_guardrails(
        action,
        snapshot,
        policy_model,
        agent_state,
        killswitch_active=False,
        live_addresses=live_addresses,
        unsigned_tx=unsigned_tx,
        gas_cost_wei=gas_cost_wei,
        eth_balance_wei=eth_balance_wei,
        now=now,
    )
    if build_error:
        guard_report.verdicts.append(GuardVerdict(gate="walletctl_build", status=GuardStatus.ESCALATE, detail=build_error))
        if guard_report.decision is Decision.ALLOW:
            guard_report.decision = Decision.ESCALATE

    # Step 7: send, only if ALLOW, tier>=2, and not dry-run. Fix 3: `require_confirmation`
    # (default true) means tick builds/guards/proposes but stops short of sending -- a
    # human runs the printed `walletctl send ... --confirm` command themselves. This is
    # NOT an error path; the tick still ends cleanly with a full, honest report.
    executed = False
    send_outcome: str | None = None
    confirm_hint: str | None = None
    tx_hash: str | None = None
    can_send = (
        guard_report.decision is Decision.ALLOW
        and policy_model.tier is not Tier.ADVISOR
        and not effective_dry_run
        and unsigned_tx is not None
    )
    if can_send:
        if policy_model.wallet_engine.require_confirmation:
            confirm_hint = (
                f"AWAITING HUMAN CONFIRMATION (wallet_engine.require_confirmation=true) -- run: "
                f"walletctl send --tx {built_tx_path} --confirm"
            )
            log.append_strategy(
                f"tick {agent_state.tick_count}: built and guard-ALLOWed {guard_mod.idempotency_key(action)}, "
                f"but wallet_engine.require_confirmation is true so tick did not send it. {confirm_hint}",
                now=now,
            )
        else:
            executed, send_outcome, tx_hash = _send_and_await(
                policy_model, agent_state, action, unsigned_tx, snapshot, now, gas_cost_wei_estimate=gas_cost_wei
            )

    human_activity_record, human_activity_line = _maybe_check_human_activity(policy_model, previous_unresolved, now=now)

    _finish_tick(
        policy_model,
        agent_state,
        snapshot,
        action,
        guard_report,
        unsigned_tx,
        executed=executed,
        format=format,
        now=now,
        send_outcome=send_outcome,
        confirm_hint=confirm_hint,
        tx_hash=tx_hash,
        human_activity_record=human_activity_record,
        human_activity_line=human_activity_line,
    )


_RECEIPT_WAIT_S = 120  # bounded poll for a mined receipt with a known status; see _await_receipt


def _await_receipt(tx_hash: str, *, max_wait_s: int | None = None) -> dict[str, Any] | None:
    """Poll `walletctl receipt` until it returns a receipt carrying a `status`, or
    `max_wait_s` elapses. Returns the receipt dict only once `status` is present; `None`
    otherwise -- callers must treat `None` as an UNKNOWN outcome, never as success
    (Fix 2). `max_wait_s` defaults to the module-level `_RECEIPT_WAIT_S`, looked up at
    call time (not bound into the signature) so tests can shrink the poll window via
    `monkeypatch.setattr(tick, "_RECEIPT_WAIT_S", 0)` without needing to pass it
    explicitly through every caller."""
    if max_wait_s is None:
        max_wait_s = _RECEIPT_WAIT_S
    deadline = time.monotonic() + max_wait_s
    receipt = _walletctl_receipt(tx_hash)
    while (receipt is None or receipt.get("status") is None) and time.monotonic() < deadline:
        time.sleep(_INDEX_POLL_INTERVAL_S)
        receipt = _walletctl_receipt(tx_hash)
    return receipt if receipt is not None and receipt.get("status") is not None else None


def _send_and_await(
    policy_model: Policy,
    agent_state: AgentState,
    action: Action,
    unsigned_tx: UnsignedTx,
    snapshot: Snapshot,
    now: datetime,
    *,
    gas_cost_wei_estimate: int | None,
) -> tuple[bool, str, str | None]:
    """Step 7's send + status-await + indexed-wait. Returns `(executed, outcome,
    tx_hash)` where `outcome` is one of `"success"`, `"reverted"`, `"unknown"`, or
    `"send_failed"`. `executed` is True **only** for `"success"` -- Fix 2's core rule: a
    reverted or unknown-outcome send is never reported, or counted in
    `AgentState.executions_count`, as a success.

    Not exercised in this WP's own verification (no tier>=2 policy or wallet credentials
    were configured while building/testing this package) -- implemented to spec, guarded
    by the same ALLOW/tier/dry-run/require_confirmation checks the caller already
    applied.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd-tick-send-"))
    tx_file = tmp_dir / "tx.json"
    tx_file.write_text(
        json.dumps(
            {
                "to": unsigned_tx.to,
                "data": unsigned_tx.data,
                "value": str(unsigned_tx.value),
                "chainId": unsigned_tx.chain_id,
                "gas": str(unsigned_tx.gas) if unsigned_tx.gas else None,
                "purpose": action.rationale,
            }
        )
    )
    key = guard_mod.idempotency_key(action)
    tx_hash, error = _walletctl_send(tx_file, tier=policy_model.tier, provider=policy_model.wallet_engine.provider)
    if tx_hash is None:
        log.append_strategy(f"send failed for {key}: {error}", now=now)
        return False, "send_failed", None

    agent_state.pending = PendingTx(
        key=key, tx_hash=tx_hash, planet_id=action.planet_id, function=action.function, entity_id=action.entity_id, sent_at=now
    )

    receipt = _await_receipt(tx_hash)
    status = receipt.get("status") if receipt else None

    block = _parse_hex_or_int(receipt.get("blockNumber")) if receipt else None
    if block is not None:
        agent_state.pending.block = block
        agent_state.pending.receipt_at = datetime.now(UTC)

    # Fix 1 + Fix 2: charge what was ACTUALLY burned (receipt's actualCostWei, wei-scale)
    # when known; fall back to the pre-send wei estimate (also wei-scale, per Fix 1) only
    # when the actual isn't available yet. Never substitute an unmeasured 0.
    actual_cost_wei = _parse_wei_str(receipt.get("actualCostWei")) if receipt else None
    cost_to_charge = actual_cost_wei if actual_cost_wei is not None else gas_cost_wei_estimate
    if cost_to_charge is not None:
        agent_state.record_gas_spent(cost_to_charge, now=now)
        agent_state.pending.gas_wei = cost_to_charge  # marks "already charged" for _reconcile_pending

    common_log_fields = {
        "ts": now.isoformat(),
        "tx_hash": tx_hash,
        "function": action.function,
        "planet_id": action.planet_id,
        "entity_id": action.entity_id,
        "gas_wei": cost_to_charge or 0,
        "block": block,
    }

    if status == "reverted":
        agent_state.pending.reverted = True
        revert_count = agent_state.record_revert(key)
        agent_state.pending = None  # it already landed (reverted) -- nothing left to wait on
        log.append_strategy(
            f"REVERTED: {key} reverted on-chain (tx {tx_hash}); revert count {revert_count} "
            f"(escalation.on_revert_count={policy_model.escalation.on_revert_count}). Gas was still spent.",
            now=now,
        )
        # Fix 2: hiding a revert is worse than recording it -- still write to
        # actions.jsonl, explicitly marked, never silently counted as a success.
        log.log_action({**common_log_fields, "status": "reverted", "indexed": False})
        return False, "reverted", tx_hash

    if status == "success":
        agent_state.executions_count += 1
        indexed = False
        if block is not None:
            indexed = _await_indexed(
                wallet=policy_model.wallet, policy_planets=policy_model.planets, target_block=block, max_wait_s=policy_model.limits.max_index_wait_s
            )
            if indexed:
                agent_state.pending.indexed_at = datetime.now(UTC)
                agent_state.pending = None
        log.log_action({**common_log_fields, "status": "success", "indexed": indexed})
        return True, "success", tx_hash

    # status missing / receipt fetch failed / never resolved within _RECEIPT_WAIT_S --
    # unknown, NOT success (Fix 2's other rule: "if status is missing or the receipt
    # fetch fails, treat it as unknown, not success -- escalate rather than assume").
    # `agent_state.pending` is deliberately left in place: guard.py's `idempotency` gate
    # blocks a duplicate proposal for the same key while it's unresolved, and the next
    # tick's `_reconcile_pending` gets another chance to discover the real outcome.
    log.append_strategy(
        f"UNKNOWN outcome for {key} (tx {tx_hash}): could not confirm success/revert within "
        f"{_RECEIPT_WAIT_S}s. Treated as unresolved, NOT as success -- will be re-checked next tick.",
        now=now,
    )
    log.log_action({**common_log_fields, "status": "unknown", "indexed": False})
    return False, "unknown", tx_hash


_FINGERPRINT_EXCLUDED_KEYS = {"ts", "tick", "human_activity_check"}


def _fingerprint_proposal(record: dict[str, Any]) -> str:
    """Stable content fingerprint of a proposals.jsonl record, excluding `ts`/`tick` --
    the only two fields expected to differ between a genuine content-identical repeat and
    a first-time proposal -- and `human_activity_check`: that field describes a
    best-effort /activity lookup about a DIFFERENT (earlier, unresolved) proposal, not
    this one, and its content (since_ts, items found) legitimately varies tick to tick
    even when this tick's own proposed action is a genuine content-identical repeat of
    the last one. Including it would silently defeat dedup on every tick that has
    anything unresolved to check -- i.e. almost every tick at tier 1. `sort_keys=True`
    makes this order-independent even though `guard_verdicts` is already
    gate-order-deterministic; `default=str` covers any non-JSON-native value the same way
    `log.py`'s own serialisation would. Computed over the in-memory record, deliberately
    never over a re-read/re-parsed `proposals.jsonl` line -- `log.py`'s `_json_line`
    scrubs secrets/hex before writing, and comparing pre-scrub to post-scrub content
    risks a false match or mismatch from the scrub step itself."""
    comparable = {k: v for k, v in record.items() if k not in _FINGERPRINT_EXCLUDED_KEYS}
    return hashlib.sha256(json.dumps(comparable, sort_keys=True, default=str).encode()).hexdigest()


def _finish_tick(
    policy_model: Policy,
    agent_state: AgentState,
    snapshot: Snapshot,
    action: Action,
    guard_report: GuardReport,
    unsigned_tx: UnsignedTx | None,
    *,
    executed: bool,
    format: str,
    now: datetime,
    send_outcome: str | None = None,
    confirm_hint: str | None = None,
    tx_hash: str | None = None,
    human_activity_record: dict[str, Any] | None = None,
    human_activity_line: str | None = None,
) -> None:
    proposal_record = {
        "ts": now.isoformat(),
        "tick": agent_state.tick_count + 1,  # prospective; corrected below if not a duplicate
        "wallet": policy_model.wallet,
        "tier": policy_model.tier.value,
        "planet_id": action.planet_id,
        "rule": action.rule,
        "kind": action.kind.value,
        "function": action.function,
        "entity_id": action.entity_id,
        "entity_name": action.entity_name,
        "target_level": action.target_level,
        "quantity": action.quantity,
        "cost": action.cost.model_dump(),
        "rationale": action.rationale,
        "guard_decision": guard_report.decision.value,
        "guard_verdicts": [v.model_dump() for v in guard_report.verdicts],
        "tx": unsigned_tx.model_dump() if unsigned_tx else None,
        "tx_hash": tx_hash,  # Fix 6c: previously always None (`action.function and None`)
        "send_outcome": send_outcome,
        "executed": executed,
        "human_activity_check": human_activity_record,
    }

    # Dedup: a content-identical repeat of the immediately-previous logged proposal (e.g.
    # a human/agent re-running `vd tick` seconds later just to re-inspect a different
    # --format) is not new evidence -- see tick.py's module docstring and
    # docs/SPEC.md §5.7 step 8. Fingerprint excludes only `ts`/`tick`; everything else
    # (including wallet/tier, so a mid-session promotion still counts as a real change)
    # must match.
    fingerprint = _fingerprint_proposal(proposal_record)
    is_duplicate = agent_state.last_proposal_fingerprint is not None and fingerprint == agent_state.last_proposal_fingerprint

    duplicate_note: str | None = None
    if is_duplicate:
        duplicate_note = (
            f"duplicate of tick {agent_state.tick_count} -- content-identical to the last logged "
            "proposal (excl. ts/tick); not counted as a new tick, not written to "
            "proposals.jsonl/strategy.md."
        )
    else:
        agent_state.record_tick(now=now)
        agent_state.proposals_count += 1
        agent_state.last_proposal_fingerprint = fingerprint
        # This tick's own proposal becomes the NEXT tick's "unresolved" check target only
        # if it's on-chain and this tick itself did not execute it (tier 1, or
        # require_confirmation stopped the send -- confirm_hint is only ever set in that
        # branch). A guard-BLOCKed tier>=2 proposal never reaches a send decision at all,
        # so it falls through to the `else` (cleared) case, same as a noop/escalate.
        if action.is_onchain() and (policy_model.tier is Tier.ADVISOR or confirm_hint is not None):
            agent_state.last_unresolved_onchain_proposal = UnresolvedProposal(
                ts=now,
                planet_id=action.planet_id,
                function=action.function,
                entity_id=action.entity_id,
                entity_name=action.entity_name,
                target_level=action.target_level,
                quantity=action.quantity,
            )
        else:
            agent_state.last_unresolved_onchain_proposal = None
        proposal_record["tick"] = agent_state.tick_count
    save_agent_state(agent_state)

    block_text = log.format_tick_block(
        tick_number=agent_state.tick_count,
        taken_at=now,
        tier=policy_model.tier.value,
        planet_line=_planet_line(snapshot, action.planet_id),
        state_line=_resources_summary(snapshot, action.planet_id),
        queues_line=_queues_summary(snapshot, action.planet_id),
        incoming_line=f"{len(snapshot.incoming_fleets)} fleet(s)" if snapshot.incoming_fleets else "none",
        proposal_lines=_proposal_lines(
            action, guard_report, unsigned_tx, executed, policy_model.tier, send_outcome=send_outcome, confirm_hint=confirm_hint
        ),
        duplicate_of=duplicate_note,
        human_activity_line=human_activity_line,
    )

    if not is_duplicate:
        log.log_proposal(proposal_record)

    # Fix 5: a *structural* tier block (the ONLY reason decision != ALLOW is the `tier`
    # gate itself) is expected at every tick until the policy is promoted and carries no
    # information -- see guard.is_structural_tier_block's docstring. Logging it to
    # strategy.md every tick would drown the genuinely useful entries (a substantive
    # gate -- affordability/energy/gas/etc -- actually firing). The full verdict list,
    # including the tier verdict, still always goes to proposals.jsonl above (unless this
    # tick is itself a duplicate, in which case nothing goes to proposals.jsonl at all);
    # only the strategy.md narration is suppressed for the purely-structural or
    # duplicate case.
    non_passing = [(v.gate, v.status.value) for v in guard_report.verdicts if v.status is not GuardStatus.PASS]
    structural = guard_mod.is_structural_tier_block(non_passing)
    if (action.kind is ActionKind.ESCALATE or guard_report.decision is not Decision.ALLOW) and not structural and not is_duplicate:
        log.append_strategy(f"tick {agent_state.tick_count}: {action.rule} -- {action.rationale} (guard={guard_report.decision.value})", now=now)

    log.write_tick_markdown(block_text, taken_at=now)

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "tick": agent_state.tick_count,
                    "action": json.loads(action.model_dump_json()),
                    "guard": json.loads(guard_report.model_dump_json()),
                    "executed": executed,
                    "send_outcome": send_outcome,
                    "duplicate": is_duplicate,
                },
                indent=2,
            )
        )
    else:
        log.print_tick_report(block_text, tick_number=agent_state.tick_count)


def _print_readiness() -> None:
    """`vd tick --readiness`: promotion evidence (docs/SPEC.md §4), not a tick. Divergence
    between proposal and human action is only observable through what mechanically ends
    up in `actions.jsonl` -- a human who executes a T1 proposal by hand through
    `walletctl` directly, without ever recording it back into this tool, leaves no trace
    this command can see. That limitation is stated in the output itself, not hidden.

    `_maybe_check_human_activity` NARROWS this blind spot on a best-effort basis (raw
    `/wallet/{addr}/activity` items surfaced for a human to read, never a confirmed
    match) -- it does not close it. `human_activity_checked`/`human_activity_hits` below
    summarise what's already embedded in each `proposals.jsonl` entry, no new I/O.

    Fix 5: structural tier blocks (the `tier` gate BLOCKing alone, expected at every tick
    below the function's minimum tier) are counted and reported SEPARATELY from
    substantive guardrail fires -- see `guard.is_structural_tier_block`. Mixing the two
    would make `--readiness` look swamped by noise precisely when the real promotion
    signal (which substantive gates fired, and how often) is what matters most.
    """
    agent_state = load_agent_state()
    proposals = log.read_proposals()
    actions = log.read_actions()

    structural_tier_blocks = 0
    gate_fires: dict[str, int] = {}
    for p in proposals:
        non_passing = [(v.get("gate"), v.get("status")) for v in (p.get("guard_verdicts") or []) if v.get("status") != "pass"]
        if guard_mod.is_structural_tier_block(non_passing):
            structural_tier_blocks += 1
            continue
        for gate, status in non_passing:
            key = f"{gate}:{status}"
            gate_fires[key] = gate_fires.get(key, 0) + 1

    uptime = (
        (agent_state.last_tick_at - agent_state.first_tick_at) if agent_state.first_tick_at and agent_state.last_tick_at else None
    )
    gas_spent = sum(int(a.get("gas_wei") or 0) for a in actions)
    # Fix 2: actions.jsonl now records every real send attempt, including reverts and
    # unresolved outcomes, each explicitly tagged (`status`). A legacy entry from before
    # this fix has no `status` field at all -- it predates revert detection, and every
    # entry written back then really was an unconditional (mis-recorded) "success", so
    # treating a missing status as "success" here is a correct reading of old data, not a
    # new vacuous-pass.
    outcome_counts: dict[str, int] = {}
    for a in actions:
        outcome = a.get("status") or "success"
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    activity_checks = [p for p in proposals if (p.get("human_activity_check") or {}).get("checked")]
    human_activity_checked = len(activity_checks)
    human_activity_hits = sum(1 for p in activity_checks if (p.get("human_activity_check") or {}).get("items_found"))

    lines = [
        f"tick_count:        {agent_state.tick_count}",
        f"uptime:            {uptime}",
        f"proposals:         {len(proposals)}",
        f"executed:          {len(actions)} (by outcome: {', '.join(f'{k}={v}' for k, v in sorted(outcome_counts.items())) or 'none'})",
        f"divergence:        {len(proposals) - len(actions)} proposals with no matching actions.jsonl entry "
        "(NOTE: a human executing a T1 proposal by hand, outside this tool, is not observable here)",
        f"human_activity_checked: {human_activity_checked} tick(s) checked /wallet/{{addr}}/activity for evidence "
        f"of a human executing an unresolved T1/require-confirmation proposal by hand; {human_activity_hits} found "
        "≥1 raw item (titles only, see proposals.jsonl -- NOT a confirmed match to the proposal)",
        f"gas_spent_wei:     {gas_spent}",
        f"structural_tier_blocks: {structural_tier_blocks} (the `tier` gate BLOCKing alone -- expected below "
        "the function's minimum tier, NOT promotion evidence; see references/guardrails.md)",
        "guardrails_fired (substantive -- excludes the structural tier blocks counted above):",
    ]
    if gate_fires:
        for key, count in sorted(gate_fires.items()):
            lines.append(f"  {key}: {count}")
    else:
        lines.append("  (none)")
    typer.echo("\n".join(lines))


if __name__ == "__main__":
    app()
