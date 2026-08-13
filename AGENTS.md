# AGENTS.md — instructions for coding agents working on this repo

This file is for an agent (or human) **continuing development** of this codebase — build
commands, conventions, and the invariants a change must not break. If you're looking for
what this project *is* or how to *use* the deployed skills, read [`README.md`](README.md)
first; this file deliberately doesn't repeat that.

`CLAUDE.md` at the repo root is a one-paragraph pointer to this file.

## 1. Orientation

Two installable Claude/Hermes skills:

- **`skills/veydrift-agent/`** (Python, uv) — reads Veydrift's API, runs deterministic
  calculators, proposes zero or one action per tick. Never signs anything; never imports
  `viem`/`ethers`/`web3` (grep-verifiable — acceptance criterion 15 of `docs/SPEC.md`).
- **`skills/veydrift-wallet/`** (TypeScript, npm) — the only thing in this repo that builds
  real calldata, signs, or submits. Independently re-validates every transaction against
  its own allowlist regardless of what the agent skill already checked.

Full spec: `docs/SPEC.md`. Contract/backend research: `docs/RESEARCH-ADDENDUM.md`.
Deferred wallet-provider evaluation: `docs/wallet-provider-research.md`.

## 2. Repository map

```
skills/
├── veydrift-agent/
│   ├── SKILL.md                 # progressive-disclosure entry point
│   ├── pyproject.toml, uv.lock
│   ├── src/veydrift_agent/
│   │   ├── models.py            # pydantic: Policy, Action, Snapshot, GuardReport — see §4
│   │   ├── cli.py                # typer app; mounts each module's `app` sub-app — see §4
│   │   ├── read.py, calc.py, plan.py, guard.py, tick.py, log.py
│   │   └── ids.py, http.py, state.py, fmt.py
│   ├── schemas/                 # GENERATED from the pydantic models — do not hand-edit
│   ├── references/              # loaded into context on demand; see SKILL.md's routing table
│   ├── assets/                  # policy.example.json, launchd plist template
│   └── tests/
└── veydrift-wallet/
    ├── SKILL.md
    ├── package.json, tsconfig.json
    ├── abi/                     # pinned ABI + provenance — see §6
    ├── src/{cli,abi,allowlist,tx,fleet,policy}.ts, src/providers/
    ├── references/
    └── tests/
```

Runtime state lives in `$VEYDRIFT_HOME` (default `~/.veydrift`), **outside this repo
entirely** — never write there from a test, and never assume anything under it exists.

## 3. Setup and test commands

```bash
# Python skill
uv run --directory skills/veydrift-agent pytest -q
uv run --directory skills/veydrift-agent vd doctor      # confirms every subcommand wired

# Wallet skill
npm --prefix skills/veydrift-wallet install
npm --prefix skills/veydrift-wallet test
npm --prefix skills/veydrift-wallet run typecheck
```

`uv run` creates and caches its own venv on first use — no separate install step. Current
baseline: **257 Python tests, 104 TypeScript tests**, both suites green. Run both before
calling any change done; they are independent projects but cover a system with two
enforcement layers that must agree (§6).

**Never run these against your real `$VEYDRIFT_HOME`.** Point `VEYDRIFT_HOME` at a scratch
directory for any manual testing — ticks, mock policies, anything — so you don't corrupt a
real account's proposal/action history or trip the shared killswitch.

## 4. The frozen interfaces

`skills/veydrift-agent/src/veydrift_agent/models.py`, `cli.py`, and `pyproject.toml` are
the contract every other module in that package codes against. Treat schema changes here
as breaking:

- `models.py`'s pydantic models are the on-disk JSON format for `policy.json`,
  `proposals.jsonl`, `actions.jsonl`, and the generated `schemas/*.json`. Renaming or
  retyping a field breaks anything that reads old log lines.
- `cli.py` mounts each module's top-level `app: typer.Typer` with **tolerant imports** —
  a half-built tree still runs the parts that exist. Don't hardcode subcommand wiring
  anywhere else; add a new module to `_SUBAPPS` instead.
