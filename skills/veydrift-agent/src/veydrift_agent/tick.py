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

**`--action <file>` (step 5) substitutes a caller-supplied `Action` for the planner's own
choice, gated by `policy.strategy.allow_agent_action_override` (default `false`, refused
otherwise)** — every step after this one runs identically regardless of which one fired,
including the full `guard.py` pipeline, the tier ceiling, `require_confirmation`, and this
docstring's own dedup/logging contract. See `references/manual-action-override.md` and
`_load_override_action`/`_describe_override` below.

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

from veydrift_agent import alliance_ids, guard as guard_mod
from veydrift_agent import http, ids, log, read
from veydrift_agent import plan as plan_mod
from veydrift_agent.models import (
    Action,
    ActionKind,
    AllianceDirectoryEntry,
    AllianceJoinRequestForOwner,
    AllianceMember,
    AllianceMembership,
    AlliancePendingInvite,
    AlliancePendingJoinRequest,
    AllianceState,
    Decision,
    GameMaintenance,
    GuardReport,
    GuardStatus,
    GuardVerdict,
    PlanetSnapshot,
    Policy,
    RandomnessReadiness,
    Resources,
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
#: Phase 5 (docs/SPEC.md §5.4): how long past `arrivalAt` an own outbound mission must
#: sit before `_resolvable_mission_ids` proposes resolving it -- matches plan.py's rung-3
#: docstring ("mission Resolving > 60s"), a small grace window so the ladder doesn't race
#: a transaction that would revert because it lands the same second as arrival.
_RESOLVE_GRACE_S = 60
#: Commit 6 of the launch-actions plan: `read.fetch_highscores`'s `page_size` for
#: `_attack_targets`. Deliberately small -- `references/api-routes.md` §3.18's ~2.2 MB
#: warning is for the default `pageSize=50` across all 8 categories; this codebase only
#: ever reads one category's rows (`rankings["economy"]`), but the response still carries
#: all 8 regardless of the `category` query param, so a small page size keeps the
#: response bounded rather than relying on the unused categories being cheap to ignore.
_ATTACK_TARGET_PAGE_SIZE = 25


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
    """Fix 4 (pre-Phase-5c) warned that `actions.allow_fleet_noncombat` was dead config --
    the planner had no fleet rung yet. Phase 5c (docs/SPEC.md §5.4) gave it one
    (`plan.py`'s band 5, `candidates.select_logistics_candidate`), so the key is live now
    and this function has nothing left to warn about. Kept as a hook (rather than deleted
    outright) for the same reason it existed in the first place: a future dead policy key
    should surface here, at load time, the same way `allow_ships` and
    `allow_fleet_noncombat` each did before they grew a rung -- `cli.py`'s `doctor`
    command is frozen and cannot be extended to report this instead."""
    return


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


#: Both overloaded forms of `launchFleetMission` on the deployed ABI (AGENTS.md §7 trap
#: #2). Must match `veydrift-wallet/src/allowlist.ts`'s `LAUNCH_FLEET_MISSION_SIGNATURES`
#: character-for-character -- these are what disambiguates the overload for
#: `veydrift-wallet/src/tx.ts`'s `buildTx` (`resolveFunctionAbi` throws on the bare name
#: "launchFleetMission" because it is ambiguous on the pinned ABI; a full signature is the
#: only way to select one). Confirmed against the deployed contract source directly:
#: `VeydriftGameplayModule.sol:44-59` (6-arg, no `speedPercent` -- hardcodes
#: `FULL_MISSION_SPEED_PERCENT`) and `:63-79` (7-arg, explicit `speedPercent`).
_LAUNCH_FLEET_MISSION_7ARG_SIGNATURE = (
    "launchFleetMission(uint256,uint256,uint8,"
    "(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),"
    "(uint128,uint128,uint128),uint16,uint256)"
)
_LAUNCH_FLEET_MISSION_6ARG_SIGNATURE = (
    "launchFleetMission(uint256,uint256,uint8,"
    "(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),"
    "(uint128,uint128,uint128),uint256)"
)

#: The Colonize-only `targetPlanetId` encoding. Confirmed directly against
#: `VeydriftColonizationModule.sol:42-46,472-479` (`_encodeColonyTarget`) -- a new colony
#: has no planet id yet, so this argument carries `(galaxy, system, position)` packed into
#: a `uint256` with a high-bit flag instead, decoded again on resolution via
#: `_decodeColonyTarget`. See docs/RESEARCH-ADDENDUM.md §4 / references/contract-writes.md
#: §1.2.
_COLONIZATION_COORDINATE_FLAG = 1 << 255
_COLONIZATION_GALAXY_SHIFT = 24
_COLONIZATION_SYSTEM_SHIFT = 8

#: Field widths `_decodeColonyTarget` masks against
#: (`VeydriftColonizationModule.sol:42-46,482-492`, pinned commit 701bed35):
#: ``COLONIZATION_COORDINATE_MASK = 0xffff`` for both galaxy and system (each packed as a
#: `uint16`), ``COLONIZATION_POSITION_MASK = 0xff`` for position (packed as a `uint8`
#: occupying the low byte directly, not shifted). Verified directly against the pinned
#: source (judge finding 2, 2026-08-17), not merely trusted from a brief.
_COLONIZATION_GALAXY_MAX = 0xFFFF
_COLONIZATION_SYSTEM_MAX = 0xFFFF
_COLONIZATION_POSITION_MAX = 0xFF


def _encode_colony_target(coordinates: str) -> int:
    """Judge finding 2 (2026-08-17): this function had no bounds check. A galaxy/system/
    position value outside the widths above does not raise on-chain -- there is no
    Solidity call in this path, this function IS the encoder -- it silently collides with
    an adjacent field's bits during the final `|`, producing a *different, still-valid-
    looking* packed target instead of an error. Confirmed: `_encode_colony_target(
    "1:2:300")` previously returned a value that `_decodeColonyTarget` reads back as
    galaxy 1, system 3, position 44 (position's low-byte overflow adds 1 to system) --
    the corrupted target is itself in-range, so gas estimation and both allowlist layers
    (which check only mission type, never the packed coordinate) would pass, and a real
    Colony Ship would launch at the wrong slot. Now raises loudly on any out-of-range
    field or malformed 'G:S:P' string, consistent with how this encoder already raises on
    a missing `mission_type` elsewhere in this module."""
    parts = coordinates.split(":")
    if len(parts) != 3:
        raise ValueError(f"_encode_colony_target: {coordinates!r} is not a 'G:S:P' coordinate string")
    try:
        galaxy, system, position = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"_encode_colony_target: {coordinates!r} is not a 'G:S:P' coordinate string") from exc
    if not (0 <= galaxy <= _COLONIZATION_GALAXY_MAX):
        raise ValueError(
            f"_encode_colony_target: galaxy {galaxy} out of range [0, {_COLONIZATION_GALAXY_MAX}] for {coordinates!r}"
        )
    if not (0 <= system <= _COLONIZATION_SYSTEM_MAX):
        raise ValueError(
            f"_encode_colony_target: system {system} out of range [0, {_COLONIZATION_SYSTEM_MAX}] for {coordinates!r}"
        )
    if not (0 <= position <= _COLONIZATION_POSITION_MAX):
        raise ValueError(
            f"_encode_colony_target: position {position} out of range [0, {_COLONIZATION_POSITION_MAX}] for {coordinates!r}"
        )
    return (
        _COLONIZATION_COORDINATE_FLAG
        | (galaxy << _COLONIZATION_GALAXY_SHIFT)
        | (system << _COLONIZATION_SYSTEM_SHIFT)
        | position
    )


def _ship_counts_to_fleet_tuple(ships: dict[int, int]) -> list[int]:
    """`Action.ships` (Ship id -> count, `models.py`'s deliberate sparse-map shape) -> the
    14-slot tuple `launchFleetMission` expects, in `ids.FLEET_TUPLE_ORDER` -- the same
    shifted order `veydrift-wallet`'s `shipCountsToFleetTuple()` (`fleet.ts`) produces
    (AGENTS.md §7 trap #1: Destroyer sits at tuple index 9, not 10). Mirrors that
    function's own refusal: a non-flyable ship id (SolarSatellite, Crawler) present in
    `ships` -- even at count 0 -- raises, the same "a caller who put one there thinks it
    belongs in a fleet" reasoning `fleet.ts`'s own docstring gives."""
    for ship_id, count in ships.items():
        if ship_id in ids.NON_FLYABLE_SHIPS:
            raise ValueError(
                f"_ship_counts_to_fleet_tuple: Ship id {ship_id} ({ids.ship_name(ship_id)}) cannot "
                "fly and has no slot in the 14-slot fleet tuple"
            )
        if ship_id not in ids.FLEET_TUPLE_ORDER:
            raise ValueError(f"_ship_counts_to_fleet_tuple: unknown Ship id {ship_id}")
        if count < 0:
            # Mirrors fleet.ts's negative-count rejection (also-worth-fixing #1, judge
            # review 2026-08-17) -- before this fix, fleet.ts raised on `count < 0` but
            # this function did not, so the two encoders could disagree on the same
            # malformed input.
            raise ValueError(f"_ship_counts_to_fleet_tuple: negative count for {ids.ship_name(ship_id)}")
    return [ships.get(int(ship_id), 0) for ship_id in ids.FLEET_TUPLE_ORDER]


