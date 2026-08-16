# Technical Walkthrough

A full review of the spec, architecture, codebase and documentation, for someone joining
this project as a developer — extending it, auditing it, or just trying to understand why
it's shaped the way it is before touching anything. If you're a player wanting to *use*
this, [`PLAYER-GUIDE.md`](PLAYER-GUIDE.md) is what you want instead; this document assumes
you're reading code, not running ticks.

This is a synthesis, not a replacement for the primary sources it points to. Where this
document simplifies, the primary source (`docs/SPEC.md`, `AGENTS.md`, a specific
`references/*.md` file) is named so you can go deeper.

## Table of contents

1. [The one-paragraph version](#1-the-one-paragraph-version)
2. [Why two skills, not one](#2-why-two-skills-not-one)
3. [The spec, reviewed](#3-the-spec-reviewed)
4. [Repository architecture](#4-repository-architecture)
5. [`veydrift-agent`, module by module](#5-veydrift-agent-module-by-module)
6. [`veydrift-wallet`, module by module](#6-veydrift-wallet-module-by-module)
7. [A tick, end to end](#7-a-tick-end-to-end)
8. [The two independent enforcement layers](#8-the-two-independent-enforcement-layers)
9. [Silent-corruption traps (and how each is closed)](#9-silent-corruption-traps-and-how-each-is-closed)
10. [How this was built, and what that explains about the code](#10-how-this-was-built-and-what-that-explains-about-the-code)
11. [What's tested, what's fixture-only, what's genuinely unverified](#11-whats-tested-whats-fixture-only-whats-genuinely-unverified)
12. [Documentation map](#12-documentation-map)
13. [Extending this system](#13-extending-this-system)

---

## 1. The one-paragraph version

An agent reads Veydrift's public game API, runs deterministic calculators against it, and
proposes at most one action per tick — a typed `Action`, never signed bytes. A completely
separate program builds the actual transaction for that action, checks it against its own
independent allowlist regardless of what the first program already validated, and only
submits it if a human (or a tier-2+ policy that a human explicitly configured) types an
exact confirmation flag. A three-tier policy field, edited by hand, is the only thing that
ever lets more of that pipeline run for real. Nothing has ever submitted a transaction from
this codebase to Veydrift.

## 2. Why two skills, not one

This isn't an accident of packaging — it's the load-bearing design decision, and almost
every other choice in the repo follows from it.

`veydrift-agent` is Python, reads an HTTP API, and **never imports a signing library** —
`grep -rn "viem\|ethers\|web3\|eth_account" skills/veydrift-agent/src/` returning nothing
is an actual, checked invariant (`AGENTS.md` §5), not a description. `veydrift-wallet` is
TypeScript, is the only thing that ever calls `signAndSend`, and independently re-derives
its own allowlist from a live `/runtime-config` fetch and the pinned ABI — it does not
trust anything the agent skill already checked. A fully compromised `veydrift-agent` can
construct any `Action` it wants and hand it to `walletctl build`/`simulate` freely; it
cannot make `walletctl send` submit anything without `--confirm` on that exact command
line, and even then the destination, selector, and (at tier 3) mission type are checked
again from scratch.

The practical benefit: the two languages can't accidentally share a dependency, a global,
or a mutable object that lets a proposal bypass the allowlist. The practical cost: the two
enforcement layers have to be kept in sync by hand across a language boundary, which is
exactly the kind of thing that drifts silently — see §8 for how that drift actually
happened once, and the test that now catches it.

## 3. The spec, reviewed

The full spec is [`SPEC.md`](SPEC.md) (620+ lines as of this writing, v2.1). Read that
document for the acceptance criteria and work-package breakdown; this section is what
matters for understanding the *shape* of the result.

**Decisions taken, and why (§0 of the spec):**

| Decision | Choice | Why |
| --- | --- | --- |
| API layer | Scripts, not an MCP server | Zero ops, native fit for a skill's progressive disclosure; an MCP server later would wrap the same module rather than reimplement it |
| Runtime split | Python for reads/calc/loop, TypeScript for signing | `viem` lives naturally in TS; the wallet engine is meant to be swappable independent of the planner |
| Scheduler | One idempotent `tick` entrypoint | Harness-agnostic — Claude Code's `/loop`, Hermes' own scheduler, and bare `launchd` all just call the same command at their own cadence |
| Config format | JSON, not YAML | A hand-rolled YAML parser was the actual weak point of an earlier draft, for a file that is a *security policy* |
| Wallet providers | `keystore` + `envkey`, both real; no hosted/MPC provider | Two genuinely working implementations prove an interface is swappable; a stub proves nothing |

**What the spec explicitly does not attempt (§1's non-goals):** combat, alliances, ACS,
migration, referrals, NFT burns, the ERC-20 market bridge, and any raid-profitability
model — `protectedResources`' actual semantics are unconfirmed, so nothing here builds
loot logic on it. Read `docs/RESEARCH-ADDENDUM.md` §6 before assuming otherwise.

**The spec was wrong at least three times, and says so inline rather than being silently
rewritten.** Worth reading these corrections specifically, because each one describes a
real defect that shipped and was later caught:

- §4's original tier table never allocated `startShipProduction` to any tier, while the
  planner could propose it — a config knob (`allow_ships`) that could never actually work.
- §5.9's worked pretty-print example showed `guards: 16/16 pass` at tier 1, which is
  impossible — the tier gate must fail at tier 1 by definition. Left uncorrected, every
  tier-1 proposal would report `decision=block` and drown the promotion-evidence signal
  `--readiness` exists to surface.
- §6.4 never said where `walletctl` should read the tier it enforces from — the
  implementation took it from the caller, which is exactly the actor the check exists to
  defend against.

Each is dated and marked as a **correction**, not deleted and rewritten as if it were
always correct. If you're auditing this codebase, that inline history is worth reading —
it's a record of what a reviewer should specifically re-check in code that looks similar.

## 4. Repository architecture

```
skills/
├── veydrift-agent/            Python (uv). Reads, calculates, plans. Never signs.
│   ├── SKILL.md                progressive-disclosure entry point
│   ├── pyproject.toml, uv.lock
│   ├── src/veydrift_agent/     9 modules, ~5,700 lines — see §5
│   ├── schemas/                policy.schema.json, action.schema.json — GENERATED, don't hand-edit
│   ├── references/             10 files, ~2,700 lines, loaded on demand — see §12
│   ├── assets/                 policy.example.json, launchd plist template
│   └── tests/                  257 tests
└── veydrift-wallet/            TypeScript (npm). Signs. Nothing else does.
    ├── SKILL.md
    ├── package.json, tsconfig.json
    ├── abi/                     pinned ABI + provenance — see §6, §9
    ├── src/                     10 modules, ~1,800 lines — see §6
    ├── references/              3 files, ~480 lines
    └── tests/                   104 tests
```

Both trees are self-contained on purpose: `npx skills add` copies only `skills/<name>/`
into wherever a harness keeps its skills, so nothing at the repository root (`docs/`,
`AGENTS.md`, `README.md`) travels with an install. Every citation inside a `SKILL.md` or
`references/*.md` file to something outside its own skill tree is either (a) a link to the
sibling skill, explicitly framed as "only if that skill is also installed," (b) a link to
the deployed contract's public GitHub source (externally hosted, independently
addressable, not part of this repo's own copy problem), or (c) gone — internal-repo
citations to `docs/*.md` were deliberately stripped from both skills' shipped files once
this was audited; see `git log` for that pass and why (short version: `SKILL.md` loads on
every single trigger, so a citation nobody can follow sitting there is a standing context
tax, not a convenience).

**Runtime state lives outside this repo entirely** — `$VEYDRIFT_HOME`, default
`~/.veydrift`, created on first `vd tick init`. Nothing under it is tracked by git, and no
test should ever write there; point `VEYDRIFT_HOME` at a scratch directory for any manual
verification.

## 5. `veydrift-agent`, module by module

| Module | Lines | Role |
| --- | --: | --- |
| `models.py` | 332 | **Frozen.** Every pydantic model — `Policy`, `Action`, `Snapshot`, `GuardReport`, `UnsignedTx` — is the on-disk JSON shape for `policy.json`, both log files, and the generated schemas. Treat a field rename here as a breaking change to every log line ever written. |
| `cli.py` | 59 | **Frozen.** Mounts each module's `app: typer.Typer` with tolerant imports — a half-built tree still runs the parts that exist. Add a new module to `_SUBAPPS`, never wire a subcommand elsewhere. |
| `ids.py` | 362 | The six canonical contract enums (Building/Technology/Ship/Defense/FleetMissionType/Resource), transcribed directly from the deployed contract source, not from Veydrift's own docs (which get two of them wrong). |
| `http.py` | 199 | The API client: `httpx`, `tenacity` retry (5xx/network only, never 4xx), a disk cache under `$VEYDRIFT_HOME/cache/` honoring per-route max-age. |
| `read.py` | 830 | One `vd read` subcommand per API route (18 targets). `--summary` mode is the default and is capped near 2KB; `battle-reports`/`highscores` refuse to print to stdout at all — `--out` is mandatory, since they run 60KB–2.2MB uncompressed. |
| `calc.py` | 737 | Pure formulas, no network calls, one docstring citation per function. Contains **no cost-scaling function** by design — live cost always comes from the API's own `cost` field, since the per-building factors are unpublished rationals and recomputing them is exactly how affordability checks go wrong silently. |
| `plan.py` | 658 | The decision ladder (§7 below) and the planet-trait-derived build order. The energy-first invariant lives here: before proposing a mine upgrade, it computes post-upgrade energy `required` vs. current `produced` fresh, every tick — never a fixed level-offset heuristic, because the true gap between mine level and required Solar Plant level widens as levels climb. |
| `guard.py` | 640 | 16 guardrail gates, every one evaluated and reported on every call — never short-circuited, so a passing tick's verdict list is as informative as a blocked one. The rule every gate follows: missing data resolves toward `BLOCK`/`ESCALATE`, never `PASS`. |
| `state.py` | 287 | `$VEYDRIFT_HOME` resolution, `AgentState` (pending txs, cumulative gas, revert counts), the tick lockfile, `KILLSWITCH` detection. |
| `tick.py` | 978 | The orchestrator — the nine-step loop (§7), the `walletctl` subprocess bridge, `--readiness`. The single largest module, and where both criticals a first-pass judge review found actually lived (§10, §11). |
| `log.py` | 377 | Four log sinks, secret scrubbing (`0x[0-9a-fA-F]{64}` patterns that aren't a known tx hash never get written), the pretty-report renderer, `--digest`. |

## 6. `veydrift-wallet`, module by module

| Module | Lines | Role |
| --- | --: | --- |
| `abi.ts` | 225 | Loads the pinned ABI, resolves a function by **full canonical signature** (never a bare name — `launchFleetMission` is overloaded on the deployed contract), computes selectors. |
| `allowlist.ts` | 214 | `checkAllowlist` — five checks, always all five evaluated and reported: destination against a **live** `/runtime-config` fetch, selector against the tier's set (computed from the pinned ABI, never a hand-typed hex constant), `value == 0`, `chainId == 8453`, and at `operator` tier, the `launchFleetMission` mission-type argument decoded from calldata and restricted to Transport/Deploy/Harvest. |
| `tx.ts` | 400 | `build`/`simulate`/`send`/`receipt`. `build` emits `estimatedCostWei` (gas units × live `maxFeePerGas`), not a bare gas-unit number — see §10 for why that distinction is load-bearing. `receipt` reports real `status: "success" \| "reverted"` from the actual receipt, never synthesized. RPC target is `VEYDRIFT_RPC_URL` (default `https://mainnet.base.org`), the one chokepoint every read and write resolve through — swap in a dedicated endpoint (Alchemy etc.) to avoid the public endpoint's rate limits. |
| `fleet.ts` | 147 | `shipCountsToFleetTuple()` — the one function permitted to build the 14-slot fleet-mission tuple; see §9. |
| `policy.ts` | 116 | Reads `tier` from `$VEYDRIFT_HOME/policy.json` rather than trusting a caller-supplied flag; refuses (exit 4) on disagreement or a malformed file rather than falling back to a permissive default. |
| `cli.ts` | 329 | The `walletctl` command surface: `status`, `verify-abi`, `build`, `simulate`, `send`, `receipt`. `send` without `--confirm` always exits non-zero and prints exactly what it would have sent — the same code path as a real send, short-circuited right before `provider.signAndSend`. |
| `providers/keystore.ts` | 113 | Default provider. Encrypted EIP-2335/geth JSON keystore, decrypted via `ethers.Wallet.fromEncryptedJson` — the one place `ethers` earns its dependency slot in a codebase that otherwise uses `viem` for everything chain-side, because scrypt+AES decryption isn't something to hand-roll. |
| `providers/envkey.ts` | 152 | Testing-only provider. Raw `VEYDRIFT_PRIVATE_KEY`, loud startup warning every use, a best-effort (not primary-control) check that refuses to start if the key value is also found anywhere in the containing git repo outside `tests/`. |
| `providers/types.ts`, `providers/index.ts` | 37 + 40 | The `WalletProvider` interface and the selection logic (`policy.wallet_engine.provider`, overridable by `WALLET_PROVIDER`). |

## 7. A tick, end to end

```
1. load + validate policy      →  extra key = hard error, missing required field = hard error
2. killswitch check             →  $VEYDRIFT_HOME/KILLSWITCH present? halt before any
                                    network call beyond /health
3. reconcile pending txs        →  resolve anything left over from a prior tick first
4. snapshot                     →  read.py: /health, /infrastructure, /research, /shipyard,
                                    /defenses, /fleet-visibility -- composed into one Snapshot
5. plan                         →  plan.py: the decision ladder, zero or one Action out
6. guard                        →  guard.py: all 17 gates, full verdict list, one Decision
7. if ALLOW and tier>=2         →  walletctl build -> simulate -> send, await receipt,
   and not --dry-run               THEN await INDEXED (a confirmed receipt is not the
                                    same as indexed state -- no dependent action follows
                                    until the index actually reflects it)
8. log                          →  proposal logged, unless content-identical (excl.
                                    ts/tick) to the immediately-previous logged proposal
                                    (dedup, tick.py's _fingerprint_proposal); action only
                                    if actually executed
9. pretty report                →  stdout + logs/ticks/<iso>.md
```

The decision ladder inside step 5, first match wins:

```
0. KILLSWITCH present               -> HALT
1. /health not ok                   -> NO-OP, reason recorded
2. pending tx unreconciled          -> NO-OP, reconcile first
3. mission Resolving > 60s          -> resolveFleetMission (permissionless, free)
4. incoming hostile fleet           -> ESCALATE, no proposal at all
5. a resource near its storage cap  -> spend it, or build the matching storage
6. building queue empty             -> next build
7. research queue empty             -> next research
8. shipyard idle, economy on track  -> ships/defense, only if policy allows
9. otherwise                        -> NO-OP, explicit reason
```

`--dry-run` is the default at tier 1 and cannot be disabled there — `tick._effective_dry_run()`
forces it regardless of the flag whenever `policy.tier is Tier.ADVISOR`. It's doubly
enforced: even if `--dry-run` were somehow bypassed, `guard.py`'s own `tier` gate BLOCKs
every onchain function below its minimum tier, so step 7's `decision is ALLOW` condition is
never true at tier 1 either. Two independent reasons landing on the same outcome is
intentional redundancy, not accidental duplication.

## 8. The two independent enforcement layers

`guard.py`'s `_MIN_TIER_FOR_FUNCTION` (Python) and `allowlist.ts`'s `ECONOMY_SIGNATURES` /
`LAUNCH_FLEET_MISSION_SIGNATURES` (TypeScript) encode the **same** tier policy in two
languages, on purpose — the wallet engine re-checks independently of whatever the agent
already validated, so a compromised agent can't talk its way past the allowlist by lying
about the tier. They drifted once: `startShipProduction` was granted to the `economy`
tier's *table* in the spec while `allowlist.ts` and the original `guard.py` map both still
excluded it (§3's first spec correction), and nothing caught the mismatch for a while.

The fix wasn't just adding the missing entry — it was adding
`tests/test_guard.py::test_tier_map_agrees_with_the_wallet_engines_allowlist`, which
**parses `allowlist.ts` directly** (regex over the `const ECONOMY_SIGNATURES = [...]`
block) and diffs the resulting set against `guard.py`'s own map, failing with the exact
names on each side if they ever disagree again. This is deliberately not a
"remember to update both" comment — a comment doesn't run in CI. If you're adding a new
tier-gated function, that test is the one to run before you're done, not just the two
suites separately.

## 9. Silent-corruption traps (and how each is closed)

Two things about the deployed contract produce a *wrong transaction*, not an error, if you
get them wrong — the kind of bug that's expensive precisely because nothing crashes.

**The 14-slot fleet tuple is not the 16-entry `Ship` enum.** Every fleet entrypoint takes a
fixed `(uint32 × 14)` tuple, but `SolarSatellite` (id 9) and `Crawler` (id 15) can't fly
and have no slot — every flyable ship id from 10 upward is shifted down one tuple index. A
Destroyer, Ship id 10, lands at tuple index **9**, not 10. `shipCountsToFleetTuple()`
(`fleet.ts`) is the *only* function permitted to build this tuple; `fleet.test.ts` pins a
Destroyer at index 9 specifically, and the function throws on a non-flyable ship input even
at count zero rather than silently dropping it.

**`launchFleetMission` is overloaded.** A 7-arg and a 6-arg form both exist on the deployed
ABI simultaneously. Selecting by bare function name is genuinely ambiguous to both `viem`
and `ethers` — `abi.ts`'s `resolveFunctionAbi()` requires the full canonical signature
string everywhere in this codebase, never a name lookup.

A third, related class: six ABI functions (`protectedResources`, `raidableResources`,
`maxRaidLoot`, `debrisField`, `collectResources`, `attackProtectionStatus`) are declared
`nonpayable` — no `view`/`pure` modifier — because they lazily settle state before
returning, not because they're meant to be sent as transactions. `walletctl send` refuses
all six unconditionally, at every tier, even with `--confirm`; the correct path is
`simulate`, or you pay real gas for what is semantically a read.

## 10. How this was built, and what that explains about the code

This repo was built by a multi-agent pipeline: a planning pass wrote `docs/SPEC.md`,
parallel Sonnet-model builder agents implemented independent work packages against that
frozen spec, and Fable-model judge passes adversarially reviewed the result twice. That
history is directly visible in the code, and worth knowing before you assume something is
either over-engineered or under-tested:

**Two real, previously-unnoticed defects survived 316 passing tests** before the first
judge pass, both living exactly at the seam between the two languages — the place neither
side's own test suite could see:

- `walletctl build` emitted `estimateGas`'s result — **gas units** — and `tick.py` compared
  it directly against `gas_per_tx_wei`/`gas_per_day_wei` — **wei** ceilings. A typical Base
  transaction is ~10⁵ gas units against a 3×10¹⁵ wei ceiling: the gate could never fire, at
  any gas price, because nothing crossed the package boundary in a test. Fixed by having
  `build` emit `estimatedCostWei` (gas units × live `maxFeePerGas`) as a decimal string,
  `null` rather than a substituted zero on a failed fee fetch, and a dedicated
  unit-boundary regression test that asserts wei-scale numbers are what's actually compared.
- `tick.py` never read `receipt.status`. A reverted transaction was logged to
  `actions.jsonl` — the "executed only" audit trail — as a success, and
  `AgentState.record_revert` was called by no production code at all, so the
  `revert_streak` gate could never fire and `escalation.on_revert_count` was dead
  config. Fixed by having `receipt` report the real `status` from the actual receipt,
  `tick.py` charging `actualCostWei` and calling `record_revert` on a revert, and treating
  an unfetchable/unknown status as unknown — never success.

**The second judge pass found the residual gap after the first round of fixes**: the
`allow_ships` policy flag was enforced on one path that could emit a ship-production action
but not another — a hot planet's energy-first branch could still propose a Solar Satellite
regardless of the flag, because the flag check lived at the ladder's rung-8 shipyard-idle
path, not inside the energy-source-selection helper both paths share. Same defect *class*
as the tier-map drift in §8: two things that needed to agree, and nothing forced them to.

The practical lesson for anyone extending this code: **a config flag or an enforcement
rule that's checked in one place and emitted from two is exactly where the next bug is
likely to live.** If you add a second path that can produce the same kind of `Action` an
existing gate already checks, verify the gate actually covers the new path — don't assume
it does because it covers the first one.

## 11. What's tested, what's fixture-only, what's genuinely unverified

Precision matters here more than a clean "it's tested" claim would suggest.

- **257 Python + 104 TypeScript = 361 tests, all currently passing.** Run both before
  calling any change done (`AGENTS.md` §3); they cover a system with two enforcement
  layers that must agree (§8), and neither suite alone would catch a drift between them.
- **The read → plan → guard pipeline is exercised against the live API**, not just
  fixtures — `vd calc verify` re-runs three independent duration formulas against
  `https://api.veydrift.com` and confirms `universe_speed == 1` still holds; a live
  `vd tick --dry-run` against the real API is part of the standard verification loop.
- **`tick.py`'s tier-2+ send path (`_send_and_await`) has real, substantive test
  coverage** — the tests monkeypatch the `walletctl` subprocess boundary and assert on
  `executions_count`, `revert_counts`, the gas ledger, and `actions.jsonl` contents across
  success/revert/unknown/send-failed cases. That is real coverage of the Python-side logic.
  **It has never run against an actual chain.** The full `build → simulate → send → await
  receipt → await indexed` sequence against mainnet is, as of this writing, entirely
  unexercised in practice — the first real run of it will be someone's actual tier-2
  promotion, not a controlled test.
- **Cost scaling, queue behavior under load, and lazy settlement above level 0 are
  unobserved**, full stop — not undertested, *unobserved*. The reference account this
  system was built and verified against has taken zero on-chain actions: every level is 0,
  every queue is idle. The duration formulas are verified live at level 0; nothing here has
  watched a live cost or queue field respond to an actual level-up.
- **`walletctl`'s tier check defends against a misconfigured caller, not a hostile one.**
  It reads tier from `policy.json`, but falls back to a caller-supplied `--tier` when no
  policy file exists at all — a process that controls its own environment can point
  `VEYDRIFT_HOME` at an empty directory and assert any tier it likes. Documented, not
  fixed, because the fallback is legitimately needed for standalone use — see
  `skills/veydrift-wallet/references/tx-safety.md`'s residual-limit section before
  treating this specific check as a security boundary rather than a footgun guard.

## 12. Documentation map

| Question | Where |
| --- | --- |
| The full spec, every acceptance criterion, work-package breakdown | `docs/SPEC.md` |
| Contract- and backend-source-derived corrections to earlier research | `docs/RESEARCH-ADDENDUM.md` |
| Every wallet-provider candidate evaluated beyond the two shipped | `docs/wallet-provider-research.md` |
| Earlier research inputs, superseded in places, kept for provenance | `docs/NOTES.md`, `docs/veydrift-agent-prompt.md`, `docs/veydrift-agent-resources.md`, `docs/veydrift-briefing.html` |
| Build/test commands, frozen interfaces, invariants a change must not break, known gaps | `AGENTS.md` |
| Product overview, tier model, install, usage, safety contract, key custody | `README.md` |
| A player's start-to-finish tutorial | `docs/PLAYER-GUIDE.md` |
| This document | `docs/TECHNICAL-WALKTHROUGH.md` |
| Everything the agent skill needs at a glance, routed to `references/` on demand | `skills/veydrift-agent/SKILL.md` |
| Same, for the wallet skill | `skills/veydrift-wallet/SKILL.md` |
| API routes, formulas, canonical enums, the strategy derivation, contract-write traps, guardrails, scheduling | `skills/veydrift-agent/references/*.md` |
| ABI pinning, wallet providers, transaction safety | `skills/veydrift-wallet/references/*.md` |

**A maintenance note, since this table and the two "101"/walkthrough documents above it are
themselves synthesis, not source of truth.** If `docs/SPEC.md` changes in a way that
affects the tier model, the module boundaries, or an invariant listed in `AGENTS.md` §5,
this file's §3, §7, §8, and §10 need a matching update — they restate spec content in
prose, and prose that quietly drifts from its source is worse than no prose at all. The
same applies to `docs/PLAYER-GUIDE.md` wherever it shows a command, a policy field, or a
verified transcript: if the underlying command or schema changes, that transcript is now
lying about what the tool does, not just stale. `AGENTS.md` and `README.md` both link to
these two documents specifically so a spec change has a visible reminder to check them,
not so they can be linked once and left in whatever state they were written.

## 13. Extending this system

**Adding a new guardrail gate:** it belongs in `guard.py`, following the pattern every
existing gate follows — return a `GuardVerdict`, resolve missing/absent data toward
`BLOCK`/`ESCALATE` rather than `PASS`, and add both a happy-path test and a
missing-data test in `test_guard.py`. Document it in `references/guardrails.md`, gate by
gate, not just in the code.

**Adding a new tier-gated action:** update `guard.py`'s `_MIN_TIER_FOR_FUNCTION` **and**
`allowlist.ts`'s selector sets in the same change, then run
`test_tier_map_agrees_with_the_wallet_engines_allowlist` before anything else — §8 exists
precisely so you don't have to remember to keep them in sync by discipline alone.

**Adding a new wallet provider:** implement `WalletProvider` (`providers/types.ts`), wire
it into `providers/index.ts`'s selection logic, and — this is the standard the existing two
set — make it a genuinely working implementation with its own address-derivation test
proving it agrees with the other providers on the same key material, not a stub. Read
`docs/wallet-provider-research.md` first; it evaluates several candidates against the
address-binding constraint (a Veydrift planet is permanently bound to the EOA that settled
it) that rules most hosted/MPC options out before you start.

**Re-pinning the ABI after a contract upgrade:** the exact recipe, including the foundry
settings that affect reproducibility, is in `AGENTS.md` §6 and
`skills/veydrift-wallet/references/abi-pinning.md`. Never build from `main` on the
contracts repo — it has already drifted from the deployed implementation once, silently
adding and removing functions, and there's no reason to expect that stops.

**Before shipping any change:** both test suites (`AGENTS.md` §3), a live
`vd tick --dry-run`, and — for anything touching the write path, the tier model, or a
guardrail — a fresh adversarial review pass. Two judge passes on this codebase have each
found real defects the builder and the test suite both missed; there's no reason to expect
a third pass on a substantial future change would find nothing.
