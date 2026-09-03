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
│   ├── CHANGELOG.md             # semver, Keep a Changelog format — see below
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
    ├── CHANGELOG.md             # semver, Keep a Changelog format — see below
    ├── package.json, tsconfig.json
    ├── abi/                     # pinned ABI + provenance — see §6
    ├── src/{cli,abi,allowlist,tx,fleet,policy}.ts, src/providers/
    ├── references/
    └── tests/
```

Runtime state lives in `$VEYDRIFT_HOME` (default `~/.veydrift`), **outside this repo
entirely** — never write there from a test, and never assume anything under it exists.

**Versioning:** each skill keeps its own `CHANGELOG.md` (semver, [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/) format) and its own version number
(`pyproject.toml` for the agent, `package.json` for the wallet) — the two are **not**
versioned in lockstep, since they change at different rates. There's no dedicated
`version` key in `SKILL.md`'s frontmatter (the spec only allows `name`, `description`,
`license`, `allowed-tools`, `metadata`, `compatibility`), so this stays a plain
package-version + changelog convention rather than something either skill reports at
runtime. When you change either skill's behavior, add an entry under that skill's
`CHANGELOG.md` `[Unreleased]` section — patch for fixes/docs, minor for additive
backward-compatible changes, major for a breaking CLI/schema/ABI change.

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
baseline: **518 Python tests, 123 TypeScript tests** (121 passed + 2 intentionally
skipped), both suites green. Run both before calling any change done; they are independent
projects but cover a system with two enforcement layers that must agree (§6).

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
- Any new or changed field on `Policy`/`StrategyCfg`/`ActionsCfg` (or any nested config
  model) must land in `assets/policy.example.json` in the same change — it's the literal
  file `vd init` copies to a fresh `$VEYDRIFT_HOME/policy.json`, not just a spec
  restatement.
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
- **Most of combat stays unreachable by code, not by config.** The `FleetMissionType`
  enum's `AcsDefend`/`Intercept`/`MissileAttack`/`AcsAttack`/`DefenseHold` values require
  an actual source change to both `guard.py`'s `_ALLOWED_MISSION_TYPES`/
  `_COMBAT_MISSION_TYPES` and `allowlist.ts`'s matching pair — that friction is
  deliberate; don't lower it in passing while fixing something else. **`Attack` (via
  `launchFleetMission` mission type 3) and `Missile` (via the wholly separate
  `launchInterplanetaryMissileAttack` entrypoint) are the two exceptions**, since the
  launch-actions plan's commits 5-7 (2026-08-28): `policy.json`'s `allow_combat` key is a
  real, independently-checked gate for both, at `operator` tier — for Attack, resolved by
  `guard.py`'s `_gate_mission_type` (agent side) and `veydrift-wallet`'s
  `resolveAllowCombat` (`policy.ts`, wallet side); for Missile, by a new dedicated
  `guard._gate_missile_target` (agent side) and, on the wallet side, `allowlist.ts`'s
  `checkAllowlist` selector check itself (since Missile has no shared non-combat sibling
  function the way Attack does, its selector is pulled out of the unconditional
  `tierSelectors('operator')` set entirely rather than decoded as a calldata argument —
  a deliberate, documented shape difference between the two layers for this one
  function, see `docs/SPEC.md` correction 72). Neither ever trusts a CLI flag or
  environment variable for `allow_combat` (see `skills/veydrift-wallet/references/
  tx-safety.md`'s residual-limit section for exactly why). Candidate generators exist
  for both — `candidates.generate_attack_candidates` (commit 6, ladder rung `8e:attack`)
  and `candidates.generate_missile_candidates` (commit 7, ladder rung `8f:missile`, more
  conservative still) — each reached only once every earlier rung has found nothing at
  all, targeting the highest-value reachable `/highscores` result whose attack-protection
  is confirmed allowed by a fresh, guard-time re-check (`guard._gate_attack_protection`,
  a gate shared by both action types — never trusting either generator's earlier,
  generation-time read).
- **Alliance membership actions require `policy.actions.allow_alliance` at both layers
  independently, floor `economy` not `operator`.** Unlike combat, this is a real
  config-unlockable feature, not a code-friction-gated one: membership actions on
  `VeydriftAllianceSystem` (a wholly separate deployed contract, its own pinned ABI, its
  own address) carry no fund/combat risk, so the bar is deliberately lower. Checked
  independently by `guard.py`'s `_gate_alliance_action` (agent side, the 23rd gate) and
  `veydrift-wallet`'s `checkAllowlist` (`allowlist.ts`'s `ALLIANCE_SIGNATURES`, checked at
  an inclusive `economy`-or-`operator` tier, unlike combat's single-tier check — `economy`
  is a floor here, not a ceiling). Never planner-proposed — reachable only via
  `vd tick --action`, gated additionally on `policy.strategy.allow_agent_action_override`
  (two independent requirements stack, per `skills/veydrift-agent/references/
  manual-action-override.md`). `_gate_abi_hash` PASSes an alliance action
  unconditionally: `/runtime-config` has no live hash/commit field for this contract to
  compare against, ever, a permanent limit stated in `skills/veydrift-wallet/references/
  abi-pinning.md`'s "Second contract" section, not papered over.
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

**A second, independent pin exists since the alliance feature (2026-09-01)**:
`abi/PINNED.alliance.json` + `abi/VeydriftAllianceSystem.701bed3.json`, same commit, same
`forge build` settings — but with a narrower guarantee than the pin above. `/runtime-
config` exposes `allianceContractAddress` directly but has no `allianceAbiHash`/
`allianceDeploymentCommit` field anywhere, so this pin was verified exactly once, by
construction, and can never be automatically re-checked against a live hash the way
`verify-abi` re-checks the game contract's pin on every call. See `references/
abi-pinning.md`'s "Second contract" section — this is a permanent limit of the upstream
API, not something to work around by inventing a substitute check.

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
`$VEYDRIFT_HOME/logs/proposals.jsonl` and `ticks/<timestamp>.md`, and write **nothing** to
`logs/actions.jsonl` — tier 1 never executes. `logs/strategy.md` gets an entry only when
there's something worth narrating (`tick.py`'s own comment: an ESCALATE, or `guard_report.
decision` not `ALLOW`, and not the purely-structural tier-1 block every tick produces
until promotion) — a routine live NOOP/ALLOW dry-run against an account with nothing to
propose writes `proposals.jsonl` and a `ticks/` entry but no `strategy.md` line; that's
pre-existing suppression, not a bug (corrected here 2026-08-17, judge review — this
section previously implied every dry-run writes `strategy.md`, which is only true for a
tick with something to narrate). `touch $VEYDRIFT_HOME/KILLSWITCH` before a tick to
confirm it halts before any network call beyond `/health`.

The next rung up from a dry-run tick against the live API is exercising a real send — against a
local fork, never mainnet. `skills/veydrift-wallet/src/providers/fork-impersonate.ts` runs the
exact production `sendTx` → `provider.signAndSend` path against a local Anvil fork with an
impersonated account instead of a held key, gated by a loopback guard that makes it inert outside a
fork. `references/fork-testing.md` is the full runbook: starting Anvil, the env vars, the
per-selector command sequence, the two gotchas that cost time otherwise (the memoized public
client, and `/runtime-config` being ungoverned by `VEYDRIFT_RPC_URL`), and four verifications
worth doing beyond a routine sweep — colony-target packing, the two fleet-tuple encoders cross-
checked against real contract state, the fuel formula compared against a real chain-emitted event
(not a balance delta — a first attempt at that method was noisy and is documented as the wrong
tool for this check, `references/fork-testing.md` §8.3), and `simulate`'s gas cap. §9 of that same
document is the round-2 sweep that carried all 7 allowlisted selectors through this same fork
technique — see the bullet above for what that closed and what it didn't.

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

For the full, function-by-function ledger of what's implemented/planned/deferred/out of
scope — derived from the pinned ABI, not hand-maintained — see `docs/COVERAGE.md` rather than
expecting this section to enumerate it; the bullets below are the handful of gaps significant
enough to call out here specifically, not a duplicate of that ledger.

- **`skills/veydrift-agent/src/veydrift_agent/tick.py`'s tier≥2 send path
  (`_send_and_await`) has since run against mainnet for real**, at tier 2 (`economy`) and
  tier 3 (`operator`) — see `README.md`'s Status section. That is separate from, and later
  than, the fork-testing history this bullet otherwise documents in detail below; the fork
  rounds (Anvil, impersonated accounts, never mainnet) predate the real mainnet use and
  remain an accurate record of what fork testing specifically verified — they are not
  superseded by it. It's unit-tested by monkeypatching
  the `walletctl` subprocess boundary, which is real coverage of the Python-side logic,
  and it has now run once against a real chain state: `startBuildingUpgrade` completed
  end-to-end against a local Anvil fork of Base (`status: "success"`, Metal Mine 10 → 11
  on planet 664), the first observation of queue behaviour and lazy settlement above
  level 0 (§10's later bullet), with `calc.build_seconds` confirmed to match the chain
  exactly at 1556s. That same fork run is what surfaced the defect this package's
  `1.1.1` fixed — `_send_and_await` built and sent without ever calling `walletctl
  simulate` first, so a tx that would revert burned real gas to find out instead of a
  free `eth_call` (see `skills/veydrift-agent/CHANGELOG.md`'s `1.1.1` entry). That same
  fork-testing effort has since found a second, related defect in `simulate` itself:
  `simulateTx` (`skills/veydrift-wallet/src/tx.ts`) ran its `eth_call` uncapped, against
  the node's block gas limit rather than `tx.gas` (the figure `send` actually submits) —
  so `simulate` could report `ok: true` for a call that would revert `OutOfGas` once sent
  at its real gas limit. Reproduced live: a `startResearch` call on real accumulated
  state (planet 664) built at gas limit 465588, simulated `ok: true` pre-fix, was sent at
  that same limit, and reverted `OutOfGas` after genuinely executing most of its
  settlement sweep; the identical calldata resent at 931176 (2x) succeeded, proving a
  pure gas shortfall. Fixed by capping `simulate`'s `eth_call` at `tx.gas` (or a
  freshly-fetched, validated-against estimate when `tx.gas` isn't yet known) — see
  `skills/veydrift-wallet/references/tx-safety.md` and `references/fork-testing.md` §8.4.
  This closes the gap in the *simulate mechanism* specifically; it is not a claim that
  gas estimation is now always sufficient, only that an insufficient estimate is now
  caught before send rather than after.
  **Round 2 (2026-08-19, `references/fork-testing.md` §9) exercised the remaining 5 of
  the 7 allowlisted selectors on the same kind of fork.** `startShipProduction` (Solar
  Satellite qty 1) and `startDefenseProduction` (Rocket Launcher qty 1) both live-sent
  from the project's own account, `status: "success"`. `launchFleetMission`'s 6-arg
  (Transport) and 7-arg (Transport with explicit `speedPercent`) overloads were both
  live-sent, `status: "success"` on both — the project's own account structurally cannot
  do this (see below), so a second, real, multi-planet account
  (`0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1`) was temporarily impersonated instead,
  the same no-real-key technique as every other account in this runbook.
  Round 2 also found a previously undocumented contract rule, source-read from
  `VeydriftGameplayModule.sol`'s `_launchFleetMission`: Transport and Deploy additionally
  require `_requirePlanetOwner(targetPlanetId)` — the mission target must itself be a
  planet the sender owns, confirmed by reproduction (`NotPlanetOwner()`, selector
  `0xab2bcfd3`, sending a Transport from planet 664 to a real third-party-owned planet).
  This is why the project's own single-planet account needed the second impersonated
  account above for Transport specifically, and it retroactively confirms
  `candidates.py`'s `generate_transport_candidates` ≥2-owned-planets precondition is the
  literal contract requirement, not an overcautious heuristic — see
  `docs/RESEARCH-ADDENDUM.md` §4.3.
  **Round 3 (2026-08-19, `references/fork-testing.md` §10) closed both of round 2's
  remaining caveats.** `resolveFleetMission` — round 2 could only confirm it by reading
  `VeydriftColonizationModule.sol:237-240` (an invalid/nonexistent mission id silently
  no-ops rather than reverting, by design), since neither test account had an unresolved
  mission — has now been **live-sent** through the exact production `walletctl build →
  simulate → send` path: the same impersonated multi-planet account
  (`0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1`) produced a Colony Ship, launched a
  Colonize mission (id `26480`), and resolved it (`status: "success"`,
  tx `0xb409b6a34413a60fe0ced28a4778ed69d99c6eccde94047d23c3c1b3553002ff`). The source
  read and the live send are complementary, not redundant — the source explains why an
  *invalid* mission id is safe, the send confirms a *valid* one resolves correctly.
  **Colonize's slot-claiming behavior is also now verified live**, not just its encoding:
  `isCoordinateAvailable(2,477,9)` read `true`/`planetCountOf` `10` before the send, and
  `false`/`11` after — the exact targeted coordinate, packed by this codebase's own
  `_encode_colony_target`, genuinely claimed the slot it named on the real deployed
  contract. Producing the Colony Ship needed no unlock chain for this account — its home
  planet already had Shipyard 10 and Impulse Drive 6, both above the
  `VeydriftDependencies.sol:220,223` thresholds (Shipyard ≥4, Impulse Drive ≥3) — so
  **this does not demonstrate the unlock grind itself works**; a single-planet, low-tier
  account (the project's own, planet 664, Shipyard 1) would still need to walk that chain
  from scratch, and round 3 did not exercise that path. One genuine game rule surfaced
  along the way and was worked around, not avoided: the account was already at its
  Astrophysics-derived colony cap (`PlanetLimitReached`, `limit = 1 +
  astrophysicsLevel = 10`, `VeydriftColonizationModule.sol:289-301`) — raising the real
  research cost being unaffordable and orthogonal to the thing under test, a single
  `anvil_setStorageAt` write raised the account's on-chain Astrophysics level by one,
  analogous to this runbook's existing `anvil_setBalance` gas top-up. That write is test
  scaffolding for an unrelated precondition; the Colonize send and resolve themselves ran
  against real, unmodified contract logic. **All 7 selectors are now accounted for via
  fork testing, and the two round-2 caveats are closed.** Mainnet itself has since been
  touched by this codebase for real, separately from this fork-testing effort — see this
  bullet's opening and `README.md`'s Status section.
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
- **Cost scaling above level 0 is still unobserved by this codebase; queue behaviour and
  lazy settlement above level 0 are not anymore.** Verified on-chain 2026-08-17
  (`cast call buildingLevel(uint256,uint8)` against the deployed contract, planet 664):
  Metal Mine 10, Crystal Mine 9, Deuterium Synthesizer 5, Solar Plant 11, Robotics Factory
  2, Shipyard 1, Research Lab 1; `cast call technologyLevel(address,uint8)` gives Energy
  Technology 2, Computer 0. That account was played by hand through the game UI, not
  through this codebase's `walletctl`, at the time this reading was taken — this codebase
  has since submitted real transactions to mainnet itself, at tier 2 (`economy`) and tier 3
  (`operator`); see `README.md`'s Status section. What changed first, before that: a local
  Anvil fork of Base seeded
  from that same chain state (§10's first bullet) has since run this codebase's own
  `build → simulate → send → await receipt → await indexed` path for real, for one
  selector (`startBuildingUpgrade` on the Metal Mine, level 10 → 11) — the first time this
  system, not a human through the UI, has observed a queue actually populate and later
  lazily settle above level 0. `vd calc verify` also cross-checks three duration formulas
  against live API data and passes (confirmed 2026-08-17), and the fork run additionally
  confirmed `calc.build_seconds` matched the chain's own resolved duration exactly
  (1556s) for that one upgrade. What still stands, narrower than before: no per-building
  cost-scaling factor has been observed or verified by this codebase at any level (§5's
  "no cost-scaling function" invariant is about never *computing* one, not about having
  verified the real curve). Every other selector's queue/settlement behaviour above level
  0 was, at the time of this fork round, still unexercised by this system's own
  observation — real mainnet sends have since happened separately (this bullet's opening),
  though this entry doesn't itself catalog which selectors' queue/settlement behavior those
  specifically exercised.
- **A declared `research_priority`/`building_priority` entry never cedes its slot once it
  becomes reachable.** Neither field has a completion criterion — unlike `ship_targets`/
  `defense_targets`'s `count` — so a multi-name list gets stuck on entry #1 forever instead
  of advancing once that entry is built/researched. `candidates.py`'s `unlock_breadth`
  fallback ranking (`docs/SPEC.md` AC64) makes the *undeclared* tail smarter but does
  nothing for this; fixing it needs a structured replacement for `list[str]` carrying an
  optional `target_level` (mirroring `EntityTarget`'s `count`), touching `models.py`, both
  `select_*` functions in `candidates.py`, ~2 dozen test call sites, `policy.example.json`,
  and the inline JSON examples in `docs/PLAYER-GUIDE.md`/`.html`.
- **An empty `building_priority` makes all six infrastructure buildings (Robotics Factory,
  Nanite Factory, Shipyard, Research Lab, Terraformer, Missile Silo) structurally
  unreachable**, not merely deprioritized — `generate_infrastructure_candidates`
  (`candidates.py:732`) returns `[]` unconditionally when the list is empty, unlike
  `research_priority`, which already falls back to ranking every undeclared technology.
  Giving the empty case the same kind of fallback `unlock_breadth` gives research would
  close this, but touches the pinned Phase 2/3 byte-identical-empty-`StrategyCfg`
  acceptance criterion (`docs/SPEC.md` AC25-31) and needs a ladder-position decision —
  does the fallback outrank mines, or sit behind them like research/ship/defense already
  do.

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
- `skills/veydrift-wallet/references/fork-testing.md` — the fork-testing runbook: exercising
  tier≥2 sends against a local Anvil fork with an impersonated account, the intended first real
  use of `sendTx`'s send path against a real chain state (never mainnet). As of round 2 (§9 of
  that document, 2026-08-19) all 7 originally-allowlisted selectors have been either live-sent
  this way or, for `resolveFleetMission`, confirmed correct by source in the absence of a real
  mission to resolve — see this file's §10 for the precise, non-overclaiming summary. **Round 4
  (§11, 2026-08-30)** live-sent both launch-actions-plan combat additions: Attack's full launch
  path (ships/fuel/mission-type/randomness-request all confirmed against real emitted events,
  though battle resolution itself remains out of reach — it needs an off-chain randomness reveal
  this codebase has no part in) and Missile's complete launch-through-resolution path
  (fully deterministic, confirmed against real before/after on-chain defense counts on both the
  origin and target planets, the strongest verification any selector in this runbook has
  received). That round also surfaced a previously-undocumented contract mechanic: an unresolved
  Attack mission locks *both* the origin and the target planet, not just the attacker's side.
  **Round 5 (§12, 2026-09-02)** live-sent 13 of the 15 alliance-membership functions
  (`kickMembers`/`setMembersRole`, the two batch variants, are the only ones not sent —
  structurally identical to their already-verified singular siblings) across a full lifecycle —
  create, invite/cancel, accept, request/cancel/dismiss/approve, role change, ownership
  transfer, kick, leave — using three fresh, alliance-free accounts, every step confirmed
  against real on-chain state (`allianceOf`/`allianceMembers`/`allianceProfile`/
  `allianceInvite`/`allianceJoinRequests` reads), not just emitted events.
- `skills/veydrift-agent/references/radar.md` — the attack/resolved-battle/debris radar
  (new module, `radar.py`, read-only, no `veydrift-wallet` involvement): why
  `incoming_fleets` alone missed a real live attack during this feature's own planning,
  the three independent signals, the two entry points (`vd tick`'s `policy.radar.enabled`
  and standalone `vd radar check --wallet | --alliance-id`), and a real bug this same
  live-verification discipline caught before shipping — `/wallet/{addr}/missions`'
  resolved-attack rows arrive as `kind: "mission"` with a `report` attached, not the
  `kind: "battleReport"` shape the initial implementation assumed.
- `skills/veydrift-agent/references/opportunities.md` — attack/missile/colonize/
  foreign-harvest candidates surfaced independent of `plan.py`'s ladder (new module,
  `opportunities.py`): the ladder is a straight early-return chain, so a lower-priority
  band's candidate is never even generated once a higher band wins that tick; this
  module calls the same `candidates.py` generators a second time, unchanged, to surface
  them anyway. Zero changes to `plan.py`/`candidates.py`/`guard.py`, no new policy flag,
  no persisted state, `vd tick` only.
- `docs/NOTES.md`, `docs/veydrift-agent-prompt.md`, `docs/veydrift-agent-resources.md`,
  `docs/veydrift-briefing.html` — earlier inputs this project was built from; superseded
  in places by the addendum but kept for provenance.
- `docs/COVERAGE.md` — the standing coverage ledger: every pinned-ABI write entrypoint
  (implemented / planned / deferred / out of scope, with why), the game surfaces not yet
  reduced to a single entrypoint, and the verified-but-unused `calc.py` formulas. Regenerated
  against the pinned ABI, not hand-maintained; guarded against silently going stale by
  `skills/veydrift-agent/tests/test_coverage_doc.py`.

**Maintenance note — read this before changing `docs/SPEC.md`.**
`docs/PLAYER-GUIDE.md` and `docs/TECHNICAL-WALKTHROUGH.md` both restate spec content in
prose: the tier table, the policy schema, the decision ladder, the guardrail list, module
responsibilities. That restatement is deliberate — it's what makes them readable without
first internalizing the spec — but it means they go stale exactly when `SPEC.md` changes
underneath them, silently, unless someone remembers to check. **Any change to
`SPEC.md`** — like altering the tier model, the `Policy`/`Action` schema, the decision
ladder's rungs, or a module's responsibilities — needs the same check:
**grep both files for the thing you changed** before considering the change done.
Neither document has a test that would catch drift — this note is the only thing that
currently does.