def _resolve_target_planet_id(action: Action, snapshot: Snapshot) -> int:
    """The real on-chain `targetPlanetId` a non-Colonize `launchFleetMission` needs.
    `VeydriftGameplayModule.sol`'s `_launchFleetMission` requires
    `_planets[targetPlanetId].owner != address(0)` (and, for Transport/Deploy,
    `_requirePlanetOwner(targetPlanetId)`) -- an actual planet id, never coordinates.

    Three sources, in priority order:

    1. `action.target_planet_id`, when set -- commit 3 of the launch-actions plan, for a
       foreign target (`generate_foreign_harvest_candidates`). The generator that set it
       already knows the real id from its own data source (`/raid-finder/debris`, not
       `Snapshot`), so this is used directly with no lookup.
    2. `action.target_coordinates`, resolved against the wallet's own planets in
       `snapshot` -- the shape every mission against an owned planet uses
       (`Action.target_coordinates` only ever carries a `"G:S:P"` string; `models.py`
       frozen for this phase): Transport between own planets, Deploy (once wired), and
       Colonize's target isn't reached here at all (its own packed-coordinate encoding,
       see `_encode_colony_target`).
    3. Neither set: the local-Harvest special case, where the contract's own rule
       (`originPlanetId == targetPlanetId && missionType == Harvest`) makes
       `origin_planet_id` the correct target directly."""
    if action.target_planet_id is not None:
        return action.target_planet_id
    if action.target_coordinates is None:
        if action.mission_type == ids.FleetMissionType.HARVEST and action.origin_planet_id is not None:
            return action.origin_planet_id
        raise ValueError("launchFleetMission action has no target_coordinates")
    for planet in snapshot.planets:
        if planet.coordinates == action.target_coordinates:
            return planet.planet_id
    raise ValueError(
        "tick.py can only resolve a launchFleetMission target among the wallet's own "
        f"planets in the snapshot; no planet at {action.target_coordinates!r} was found, "
        "and action.target_planet_id was not set for a foreign target"
    )


def _fleet_mission_args(action: Action, snapshot: Snapshot) -> tuple[str, list[Any]]:
    """`(signature, args)` for a `launchFleetMission` `Action` -- resolves the overload
    (Trap #2) and the target-planet-id encoding (Colonize's packed-coordinate special
    case vs. every other mission type's real planet id) together, since both depend on
    `action.mission_type`.

    `speed_pct is None` selects the 6-arg overload (the contract's own default,
    `FULL_MISSION_SPEED_PERCENT` == 100 -- `VeydriftGameplayModule.sol:44-59`) rather than
    this encoder substituting `100` itself: `models.py`'s own comment on `speed_pct`
    ("never silently substitute a default at the encoder") is honoured by *choosing the
    overload that omits the argument*, not by inventing a value for it.

    The trailing `uint256` both overloads share is `randomnessRequestId` in the deployed
    source (`VeydriftGameplayModule.sol`/`VeydriftColonizationModule.sol`) -- confirmed
    directly. It is only ever meaningfully set by the contract itself, for `Attack`
    (`_requestAttackBattleRandomness`) and for the two counterplay mission types
    (AcsDefend/Intercept, neither reachable from this codebase). For every mission type
    this codebase can produce, the contract either ignores the caller-supplied value
    (Transport/Deploy/Harvest) or requires it to be exactly `0`
    (`VeydriftColonizationModule.sol`'s `_launchColonizeFleetMission`: `if
    (randomnessRequestId != 0) revert InvalidId();`) -- so
    `action.randomness_request_id` is encoded as-is (defaulting to `0`, never fabricated)
    and is expected to always be `None`/unset from every generator this codebase ships
    today. The field was briefly named `holding_seconds` on a guess about its meaning;
    it was renamed once the source was read, so that nobody sets a duration here and hits
    Colonize's revert.
    """
    if action.mission_type is None:
        raise ValueError("launchFleetMission action has no mission_type")
    ships_tuple = _ship_counts_to_fleet_tuple(action.ships)
    cargo_tuple = [action.cargo.metal, action.cargo.crystal, action.cargo.deuterium]
    trailing = int(action.randomness_request_id or 0)

    if action.mission_type == ids.FleetMissionType.COLONIZE:
        if action.target_coordinates is None:
            raise ValueError("launchFleetMission Colonize action has no target_coordinates")
        target_planet_id = _encode_colony_target(action.target_coordinates)
    else:
        target_planet_id = _resolve_target_planet_id(action, snapshot)

    origin = action.origin_planet_id
    if origin is None:
        raise ValueError("launchFleetMission action has no origin_planet_id")

    if action.speed_pct is not None:
        args = [origin, target_planet_id, int(action.mission_type), ships_tuple, cargo_tuple, action.speed_pct, trailing]
        return _LAUNCH_FLEET_MISSION_7ARG_SIGNATURE, args
    args = [origin, target_planet_id, int(action.mission_type), ships_tuple, cargo_tuple, trailing]
    return _LAUNCH_FLEET_MISSION_6ARG_SIGNATURE, args


def _require(value: object, message: str) -> object:
    """Raise `ValueError(message)` if `value` is `None` -- shared by every alliance
    arg-builder below so a missing required field fails loudly at build time rather than
    encoding `None`/`null` into calldata."""
    if value is None:
        raise ValueError(message)
    return value


#: Alliance feature, commit 4. One positional-arg builder per in-scope
#: `VeydriftAllianceSystem` function, keyed by function name -- `_action_to_walletctl_json`
#: dispatches through this dict rather than 15 more `if fn == "...":` branches inline,
#: since every builder here is a one-liner and the dict keeps the dispatch table itself
#: readable as a single unit (which functions exist, at a glance) separately from each
#: one's own argument-order derivation.
_ALLIANCE_ARG_BUILDERS: dict[str, Any] = {
    "createAlliance": lambda a: [
        _require(a.alliance_tag, "createAlliance action has no alliance_tag"),
        _require(a.alliance_name, "createAlliance action has no alliance_name"),
        _require(a.alliance_description, "createAlliance action has no alliance_description"),
    ],
    "updateAllianceProfile": lambda a: [
        _require(a.alliance_id, "updateAllianceProfile action has no alliance_id"),
        _require(a.alliance_tag, "updateAllianceProfile action has no alliance_tag"),
        _require(a.alliance_name, "updateAllianceProfile action has no alliance_name"),
        _require(a.alliance_description, "updateAllianceProfile action has no alliance_description"),
    ],
    "inviteMember": lambda a: [
        _require(a.alliance_id, "inviteMember action has no alliance_id"),
        _require(a.target_player, "inviteMember action has no target_player"),
    ],
    "cancelInvite": lambda a: [
        _require(a.alliance_id, "cancelInvite action has no alliance_id"),
        _require(a.target_player, "cancelInvite action has no target_player"),
    ],
    "acceptInvite": lambda a: [_require(a.alliance_id, "acceptInvite action has no alliance_id")],
    "requestJoinAlliance": lambda a: [_require(a.alliance_id, "requestJoinAlliance action has no alliance_id")],
    "cancelJoinRequest": lambda a: [_require(a.alliance_id, "cancelJoinRequest action has no alliance_id")],
    "dismissJoinRequest": lambda a: [
        _require(a.alliance_id, "dismissJoinRequest action has no alliance_id"),
        _require(a.target_player, "dismissJoinRequest action has no target_player"),
    ],
    "approveJoinRequest": lambda a: [
        _require(a.alliance_id, "approveJoinRequest action has no alliance_id"),
        _require(a.target_player, "approveJoinRequest action has no target_player"),
    ],
    "kickMember": lambda a: [
        _require(a.alliance_id, "kickMember action has no alliance_id"),
        _require(a.target_player, "kickMember action has no target_player"),
    ],
    "kickMembers": lambda a: [
        _require(a.alliance_id, "kickMembers action has no alliance_id"),
        _require(a.target_players or None, "kickMembers action has no target_players"),
    ],
    "leaveAlliance": lambda a: [],
    "setMemberRole": lambda a: [
        _require(a.alliance_id, "setMemberRole action has no alliance_id"),
        _require(a.target_player, "setMemberRole action has no target_player"),
        _require(a.role, "setMemberRole action has no role"),
    ],
    "setMembersRole": lambda a: [
        _require(a.alliance_id, "setMembersRole action has no alliance_id"),
        _require(a.target_players or None, "setMembersRole action has no target_players"),
        _require(a.role, "setMembersRole action has no role"),
    ],
    "transferAllianceOwnership": lambda a: [
        _require(a.alliance_id, "transferAllianceOwnership action has no alliance_id"),
        _require(a.target_player, "transferAllianceOwnership action has no target_player"),
    ],
}


def _action_to_walletctl_json(action: Action, snapshot: Snapshot | None = None) -> dict[str, Any]:
    """`Action` (this package's pydantic model) -> the `{function, args, purpose}` shape
    `walletctl build --action` expects (`veydrift-wallet/src/tx.ts`'s `Action` interface).
    Positional `args` match the ABI's declared input order exactly.

    `snapshot` is only required for `launchFleetMission` and (commit 7)
    `launchInterplanetaryMissileAttack` (both resolve a target via
    `_resolve_target_planet_id`) -- every other branch ignores it, so callers building a
    non-fleet, non-missile action (still the vast majority in this codebase today) need
    not supply one."""
    fn = action.function
    if fn == "startBuildingUpgrade" or fn == "startResearch":
        args = [action.planet_id, action.entity_id]
        return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200]}
    if fn == "startShipProduction" or fn == "startDefenseProduction":
        args = [action.planet_id, action.entity_id, action.quantity or 0]
        return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200]}
    if fn == "resolveFleetMission":
        args = [action.mission_id]
        return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200]}
    if fn == "launchFleetMission":
        if snapshot is None:
            raise ValueError("tick.py needs a Snapshot to build launchFleetMission calldata")
        signature, args = _fleet_mission_args(action, snapshot)
        return {"function": signature, "args": args, "purpose": (action.rationale or "")[:200]}
    if fn == "launchInterplanetaryMissileAttack":
        # Commit 7 of the launch-actions plan. Not overloaded on the deployed ABI -- the
        # bare name resolves unambiguously (`abi.ts`'s `resolveFunctionAbi`), the same
        # posture every other non-overloaded function branch above already takes; only
        # `launchFleetMission` needs the full-signature dance (AGENTS.md §7 trap #2).
        # Shares nothing else with the fleet-mission path: no fleet tuple, no mission
        # type, fully synchronous -- (origin, target, primaryTarget, quantity) directly.
        if snapshot is None:
            raise ValueError("tick.py needs a Snapshot to build launchInterplanetaryMissileAttack calldata")
        if action.origin_planet_id is None:
            raise ValueError("launchInterplanetaryMissileAttack action has no origin_planet_id")
        if action.primary_target is None:
            raise ValueError("launchInterplanetaryMissileAttack action has no primary_target")
        target_planet_id = _resolve_target_planet_id(action, snapshot)
        args = [action.origin_planet_id, target_planet_id, int(action.primary_target), action.quantity or 0]
        return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200]}
    if fn in _ALLIANCE_ARG_BUILDERS:
        # Alliance feature, commit 4. `VeydriftAllianceSystem` -- a wholly separate
        # contract, its own address, its own pinned ABI. Every branch below sets
        # `"contract": "alliance"`, the field `veydrift-wallet`'s `buildTx` reads to
        # resolve both the ABI and the destination address (defaults to "game" when
        # absent, so every non-alliance branch above is unaffected). None of these 15
        # functions is overloaded, so a bare name resolves unambiguously, same posture
        # as `launchInterplanetaryMissileAttack` above.
        args = _ALLIANCE_ARG_BUILDERS[fn](action)
        return {"function": fn, "args": args, "purpose": (action.rationale or "")[:200], "contract": "alliance"}
    # `settlePlanet` had a branch here through Phase 4. Removed in Phase 5
    # (docs/SPEC.md §5.4/§9): its body at the pinned commit
    # (`_touchPlayer(msg.sender); _collectPlanetResources(planetId);`) is byte-identical
    # to `collectResources`, which `veydrift-wallet/src/abi.ts`'s
    # `NONPAYABLE_READ_FUNCTIONS` already refuses in `sendTx` as a disguised read. No
    # planner rung ever produced this action -- it was allowlisted capacity that could
    # only ever burn gas for zero effect. See guard.py's `_MIN_TIER_FOR_FUNCTION` and
    # veydrift-wallet's `allowlist.ts` `ECONOMY_SIGNATURES`, both updated in the same
    # change.
    raise ValueError(f"tick.py does not know how to build calldata for function {fn!r}")


