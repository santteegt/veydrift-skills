# Scheduling — the tick contract and its four adapters

`tick.py` is the frozen contract this document explains.

## The one thing every adapter shares: `vd tick`

```
vd tick [--policy PATH] [--dry-run] [--readiness] [--format md|json]
```

The skill owns `tick` — one idempotent, lockfile-protected entrypoint. The harness (a
human's `/loop`, a cron-like scheduler, or bare `launchd`) owns *cadence*: how often it
gets called. Nothing in `tick.py` schedules itself; it runs once and returns.

`vd tick` (no subcommand) is a `typer` **callback**, not a `run` subcommand — so the
flags above go directly on `vd tick`, not `vd tick run`. The one actual subcommand is
`vd tick init`, which copies `assets/policy.example.json` to
`$VEYDRIFT_HOME/policy.json` (see "Why `vd tick init`, not `vd init`" below).

### The 9 steps

```
1. load + validate policy         6. guard
2. killswitch check                7. if ALLOW and tier>=2 and not --dry-run:
3. reconcile pending txs                 walletctl build -> simulate -> send
4. snapshot                              await receipt, THEN await INDEXED
5. plan                            8. log: proposal always; action only if executed
                                    9. pretty report -> stdout + logs/ticks/
```

Two things worth calling out that aren't obvious from the numbered list:

- **Killswitch halts before any network call beyond `/health`.** Step 2 fetches `/health`
  (so the halt report still shows accurate current health), checks for
  `$VEYDRIFT_HOME/KILLSWITCH`, and if present, returns immediately with a `HALT` action —
  skipping steps 3–5 (reconcile/snapshot/plan) and therefore never fetching
  `/runtime-config`, `/infrastructure`, etc. `tests/test_tick.py::test_killswitch_halts_and_touches_only_health`
  asserts this by making the snapshot-fetch helper raise if it's ever called on this path.
- **Guard gathers live-only facts only for on-chain actions.** A `noop`/`escalate`/`halt`
  action triggers zero extra network calls in step 6 — no `/runtime-config` fetch, no
  `walletctl build` — because `Action.is_onchain()` gates all of it. This isn't spelled
  out as its own step in §5.7, but it follows the same "don't do unnecessary network work"
  posture as the killswitch path, and `tests/test_tick.py::test_noop_action_produces_no_tx_and_no_extra_network_calls`
  pins it down.

### `--dry-run` at tier 1

**Tier 1 cannot disable `--dry-run`, ever** — `tick._effective_dry_run()` forces it `True`
whenever `policy.tier is Tier.ADVISOR`, regardless of the flag. Tier 1 still runs the
full pipeline through step 6 (guard) and still builds real calldata via `walletctl build`
in step 6's data-gathering (`walletctl build` never signs — it's a pure ABI-encode plus a
best-effort gas estimate) — that's what lets the tick report print a complete,
ready-to-submit transaction for manual review: tier 1 still builds
calldata, and that is what makes the T1→T2 decision evidence-based rather than a guess.
It just never reaches
step 7's `send`. In practice this is doubly enforced: `guard.py`'s own `tier` gate BLOCKs
every on-chain function at `advisor` tier (see `references/guardrails.md`), so step 7's
`guard_report.decision is ALLOW` condition is never true at tier 1 either. Two independent
reasons the same real-world outcome holds is intentional, not redundant plumbing to trim.

### The indexed-wait is mandatory

A confirmed receipt is **not** indexed state. After `walletctl send` returns a tx hash,
`tick.py` polls `walletctl receipt` for a block number, then polls a **fresh snapshot's**
`latest_indexed_block` (`_await_indexed`) until it covers that block or
`policy.limits.max_index_wait_s` elapses. If the wait times out, the pending entry in
`agent-state.json` is **not** cleared — the next tick's `guard.index_lag` gate will BLOCK
further action on that identity until the index catches up, rather than silently treating
a confirmed-but-unindexed receipt as settled fact.

### `walletctl` is only ever a subprocess

