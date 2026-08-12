"""`$VEYDRIFT_HOME` resolution, on-disk agent state, and the tick lockfile.

**Why this module exists at all (docs/SPEC.md §2.1):** `npx skills add .` *copies* the
skill tree into the agent's skills directory. Anything written inside the skill tree is
destroyed on the next install/update. So every mutable byte this package ever writes —
policy, cache, logs, the lockfile, pending-tx bookkeeping — lives under `$VEYDRIFT_HOME`
(default `~/.veydrift`), never under `skills/veydrift-agent/`. Every path this module
hands out for *bundled* assets (e.g. `assets/policy.example.json`) is resolved from
`__file__`, never `cwd`, so the skill also works when invoked from an arbitrary directory
after install (acceptance criterion 13).

`cli.py`'s `doctor` command imports `veydrift_home` directly from this module — that is
the one hard contract this file must keep.

**Known duplication:** `http.py` carries its own tiny fallback copy of the
`$VEYDRIFT_HOME` env/default resolution (marked `# TODO(WP3)` there), used only until this
module exists. `http.py` is frozen for this work package (owned by WP1), so that fallback
cannot be deleted here — it now simply goes dead code the moment `state.py` is importable,
because `http.py` prefers `from veydrift_agent.state import veydrift_home` when it can.
The two implementations are kept byte-for-byte equivalent on purpose; if you ever touch
this function's semantics, check `http.py`'s fallback by eye (do not edit it).
"""

from __future__ import annotations

import errno
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------------------
# $VEYDRIFT_HOME
# --------------------------------------------------------------------------------------

_DEFAULT_HOME = "~/.veydrift"


def veydrift_home() -> Path:
    """Resolve, create-if-missing, and return `$VEYDRIFT_HOME`.

    Semantics (docs/SPEC.md §2.1) — identical to `http.py`'s temporary fallback, which
    this function is meant to obsolete:

    * `$VEYDRIFT_HOME` env var if set (expanded, `~` allowed).
    * Otherwise `~/.veydrift`.
    * Created on first use (`mkdir(parents=True, exist_ok=True)`) — callers never need to
      check existence themselves.
    """
    home = Path(os.environ.get("VEYDRIFT_HOME", _DEFAULT_HOME)).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def bundled_asset(*parts: str) -> Path:
    """Resolve a path *inside this installed skill's own tree* (e.g. `assets/policy.example.json`),
    from `__file__` -- never `cwd`. Used only for read-only bundled assets that ship with the
    skill; never for anything this package writes (that's always under `veydrift_home()`).
    """
    return Path(__file__).resolve().parent.parent.parent / "assets" / Path(*parts)


def cache_dir() -> Path:
    d = veydrift_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = veydrift_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ticks_dir() -> Path:
    d = logs_dir() / "ticks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def policy_path() -> Path:
    return veydrift_home() / "policy.json"


def agent_state_path() -> Path:
    return veydrift_home() / "agent-state.json"


def killswitch_path() -> Path:
    return veydrift_home() / "KILLSWITCH"


def killswitch_active() -> bool:
    """A plain existence check, deliberately not cached: the whole point of the killswitch
    is that dropping the file takes effect on the very next check, not after some TTL."""
    return killswitch_path().exists()


def lockfile_path() -> Path:
    return veydrift_home() / "tick.lock"


# --------------------------------------------------------------------------------------
# vd init — copies assets/policy.example.json to $VEYDRIFT_HOME/policy.json.
#
# Lives here (not in the frozen cli.py) per docs/SPEC.md's instruction. cli.py's
# `_SUBAPPS` list only mounts read/calc/plan/guard/tick/log as named sub-apps, so this
# module is not independently reachable as a top-level `vd` subcommand -- `tick.py`
# exposes it as `vd tick init`, calling straight into `init_policy()` below. See
# tick.py's docstring for that wiring and why "vd init" as a bare top-level command is not
# achievable without editing the frozen cli.py.
# --------------------------------------------------------------------------------------


class PolicyInitError(Exception):
    pass


def init_policy(*, force: bool = False) -> Path:
    """Copy `assets/policy.example.json` to `$VEYDRIFT_HOME/policy.json`.

    Refuses to overwrite an existing policy unless `force=True` -- an invalid or
    accidentally-clobbered policy is a hard stop, never a silent fallback to defaults
    (docs/SPEC.md §5.6).
    """
    source = bundled_asset("policy.example.json")
    if not source.exists():
        raise PolicyInitError(f"bundled example policy not found at {source}")
    dest = policy_path()
    if dest.exists() and not force:
        raise PolicyInitError(f"{dest} already exists; pass --force to overwrite")
    dest.write_text(source.read_text())
    return dest