- These names were "frozen" during the original multi-agent build so parallel work
  packages couldn't collide on them (see `docs/SPEC.md` §7). That build is finished, but
  the discipline is still the right one: change these deliberately, not incidentally.

## 5. Invariants a change must not break

These are enforced by tests and/or grep-verifiable, not just documented — if you're
touching related code, re-run the check named alongside each one.

- **`veydrift-agent` never signs and never imports a signing library.**
  `grep -rn "viem\|ethers\|web3\|eth_account" skills/veydrift-agent/src/` must return
  nothing. Signing lives exclusively in `veydrift-wallet`.
- **`calc.py` contains no cost-scaling function.** Live cost at the current level comes
  from the API's `cost` object; per-building factors are unpublished rationals, and
  recomputing them is exactly how an affordability check goes silently wrong. If you find
  yourself writing `base * factor ** level`, stop.
- **The two tier-enforcement layers must agree.** `guard.py`'s `_MIN_TIER_FOR_FUNCTION`
  and `allowlist.ts`'s `ECONOMY_SIGNATURES`/`LAUNCH_FLEET_MISSION_SIGNATURES` encode the
  same policy in two languages on purpose — the wallet engine re-checks independently of
  whatever the agent already validated. They drifted once already (`startShipProduction`
  was in one tier's set and not the other, and nothing caught it for a while).
  `tests/test_guard.py::test_tier_map_agrees_with_the_wallet_engines_allowlist` parses
  both sides and fails naming the diff — **run it whenever you touch either map.**
- **A guardrail must never pass vacuously on absent data.** `models.py` defaults many
  numeric fields to `0` and objects to empty, so "the API omitted this" and "this is
  genuinely zero" can look identical. Every gate in `guard.py` that depends on optional
  data resolves that ambiguity toward `BLOCK`/`ESCALATE`, never `PASS` — see
  `references/guardrails.md` for the gate-by-gate rationale. This was the single highest-
  value class of bug two rounds of adversarial review found; assume a new gate can
  reintroduce it and write the missing-data test alongside the happy-path test.
- **No dollar amount is ever compared across a unit mismatch.** The first review found gas
  *units* being compared against a *wei* ceiling — inert by ~10 orders of magnitude,
  because nothing crossed the `veydrift-wallet` → `veydrift-agent` boundary in a test.
  `walletctl build`/`receipt` emit `estimatedCostWei`/`actualCostWei` as decimal strings,
  never a substituted `0` — a failed fee fetch must emit `null` and let the Python side
  escalate, not silently pass a ceiling check. `test_tick.py`'s unit-boundary test exists
  specifically to catch a regression here.
- **A reverted transaction is never recorded as a success.** `receipt --hash` reports
  `status: "success" | "reverted"` from the real receipt; `tick.py` calls `record_revert`
  on a revert and never counts it toward `executions_count`. An unknown/unfetchable status
  is treated as unknown, never success.
- **`send` never becomes implicit.** No env var, policy field, or flag makes `--confirm`
  optional. `policy.wallet_engine.require_confirmation` gates whether `tick` sends
  automatically at all — it does not weaken the CLI-level `--confirm` requirement, ever.
- **Combat stays unreachable by code, not by config.** `policy.json`'s `allow_combat` key
  is read and then ignored everywhere. Enabling `Attack`/`AcsAttack`/`MissileAttack`/
  `Intercept` requires an actual source change to `_MIN_TIER_FOR_FUNCTION` and
  `allowlist.ts` — that friction is deliberate; don't lower it in passing while fixing
  something else.
- **Secrets never reach a log or a tracked file.** `log.py` scrubs any
  `0x[0-9a-fA-F]{64}` that isn't a known tx hash, and refuses to write a value matching a
  configured secret env var. Before committing, `git diff --cached` anything touching
  `providers/`, `state.py`, or `log.py` by eye, not just by test — a scrub filter is a
  second layer, not a substitute for looking.

## 6. The ABI pin — how to re-verify or re-pin it

`skills/veydrift-wallet/abi/PINNED.json` records the ABI hash from the **deployed**
contract at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(`sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99`). **`main` on
the Veydrift contracts repo has already drifted from this** — building from `main` gives a
different, wrong ABI (confirmed: it's missing `firstPlanetOf`/`hasFirstPlanet` and adds
functions like `playerScore` that revert on the deployed contract). Always check out the
pinned commit, never `main`, before rebuilding:

```bash
git -C /Users/santteegt/GitRepositories/clones/veydrift checkout 701bed3578cff4d134657c714c599dbdb55a4b6a
git -C /Users/santteegt/GitRepositories/clones/veydrift submodule update --init --recursive --depth 1
cd /Users/santteegt/GitRepositories/clones/veydrift/packages/contracts
rm -rf out && forge build --skip test --skip script
```

Then recompute `sha256(JSON.stringify(artifact.abi))` (compact separators, forge's key
order) and compare against a live `GET https://api.veydrift.com/runtime-config`'s
`backend.build.deploymentAbiHash`. Full recipe and the exact foundry settings that affect
reproducibility (`solc 0.8.28`, `optimizer_runs 1`, `via_ir true`, `cbor_metadata false`)
are in `skills/veydrift-wallet/references/abi-pinning.md`. If the contract has genuinely
been redeployed, re-pin deliberately — don't let a mismatch silently pass by relaxing the
comparison.

## 7. Two silent-corruption traps in the write path

Neither produces an error; both produce a wrong transaction. If you touch fleet-mission
encoding, re-read `docs/RESEARCH-ADDENDUM.md` §3–§4 in full, not just this summary:

1. **The 14-slot fleet tuple is not the 16-entry Ship enum.** SolarSatellite (id 9) and
   Crawler (id 15) can't fly and are omitted, so tuple indices 9–13 map to Ship ids 10–14.
   Always go through `shipCountsToFleetTuple()` (`fleet.ts`) — never index the tuple with
   a raw Ship id. `fleet.test.ts` pins a Destroyer at tuple index 9, not 10.
2. **`launchFleetMission` is overloaded** — a 7-arg and a 6-arg form both exist on the
   deployed ABI. Always resolve by full signature (`resolveFunctionAbi`), never by name.

Also: `attackProtectionStatus`, `collectResources`, `debrisField`, `maxRaidLoot`,
`protectedResources`, `raidableResources` are `nonpayable` in the ABI but semantically
reads (they lazily settle before returning). `sendTx` refuses them outright — route them
through `simulate` instead, or you pay gas for a read.

## 8. Verifying a change end-to-end

Beyond the unit suites in §3:

```bash
# A full tick against the live API, writing nothing but a dry-run report
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd tick init
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd tick --dry-run

# The formula layer against live data
uv run --directory skills/veydrift-agent vd calc verify

# The ABI pin against live /runtime-config
cd skills/veydrift-wallet && npx tsx src/cli.ts verify-abi
```

A dry-run tick should write a pretty report plus one entry each to
`$VEYDRIFT_HOME/logs/proposals.jsonl` and `logs/strategy.md`, and write **nothing** to
`logs/actions.jsonl` — tier 1 never executes. `touch $VEYDRIFT_HOME/KILLSWITCH` before a
tick to confirm it halts before any network call beyond `/health`.

## 9. Multi-agent build/judge workflow used on this repo

`.claude/agents/veydrift-builder.md` and `.claude/agents/veydrift-judge.md` define two
Claude Code subagents (`model`/`effort` in frontmatter) used to build and review this
repository's work packages, and remain available for future work in the same shape.

**A new definition is not available immediately, but it does not require a restart
either.** Observed directly: both files were written mid-session, and an `Agent` call
naming `veydrift-builder` failed moments later with `Agent type 'veydrift-builder' not
found`, listing only the agents present at session start. Later in the same session, with
no restart, both types appeared and became usable. The registry does refresh — just not
synchronously with the write.

Practical consequence for an orchestrator: **do not block on a definition you just
wrote.** Either create subagent definitions before the session that will use them, or fall
back to `subagent_type: general-purpose` with an explicit `model` override, folding the
definition's system prompt into the task prompt. That fallback costs you the `effort`
frontmatter — the `Agent` tool's parameters can set the model but not the reasoning
effort, which only a definition file can pin — so a fallback run inherits the parent
session's effort level instead.

Two adversarial review passes (Fable 5) have run against this codebase so far; both found
real, previously-unnoticed defects — see `git log` for what each caught and fixed. If
you're extending this system, a fresh judge pass after a substantial change is cheap
insurance, not ceremony.

## 10. Known gaps — the places most likely to hide the next bug

- **`skills/veydrift-agent/src/veydrift_agent/tick.py`'s tier≥2 send path
  (`_send_and_await`) has never run against a real chain.** It's unit-tested by
  monkeypatching the `walletctl` subprocess boundary, which is real coverage of the
  Python-side logic, but the actual `build → simulate → send → await receipt → await
  indexed` sequence against mainnet is unexercised. If you're the one who first runs it
  for real, budget extra scrutiny there.
- **`walletctl`'s tier check defends against a misconfigured caller, not a hostile one.**
  It reads tier from `$VEYDRIFT_HOME/policy.json`, but falls back to a caller-supplied
  `--tier` when no policy file exists — a process that controls its own environment can
  point `VEYDRIFT_HOME` at an empty directory and assert any tier. Documented, not fixed,
  because the fallback is legitimately needed for standalone use; see
  `skills/veydrift-wallet/references/tx-safety.md`'s residual-limit section before
  treating this check as a security boundary rather than a footgun guard.
- **`plan.py`'s energy-first branch can select `startShipProduction` (a Solar Satellite)
  on a hot planet.** This must respect `policy.actions.allow_ships` on every path that can
  emit it, not just the shipyard-idle rung — it missed one path once already
  (`test_plan.py::test_planet_hot_falls_back_to_solar_plant_when_ships_disallowed` pins
  the fix). If you add a new path that can produce a `ShipAction`, gate it explicitly.
- **Cost scaling, queue behaviour, and lazy settlement above level 0 are unobserved.**
  The account this was built and tested against has taken zero on-chain actions — every
  level is 0, every queue is idle. Formulas are verified against contract source and
  live level-0 data only. Any planner path that assumes a populated queue or a
  non-trivial cost curve is correct by derivation, not by observation.

## 11. Pointers into `docs/`

- `docs/PLAYER-GUIDE.md` — a full player-facing tutorial: environment and skill install,
  wallet/keystore setup, a `policy.json` walkthrough with every field explained, and the
  tier-promotion checklist. Synthesis, not source of truth — see the maintenance note below.
- `docs/TECHNICAL-WALKTHROUGH.md` — a full developer-facing review: the spec, the
  architecture, the codebase module by module, the two enforcement layers, the known
  silent-corruption traps, and what's tested versus genuinely unverified. Also synthesis;
  same maintenance note applies.
- `docs/SPEC.md` — the full implementation spec and every acceptance criterion this repo
  is checked against, including inline-dated corrections from both judge passes.
- `docs/RESEARCH-ADDENDUM.md` — contract- and backend-source-derived corrections: the real
  API route list, the real `Defense`/`FleetMissionType` enums, the ABI hash, the
  write-entrypoint list.
- `docs/wallet-provider-research.md` — every wallet-provider candidate evaluated against
  the address-binding constraint (`README.md` has the short version).
- `docs/NOTES.md`, `docs/veydrift-agent-prompt.md`, `docs/veydrift-agent-resources.md`,
  `docs/veydrift-briefing.html` — earlier inputs this project was built from; superseded
  in places by the addendum but kept for provenance.

**Maintenance note — read this before changing `docs/SPEC.md`.**
`docs/PLAYER-GUIDE.md` and `docs/TECHNICAL-WALKTHROUGH.md` both restate spec content in
prose: the tier table, the policy schema, the decision ladder, the guardrail list, module
responsibilities. That restatement is deliberate — it's what makes them readable without
first internalizing the spec — but it means they go stale exactly when `SPEC.md` changes
underneath them, silently, unless someone remembers to check. If a change alters the tier
model, the `Policy`/`Action` schema, the decision ladder's rungs, or a module's
responsibilities, **grep both files for the thing you changed** before considering the
change done. Neither document has a test that would catch drift — this note is the only
thing that currently does.