`tick.py` never imports a chain-signing library and never signs. Tier ≥2 reaches the
wallet engine exclusively through `subprocess.run([...])` calls into `walletctl`
(`skills/veydrift-wallet`'s CLI — `build`, `status`, `receipt`, `send`). This is
grep-verifiable: this package's `src/` contains no import of a JS signing library, because
it isn't Python code at all — it's a different project invoked as an external tool.

`_wallet_skill_dir()` resolves `skills/veydrift-wallet` as a **sibling** of this
installed skill (both are installed together via
`npx skills add . -a claude-code -a hermes-agent`, so the sibling relationship should
survive install). If that resolution fails — e.g. a harness that isolates each skill into
its own root — set `VEYDRIFT_WALLET_DIR` to the wallet skill's install path, or install
`walletctl` globally (`npm link` in that project) so it resolves from `PATH` instead. This
is a real assumption, not a guarantee, which is exactly why the three-tier fallback exists
rather than a bare hardcoded path.

### Why `vd tick init`, not `vd init`

An earlier draft of this project's spec ("Add a `vd init` path...") reads as if a bare
top-level `vd init` exists. It doesn't, and can't without editing the frozen `cli.py`: `cli.py`'s
`_SUBAPPS` list only ever mounts this module under the name `tick` (alongside
`read`/`calc`/`plan`/`guard`/`log`). The closest achievable command is therefore
**`vd tick init`**, which is what this package actually ships. If `cli.py` is ever
unfrozen, promoting this to a bare `vd init` is a one-line change to `_SUBAPPS`.

### `vd tick --readiness`

Prints the promotion evidence — tick count, uptime, proposal count,
executed count, a rough divergence figure, which guardrails fired and how often, and gas
spent — **without running a tick**. Read honestly: "divergence" here is
`proposals - actions.jsonl entries`, which only captures what this tool itself executed
(tier ≥2 auto-sends) or what a human separately recorded back into `actions.jsonl` — there
is currently no command for a human to log "I executed proposal N by hand," so a T1
proposal a human executes entirely outside this tool leaves no trace `--readiness` can
see. The output says so directly rather than presenting an inflated confidence number.

## The four adapters

| Harness | Adapter | Notes |
| --- | --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` | A human is present; `--format md` renders the same pretty block the rich `Panel` prints to a terminal, suitable for chat. |
| Claude Code, unattended | `claude -p "run a veydrift tick"` from `launchd` | The agent itself invokes `vd tick`; no human review of the immediate output, so `logs/strategy.md` and `logs/proposals.jsonl` are what a human later reviews. |
| Hermes | register `vd tick` on Hermes' scheduler at `cadence.economy_minutes` | Hermes owns the interval; `vd tick` is still the same idempotent command with no awareness of who called it. |
| Bare OS | `assets/com.veydrift.agent.plist.template` — launchd, `StartInterval` | See below. Not installed by this package. |

Every adapter is running the exact same command with the exact same safety posture — the
only thing that differs is who decides *when* to call it. `tick.py`'s own lockfile
(`state.tick_lock`) is what makes two adapters firing at once a benign skip rather than a
race: `tests/test_tick.py::test_concurrent_tick_is_skipped_not_crashed` exercises this.

### Bare launchd

`assets/com.veydrift.agent.plist.template` ships as a template with four `__PLACEHOLDER__`
tokens (`__UV_BIN__`, `__SKILL_DIR__`, `__VEYDRIFT_HOME__`, `__INTERVAL_SECONDS__`) and a
documented install/uninstall recipe in its own header comment. **This package does not
install it** — no code path here runs `launchctl load`. A human decides to schedule the
agent unattended; that decision, and the one-time `sed` + `launchctl load` it takes, stays
manual on purpose.

A reasonable `__INTERVAL_SECONDS__` is `policy.cadence.economy_minutes * 60` (the example
policy's default of 10 minutes → 600 seconds), but nothing enforces that — the plist and
`policy.json` are two independent files a human keeps in sync by hand, the same way the
interactive `/loop` cadence is chosen independently of the policy file.

## A fifth, narrower thing to schedule: `vd radar check`

`vd tick` already runs the radar as part of every normal tick (`policy.radar.enabled`,
default `true` — see `references/radar.md`), scoped to `policy.planets`. `vd radar check`
is a separate, lighter-weight command for the case where a human wants a standing
notification watch **without** running the full tick loop — no `policy.json` required at
all, just `--wallet` or `--alliance-id`:

```
vd radar check --alliance-id 29
```

Same "this package owns *what*, the harness owns *cadence and delivery*" split as `vd
tick` above — there is no notification/webhook mechanism anywhere in this codebase, so the
exit code (`0` clean, `1` findings, `2` could not complete the check — see
`references/radar.md`) is the actual contract a wrapper acts on. A minimal cron entry:

```bash
#!/usr/bin/env bash
# radar-watch.sh — run this from cron/launchd at whatever interval is reasonable for the
# alliance's size and activity; the disk cache (60s default TTL) makes a re-run within
# that window nearly free either way.
if ! vd radar check --alliance-id 29 --json > /tmp/radar-last.json; then
  case $? in
    1) osascript -e 'display notification "Radar found something" with title "Veydrift"' ;;
    2) echo "veydrift radar: check could not complete" | mail -s "radar degraded" you@example.com ;;
  esac
fi
```

`osascript`/`mail` above are illustrative, not this package's concern — swap in whatever
notification path the harness actually has (a Slack webhook, `terminal-notifier`, Hermes'
own alerting). The one thing worth keeping regardless of the notifier: branch on the exit
code, not on parsing the human `rich` report — `--json`'s output is what's meant to be
parsed.