# --------------------------------------------------------------------------------------
# agent-state.json — pending txs, cursors, cumulative daily gas, revert counts.
#
# Deliberately a *local* model, not added to the frozen models.py: this state is
# tick.py/guard.py/log.py's own bookkeeping, never part of the Snapshot/Action/GuardReport
# contract those modules share with WP1/WP2.
# --------------------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PendingTx(BaseModel):
    """One in-flight action, from the moment `walletctl send` returns a hash until the
    index catches up with the block it landed in. `guard.py`'s `idempotency` gate refuses
    a second proposal for the same `key` while this is set; the `index_lag` gate blocks
    further action until `indexed_at` is set or `max_index_wait_s` is exceeded.
    """

    model_config = ConfigDict(extra="ignore")

    key: str  # f"{planet_id}:{function}:{entity_id}" -- see guard.py's `idempotency_key`
    tx_hash: str | None = None
    planet_id: int | None = None
    function: str | None = None
    entity_id: int | None = None
    sent_at: datetime | None = None
    receipt_at: datetime | None = None
    block: int | None = None
    indexed_at: datetime | None = None
    gas_wei: int | None = None
    reverted: bool = False


class AgentState(_Base):
    version: int = 1

    tick_count: int = 0
    first_tick_at: datetime | None = None
    last_tick_at: datetime | None = None

    proposals_count: int = 0
    executions_count: int = 0

    #: UTC calendar day ("YYYY-MM-DD") the cumulative gas counter below applies to. Reset
    #: to 0 whenever `record_gas_spent` sees a new day -- this is what makes
    #: `gas_per_day_wei` a genuine daily ceiling rather than a lifetime one.
    gas_day: str | None = None
    cumulative_gas_wei_today: int = 0

    #: Keyed the same way as `PendingTx.key`. Incremented by `record_revert`, read by
    #: guard.py's `revert_streak` gate, never decremented automatically (a human clearing
    #: `agent-state.json` -- or a future `vd tick --reset-reverts` -- is the only reset).
    revert_counts: dict[str, int] = Field(default_factory=dict)

    #: At most one in-flight action at a time -- docs/SPEC.md's tick loop is a single
    #: sequential ladder, never a batch of parallel proposals.
    pending: PendingTx | None = None

    def record_tick(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self.first_tick_at is None:
            self.first_tick_at = now
        self.last_tick_at = now
        self.tick_count += 1

    def record_gas_spent(self, gas_wei: int, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        day = now.strftime("%Y-%m-%d")
        if self.gas_day != day:
            self.gas_day = day
            self.cumulative_gas_wei_today = 0
        self.cumulative_gas_wei_today += gas_wei

    def gas_spent_today(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        day = now.strftime("%Y-%m-%d")
        return self.cumulative_gas_wei_today if self.gas_day == day else 0

    def record_revert(self, key: str) -> int:
        self.revert_counts[key] = self.revert_counts.get(key, 0) + 1
        return self.revert_counts[key]


def load_agent_state() -> AgentState:
    """Never raises on a missing file (a fresh `$VEYDRIFT_HOME` is normal) and never
    silently swallows a *corrupt* one -- a hand-edited-into-garbage `agent-state.json`
    should fail loudly rather than quietly reset progress counters to zero."""
    path = agent_state_path()
    if not path.exists():
        return AgentState()
    raw = path.read_text()
    if not raw.strip():
        return AgentState()
    return AgentState.model_validate(json.loads(raw))


def save_agent_state(state: AgentState) -> None:
    path = agent_state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    tmp.replace(path)  # atomic on POSIX -- never leaves a half-written agent-state.json


# --------------------------------------------------------------------------------------
# Tick lockfile — a plain PID-stamped advisory lock (POSIX `fcntl.flock`). No new
# dependency: `filelock` is not in `pyproject.toml` (frozen) and stdlib `fcntl` already
# does exactly this on the only platform this ships to (`docs.md`/CI both being macOS/
# Linux). A stale lock from a killed process releases automatically -- `flock` locks are
# held by the OS per open file descriptor/process, not by file contents.
# --------------------------------------------------------------------------------------


class TickLockedError(Exception):
    """Raised when another `vd tick` already holds the lock. Callers should treat this as
    "skip this run", not a crash -- two overlapping schedulers (e.g. a human's `/loop` and
    launchd firing at the same time) is an expected, benign race, not a bug."""


@contextmanager
def tick_lock() -> Iterator[None]:
    if sys.platform == "win32":  # pragma: no cover - this project never runs on Windows
        yield
        return

    import fcntl

    path = lockfile_path()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise TickLockedError(
                    f"another `vd tick` holds the lock at {path}; skipping this run."
                ) from exc
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
