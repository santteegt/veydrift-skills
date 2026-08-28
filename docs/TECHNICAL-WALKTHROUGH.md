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
9. [How this was built, and what that explains about the code](#9-how-this-was-built-and-what-that-explains-about-the-code)
10. [What's tested, what's fixture-only, what's genuinely unverified](#10-whats-tested-whats-fixture-only-whats-genuinely-unverified)
11. [Documentation map](#11-documentation-map)
12. [Extending this system](#12-extending-this-system)

---

## 1. The one-paragraph version

An agent reads Veydrift's public game API, runs deterministic calculators against it, and
proposes at most one action per tick — a typed `Action`, never signed bytes. A completely
separate program builds the actual transaction for that action, checks it against its own
independent allowlist regardless of what the first program already validated, and only
submits it if a human (or a tier-2+ policy that a human explicitly configured) types an
exact confirmation flag. A three-tier policy field — `advisor` (propose only), `economy`
(also executes building/research/defense/ship actions), `operator` (also executes fleet
logistics) — edited by hand, is the only thing that ever lets more of that pipeline run
for real.

## 2. Why two skills, not one

This isn't an accident of packaging — it's the load-bearing design decision, and almost
every other choice in the repo follows from it.

`veydrift-agent` is a skill with scripts written in Python that reads an HTTP API and
**never imports a signing library** — this is an actual, checked invariant (`AGENTS.md`
§5), not a description. `veydrift-wallet` is a skill with scripts written in TypeScript
that is the only thing that ever calls `signAndSend`, and independently re-derives
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

The full spec is [`SPEC.md`](SPEC.md) (1,500+ lines as of this writing, v2.1). Read that
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

**What the spec explicitly does not attempt (§1's non-goals):** alliances, ACS,
migration, referrals, NFT burns, the ERC-20 market bridge, and any raid-profitability
model — `protectedResources`' actual semantics are unconfirmed, so nothing here builds
loot logic on it. Combat is almost entirely a non-goal too, with two narrow exceptions:
`policy.actions.allow_combat` gates Attack (`launchFleetMission` mission type 3, since
commit 5, 2026-08-28) and Missile (`launchInterplanetaryMissileAttack`, a wholly separate
contract entrypoint, since commit 7, same date), both at `operator` tier — and,
since commits 6/7 respectively, `plan.py`'s own two most conservative ladder rungs
(`8e:attack`, `8f:missile`) can actually propose one — see §5's `guard.py` row and §8.
Read `docs/RESEARCH-ADDENDUM.md` §6 before assuming otherwise.

## 4. Repository architecture

```
skills/
├── veydrift-agent/            Python (uv). Reads, calculates, plans. Never signs.
│   ├── SKILL.md                progressive-disclosure entry point
│   ├── pyproject.toml, uv.lock
│   ├── src/veydrift_agent/     14 modules, ~11,400 lines — see §5
│   ├── schemas/                policy.schema.json, action.schema.json — GENERATED, don't hand-edit
│   ├── references/             7 files, ~3,050 lines, loaded on demand — see §11
│   ├── assets/                 policy.example.json, launchd plist template
│   └── tests/                  714 tests
└── veydrift-wallet/            TypeScript (npm). Signs. Nothing else does.
    ├── SKILL.md
    ├── package.json, tsconfig.json
    ├── abi/                     pinned ABI + provenance — see §6; write-path traps: AGENTS.md §7
    ├── scripts/                 gen-keystore.mjs — interactive keystore creation (npm run wallet:new)
    ├── src/                     11 modules, ~1,970 lines — see §6
    ├── references/              4 files, ~1,300 lines
    └── tests/                   143 tests (141 passed + 2 fork-only, skipped outside a live Anvil fork)
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

**What it does.** Reads Veydrift's public game API, runs the deterministic calculators in
`calc.py` against it, and proposes at most one `Action` per tick through the decision
ladder below (§7) — it never signs or submits anything itself; that's `veydrift-wallet`'s
job exclusively (§2).

**How it's triggered.** As an installed Claude/Hermes skill, `SKILL.md`'s frontmatter
routes to it on a Veydrift planet id/coordinate, "what should I build next," "run a tick,"
"check my queues/energy/resources," or the tier/guardrail/promotion vocabulary — see
`SKILL.md`'s own `description` for the exact trigger phrases. Standalone, it's a single
entrypoint: `uv run --directory <path-to-this-skill> vd <subcommand>`; run `vd doctor`
first to confirm which subcommands are wired in the copy you're running against.

**References, loaded on demand.** `SKILL.md` stays deliberately thin and routes to
`references/` only when a question needs it: `api-routes.md` (route table, health-gating
rules), `formulas.md` (every `calc.py` formula's derivation), `entity-ids.md` (id↔name
tables, the fleet-tuple index shift), `strategy-playbook.md` (why the planner proposed
*this specific* action, worked cold/hot-planet examples), `contract-writes.md` (which
`Action.function` maps to which deployed entrypoint, and its traps), `guardrails.md`
(every gate, gate-by-gate, current wiring status), `scheduling.md` (driving `vd tick`
under Claude Code, Hermes, or bare `launchd`).

### Module by module

| Module | Lines | Role |
| --- | --: | --- |
| `models.py` | 601 | **Frozen.** Every pydantic model — `Policy`, `Action`, `Snapshot`, `GuardReport`, `UnsignedTx` — is the on-disk JSON shape for `policy.json`, both log files, and the generated schemas. Treat a field rename here as a breaking change to every log line ever written. The launch-actions plan (2026-08-28) added two fields under this same "frozen means additive, not immutable" discipline: `Action.target_planet_id` (a foreign Harvest target's real on-chain id, since a foreign planet is never in `Snapshot.planets` for the existing `target_coordinates` lookup to resolve) and `StrategyCfg.colonize`/`fleet_home_planet_id` (gate Colonize/Deploy respectively) — both additive, both regenerated into `schemas/`. |
| `cli.py` | 59 | **Frozen.** Mounts each module's `app: typer.Typer` with tolerant imports — a half-built tree still runs the parts that exist. Add a new module to `_SUBAPPS`, never wire a subcommand elsewhere. |
| `ids.py` | 362 | The six canonical contract enums (Building/Technology/Ship/Defense/FleetMissionType/Resource), transcribed directly from the deployed contract source, not from Veydrift's own docs (which get two of them wrong). |
| `http.py` | 184 | The API client: `httpx`, `tenacity` retry (5xx/network only, never 4xx), a disk cache under `$VEYDRIFT_HOME/cache/` honoring per-route max-age. |
| `read.py` | 993 | One `vd read` subcommand per API route (18 targets), plus two general-purpose fetchers new in the launch-actions plan that bypass the CLI layer entirely (`fetch_universe_system`, `fetch_raid_finder_debris` — the same "raw dict, no `vd read` target" posture `fetch_fleet_visibility`/`fetch_activity` already used). `--summary` mode is the default and is capped near 2KB; `battle-reports`/`highscores` refuse to print to stdout at all — `--out` is mandatory, since they run 60KB–2.2MB uncompressed. |
| `fmt.py` | 242 | Rich-based summary rendering behind `read.py`'s `--summary` output — the module that actually enforces `snapshot`'s <=2KB budget, self-truncating if a render ever overruns it. Every other target gets a best-effort compact rendering with no byte budget. |
| `calc.py` | 917 | Pure formulas, no network calls, one docstring citation per function. Contains **no cost-scaling function** by design — live cost always comes from the API's own `cost` field, since the per-building factors are unpublished rationals and recomputing them is exactly how affordability checks go wrong silently. |
| `plan.py` | ~510 | Rungs 0-4 of the decision ladder (§7 below) — vetoes, not strategy — plus the ladder's now **eight**-band precedence calling into `candidates.py`. As of Phase 2 (the general-strategy-engine program) it no longer decides *which entity*; that moved out. Phase 4 added rung `8b`; Phase 5c added rung `8c` (logistics); the launch-actions plan (2026-08-28) extended `8c` with two more families (Deploy, foreign Harvest), added a sixth band, rung `8d` (Colonize, commit 4), a seventh, rung `8e` (Attack, commit 6), and an eighth, rung `8f` (Missile, commit 7) — see §7 for the full current ladder. A separate addition adds veto rung `1b` — `gameMaintenance.paused` from `/health`, ESCALATE (or NO-OP, `escalation.on_game_paused`) before any candidate pipeline runs. |
| `candidates.py` | ~3070 | New in Phase 2, roughly doubled in Phase 3, gained a fifth family in Phase 4 and a sixth in Phase 5c (all general-strategy-engine program); the launch-actions plan (2026-08-28) added three more generators inside that sixth family plus a seventh (`attack`, commit 6) and eighth (`missile`, commit 7) family outright. The generate/filter/score/select pipeline behind rungs 5-12: one pure generator per family (`mine`, `energy`, `storage`, `research`, `ship`, `defense`, Phase 3's `infrastructure` and `crawler`, Phase 4's `unlock`, Phase 5c's `logistics`, the launch-actions plan's `colonize`/`attack`/`missile`), `score_payback` (payback-hours scoring, scored iff a level change moves `calc.production_per_hour`'s output), and a `select_*` function per rung that replays the pre-Phase-2 ladder's exact priority order when nothing new is configured. The energy-first invariant lives here: before generating a mine candidate, it computes post-upgrade energy `required` vs. current `produced` fresh, every tick — never a fixed level-offset heuristic, because the true gap between mine level and required Solar Plant level widens as levels climb. Phase 3 adds declared-target stock-keeping for ships/defenses (`ship_targets`/`defense_targets`), priority ordering for research/infrastructure (`research_priority`/`building_priority`), and two new scored/unscored families (Crawler, proactive storage) — every one of the four new `StrategyCfg` fields defaults empty and reproduces Phase 2's output exactly when left unset. **Correction (judge finding 4):** that "reproduces exactly" claim was false for Crawler specifically — `generate_crawler_candidates` was gated only on `allow_ships`, so an unlocked, scoreable Crawler could still outrank Solar Satellite with every `StrategyCfg` field at its default. Fixed with a fifth field, `enable_crawler` (default `false`): the scored Crawler family now returns `[]` unless explicitly opted into. **Fix:** a scored winner's cost could exceed the planet's storage cap for a resource it needed — "not affordable ever" until storage is raised, which `generate_proactive_storage_candidates` existed for but, by design, could never outrank a scored winner. New `_exceeds_storage_cap`/`_resolve_storage_precondition` helpers made this a hard precondition, mirroring the energy-first filter. **Fix:** `_mine_priority_order`'s exact-tie case now breaks by ascending payback hours instead of dict-declaration order, which previously always favored Metal Mine. Phase 4 adds `generate_unlock_chain_candidates`/`select_unlock_chain_candidate`, `score=None` always, driven by `techtree.next_step_toward` rather than any new policy field. Phase 5c adds `generate_transport_candidates`/`generate_harvest_candidates`/`select_logistics_candidate` — non-combat `launchFleetMission` proposals, gated on `policy.actions.allow_fleet_noncombat` (default `false`), using a new `calc.py` ship-movement-stats layer (`SHIP_CARGO_CAPACITY`, `ship_fuel_consumption`, `ship_speed`). **The launch-actions plan (2026-08-28, seven commits)** closed every gap that history left, then went further: `generate_harvest_candidates` finally got a live `own_planet_debris` caller (`tick._own_planet_debris`, reading `/universe/galaxies/{g}/systems/{s}`'s `debrisField`); `generate_foreign_harvest_candidates` extended Harvest to a third party's debris field (`/raid-finder/debris`); `generate_deploy_candidates` moves an entire flyable fleet to a declared `policy.strategy.fleet_home_planet_id`; `generate_colonize_candidates`/`select_colonize_candidate` (ladder rung `8d`, commit 4) picks a free coordinate slot by live `deuteriumMultiplierBps`; `generate_attack_candidates`/`select_attack_candidate` (ladder rung `8e`, commit 6) attacks the highest-raidable reachable `/highscores` target with every combat-capable ship built on the origin planet; `generate_missile_candidates`/`select_missile_candidate` (ladder rung `8f`, commit 7, the most conservative rung) fires every owned Interplanetary Missile at a target's most-numerous eligible defense type via the wholly separate, synchronous `launchInterplanetaryMissileAttack` entrypoint. Both combat families gated on `policy.actions.allow_combat`. `select_logistics_candidate` ranks four families per planet (Transport, Deploy, local Harvest, foreign Harvest) instead of two. |
| `techtree.py` | 716 | On-chain prerequisite table for all four entity families, transcribed from the deployed contract (Phase 1). `unmet()` is the fail-closed core every other module's legality checks build on (`plan.py`/`candidates.py` never propose a locked entity; `guard.py`'s `prerequisites` gate independently re-checks). Phase 4 adds `next_step_toward` — a breadth-first walk of `unmet()`'s own output, backwards, to find the shallowest currently-buildable prerequisite toward a locked target; no cost math, same "compare levels only" discipline as `unmet()`. Also adds `unlock_breadth` — the forward mirror of that walk, one hop only: how many other entities would have a requirement drop out of their own `unmet()` if this one's level were +1, re-derived by re-calling `unmet()` rather than a hand-built reverse index. Feeds `candidates.py`'s `_infrastructure_priority_order`/`_research_priority_order` fallback ranking; a structural fact, never a value judgement, so it stays clear of `calc.py`'s cost-scaling ban and `candidates.py`'s "no ROI verdict" refusal. |
| `guard.py` | ~1500 | **22** guardrail gates (17 through Phase 4; Phase 5c added `mission_type`; a later addition added `game_paused`; the launch-actions plan added `fleet_slots` (commit 2), `attack_protection` (commit 6), and `missile_target` (commit 7), 2026-08-28), every one evaluated and reported on every call — never short-circuited, so a passing tick's verdict list is as informative as a blocked one. The rule every gate follows: missing data resolves toward `BLOCK`/`ESCALATE`, never `PASS`. **Fix, then completed:** `health` gained a narrow exception, `Snapshot.combat_only_degradation()` — `/health`'s `ok:false` caused *solely* by a combat-only `randomnessReadiness` degradation (positively confirmed, everything else on the snapshot fine) PASSes instead of BLOCKing for a non-combat action. Commit 5 made this exception's premise narrower (Attack became conditionally reachable via `policy.actions.allow_combat`) without yet changing its behavior; **commit 6 completed the correction**: `_gate_health` now takes `action` as a parameter and withdraws the exception specifically for a combat (Attack) action, which requests VRF at launch and cannot resolve while randomness is degraded — every non-combat action still gets the exception, unchanged. Confirmed live and persistent, served via HTTP 503 — `read._fetch_or_exit()` now defensively recovers a parseable `/health` 5xx body (narrowly scoped to that one route) instead of hard-aborting before this check could ever run. **The launch-actions plan** also fixed idempotency-key collisions in `idempotency_key` (every fleet mission from one planet, every resolve action across all planets, and — commit 7 — every missile launched from one planet each shared a single key and revert-streak counter), closed an in-flight-Colonize blind spot in `_colony_cap_violation` (`Snapshot.owned_planet_count` alone only reflects already-resolved planets; a new `outgoing_colonize_count` parameter, fed by `tick._outgoing_colonize_count`, folds in still-`Outbound` Colonize missions before the cap check, failing closed on `None` exactly like the field it complements), added `attack_protection` (commit 6, extended to Missile in commit 7 with a bashing-limit exemption — `launchInterplanetaryMissileAttack` calls `_enforceAttackProtection` with `countsBashing=false`): a live, target-specific `/wallet/{addr}/attack-protection` re-check for an Attack or Missile action — `None`/unknown **BLOCK**s, `true` **PASS**es, `false` **BLOCK**s unless the action is Missile and `blockedReason=="bashing"`; and added `missile_target` (commit 7): independently re-derives `launchInterplanetaryMissileAttack`'s range/primary-target/owned-count preconditions from `Snapshot` alone, no live data needed. |
| `state.py` | 350 | `$VEYDRIFT_HOME` resolution, `AgentState` (pending txs, cumulative gas, revert counts), the tick lockfile, `KILLSWITCH` detection. |
| `tick.py` | ~2500 | The orchestrator — the nine-step loop (§7), the `walletctl` subprocess bridge, `--readiness`, `--action`'s manual-override substitution point. The largest module in the package, and where the criticals a first-pass judge review found actually lived (`AGENTS.md` §5's unit-mismatch and revert-recording invariants). The launch-actions plan added six out-of-band fetchers here, each following the same established pattern (`_resolvable_mission_ids`'s "bypass the frozen `Snapshot`, best-effort, never raise" contract): `_own_planet_debris`, `_foreign_debris_targets`, `_colonize_targets`, `_outgoing_colonize_count` (commits 1-4), and `_attack_targets`/`_missile_targets` (commits 6-7, both gated on `policy.actions.allow_combat`). One live-data fetcher in this family is NOT a courtesy/generation-time read: `_attack_protection_allowed` (commit 6, extended to return `(allowed, blocked_reason)` in commit 7) feeds `guard._gate_attack_protection` directly, fetched fresh at guard-evaluation time for the specific resolved target of an Attack or Missile proposal. `_action_to_walletctl_json` also gained a `launchInterplanetaryMissileAttack` branch (commit 7) — not overloaded on the deployed ABI, so it's resolved by bare name, unlike `launchFleetMission`'s two forms. |
| `log.py` | 392 | Four log sinks, secret scrubbing (`0x[0-9a-fA-F]{64}` patterns that aren't a known tx hash never get written), the pretty-report renderer, `--digest`. |

## 6. `veydrift-wallet`, module by module

**What it does.** The only thing in this repo that builds real calldata, signs, or
submits a transaction — `build`/`simulate`/`send`/`receipt` via `walletctl`.
Independently re-validates every transaction against its own allowlist regardless of what
`veydrift-agent` already checked (§2); never decides *what* to do, only signs and sends
what it's given.

**How it's triggered.** As an installed skill, `SKILL.md`'s frontmatter routes to it on
"sign this," "send the tx," `walletctl`, wallet/balance/provider status checks, ABI-pin
verification, fleet-mission encoding, or keystore/envkey provider questions — see
`SKILL.md`'s own `description` for the exact trigger phrases. Standalone, it's
`npx tsx src/cli.ts <subcommand>` from the skill's own directory.

**References, loaded on demand.** `providers.md` (provider selection, the swap
procedure, the address-binding constraint in full), `tx-safety.md` (the exact allowlist
checks, the `--confirm` invariant, what this engine deliberately does not do),
`abi-pinning.md` (pin provenance, the rebuild recipe, `main`-vs-deployed divergence),
`fork-testing.md` (the fork-testing runbook — exercising tier≥2 sends against a local
Anvil fork with an impersonated account; `AGENTS.md` §10's fork-round history).

### Module by module

| Module | Lines | Role |
| --- | --: | --- |
| `abi.ts` | 225 | Loads the pinned ABI, resolves a function by **full canonical signature** (never a bare name — `launchFleetMission` is overloaded on the deployed contract), computes selectors. |
| `allowlist.ts` | ~330 | `checkAllowlist` — five checks, always all five evaluated and reported: destination against a **live** `/runtime-config` fetch, selector against the tier's set (computed from the pinned ABI, never a hand-typed hex constant), `value == 0`, `chainId == 8453`, and at `operator` tier, the `launchFleetMission` mission-type argument decoded from calldata and restricted to `OPERATOR_ALLOWED_MISSION_TYPES` (Transport/Deploy/Harvest, plus Colonize (2) as of Phase 5b) **unconditionally, or `COMBAT_ALLOWED_MISSION_TYPES` (Attack (3) only) when `resolveAllowCombat` resolves `policy.json`'s `actions.allow_combat` to `true`** (launch-actions plan commit 5, 2026-08-28 — no CLI flag or env var can assert this, unlike `resolveTier`'s own fallback). `guard.py`'s matching `mission_type` gate mirrors this exact two-set split. **Commit 7 (same date) adds a second, structurally different conditional path**: `launchInterplanetaryMissileAttack` (its own selector, sharing nothing with `launchFleetMission`) is pulled out of `tierSelectors('operator')`'s unconditional set entirely — via a new `COMBAT_SIGNATURES`/`combatSelectorSet()` — and checked the same lazy way inside the selector check itself, since there's no shared function to decode a conditional argument from the way Attack's mission type is. `guard.py`'s `_MIN_TIER_FOR_FUNCTION` maps the function unconditionally to `operator` instead (the `allow_combat` check lives in a separate `_gate_missile_target`); `test_tier_map_agrees_with_the_wallet_engines_allowlist` accounts for this shape difference via a new `guard._COMBAT_ONLY_FUNCTIONS` constant, diffed against `COMBAT_SIGNATURES` directly. |
| `tx.ts` | 400 | `build`/`simulate`/`send`/`receipt`. `build` emits `estimatedCostWei` (gas units × live `maxFeePerGas`), not a bare gas-unit number — see `AGENTS.md` §5's unit-mismatch invariant for why that distinction is load-bearing. `receipt` reports real `status: "success" \| "reverted"` from the actual receipt, never synthesized. RPC target is `VEYDRIFT_RPC_URL` (default `https://mainnet.base.org`), the one chokepoint every read and write resolve through — swap in a dedicated endpoint (Alchemy etc.) to avoid the public endpoint's rate limits. |
| `fleet.ts` | 147 | `shipCountsToFleetTuple()` — the one function permitted to build the 14-slot fleet-mission tuple; see `AGENTS.md` §7. |
| `policy.ts` | 116 | Reads `tier` from `$VEYDRIFT_HOME/policy.json` rather than trusting a caller-supplied flag; refuses (exit 4) on disagreement or a malformed file rather than falling back to a permissive default. |
| `cli.ts` | 329 | The `walletctl` command surface: `status`, `verify-abi`, `build`, `simulate`, `send`, `receipt`. `send` without `--confirm` always exits non-zero and prints exactly what it would have sent — the same code path as a real send, short-circuited right before `provider.signAndSend`. |
| `providers/keystore.ts` | 113 | Default provider. Encrypted EIP-2335/geth JSON keystore, decrypted via `ethers.Wallet.fromEncryptedJson` — the one place `ethers` earns its dependency slot in a codebase that otherwise uses `viem` for everything chain-side, because scrypt+AES decryption isn't something to hand-roll. |
| `providers/envkey.ts` | 152 | Testing-only provider. Raw `VEYDRIFT_PRIVATE_KEY`, loud startup warning every use, a best-effort (not primary-control) check that refuses to start if the key value is also found anywhere in the containing git repo outside `tests/`. |
| `providers/fork-impersonate.ts` | 132 | Runs the exact production `sendTx` → `provider.signAndSend` path against a local Anvil fork with an impersonated account instead of a held key — gated by a loopback guard that makes it inert outside a fork. The intended first real exercise of a tier-2+ send path; see `references/fork-testing.md` and `AGENTS.md` §8. |
| `providers/types.ts`, `providers/index.ts` | 37 + 44 | The `WalletProvider` interface and the selection logic (`policy.wallet_engine.provider`, overridable by `WALLET_PROVIDER`). |

## 7. A tick, end to end

```
1. load + validate policy      →  extra key = hard error, missing required field = hard error
2. killswitch check             →  $VEYDRIFT_HOME/KILLSWITCH present? halt before any
                                    network call beyond /health
3. reconcile pending txs        →  resolve anything left over from a prior tick first
4. snapshot                     →  read.py: /health, /infrastructure, /research, /shipyard,
                                    /defenses, /fleet-visibility -- composed into one Snapshot
5. plan                         →  plan.py: the decision ladder, zero or one Action out
                                    (or: `vd tick --action <file>`, gated by
                                    policy.strategy.allow_agent_action_override, substitutes
                                    an operator-supplied Action here — every step after this
                                    one, including guard, runs identically either way; see
                                    references/manual-action-override.md)
6. guard                        →  guard.py: all 21 gates, full verdict list, one Decision
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

**"Rung" and "Phase" are two different axes — easy to conflate, worth separating once.** A
*rung* (`0`-`10`, `8b`, `8c`, `8d`) is a position in the per-tick decision ladder below — a
runtime concept `plan.py`/`candidates.py` actually check against, in order, every single
tick. A *Phase* (1 through 5c, used throughout §5's module table and this section) is a
development-time milestone from this project's own build history — when a capability was
added, cited for traceability, not something the running code has any notion of. The two
don't map one-to-one: Phase 3 changed rungs 5-9's entity-generation logic without adding a
new rung; Phase 4 added rung `8b` specifically; Phase 5c added rung `8c`; the
launch-actions plan (2026-08-28, outside the Phase-numbered history — see below) extended
`8c` with two more families and added rungs `8d` (commit 4) and `8e` (commit 6). If you're
reading the running code, only rung numbers matter — Phase labels are documentation-only
history.

The decision ladder inside step 5, first match wins. Rungs 0-4 are vetoes; rungs 5-12 are
an **eight**-band candidate pipeline (`candidates.py`, added in Phase 2 of the
general-strategy-engine program, extended to a fourth band by Phase 4, a fifth (logistics,
rung `8c`) by Phase 5c, a sixth (Colonize, rung `8d`) by commit 4 of the launch-actions
plan, a seventh (Attack, rung `8e`) by commit 6, and an eighth (Missile, rung `8f`) by
commit 7 of the same plan — see §5's module table):

```
0. KILLSWITCH present               -> HALT
1. /health not ok                   -> NO-OP, reason recorded (unless positively
                                        confirmed combat-only -- falls through instead,
                                        Snapshot.combat_only_degradation)
1b. gameMaintenance.paused          -> ESCALATE (or NO-OP if escalation.on_game_paused
                                        is false) -- a chain-side maintenance pause; any
                                        write would revert)
2. pending tx unreconciled          -> NO-OP, reconcile first
3. mission Resolving > 60s          -> resolveFleetMission (permissionless, free)
4. incoming hostile fleet           -> ESCALATE, no proposal at all
5-12. generate -> filter -> score -> select, eight bands in order:
     1. deadline-driven      -- storage overflow: spend it, or build the matching storage
                                 (plus, Phase 3: proactive storage as an always-visible
                                 Band-2 alternative, never a Band-1 winner -- but see
                                 below, it CAN win Band 2)
     2. economically scored  -- building upgrade, ascending payback hours (energy-first
                                 is a hard filter here, not a score); Phase 3: an explicit
                                 `building_priority` takes precedence over the mine walk
     3. policy-declared      -- research (Phase 3: `research_priority` first, else an
                                 unlock-breadth-ranked default -- was purely
                                 lowest-level-first -- labelled "default:" when it's the
                                 fallback), then ships/defense (Phase 3: `ship_targets`/
                                 `defense_targets` stock-keeping, plus scored Crawler),
                                 gated on economy-on-track
     4. unlock-chain (8b)    -- Phase 4: the shallowest buildable prerequisite toward a
                                 locked `ship_targets`/`defense_targets`/`research_priority`
                                 entry, `score=None`, reached only when bands 1-3 found
                                 nothing for any target planet
     5. fleet logistics (8c) -- launch-actions plan (2026-08-28): four families per
                                 planet, first selectable one wins -- Transport (moves a
                                 surplus resource between own planets), Deploy (moves an
                                 entire flyable fleet to a declared
                                 `fleet_home_planet_id` -- ranks ahead of both Harvest
                                 kinds below it, a declared destination outranking an
                                 opportunistic one), local Harvest (a planet's own debris
                                 field), foreign Harvest (a third party's, via
                                 `/raid-finder/debris`). All gated on
                                 `policy.actions.allow_fleet_noncombat` (Deploy also needs
                                 `fleet_home_planet_id` declared); reached only when bands
                                 1-4 found nothing for any target planet
     6. Colonize (8d)        -- launch-actions plan commit 4: consumes a built Colony Ship
                                 toward a free coordinate slot (ranked by live
                                 `deuteriumMultiplierBps`), gated on new
                                 `policy.strategy.colonize`. Reached only once bands 1-5
                                 found nothing at all, since it spends a hard-to-replace
                                 ship on a permanent commitment
     7. Attack (8e)          -- launch-actions plan commit 6: attacks the highest-raidable
                                 reachable target from `/highscores` (economy category),
                                 sending every combat-capable ship built on the origin
                                 planet, gated on `policy.actions.allow_combat` and, inside
                                 the generator, `snapshot.randomness_readiness.ready`.
                                 Reached only once every other band, Colonize included,
                                 found nothing at all, since committing a fleet to combat
                                 risks losing it
     8. Missile (8f)         -- launch-actions plan commit 7: launchInterplanetaryMissile
                                 Attack, a wholly separate, fully synchronous contract
                                 entrypoint sharing nothing with launchFleetMission (no
                                 fleet tuple, no mission type, no fleet slot, no travel
                                 time). Fires every owned Interplanetary Missile at the
                                 target's most-numerous eligible defense type (ids 0-7;
                                 ABM/IPM themselves are refused as targets), gated on
                                 `policy.actions.allow_combat`. The MOST conservative
                                 placement in the whole ladder, more so even than Attack
                                 -- reached only once every other band, Attack included,
                                 found nothing at all
     else                    -> NO-OP, explicit reason
```

The winning `Action` also carries `alternatives` — the runner-up candidates from the same
pass, ranked, each with a `why_not` (a payback-hours comparison, or a lock reason from
`techtree.describe()`). Purely informational: never an ROI verdict, never read by
`guard.py` or any `Decision` logic.

**Phase 3 — most planet-local entities reachable.** Before this change the
planner could only ever propose 13 of the entities `ids.py` knows about (three mines,
Solar Plant, three storages, one ship, one hardcoded defense, "whichever technology has
the lowest level"). `policy.strategy` grows four target-declaration fields
(`ship_targets`, `defense_targets`, `research_priority`, `building_priority`, all
`list[str]`/`list[EntityTarget]`, all defaulting to `[]`) that drive the rest of the
entity list without inventing a doctrine — the governing principle is that the engine
computes what's legal (`techtree.unmet()`), affordable (`guard.py`, unchanged) and
economically comparable (`score_payback`), and the policy declares intent for everything
else. `[]` on all four is this phase's own acceptance criterion: byte-identical output to
Phase 2 on the same fixtures, pinned in `tests/test_candidates.py`/`tests/test_plan.py`
and by every pre-Phase-3 test passing unmodified. **Current count, checked directly
against `ids.py`: 55 of 57 entities are reachable by some generator** (all 15
technologies via `research_priority`'s fallback tail; all 16 ships and all 10 defenses via
`ship_targets`/`defense_targets`, name-declarable without restriction; 14 of 16 buildings
— three mines, Solar Plant, three storages, Fusion Reactor, and the six
`_INFRASTRUCTURE_BUILDING_IDS` covered by `building_priority`). The two structural
exceptions: **Alliance Depot** (no generator references it — alliances are an explicit
non-goal, §3) and **Rift Stabilizer** (no generator references it either; `docs/COVERAGE.md`
notes its mechanics are unpublished and it's hard-capped at level 1). "51" was this
paragraph's original count at the time Phase 3 shipped; `ids.py` has since grown to 57
entries.

**Phase 4 — a locked declared target proposes its own build-up.** Phase 3
made every entity reachable *once its prerequisites are already met*; a `ship_targets`/
`defense_targets`/`research_priority` entry the account cannot build **yet** was still a
dead end — legal to want, correctly never proposed (every generator refuses an entity the
contract would revert on), and nothing ever proposed the prerequisite that would unlock
it. New `techtree.next_step_toward(family, entity_id, *, building_levels,
technology_levels) -> UnlockStep | None` walks `unmet()`'s output backwards, breadth-first,
to find the shallowest requirement in the chain that is itself buildable right now — its
own `unmet()` is empty *and* its own current level is known (an
`UnmetRequirement(have=None)` can never become a confidently-chosen step; the design
brief's "fails closed on absent data" invariant, extended to graph traversal). Cycle-safe
(a `visited` node set) and depth-bounded defensively, though the real tables are asserted
acyclic by test. `candidates.generate_unlock_chain_candidates` / `select_unlock_chain_
candidate` turn the result into ladder rung `8b`, `score=None` always — an unlock step's
value is entirely in what it eventually enables, which this codebase has already refused
to price three times over (no cost-scaling function, no ROI verdict on `alternatives`, no
activity-classification score). Deliberately the *last* rung: it must never outrank the
storage-overflow deadline and must never displace a scored economic or policy-declared
candidate, so it is checked only once every earlier rung has produced nothing at all — not
folded into `building_priority`'s own higher-precedence path, which is unaffected.
`guard.py`'s `prerequisites` gate needed no code change (it keys off `Action.kind`, not the
generator that produced it), confirmed by a new test rather than merely assumed. Empty
`ship_targets`/`defense_targets`/`research_priority` reproduces Phase 3's output exactly —
this phase's own acceptance criterion.

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
excluded it, and nothing caught the mismatch for a while.

The fix wasn't just adding the missing entry — it was adding
`tests/test_guard.py::test_tier_map_agrees_with_the_wallet_engines_allowlist`, which
**parses `allowlist.ts` directly** (regex over the `const ECONOMY_SIGNATURES = [...]`
block) and diffs the resulting set against `guard.py`'s own map, failing with the exact
names on each side if they ever disagree again. This is deliberately not a
"remember to update both" comment — a comment doesn't run in CI. If you're adding a new
tier-gated function, that test is the one to run before you're done, not just the two
suites separately.

**The same duplication exists one level deeper, as of Phase 5c.**
`launchFleetMission` being allowlisted at `operator` only says the *function* is
reachable — which *mission types* it may carry is a second restriction, enforced by
`guard.py`'s new `mission_type` gate (`_ALLOWED_MISSION_TYPES`) and `allowlist.ts`'s
existing calldata-level check (`OPERATOR_ALLOWED_MISSION_TYPES`). The same test now
parses and diffs both mission-type sets too, not just the function-name sets — added
in the same change that added the gate, on the theory that a second duplication without
a second test is exactly how the first one drifted.

## 9. How this was built, and what that explains about the code

This repo was built by a multi-agent pipeline: a planning pass wrote `docs/SPEC.md`,
parallel Sonnet-model builder agents implemented independent work packages against that
frozen spec, and Fable-model judge passes adversarially reviewed the result twice — see
`AGENTS.md` §9 for the workflow itself, and its own text for what each pass has caught
historically. That history is directly visible in the code, and worth knowing before you
assume something is either over-engineered or under-tested.

The practical lesson for anyone extending this code: **a config flag or an enforcement
rule that's checked in one place and emitted from two is exactly where the next bug is
likely to live** — this is the same defect class §8's tier-map drift is, and the same
class a hot-planet `allow_ships` gap turned out to be later (two things that needed to
agree, and nothing forced them to, until a test was added that parses both sides). If you
add a second path that can produce the same kind of `Action` an existing gate already
checks, verify the gate actually covers the new path — don't assume it does because it
covers the first one.

## 10. What's tested, what's fixture-only, what's genuinely unverified

Precision matters here more than a clean "it's tested" claim would suggest.

- **714 Python + 143 TypeScript = 857 tests, all currently passing** (the TypeScript
  figure includes 2 fork-only tests, skipped outside a live Anvil fork). Run both before
  calling any change done (`AGENTS.md` §3); they cover a system with two enforcement
  layers that must agree (§8), and neither suite alone would catch a drift between them.
- **The read → plan → guard pipeline is exercised against the live API**, not just
  fixtures — `vd calc verify` re-runs three independent duration formulas against
  `https://api.veydrift.com` and confirms `universe_speed == 1` still holds; a live
  `vd tick --dry-run` against the real API is part of the standard verification loop.
- **No per-building cost-scaling factor has been observed or verified by this codebase at
  any level.** Live cost always comes from the API's own `cost` field by design (§3) —
  this is about the unpublished scaling *formula* specifically, not about whether real
  actions have been taken. Queue behaviour and lazy settlement above level 0 *have* since
  been observed — both via a local Anvil fork exercising all 7 allowlisted selectors
  (`AGENTS.md` §10) and via real transactions this codebase has since submitted to
  mainnet itself, at tier 2 (`economy`) and tier 3 (`operator`) — see `README.md`'s Status
  section.
- **`walletctl`'s tier check defends against a misconfigured caller, not a hostile one.**
  It reads tier from `policy.json`, but falls back to a caller-supplied `--tier` when no
  policy file exists at all — a process that controls its own environment can point
  `VEYDRIFT_HOME` at an empty directory and assert any tier it likes. Documented, not
  fixed, because the fallback is legitimately needed for standalone use — see
  `skills/veydrift-wallet/references/tx-safety.md`'s residual-limit section before
  treating this specific check as a security boundary rather than a footgun guard.

## 11. Documentation map

| Question | Where |
| --- | --- |
| The full spec, every acceptance criterion, work-package breakdown | `docs/SPEC.md` |
| The standing coverage ledger — every pinned-ABI write entrypoint (implemented/planned/deferred/out of scope), regenerated against the ABI rather than hand-maintained | `docs/COVERAGE.md` |
| Contract- and backend-source-derived corrections to earlier research | `docs/RESEARCH-ADDENDUM.md` |
| Every wallet-provider candidate evaluated beyond the two shipped | `docs/wallet-provider-research.md` |
| Earlier research inputs, superseded in places, kept for provenance | `docs/NOTES.md`, `docs/veydrift-agent-prompt.md`, `docs/veydrift-agent-resources.md`, `docs/veydrift-briefing.html` |
| Build/test commands, frozen interfaces, invariants a change must not break, known gaps | `AGENTS.md` |
| Product overview, tier model, install, usage, safety contract, key custody | `README.md` |
| A player's start-to-finish tutorial | `docs/PLAYER-GUIDE.md` |
| This document | `docs/TECHNICAL-WALKTHROUGH.md` |
| Everything the agent skill needs at a glance, routed to `references/` on demand | `skills/veydrift-agent/SKILL.md` |
| Same, for the wallet skill | `skills/veydrift-wallet/SKILL.md` |
| API routes, formulas, canonical enums, the strategy derivation, contract-write traps, guardrails, scheduling, the `vd tick --action` manual override | `skills/veydrift-agent/references/*.md` |
| ABI pinning, wallet providers, transaction safety | `skills/veydrift-wallet/references/*.md` |

**A maintenance note, since this table and the two "101"/walkthrough documents above it are
themselves synthesis, not source of truth.** If `docs/SPEC.md` changes in a way that
affects the tier model, the module boundaries, or an invariant listed in `AGENTS.md` §5,
this file's §3, §7, §8, and §9 need a matching update — they restate spec content in
prose, and prose that quietly drifts from its source is worse than no prose at all. The
same applies to `docs/PLAYER-GUIDE.md` wherever it shows a command, a policy field, or a
verified transcript: if the underlying command or schema changes, that transcript is now
lying about what the tool does, not just stale. `AGENTS.md` and `README.md` both link to
these two documents specifically so a spec change has a visible reminder to check them,
not so they can be linked once and left in whatever state they were written.

## 12. Extending this system

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