def _walletctl_build(
    action: Action, *, provider: str, snapshot: Snapshot | None = None
) -> tuple[UnsignedTx | None, int | None, str | None, Path | None]:
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
        action_file.write_text(json.dumps(_action_to_walletctl_json(action, snapshot)))
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


def _parse_walletctl_status_lines(stdout: str) -> tuple[int | None, str | None]:
    """Parses `walletctl status`'s plain-text stdout for both the `balance:` (-> wei) and
    `address:` lines in a single pass -- shared by `_walletctl_eth_balance_wei` (wei only,
    kept for its existing external contract/tests) and `_walletctl_status` (both, added
    for the simulate fix below) so the two never drift on the same text, and so a caller
    that needs the wallet address does not have to shell out to `status` a second time in
    the same tick when the balance was already fetched from it."""
    balance_wei: int | None = None
    address: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("balance:"):
            try:
                eth_str = stripped.split(":", 1)[1].strip().split(" ")[0]
                balance_wei = int(round(float(eth_str) * 10**18))
            except (ValueError, IndexError):
                balance_wei = None
        elif stripped.startswith("address:"):
            addr = stripped.split(":", 1)[1].strip()
            address = addr or None
    return balance_wei, address


def _walletctl_eth_balance_wei(*, provider: str) -> int | None:
    """Balance-only wrapper over the same `walletctl status` parse `_walletctl_status`
    does. **`_run_tick` no longer calls this** -- it calls `_walletctl_status` instead, to
    get the balance and the address from one subprocess rather than two (1.1.1, the
    simulate fix). Kept as the narrow, independently-testable unit for the balance parse
    itself; the value it returns still feeds `guard.py`'s `eth_floor` gate via
    `_walletctl_status`, which treats `None` as "cannot verify", never as "the wallet has
    enough ETH"."""
    try:
        result = _run_walletctl("status", "--provider", provider, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    balance_wei, _address = _parse_walletctl_status_lines(result.stdout)
    return balance_wei


def _walletctl_status(*, provider: str, timeout: int = 30) -> tuple[int | None, str | None]:
    """`(eth_balance_wei, address)` from a single `walletctl status` call -- both `None`
    on any failure (unreachable, non-zero exit), never raises. This is `_run_tick`'s only
    call site for `walletctl status`: it replaces the bare `_walletctl_eth_balance_wei`
    call there so the wallet address needed by `_walletctl_simulate`'s mandatory `--from`
    (see its docstring) comes from the SAME subprocess call already being made for the
    `eth_floor` guard gate, rather than a second one."""
    try:
        result = _run_walletctl("status", "--provider", provider, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0:
        return None, None
    return _parse_walletctl_status_lines(result.stdout)


def _walletctl_simulate(
    tx_path: Path, *, address: str | None, timeout: int = 60
) -> tuple[bool | None, str | None, str | None]:
    """`walletctl simulate --tx <file> --from <address>` -- the free `eth_call` +
    `estimateGas` pre-flight that `_send_and_await` now runs before every real send. This
    closes the defect this fix exists for: previously nothing under `src/` ever called
    `simulate`, so a transaction that would revert on-chain burned real gas to find that
    out instead of a free RPC call (reproduced live on an Anvil fork of Base sending
    `startResearch(664, 0)`: `simulate` reported `ok: false`, revert
    `InsufficientResources(6798, 1874, 4444)`; `send` submitted it anyway and the receipt
    came back `status: "reverted"`).

    Returns `(ok, revert_reason, error)`:
    - `ok is True` -- simulate ran and reports the tx would succeed. Safe to send.
    - `ok is False` -- simulate ran and reports it would revert; `revert_reason` carries
      the decoded reason the CLI printed (e.g. `InsufficientResources(6798, 1874, 4444)`).
    - `ok is None` -- simulate could not be run or its output could not be parsed at all
      (`walletctl` unreachable, timed out, no wallet address, or a non-zero exit with no
      parseable `ok:` line). Callers MUST treat this the same as `ok is False` for the
      purpose of blocking a send -- "could not verify" is never "fine, proceed" (AGENTS.md
      §5's fail-closed rule for a guardrail on absent data; this is that same rule applied
      to the pre-send check). `error` carries a human-readable reason for this case only.

    **No `--provider` flag** -- confirmed against the real CLI (`veydrift-wallet/src/
    cli.ts`'s `simulate` command only declares `--tx`/`--from`) and by running it against
    a local Anvil fork: omitting `--from` simulates from a default address and fails
    `NotPlanetOwner()` rather than reflecting the real caller. A missing `address` is
    therefore treated here as an immediate fail-closed *without* shelling out -- that
    failure mode is already known well enough that spending a subprocess call to
    rediscover it adds nothing.

    Output is plain text, not JSON (confirmed against the same source: `console.log`
    lines, not `JSON.stringify`) -- parsed defensively, the same posture
    `_walletctl_eth_balance_wei` already takes toward `walletctl status`'s plain text.
    Success prints `ok:            true` (plus gas/cost lines this function does not
    need); failure prints `ok:            false` then `revert reason: <reason>` and exits
    non-zero -- but this function reads `stdout` for the `ok:`/`revert reason:` lines
    directly rather than trusting the exit code, since a reported revert is an expected,
    meaningful simulate outcome (`ok is False`), not a tooling failure (`ok is None`)."""
    if not address:
        return (
            None,
            None,
            (
                "no wallet address available to simulate from (walletctl simulate --from is "
                "mandatory; without it, simulate runs against a default address and fails "
                "NotPlanetOwner instead of reflecting the real sender)"
            ),
        )
    try:
        result = _run_walletctl("simulate", "--tx", str(tx_path), "--from", address, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, f"walletctl simulate could not be run: {exc}"

    ok: bool | None = None
    revert_reason: str | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("ok:"):
            value = stripped.split(":", 1)[1].strip().lower()
            if value in ("true", "false"):
                ok = value == "true"
        elif lowered.startswith("revert reason:"):
            revert_reason = stripped.split(":", 1)[1].strip()

    if ok is None:
        detail = (result.stderr or result.stdout).strip()[:500]
        return None, None, f"walletctl simulate produced no parseable 'ok:' line: {detail or '(empty output)'}"
    return ok, revert_reason, None


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
    # allianceContractAddress added alliance feature commit 4 -- required, not optional:
    # without it, guard._gate_address spuriously BLOCKs every alliance action, since its
    # destination is a genuinely different address from the game contract's.
    addresses = {
        a
        for a in (
            config.get("gameContractAddress"),
            config.get("contractAddress"),
            config.get("allianceContractAddress"),
        )
        if a
    }
    return addresses or None


# --------------------------------------------------------------------------------------
# Snapshot acquisition — calls straight into `read.snapshot`, WP1's own composed-snapshot
# command, rather than duplicating its HTTP/parsing logic here. `out=` is used (rather
# than `--json` to stdout) so the health-not-ok path -- which still writes the file before
# raising `typer.Exit(2)` -- is captured too; plan.py's own rung 1 is what actually acts
# on `Snapshot.health_ok`, not an exception here.
# --------------------------------------------------------------------------------------


def _fetch_snapshot(
    wallet: str, policy_planets: list[int], *, universe_cadence_hours: float | None = None
) -> Snapshot | None:
    """`universe_cadence_hours` (Phase 5, docs/SPEC.md §5.4) is forwarded straight to
    `read.snapshot`'s own opt-in flag -- `None` here (the default, used by
    `_await_indexed`'s polling loop, which only needs `latest_indexed_block`) skips the
    archetype enrichment fetch entirely; `_run_tick`'s own call passes
    `policy.cadence.universe_hours`, so the disk-cache TTL that flag drives (not a new
    timer) is what keeps a 10-minute tick cadence from re-hitting `/universe/*` every
    tick -- see `read._universe_archetype_for_planet`'s docstring."""
    planet_id = policy_planets[0] if len(policy_planets) == 1 else None
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd-tick-snapshot-"))
    out_file = tmp_dir / "snapshot.json"
    try:
        read.snapshot(
            wallet=wallet,
            planet_id=planet_id,
            json_output=False,
            out=out_file,
            max_age=None,
            universe_cadence_hours=universe_cadence_hours,
        )
    except typer.Exit:
        pass  # health-not-ok / bad-args paths still write `out_file` first when they can
    if not out_file.exists():
        return None
    return Snapshot.model_validate(json.loads(out_file.read_text()))


def _fetch_health_only() -> tuple[bool, bool, GameMaintenance | None, list[str], bool, RandomnessReadiness | None]:
    """Used only on the killswitch path (step 2): the ONE network call allowed before a
    halt (acceptance criterion: "halts before any network call beyond health"). Shares
    `read._health_ok`/`read._game_maintenance`/`read._randomness_readiness` rather than
    re-implementing the same parsing here -- this codebase has an explicit cautionary
    tale (AGENTS.md §5) about two independent implementations of the same check drifting
    apart. Also shares `read._recover_health_body` for the same reason a 5xx `/health`
    body gets recovered on the main tick path -- functionally inert here (rung 0 always
    wins under killswitch_active=True regardless of health), this is audit-record
    honesty for the halted Snapshot, not a behaviour change."""
    try:
        data = http.fetch("/health")
    except http.VeydriftServerError as exc:
        data = read._recover_health_body("/health", exc)
        if data is None:
            return False, False, None, [], False, None
    except http.VeydriftAPIError:
        return False, False, None, [], False, None
    health_ok = read._health_ok(data)
    game_paused, game_maintenance, degradation_reasons = read._game_maintenance(data)
    readiness_ready = (data.get("readiness") or {}).get("ready") is True
    randomness_readiness = read._randomness_readiness(data)
    return health_ok, game_paused, game_maintenance, degradation_reasons, readiness_ready, randomness_readiness


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


def _describe_override(
    override_action: Action,
    snapshot: Snapshot,
    policy_model: Policy,
    *,
    pending_tx_unreconciled: bool,
    resolvable_mission_ids: list[int],
    own_planet_debris: dict[int, Resources],
    foreign_debris_targets: dict[int, tuple[str, Resources]],
    colonize_targets: list[tuple[str, int]],
    attack_targets: dict[int, tuple[str, Resources, bool | None]],
    missile_targets: dict[int, tuple[str, dict[int, int], bool | None]],
) -> tuple[dict[str, Any], str]:
    """`(override_record, override_line)` for a `vd tick --action`-supplied action --
    the code-enforced disagreement record `references/manual-action-override.md`
    promises: the operator never has to hand-describe what the planner would have
    proposed instead, because this calls `plan_next_action` itself, with the exact same
    inputs `_run_tick`'s own planner branch would have used, purely for comparison. This
    is a pure, side-effect-free call over data already in hand (the fetched `snapshot`),
    so it costs nothing beyond the CPU time of a second scoring pass, and its result is
    never executed -- only recorded, in `proposals.jsonl` (`override_record`), `logs/
    strategy.md` and the printed tick report (`override_line`, both via `_finish_tick`).

    Never called on the killswitch path (`_run_tick`'s halted-snapshot branch) -- that
    path's own contract ("halts before any network call beyond /health") is unaffected by
    this, since `plan_next_action` here makes no network call, but there is nothing to
    compare against a halt in the first place, and `guard.py`'s `killswitch` gate BLOCKs
    any action unconditionally regardless of source."""
    planner_choice = plan_mod.plan_next_action(
        snapshot,
        policy_model,
        killswitch_active=False,
        pending_tx_unreconciled=pending_tx_unreconciled,
        resolvable_mission_ids=resolvable_mission_ids,
        own_planet_debris=own_planet_debris,
        foreign_debris_targets=foreign_debris_targets,
        colonize_targets=colonize_targets,
        attack_targets=attack_targets,
        missile_targets=missile_targets,
    )
    record = {
        "operator_action": {
            "rule": override_action.rule,
            "function": override_action.function,
            "rationale": override_action.rationale,
        },
        "planner_would_have_proposed": {
            "rule": planner_choice.rule,
            "kind": planner_choice.kind.value,
            "function": planner_choice.function,
            "rationale": planner_choice.rationale,
        },
    }
    line = (
        f"OVERRIDE: operator chose {override_action.rule or override_action.function or override_action.kind.value} "
        f"({override_action.rationale}) instead of the planner's "
        f"{planner_choice.rule or planner_choice.kind.value} ({planner_choice.rationale})."
    )
    return record, line


# --------------------------------------------------------------------------------------
# Phase 5 (docs/SPEC.md §5.4): revives plan.py's rung 3 (`resolveFleetMission`).
#
# `plan_next_action` has accepted `resolvable_mission_ids` since Phase 1 -- rung 3 was
# always implemented -- but nothing ever computed the argument, because `Snapshot`
# (models.py, frozen for this phase; see this WP's own report) has no field for the
# player's own outgoing/returning fleet missions. Rather than invent a home for that list
# on the frozen model, this reads `/wallet/{addr}/fleet-visibility` directly and works
# with the raw dict -- the exact same "bypass read.py's CLI layer, stay untyped" posture
# `_maybe_check_human_activity` already takes toward `/activity` for the same reason
# (reporting-only data that doesn't belong on the shared Action/Snapshot/GuardReport
# contract).
# --------------------------------------------------------------------------------------


def _resolvable_mission_ids(wallet: str) -> list[int]:
    """Which of the player's own `outgoing` fleet missions have been sitting at their
    target past `arrivalAt` for more than `_RESOLVE_GRACE_S` without being resolved yet.

    A mission qualifies when its `status` is still `"Outbound"` (the contract's
    `FleetMissionStatus` enum -- `VeydriftGameStorage.sol:179-186` -- has no "Resolving"
    member; an arrived-but-still-Outbound mission *is* what plan.py's rung-3 docstring
    calls "Resolving") **and** the API's own `needsResolution` flag is true **and**
    `arrivalAt` is more than `_RESOLVE_GRACE_S` seconds in the past. `needsResolution` is
    the API's general "arrived, not yet settled" signal (apps/backend/src/evm.ts's
    `FleetMissionSummary` -- the extra gate it documents for Attack's randomness
    fulfilment is additive, not the only condition it ever reports true for), checked
    together with -- not instead of -- the arrival-time math, since this codebase does not
    trust a single upstream flag for anything gate-adjacent without an independent check.

    Best-effort: never raises, degrades to `[]` (nothing resolvable this tick) on any
    fetch/parse failure. `resolveFleetMission` is a ladder optimisation (permissionless,
    free, and every gate downstream of it in guard.py still runs), not a safety-relevant
    input, so a failure here must not abort the tick the way a snapshot-fetch failure
    does."""
    try:
        data = read.fetch_fleet_visibility(wallet)
    except http.VeydriftAPIError:
        return []

    now = datetime.now(UTC)
    out: list[int] = []
    for item in data.get("outgoing") or []:
        if item.get("status") != "Outbound" or not item.get("needsResolution"):
            continue
        arrival_raw = item.get("arrivalAt")
        if arrival_raw is None:
            continue
        try:
            # The live API's own timestamp shape -- a decimal-string unix epoch, e.g.
            # "1786947731" (confirmed live 2026-08-17; see read._parse_datetime's
            # docstring for the same discrepancy against the synthetic ISO-format
            # fixtures elsewhere in this package).
            arrived_at = datetime.fromtimestamp(int(arrival_raw), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if (now - arrived_at).total_seconds() <= _RESOLVE_GRACE_S:
            continue
        mission_id = item.get("missionId")
        try:
            out.append(int(mission_id))
        except (TypeError, ValueError):
            continue
    return out


def _outgoing_colonize_count(wallet: str) -> int | None:
    """How many of the wallet's own outgoing fleet missions are an in-flight Colonize --
    `guard._colony_cap_violation`'s in-flight-mission blind spot, closed here (commit 4
    of the launch-actions plan). The cap check keys off `Snapshot.owned_planet_count`,
    which only reflects planets that have already resolved -- and Colonize's own
    `resolveFleetMission` re-check at arrival does *not* revert on failure
    (`VeydriftColonizationModule.sol:255-260` silently flips the mission to `Returning`
    instead), so two Colonize proposals on consecutive ticks could otherwise both pass
    the cap check and the second would silently bounce home with a `status: "success"`
    resolve receipt and no colony created.

    Reads the same `/wallet/{addr}/fleet-visibility` route `_resolvable_mission_ids`
    already reads, counting `outgoing` entries whose `missionType` wire string
    (`read.FLEET_MISSION_TYPE_IDS`, the same table `read._incoming_fleet` uses for the
    `incoming` side of this identical shape) resolves to `FleetMissionType.COLONIZE` and
    whose `status` is still `"Outbound"` -- a returned/resolved mission no longer counts
    against the cap.

    Returns `None` (never `0`) on any fetch/parse failure -- "unknown" must never be
    silently treated as "zero in flight," which would defeat the entire point of this
    check; `guard._colony_cap_violation` fails closed on `None` for exactly this
    reason."""
    try:
        data = read.fetch_fleet_visibility(wallet)
    except http.VeydriftAPIError:
        return None
    outgoing = data.get("outgoing")
    if not isinstance(outgoing, list):
        return None
    count = 0
    for item in outgoing:
        if not isinstance(item, dict) or item.get("status") != "Outbound":
            continue
        if read.FLEET_MISSION_TYPE_IDS.get(item.get("missionType")) == ids.FleetMissionType.COLONIZE:
            count += 1
    return count


def _own_planet_debris(snapshot: Snapshot) -> dict[int, Resources]:
    """Which of the wallet's own planets carry a non-empty debris field on their own slot
    -- `generate_harvest_candidates`'s `own_planet_debris` parameter
    (`candidates.py:2314`), dormant since Phase 5c because nothing supplied it (that
    function's own docstring: "no caller wires a live source for it yet"). Debris on an
    OWNED slot is real, not a contradiction -- a planet that lost ships in battle leaves
    debris at its own coordinates like any other slot; the contract's Harvest branch only
    ever checks the `DebrisField` mapping at the *target* planet id, regardless of who
    currently occupies it.

    Sourced from `/universe/galaxies/{g}/systems/{s}` (`read.fetch_universe_system`) --
    confirmed to carry a genuinely populated `debrisField` per slot (references/api-
    routes.md §3.16, 2026-08-27: `{"metal": "2400", "crystal": "2400"}` at a real,
    occupied slot) -- grouped by (galaxy, system) so a multi-planet wallet with planets
    sharing a system fetches that system only once. Deliberately NOT
    `/raid-finder/debris`: that route takes no wallet parameter, is independently
    confirmed to omit at least one indexed debris field (its own
    `indexer.indexedDebrisFields` outnumbers `targets`), and its filtering criteria are
    undocumented -- sourcing owned-planet debris from a route that might already exclude
    owned planets would make this rung silently never fire, the vacuous-pass-on-absent-
    data failure mode AGENTS.md §5 warns against.

    Best-effort, matching `_resolvable_mission_ids`'s contract: never raises, a failure
    fetching one system does not abort the others, and a planet absent from the result
    means "unverifiable this tick", not "no debris" -- `generate_harvest_candidates`
    already treats a missing key as "nothing to harvest" via its own `.get(planet_id)`,
    which is the correct degrade-to-NOOP here, not a promotion to false certainty."""
    by_system: dict[tuple[int, int], list[tuple[int, PlanetSnapshot]]] = {}
    for planet in snapshot.planets:
        if not planet.coordinates:
            continue
        parts = planet.coordinates.split(":")
        if len(parts) != 3:
            continue
        try:
            galaxy, system, position = (int(p) for p in parts)
        except ValueError:
            continue
        by_system.setdefault((galaxy, system), []).append((position, planet))

    out: dict[int, Resources] = {}
    for (galaxy, system), entries in by_system.items():
        try:
            data = read.fetch_universe_system(galaxy, system)
        except http.VeydriftAPIError:
            continue
        slots_by_position: dict[int, dict[str, Any]] = {}
        for slot in data.get("planets") or []:
            try:
                slots_by_position[int(slot.get("position"))] = slot
            except (TypeError, ValueError):
                continue
        for position, planet in entries:
            slot = slots_by_position.get(position)
            if slot is None:
                continue
            debris = slot.get("debrisField")
            if not isinstance(debris, dict):
                continue
            try:
                metal = int(debris.get("metal", 0))
                crystal = int(debris.get("crystal", 0))
            except (TypeError, ValueError):
                continue
            if metal <= 0 and crystal <= 0:
                continue
            out[planet.planet_id] = Resources(metal=metal, crystal=crystal)
    return out


def _colonize_targets(snapshot: Snapshot) -> list[tuple[str, int]]:
    """Free coordinate slots reachable for Colonize -- `generate_colonize_candidates`'s
    `colonize_targets` parameter (`candidates.py`, commit 4 of the launch-actions plan).

    Sourced from `/universe/galaxies/{g}/systems/{s}` (`read.fetch_universe_system`),
    scoped to the SAME systems the wallet's own planets are already in -- a deliberate,
    documented scope limit, not an oversight: a wider radius scan (`/universe/systems`)
    would need its own precedent for how far to look, which this codebase does not have
    and is not inventing here. A slot only qualifies when the universe route reports
    both `occupiedBy` and `migrationReservation` as `null` -- `occupiedBy == null` alone
    is what `isCoordinateAvailable` checks on-chain, but the contract also requires
    `_isPopulatedPlanetSlot` (a reserved-for-migration slot can still revert
    `CoordinatesOccupied` at launch, or silently bounce the mission home at arrival) --
    the universe route's own slot enumeration already satisfies the populated-slot half
    by construction (it only ever lists real slots), so `occupiedBy`/
    `migrationReservation` are the only two fields this function needs to check.

    Each returned entry is `(coordinates, deuterium_multiplier_bps)` -- the live value
    the API already computes for that slot, never recomputed, matching this codebase's
    posture toward every other live-vs-recomputed value.

    Best-effort, matching `_own_planet_debris`'s exact contract: never raises, a fetch
    failure for one system does not abort the others, and groups owned planets by
    `(galaxy, system)` so a multi-planet wallet sharing a system fetches it once."""
    by_system: dict[tuple[int, int], None] = {}
    for planet in snapshot.planets:
        if not planet.coordinates:
            continue
        parts = planet.coordinates.split(":")
        if len(parts) != 3:
            continue
        try:
            galaxy, system, _position = (int(p) for p in parts)
        except ValueError:
            continue
        by_system[(galaxy, system)] = None

    out: list[tuple[str, int]] = []
    for galaxy, system in by_system:
        try:
            data = read.fetch_universe_system(galaxy, system)
        except http.VeydriftAPIError:
            continue
        for slot in data.get("planets") or []:
            if slot.get("occupiedBy") is not None or slot.get("migrationReservation") is not None:
                continue
            try:
                position = int(slot.get("position"))
                deuterium_multiplier_bps = int(slot.get("deuteriumMultiplierBps"))
            except (TypeError, ValueError):
                continue
            out.append((f"{galaxy}:{system}:{position}", deuterium_multiplier_bps))
    return out


def _foreign_debris_targets(wallet: str) -> dict[int, tuple[str, Resources]]:
    """Third-party debris fields reachable for Harvest -- `generate_foreign_harvest_
    candidates`'s `foreign_debris_targets` parameter (`candidates.py`, commit 3 of the
    launch-actions plan). Sourced from `/raid-finder/debris`
    (`read.fetch_raid_finder_debris`) -- deliberately not the universe route
    `_own_planet_debris` uses above, since that would mean scanning every system near
    every owned planet for a foreign field with no bound on how far to look; a convenience
    discovery index confirmed incomplete is an acceptable trade for *discovery* (a missed
    candidate is a missed opportunity, not a wrong answer) -- see that fetcher's own
    docstring for the reasoning this doesn't extend to `_own_planet_debris`.

    Filters out any entry whose `owner` matches this wallet, case-insensitively -- an
    extra defense-in-depth check against ever treating the wallet's own planet as a
    "foreign" target, even though `/raid-finder/debris` is not documented to ever report
    one. Best-effort, matching every other out-of-band fetcher in this module: never
    raises, degrades to `{}` on any fetch failure, and skips (rather than aborts on) any
    individual entry with an unparseable id/coordinates/debris shape."""
    try:
        data = read.fetch_raid_finder_debris()
    except http.VeydriftAPIError:
        return {}

    wallet_lower = wallet.lower()
    out: dict[int, tuple[str, Resources]] = {}
    for item in data.get("targets") or []:
        owner = item.get("owner")
        if isinstance(owner, str) and owner.lower() == wallet_lower:
            continue
        coords = item.get("coordinates") or {}
        debris = item.get("debris") or {}
        try:
            planet_id = int(item.get("planetId"))
            galaxy = int(coords["galaxy"])
            system = int(coords["system"])
            position = int(coords["position"])
            metal = int(debris.get("metal", 0))
            crystal = int(debris.get("crystal", 0))
        except (TypeError, ValueError, KeyError):
            continue
        if metal <= 0 and crystal <= 0:
            continue
        out[planet_id] = (f"{galaxy}:{system}:{position}", Resources(metal=metal, crystal=crystal))
    return out


def _attack_targets(wallet: str) -> dict[int, tuple[str, Resources, bool | None]]:
    """Candidate Attack targets -- `generate_attack_candidates`'s `attack_targets`
    parameter (commit 6 of the launch-actions plan). Sourced from `/highscores`
    (`read.fetch_highscores`), `category="economy"` (resource-rich accounts are the
    raiding-relevant ranking; the API also offers total/research/researchLevels/
    military/fleet/fleetCount/defense -- see references/api-routes.md §3.18 -- economy is
    the one this codebase picks, a documented choice, not the only defensible one),
    `includeAttackProtection=true` + `currentWallet=<this wallet>` (mandatory for the
    per-row `attackProtection` block to populate at all -- confirmed live 2026-08-28:
    omitting `currentWallet` returns `null` on every row).

    Each row's `attackProtection.allowed` is an ACCOUNT-level pre-check (score protection
    + same-alliance, computed without a specific `targetPlanetId`) -- unlike
    `/wallet/{addr}/attack-protection`'s own per-planet bashing-limit dimension, which
    this coarser highscores-embedded version cannot see. Used here as a generation-time
    courtesy filter only, never a substitute for `guard._gate_attack_protection`'s fresh,
    target-specific, guard-evaluation-time re-check (`_attack_protection_allowed`,
    below) -- see that gate's own docstring for why a target that clears this coarse
    check can still legitimately be blocked at launch time (bashing-limit) or bounce at
    impact (protection is re-evaluated then, not at launch).

    A row whose `attackProtection` is missing/malformed, or whose `allowed` key isn't a
    bool, is recorded here with `allowed=None` -- *unknown*, not permitted --
    `generate_attack_candidates` excludes any target whose third tuple element isn't
    exactly `True`, so this is fail-closed all the way from the fetch to the generator,
    not just at the guard gate.

    Keyed by the row's `homePlanetId` -- a row's *other* planets, if any, are not
    considered (a documented scope limit, not an oversight: this codebase treats one
    highscores row as one candidate target, the same "one row -> one target" simplicity
    every other generator in this family takes). Best-effort, matching every other
    out-of-band fetcher in this module: never raises, degrades to `{}` on any fetch/parse
    failure, and skips (rather than aborts on) any individual row with an unparseable
    id/coordinates/resources shape. Excludes the wallet's own row."""
    try:
        data = read.fetch_highscores(category="economy", current_wallet=wallet, page_size=_ATTACK_TARGET_PAGE_SIZE)
    except http.VeydriftAPIError:
        return {}
    rankings = data.get("rankings")
    rows = rankings.get("economy") if isinstance(rankings, dict) else None
    if not isinstance(rows, list):
        return {}

    wallet_lower = wallet.lower()
    out: dict[int, tuple[str, Resources, bool | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_wallet = row.get("wallet")
        if isinstance(row_wallet, str) and row_wallet.lower() == wallet_lower:
            continue
        home_planet = row.get("homePlanet")
        if not isinstance(home_planet, dict):
            continue
        coords = home_planet.get("coordinates") or {}
        tactical = home_planet.get("tactical") or {}
        raidable_raw = tactical.get("raidableResources") or {}
        try:
            planet_id = int(row.get("homePlanetId"))
            galaxy = int(coords["galaxy"])
            system = int(coords["system"])
            position = int(coords["position"])
            metal = int(raidable_raw.get("metal", 0))
            crystal = int(raidable_raw.get("crystal", 0))
            deuterium = int(raidable_raw.get("deuterium", 0))
        except (TypeError, ValueError, KeyError):
            continue
        if metal <= 0 and crystal <= 0 and deuterium <= 0:
            continue
        protection = row.get("attackProtection")
        allowed = protection.get("allowed") if isinstance(protection, dict) else None
        allowed = allowed if isinstance(allowed, bool) else None
        out[planet_id] = (
            f"{galaxy}:{system}:{position}",
            Resources(metal=metal, crystal=crystal, deuterium=deuterium),
            allowed,
        )
    return out


def _missile_targets(wallet: str) -> dict[int, tuple[str, dict[int, int], bool | None]]:
    """Candidate Missile targets -- `generate_missile_candidates`'s `missile_targets`
    parameter (commit 7 of the launch-actions plan). Sourced from the same `/highscores`
    (economy category) response `_attack_targets` reads -- a separate fetch, not a shared
    one, matching this codebase's existing precedent of dedicated fetchers per generator
    even when their data sources overlap (`_own_planet_debris` vs.
    `_foreign_debris_targets`). Extracts each row's `homePlanet.tactical.defenses.units[]`
    (`{id: count}`) instead of `raidableResources` -- a missile snipes a specific
    DEFENSE type, not resources, so `generate_missile_candidates` needs the target's
    defense composition, not its loot.

    The `attackProtection.allowed`/`blockedReason` semantics are identical to
    `_attack_targets`'s -- both read the SAME account-level, no-`targetPlanetId` block,
    which never carries `"bashing"` as a `blockedReason` (bashing is a per-(attacker,
    defender,PLANET) triple this coarser endpoint cannot see at all). So there is no
    missile-specific interpretation needed at generation time; the missile-vs-fleet
    distinction only matters at guard time, against the richer per-planet
    `/wallet/{addr}/attack-protection` response (see `_attack_protection_allowed`'s own
    commit-7 update).

    Keyed by the row's `homePlanetId`, same one-row-one-target scope limit
    `_attack_targets` documents. Best-effort: never raises, degrades to `{}` on any
    fetch/parse failure, skips any individual row with an unparseable shape. Excludes the
    wallet's own row."""
    try:
        data = read.fetch_highscores(category="economy", current_wallet=wallet, page_size=_ATTACK_TARGET_PAGE_SIZE)
    except http.VeydriftAPIError:
        return {}
    rankings = data.get("rankings")
    rows = rankings.get("economy") if isinstance(rankings, dict) else None
    if not isinstance(rows, list):
        return {}

    wallet_lower = wallet.lower()
    out: dict[int, tuple[str, dict[int, int], bool | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_wallet = row.get("wallet")
        if isinstance(row_wallet, str) and row_wallet.lower() == wallet_lower:
            continue
        home_planet = row.get("homePlanet")
        if not isinstance(home_planet, dict):
            continue
        coords = home_planet.get("coordinates") or {}
        tactical = home_planet.get("tactical") or {}
        defense_units = (tactical.get("defenses") or {}).get("units")
        if not isinstance(defense_units, list):
            continue
        try:
            planet_id = int(row.get("homePlanetId"))
            galaxy = int(coords["galaxy"])
            system = int(coords["system"])
            position = int(coords["position"])
        except (TypeError, ValueError, KeyError):
            continue
        defense_counts: dict[int, int] = {}
        for unit in defense_units:
            if not isinstance(unit, dict):
                continue
            try:
                defense_id = int(unit.get("id"))
                count = int(unit.get("count", 0))
            except (TypeError, ValueError):
                continue
            if count > 0:
                defense_counts[defense_id] = count
        if not defense_counts:
            continue
        protection = row.get("attackProtection")
        allowed = protection.get("allowed") if isinstance(protection, dict) else None
        allowed = allowed if isinstance(allowed, bool) else None
        out[planet_id] = (f"{galaxy}:{system}:{position}", defense_counts, allowed)
    return out


def _attack_protection_allowed(wallet: str, action: Action, snapshot: Snapshot) -> tuple[bool | None, str | None]:
    """Live, target-specific re-check for the chosen Attack/Missile `action` --
    `guard._gate_attack_protection`'s `attack_protection_allowed`/
    `attack_protection_blocked_reason` parameters (commit 6, extended to Missile in
    commit 7 of the launch-actions plan). Fetched fresh at guard-evaluation time, for the
    SPECIFIC target this action encodes, never trusted from whatever `candidates.
    generate_attack_candidates`/`generate_missile_candidates` read at generation time --
    that earlier read is a courtesy filter only; `VeydriftAntiRaidPrimitives.sol`
    re-evaluates protection at IMPACT (Attack) or at launch (Missile, synchronously), not
    trusted from a potentially-stale `/highscores` fetch either way.

    Prefers `action.target_planet_id` directly when set (the normal case for both action
    families -- their generators always set it, the same foreign-target posture
    `generate_foreign_harvest_candidates` established in commit 3), falling back to
    `_resolve_target_planet_id` only for a hand-constructed override action that set
    `target_coordinates` instead.

    **Returns `(allowed, blocked_reason)`, commit 7** -- `blocked_reason` (`"score_
    protection"` / `"bashing"` / `"not_allied"`, only present when `allowed` is `false`)
    is what lets `guard._gate_attack_protection` apply the missile-specific exemption:
    `VeydriftPlanetManagementModule.sol`'s `launchInterplanetaryMissileAttack` calls
    `_enforceAttackProtection(..., countsBashing=false)` -- a missile ignores the
    bashing-limit dimension entirely, so a target whose ONLY blocked reason is bashing is
    a legal missile target even though it would be an illegal fleet Attack. Both
    positions are `None` (never a default of `True`/`False`/`""`) on any fetch/parse
    failure, an unresolvable target, or a response missing a boolean `allowed` key --
    `guard._gate_attack_protection` fails closed on `None` exactly like every other
    live-data gate in this codebase (AGENTS.md §5)."""
    if action.target_planet_id is not None:
        target_planet_id = action.target_planet_id
    else:
        try:
            target_planet_id = _resolve_target_planet_id(action, snapshot)
        except ValueError:
            return None, None
    try:
        data = read.fetch_attack_protection(wallet, target_planet_id)
    except http.VeydriftAPIError:
        return None, None
    allowed = data.get("allowed")
    if not isinstance(allowed, bool):
        return None, None
    blocked_reason = data.get("blockedReason")
    return allowed, (blocked_reason if isinstance(blocked_reason, str) else None)


def _epoch_seconds_to_datetime(raw: object) -> datetime | None:
    """The live API's own timestamp shape for this route -- a decimal-string unix epoch,
    e.g. `"1783481579"` (confirmed live 2026-09-01) -- same discrepancy against ISO-format
    fixtures `_resolvable_mission_ids` above already documents for a different route.
    `None` on anything unparseable, never a guessed/defaulted timestamp."""
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _alliance_state(wallet: str) -> AllianceState | None:
    """Live `/wallet/{addr}/alliance` fetch, once per tick -- `guard._gate_alliance_action`'s
    `alliance_state` parameter (alliance feature, commit 4). Best-effort: catches
    `http.VeydriftAPIError` and degrades to `None` -- the gate BLOCKs on `None`, never
    assumes "no alliance involvement" (AGENTS.md §5). Individual malformed sub-entries are
    skipped, never raised, matching `_attack_targets`/`_missile_targets`'s posture toward
    a single bad row in an otherwise-good response.

    **The live response's `membership` is never JSON `null`** -- confirmed live 2026-09-01
    across four real wallets: a wallet with no alliance reports `{"allianceId": "0",
    "role": "none", "joinedAt": "0"}`, a real sentinel object, not an absent key. This
    function treats `allianceId == 0` (or `role == "none"`) as "no membership," matching
    `AllianceState.membership`'s own `None`-means-not-a-member contract on the Python side
    -- a naive `data.get("membership")` truthiness check would otherwise treat every
    wallet as a member of alliance 0.

    Every numeric-looking field in the live response (`allianceId`, `createdAt`,
    `joinedAt`, `totalScore`) is a decimal STRING, not a JSON number, except
    `directory[].memberCount`, which is a genuine JSON int -- confirmed live, not
    assumed; each is coerced explicitly below, never left to pydantic's own coercion."""
    try:
        data = read.fetch_alliance_state(wallet)
    except http.VeydriftAPIError:
        return None
    if not isinstance(data, dict):
        return None

    def _role_id(raw: object) -> int | None:
        if not isinstance(raw, str):
            return None
        try:
            return alliance_ids.role_id(raw)
        except KeyError:
            return None

    membership: AllianceMembership | None = None
    raw_membership = data.get("membership")
    if isinstance(raw_membership, dict):
        try:
            alliance_id = int(raw_membership.get("allianceId"))
        except (TypeError, ValueError):
            alliance_id = None
        role = _role_id(raw_membership.get("role"))
        if alliance_id and role is not None and role != alliance_ids.AllianceRole.NONE:
            membership = AllianceMembership(
                alliance_id=alliance_id, role=role, joined_at=_epoch_seconds_to_datetime(raw_membership.get("joinedAt"))
            )

    members: list[AllianceMember] = []
    for row in data.get("members") or []:
        if not isinstance(row, dict):
            continue
        address = row.get("address")
        role = _role_id(row.get("role"))
        if not isinstance(address, str) or role is None:
            continue
        total_score_raw = row.get("totalScore")
        try:
            total_score = int(total_score_raw) if total_score_raw is not None else None
        except (TypeError, ValueError):
            total_score = None
        members.append(
            AllianceMember(address=address, role=role, joined_at=_epoch_seconds_to_datetime(row.get("joinedAt")), total_score=total_score)
        )

    def _alliance_ids_from(rows: object) -> list[int]:
        out: list[int] = []
        for row in rows or []:
            raw_id = row.get("allianceId") if isinstance(row, dict) else row
            try:
                out.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        return out

    pending_invites = [AlliancePendingInvite(alliance_id=aid) for aid in _alliance_ids_from(data.get("pendingInvites"))]
    pending_join_requests = [
        AlliancePendingJoinRequest(alliance_id=aid) for aid in _alliance_ids_from(data.get("pendingJoinRequests"))
    ]

    alliance_join_requests: list[AllianceJoinRequestForOwner] = []
    for row in data.get("allianceJoinRequests") or []:
        if not isinstance(row, dict):
            continue
        requester = row.get("requester")
        try:
            alliance_id = int(row.get("allianceId"))
        except (TypeError, ValueError):
            continue
        if not isinstance(requester, str):
            continue
        alliance_join_requests.append(AllianceJoinRequestForOwner(alliance_id=alliance_id, requester=requester))

    directory: dict[int, AllianceDirectoryEntry] = {}
    for row in data.get("directory") or []:
        if not isinstance(row, dict):
            continue
        try:
            alliance_id = int(row.get("allianceId"))
        except (TypeError, ValueError):
            continue
        member_count_raw = row.get("memberCount")
        try:
            member_count = int(member_count_raw) if member_count_raw is not None else None
        except (TypeError, ValueError):
            member_count = None
        directory[alliance_id] = AllianceDirectoryEntry(active=row.get("active"), member_count=member_count)

    return AllianceState(
        membership=membership,
        members=members,
        pending_invites=pending_invites,
        pending_join_requests=pending_join_requests,
        alliance_join_requests=alliance_join_requests,
        directory=directory,
    )


def _alliance_summary_line(state: AllianceState | None) -> str | None:
    """One-line summary of `alliance_state` for the tick report/`proposals.jsonl` --
    `None` only when the feature is off (`alliance_state` itself is `None` in that case
    too, so there is nothing to report). Satisfies the manual-override-only design's own
    "read and report alliance state" half: an operator sees pending invites/join-requests
    and current membership on every tick without having to fetch `/wallet/{addr}/alliance`
    by hand first, even on a tick that proposes nothing alliance-related at all."""
    if state is None:
        return None
    if state.membership is None:
        parts = ["not in an alliance"]
    else:
        parts = [f"alliance {state.membership.alliance_id} ({alliance_ids.role_name(state.membership.role)})"]
    if state.pending_invites:
        parts.append(f"{len(state.pending_invites)} pending invite(s)")
    if state.pending_join_requests:
        parts.append(f"{len(state.pending_join_requests)} pending join request(s) of your own")
    if state.alliance_join_requests:
        parts.append(f"{len(state.alliance_join_requests)} incoming join request(s) to review")
    return ", ".join(parts)


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
    override_line: str | None = None,
) -> list[str]:
    verb = "EXECUTE" if executed else "PROPOSE"
    if action.kind in (ActionKind.NOOP, ActionKind.ESCALATE, ActionKind.HALT):
        lines = [f"{action.kind.value.upper():9s} {action.rationale}"]
        if override_line:
            lines.append(f"  {override_line}")
        return lines
    header = f"{verb:9s} {action.function}(planet={action.planet_id}, entity={action.entity_id})"
    lines = [header]
    if override_line:
        lines.append(f"  {override_line}")
    if action.cost.metal or action.cost.crystal or action.cost.deuterium:
        lines.append(f"  cost:   M {action.cost.metal}  C {action.cost.crystal}  D {action.cost.deuterium}")
    lines.append(f"  why:    {action.rationale}")
    if action.expected_effect:
        lines.append(f"  effect: {action.expected_effect}")
    if action.alternatives:
        lines.append(f"  alts:   {len(action.alternatives)} considered and not selected --")
        for alt in action.alternatives:
            score_text = f"{alt.score:.1f}h payback" if alt.score is not None else "unscored"
            name = alt.entity_name or alt.family
            lines.append(f"          [{alt.family}] {name} ({score_text}) -- {alt.why_not}")
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
    elif send_outcome == "simulation_failed":
        # The simulate-before-send fix: no gas was spent -- this is the free pre-flight
        # check catching what `send` alone would have burned real gas to discover. Detail
        # comes from the `walletctl_simulate` GuardVerdict `_send_and_await` appended,
        # threaded the same way `build_error` already is.
        sim_verdict = next((v for v in guard_report.verdicts if v.gate == "walletctl_simulate"), None)
        detail = sim_verdict.detail if sim_verdict is not None else "walletctl simulate blocked the send"
        lines.append(f"  !! SIMULATION FAILED -- send blocked, nothing was submitted: {detail}")
    # Fix 3: require_confirmation's printed hand-off command.
    if confirm_hint:
        lines.append(f"  {confirm_hint}")
    return lines


def _load_override_action(path: Path, policy_model: Policy) -> Action:
    """Parse+validate `--action`'s file into an `Action`, forcing `source="manual_override"`
    regardless of what the file itself claims. Hard stop (never a silent fallback to the
    planner) on a missing policy opt-in or a malformed/invalid file -- mirrors
    `_load_policy`'s own JSON/pydantic error handling."""
    if not policy_model.strategy.allow_agent_action_override:
        raise typer.BadParameter(
            "--action requires policy.strategy.allow_agent_action_override=true -- see "
            "references/manual-action-override.md before enabling it."
        )
    if not path.exists():
        raise typer.BadParameter(f"--action file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON: {exc}") from exc
    try:
        action = Action.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise typer.BadParameter(f"{path} failed Action validation: {exc}") from exc
    return action.model_copy(update={"source": "manual_override"})


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="Path to policy.json (default: $VEYDRIFT_HOME/policy.json)."),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Build/plan/guard but never send. Always true at tier 1."),
    readiness: bool = typer.Option(False, "--readiness", help="Print promotion evidence instead of running a tick."),
    format: str = typer.Option("md", "--format", help="Report format: md or json."),
    action: Path = typer.Option(  # noqa: B008
        None,
        "--action",
        help="Path to an Action JSON to use instead of the planner's own choice -- "
        "requires policy.strategy.allow_agent_action_override=true. See "
        "references/manual-action-override.md.",
    ),
) -> None:
    """`vd tick [--policy PATH] [--dry-run] [--readiness] [--format md|json] [--action PATH]`
    — run one tick (see this module's docstring for the 9-step contract), unless a
    subcommand (`init`) was given, in which case that runs instead and this callback is a
    no-op."""
    if ctx.invoked_subcommand is not None:
        return
    if readiness:
        _print_readiness()
        return

    policy_file = policy or policy_path()
    policy_model = _load_policy(policy_file)
    effective_dry_run = _effective_dry_run(policy_model, dry_run)

    override_action = _load_override_action(action, policy_model) if action is not None else None

    try:
        with tick_lock():
            _run_tick(policy_model, effective_dry_run, format, override_action=override_action)
    except TickLockedError as exc:
        _console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0)


def _run_tick(policy_model: Policy, effective_dry_run: bool, format: str, *, override_action: Action | None = None) -> None:
    now = datetime.now(UTC)
    agent_state = load_agent_state()
    # Captured BEFORE this tick's own state mutations -- describes what this tick should
    # check for human activity on, not what it's about to propose itself. Never consulted
    # on the killswitch path below (must never add a network call beyond /health there).
    previous_unresolved = agent_state.last_unresolved_onchain_proposal
    agent_state.touch(now=now)  # tick_count decision is deferred to _finish_tick's dedup check

    # Step 2: killswitch check -- ONE health call, nothing else, if active.
    if _killswitch_active():
        health_ok, game_paused, game_maintenance, degradation_reasons, readiness_ready, randomness_readiness = (
            _fetch_health_only()
        )
        halted_snapshot = Snapshot(
            taken_at=now,
            wallet=policy_model.wallet,
            health_ok=health_ok,
            game_paused=game_paused,
            game_maintenance=game_maintenance,
            degradation_reasons=degradation_reasons,
            readiness_ready=readiness_ready,
            randomness_readiness=randomness_readiness,
        )
        action = plan_mod.plan_next_action(halted_snapshot, policy_model, killswitch_active=True)
        guard_report = guard_mod.evaluate_guardrails(action, halted_snapshot, policy_model, agent_state, killswitch_active=True, now=now)
        _finish_tick(policy_model, agent_state, halted_snapshot, action, guard_report, None, executed=False, format=format, now=now)
        return

    # Step 3 (part 1) + Step 4: reconcile what we can before the snapshot, fetch it, then
    # finish reconciling against its indexed block (see _reconcile_pending's docstring).
    pending_before = agent_state.pending is not None
    if pending_before:
        _reconcile_pending(agent_state, indexed_block=None, now=now)

    snapshot = _fetch_snapshot(
        policy_model.wallet, policy_model.planets, universe_cadence_hours=policy_model.cadence.universe_hours
    )
    if snapshot is None:
        _console.print("[red]tick aborted: could not fetch a snapshot (network/API failure before any usable response).[/red]")
        save_agent_state(agent_state)
        raise typer.Exit(code=3)

    pending_unreconciled = _reconcile_pending(agent_state, indexed_block=snapshot.latest_indexed_block, now=now)

    # Phase 5 (docs/SPEC.md §5.4): revives plan.py's rung 3 (resolveFleetMission), dead
    # since Phase 1 because Snapshot (models.py, frozen) has no field for the player's own
    # missions -- see `_resolvable_mission_ids`'s docstring. Best-effort; never aborts the
    # tick.
    resolvable_mission_ids = _resolvable_mission_ids(policy_model.wallet)

    # Phase A commit 1 (docs/SPEC.md, strategy-playbook §8c): revives the Harvest half of
    # band 5 (`select_logistics_candidate`), dormant since Phase 5c because nothing
    # supplied `own_planet_debris` -- see `_own_planet_debris`'s docstring. Best-effort;
    # never aborts the tick.
    own_planet_debris = _own_planet_debris(snapshot)

    # Commit 3 of the launch-actions plan: revives the foreign-Harvest half of band 5's
    # Harvest coverage (own-planet debris was commit 1). Best-effort; never aborts the tick.
    foreign_debris_targets = _foreign_debris_targets(policy_model.wallet)

    # Commit 4 of the launch-actions plan: only fetched when Colonize is actually
    # declared -- an idle wallet with `policy.strategy.colonize=false` (the default)
    # never pays for this network call, matching every other opt-in band's posture.
    colonize_targets = _colonize_targets(snapshot) if policy_model.strategy.colonize else []

    # Commit 6 of the launch-actions plan: only fetched when combat is actually enabled
    # -- an idle wallet with policy.actions.allow_combat=false (the default) never pays
    # for this network call, matching commit 4's colonize_targets posture exactly.
    attack_targets = _attack_targets(policy_model.wallet) if policy_model.actions.allow_combat else {}

    # Commit 7 of the launch-actions plan: same gating as attack_targets above.
    missile_targets = _missile_targets(policy_model.wallet) if policy_model.actions.allow_combat else {}

    # Alliance feature, commit 4: fetched whenever the flag is on, regardless of what
    # action this tick resolves to -- unlike attack_targets/missile_targets (fed to the
    # planner), this is guard-time data AND tick-report narration (the "Alliance" report
    # section below), so it must be available on every tick, not only when the chosen
    # action happens to be an alliance one. Never fed to `plan_next_action` -- alliance
    # actions are manual-override-only, the planner has no rung that reads this.
    alliance_state = _alliance_state(policy_model.wallet) if policy_model.actions.allow_alliance else None

    # Step 5: plan. `override_action` (vd tick --action) substitutes for the planner's own
    # choice only -- every rung after this one (guard, tier gates, require_confirmation,
    # the lockfile already held by the caller, dedup+logging) is unchanged and applies to
    # an override exactly as it does to a planner-chosen action.
    override_record: dict[str, Any] | None = None
    override_line: str | None = None
    if override_action is not None:
        action = override_action
        override_record, override_line = _describe_override(
            override_action,
            snapshot,
            policy_model,
            pending_tx_unreconciled=pending_unreconciled,
            resolvable_mission_ids=resolvable_mission_ids,
            own_planet_debris=own_planet_debris,
            foreign_debris_targets=foreign_debris_targets,
            colonize_targets=colonize_targets,
            attack_targets=attack_targets,
            missile_targets=missile_targets,
        )
    else:
        action = plan_mod.plan_next_action(
            snapshot,
            policy_model,
            killswitch_active=False,
            pending_tx_unreconciled=pending_unreconciled,
            resolvable_mission_ids=resolvable_mission_ids,
            own_planet_debris=own_planet_debris,
            foreign_debris_targets=foreign_debris_targets,
            colonize_targets=colonize_targets,
            attack_targets=attack_targets,
            missile_targets=missile_targets,
        )

    # Step 6: guard. Gather live-only facts ONLY when the action is on-chain -- an
    # off-chain action (noop/escalate/halt) needs none of this and triggers no extra
    # network calls, matching the same "no unnecessary network calls" posture the
    # killswitch path takes.
    unsigned_tx: UnsignedTx | None = None
    build_error: str | None = None
    live_addresses: set[str] | None = None
    eth_balance_wei: int | None = None
    wallet_address: str | None = None
    built_tx_path: Path | None = None
    gas_cost_wei: int | None = None
    outgoing_colonize_count: int | None = None
    attack_protection_allowed: bool | None = None
    attack_protection_blocked_reason: str | None = None
    if action.is_onchain():
        unsigned_tx, gas_cost_wei, build_error, built_tx_path = _walletctl_build(
            action, provider=policy_model.wallet_engine.provider, snapshot=snapshot
        )
        live_addresses = _live_addresses()
        if policy_model.tier is not Tier.ADVISOR:
            # Single `walletctl status` call serves both the `eth_floor` guard gate
            # (balance) and `_walletctl_simulate`'s mandatory `--from` (address) -- see
            # `_walletctl_status`'s docstring for why this replaced the old
            # `_walletctl_eth_balance_wei`-only call here.
            eth_balance_wei, wallet_address = _walletctl_status(provider=policy_model.wallet_engine.provider)
        # Commit 4 of the launch-actions plan: only fetched for an actual Colonize
        # proposal -- every other action kind never touches `_colony_cap_violation`'s
        # in-flight check at all, so fetching this unconditionally would be a wasted
        # network call on every single tick.
        if action.function == "launchFleetMission" and action.mission_type == ids.FleetMissionType.COLONIZE:
            outgoing_colonize_count = _outgoing_colonize_count(policy_model.wallet)
        # Commit 6 of the launch-actions plan (extended to Missile in commit 7): only
        # fetched for an actual Attack or Missile proposal -- a live, target-specific
        # re-check at guard-evaluation time, never trusted from generation time (see
        # guard._gate_attack_protection's docstring).
        is_attack_action = action.function == "launchFleetMission" and action.mission_type == ids.FleetMissionType.ATTACK
        is_missile_action = action.function == "launchInterplanetaryMissileAttack"
        if is_attack_action or is_missile_action:
            attack_protection_allowed, attack_protection_blocked_reason = _attack_protection_allowed(
                policy_model.wallet, action, snapshot
            )

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
        outgoing_colonize_count=outgoing_colonize_count,
        attack_protection_allowed=attack_protection_allowed,
        attack_protection_blocked_reason=attack_protection_blocked_reason,
        alliance_state=alliance_state,
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
                policy_model,
                agent_state,
                action,
                unsigned_tx,
                snapshot,
                now,
                gas_cost_wei_estimate=gas_cost_wei,
                wallet_address=wallet_address,
                guard_report=guard_report,
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
        override_record=override_record,
        override_line=override_line,
        alliance_state=alliance_state,
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
    wallet_address: str | None = None,
    guard_report: GuardReport | None = None,
) -> tuple[bool, str, str | None]:
    """Step 7's build(already done) -> simulate -> send -> status-await -> indexed-wait.
    Returns `(executed, outcome, tx_hash)` where `outcome` is one of `"success"`,
    `"reverted"`, `"unknown"`, `"send_failed"`, or `"simulation_failed"`. `executed` is
    True **only** for `"success"` -- Fix 2's core rule: a reverted, unknown, or blocked
    send is never reported, or counted in `AgentState.executions_count`, as a success.

    **The simulate step is the fix this docstring documents.** Before this change,
    nothing under `src/` ever called `walletctl simulate` -- `send` was the first and
    only time a proposed transaction was checked against real chain state, so a tx that
    would revert burned real gas to find out instead of a free `eth_call` +
    `estimateGas`. Reproduced live on an Anvil fork of Base: `startResearch(664, 0)`
    simulated as `ok: false` / `InsufficientResources(6798, 1874, 4444)`, then `send`
    submitted it anyway and the receipt came back `status: "reverted"`. A failed or
    unusable simulate result (`ok` is not `True` -- see `_walletctl_simulate`'s docstring
    for why `None` and `False` are both blocking) now prevents `_walletctl_send` from
    ever being called, and -- mirroring exactly how `build_error` is already threaded
    through `_run_tick` -- appends a `walletctl_simulate` `GuardVerdict` to `guard_report`
    (when the caller supplied one; the direct-call unit tests for this function do not)
    so the detail reaches both `proposals.jsonl`'s `guard_verdicts` and the printed tick
    report, not just `logs/strategy.md`. A blocked-before-send is deliberately NOT logged
    to `actions.jsonl` and does NOT call `record_revert` -- nothing was submitted, so
    there is no on-chain outcome to record; this matches how a `walletctl build` failure
    is already handled (an `ESCALATE` verdict, no `actions.jsonl` entry), not how a real
    revert is.

    Not exercised end-to-end against mainnet (no tier>=2 policy or wallet credentials were
    ever configured against real mainnet funds) -- `startBuildingUpgrade` HAS now run this
    exact path (including the new simulate step) against a local Anvil fork of Base; see
    AGENTS.md §10 and `skills/veydrift-wallet/references/fork-testing.md`.
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

    sim_ok, sim_revert_reason, sim_error = _walletctl_simulate(tx_file, address=wallet_address)
    if sim_ok is not True:
        if sim_error is not None:
            detail = sim_error
        elif sim_revert_reason is not None:
            detail = f"simulated revert: {sim_revert_reason}"
        else:
            detail = "walletctl simulate reported ok: false with no revert reason"
        if guard_report is not None:
            guard_report.verdicts.append(GuardVerdict(gate="walletctl_simulate", status=GuardStatus.ESCALATE, detail=detail))
            if guard_report.decision is Decision.ALLOW:
                guard_report.decision = Decision.ESCALATE
        log.append_strategy(f"simulate blocked send for {key}: {detail}", now=now)
        return False, "simulation_failed", None

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
    override_record: dict[str, Any] | None = None,
    override_line: str | None = None,
    alliance_state: AllianceState | None = None,
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
        "source": action.source,
        "rationale": action.rationale,
        "expected_effect": action.expected_effect,
        "alternatives": [alt.model_dump() for alt in action.alternatives],
        "guard_decision": guard_report.decision.value,
        "guard_verdicts": [v.model_dump() for v in guard_report.verdicts],
        "tx": unsigned_tx.model_dump() if unsigned_tx else None,
        "tx_hash": tx_hash,  # Fix 6c: previously always None (`action.function and None`)
        "send_outcome": send_outcome,
        "executed": executed,
        "human_activity_check": human_activity_record,
        "override": override_record,
        "alliance": alliance_state.model_dump() if alliance_state is not None else None,
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
            action,
            guard_report,
            unsigned_tx,
            executed,
            policy_model.tier,
            send_outcome=send_outcome,
            confirm_hint=confirm_hint,
            override_line=override_line,
        ),
        duplicate_of=duplicate_note,
        human_activity_line=human_activity_line,
        alliance_line=_alliance_summary_line(alliance_state),
    )

    if not is_duplicate:
        log.log_proposal(proposal_record)

    # An override is never routine enough to suppress -- reported to strategy.md
    # immediately and unconditionally, regardless of the structural-tier-block/duplicate
    # suppression the next block applies to routine narration (design decision 7,
    # references/manual-action-override.md).
    if override_line is not None:
        log.append_strategy(f"tick {agent_state.tick_count}: {override_line}", now=now)

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
                    "override": override_record,
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
