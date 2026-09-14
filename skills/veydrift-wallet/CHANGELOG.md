# Changelog

All notable changes to `veydrift-wallet` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface, the ABI
pin, or the transaction allowlist bump major, additive backward-compatible changes bump
minor, fixes and docs-only changes bump patch. This package's version lives in
`package.json`, independent of `veydrift-agent`'s — the two skills are not versioned in
lockstep.

## [Unreleased]

## [1.2.0] - 2026-09-13

### Added
- **`walletctl nonce --address`**: prints `{address, latest, pending, blockNumber}` as JSON
  (`tx.ts`'s `getNonces`) — how an uncertain broadcast is resolved without guessing.
- **`walletctl status`** prints `policy.json`'s `wallet` beside the provider address and flags a
  mismatch.

### Changed
- **`send` refuses a signer that isn't `policy.json`'s `wallet`.** `sendTx` takes a required
  `expectedAddress` (resolved by `policy.ts`'s new `resolveExpectedWallet`; `null` only when no
  policy file exists) and refuses before the allowlist when the provider's address differs, or
  can't be derived. A malformed policy or invalid `wallet` exits 4, like tier resolution.
- **A failure inside `provider.signAndSend` is reported as `BROADCAST UNCERTAIN:`**
  (`BroadcastUncertainError`), not a generic `send failed:` — the tx may already be on the
  network. A provider that fails to load is now `REFUSED:`. `REFUSED:`/`NOT SENT`/exit 4 are the
  only "nothing was signed" signals.

## [1.1.2] - 2026-09-12

### Fixed
- **`cli.ts` / `tx.ts`**: `build --out`'s JSON never carried `gasEstimateError`/
  `feeEstimateError` — `buildTx` computed them correctly (the real reason a gas/fee
  estimate genuinely failed, e.g. an actual on-chain revert, as opposed to the benign "no
  provider configured" case) but `build`'s CLI handler only ever printed them to its own
  stderr. `veydrift-agent`'s `_walletctl_build` reads only the `--out` file, so it had no
  way to see *why* `estimatedCostWei` came back `null` — a genuine revert surfaced
  identically to a mundane missing estimate as an unexplained `gas: escalate`, which is
  exactly what forced a manual `walletctl build`/`simulate`-by-hand diagnostic to find
  AGENTS.md §7 trap #4's Colonize corruption in the first place.

### Changed
- **`tx.ts`**: new exported `StoredTx` interface and `toStoredTx(built: BuiltTx):
  StoredTx` — the `build --out` mapping, previously inline in `cli.ts`'s `.action()`
  closure (untestable without a full CLI invocation), now lives as a pure, directly
  unit-tested function next to `BuiltTx`. `cli.ts`'s own `StoredTx` interface and manual
  field-by-field mapping are removed in favor of this. No behavior change beyond the two
  new fields above.

## [1.1.1] - 2026-09-09

### Fixed
- **`abi.ts` / `cli.ts`**: `walletctl simulate --json` (and the plain-text `decoded:`
  line) crashed with `Do not know how to serialize a BigInt` whenever the decoded return
  value carried a nested `bigint` — i.e. any `tuple`/struct output such as a `Resources`
  return (`previewResources`, `buildingUpgradeCost`, …). `decodeSimulateReturnData` only
  stringified `bigint`s at the top level; struct fields one level down (and `tuple[]`
  elements two levels down) passed through untouched and then broke `JSON.stringify`.
  Now deep-converts every nested `bigint` to a decimal string, and both `JSON.stringify`
  call sites in `cli.ts`'s `simulate` command pass `bigintReplacer` as a second layer.
  Also fixes a latent truncation: a single array-typed output (`tuple[]`) was keyed off
  `Array.isArray(decoded)` and collapsed to its first element — now keyed off
  `outputs.length`.

## [1.1.0] - 2026-09-08

ACS defense coordination feature (see `skills/veydrift-agent/CHANGELOG.md` `1.19.0` for
the full picture): AcsDefend(5)/Intercept(6) mission types, `launchDefenseHold`'s own
selector, and `openDefenseIntent`'s own selector become allowlisted, plus a new
`walletctl simulate --json` capability. Minor bump — purely additive: no existing
selector's tier/mission-type requirements changed, no existing CLI flag's behaviour
changed when `--json` is omitted.

### Added
- **`allowlist.ts`**: `ACS_MISSION_TYPES: ReadonlySet<number> = new Set([5, 6])`, unioned
  into `launchFleetMission`'s allowed mission-type set when `resolveAllowAcsDefense()` is
  `true` — a wholly separate flag from `allow_combat`, which must never unlock these two.
  New `DEFENSE_HOLD_SIGNATURES` (one signature) and `ACS_ALLIANCE_SIGNATURES` (one
  signature, `openDefenseIntent`) arrays, each with their own selector-set helper and
  `checkAllowlist` branch — `launchDefenseHold` at `operator` tier, `openDefenseIntent` at
  `economy`-or-`operator`, both gated on `allow_acs_defense`. Deliberately kept out of
  `LAUNCH_FLEET_MISSION_SIGNATURES`/`ALLIANCE_SIGNATURES` respectively — see
  `veydrift-agent`'s matching `_DEFENSE_HOLD_ONLY_FUNCTIONS`/`_ACS_ALLIANCE_FUNCTIONS`
  docstrings for why the cross-layer test needs each kept separate.
- **`policy.ts`**: `resolveAllowAcsDefense()`, an exact structural mirror of
  `resolveAllowCombat`/`resolveAllowAlliance` — no CLI flag or environment variable for
  this at the wallet layer, ever.
- **`abi.ts`**: `decodeSimulateReturnData(selector, returnData)` — decodes a `view`
  function's return data against its own ABI outputs, `undefined` when the function has no
  outputs or decoding fails.
- **`cli.ts`**: new `walletctl simulate --json` flag, emitting a single
  `{ok, revertReason, error, decoded}` JSON line instead of plain-text output —
  `decoded` is the new capability, letting a caller read a `view` function's actual return
  value (`counterplayDefenseFuelContext`/`defenseHoldFuelContext`/`canCoordinateDefense`'s
  `canCoordinate`/`netHoldingFuelCost` for this feature, and generalizable to any future
  read-shaped simulate call). Plain-text output is unchanged when `--json` is omitted.

### Verified
- All four new/newly-allowlisted signatures (`launchDefenseHold`, `openDefenseIntent`,
  `counterplayDefenseFuelContext`, `defenseHoldFuelContext`) cross-checked against live
  `cast sig` output in `tests/selectors.cast.test.ts` — byte-exact selector correctness.

## [1.0.0] - 2026-09-07

The Veydrift game contract was upgraded on-chain (deployed 2026-09-07T02:02:59Z). The pinned
ABI is re-pinned to the new deployment; the prior pin now fails `walletctl verify-abi` against
live `/runtime-config`. Major bump per this file's convention — a breaking change to the ABI
pin — even though **no allowlisted selector and neither silent-corruption trap changed**: a
consumer who does not take this release has a wallet whose every write path is blocked by the
ABI-hash gate.

### Changed (breaking — ABI pin)
- **Game contract re-pinned** from commit `701bed3578cff4d134657c714c599dbdb55a4b6a` (abiHash
  `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99`) to
  `202d1acd9e35d815bd66cb9bae744341b1b1cf9e` (abiHash
  `sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4`). The new hash was
  reproduced locally by `forge build` at the reported `deploymentCommit` (same foundry settings
  as before: `solc 0.8.28`, `optimizer_runs 1`, `via_ir true`, `cbor_metadata false`,
  `bytecode_hash none`) and matched against live `/runtime-config` exactly. Artifact file
  `abi/VeydriftGame.701bed3.json` → `abi/VeydriftGame.202d1ac.json`; `src/abi.ts`'s
  `ARTIFACT_FILENAMES` and `tests/abi.test.ts`'s `EXPECTED_HASH`/`EXPECTED_COMMIT` updated to
  match.
- **Alliance contract re-pinned** from the same old commit (abiHash `sha256:3992c821…`) to
  `202d1ac` (abiHash `sha256:393335c106ecf203eb63d93d21c27b51e10fb4a217a5fd6de5feb63132999535`),
  rebuilt from the same `forge build`, so both pinned artifacts come from one coherent source
  tree. `abi/VeydriftAllianceSystem.701bed3.json` → `abi/VeydriftAllianceSystem.202d1ac.json`.
  Its on-chain address is unchanged (`0x0E5a6210482B15780cf5Ec036107031dcA702001`) and
  `/runtime-config` still exposes no `allianceAbiHash`/`allianceDeploymentCommit`, so this pin
  still has no live re-verification path and is still verified only once, by construction — see
  `references/abi-pinning.md`'s "Second contract" section. The 15 in-scope membership functions'
  selectors are byte-identical between the two commits, independently checked at re-pin time.

### Unchanged (verified against the new ABI)
- All allowlisted selectors: the five `ECONOMY_SIGNATURES`, both `launchFleetMission` overloads
  (`0x60eac16f` / `0x28247df8`), `launchInterplanetaryMissileAttack` (`0xa72cd29a`),
  `resolveFleetMission` (`0xde09e7cf`), and every one of the 15 `ALLIANCE_SIGNATURES`.
- Both silent-corruption traps: the 14-slot `uint32` fleet tuple and the `launchFleetMission`
  overload pair. The `Ship` enum is still 16 members with `SolarSatellite` at 9 and `Crawler`
  at 15.
- All six `NONPAYABLE_READ_FUNCTIONS` are still ABI-`nonpayable` (not `view`).

### ABI diff (game contract, `701bed3` → `202d1ac`; 289 → 303 entries, 138 → 147 methodIds)
- **Added, not allowlisted** (default-deny — unreachable through `walletctl` without a source
  change): `playerScore(address)`, `settleProductionUntil`, `settleAllianceMembershipBoundary`,
  `depositPaidAllianceInviteFee`, `startPlanetWithAllianceInvite`, a moon-attack-parity surface
  (`launchBodyAttackMission`, `joinBodyAttackMission`, `resolveFleetMissionCombatRound`,
  `battleResolutionProgress`, `attackBodyProtectionStatus`, …), a temperature-migration surface
  (`migratePlanetTemperatures`, `planetTemperatureGenerationVersion`), and `gamePaused()`.
- **Removed**: `firstPlanetOf`, `hasFirstPlanet`, `previewFirstPlanet`, `FLEET_RECALL_COST_BPS`,
  `settleDuePlayerColonizeArrivals`, `untrackResolvedFleetMission`. None were used by `src/`.
- **`playerScore` / `firstPlanetOf` reversed.** Pre-upgrade, `playerScore` was a `main`-only
  function that reverted on-chain and `firstPlanetOf` was deployed-only; `tests/abi.test.ts`
  asserted exactly that. Post-upgrade it is the other way round, and the two assertions are
  flipped to match. `src/` never called either.

### Docs
- `references/abi-pinning.md`: "Why this exists" / "The pin, as shipped" / rebuild recipe now
  point at `202d1ac`; the old "main-vs-deployed divergence" section replaced with "What the
  2026-09-07 on-chain upgrade changed" (the full ABI diff above); "Second contract" and
  "Provenance" sections updated; foundry-settings note unchanged (settings are identical).
- `SKILL.md`: the `playerScore` aside and the `abandonPlanet` source-line citation (line 150 →
  108, commit updated); behavior unchanged, only the line moved.
- `references/providers.md`: the `abandonPlanet` / `CannotAbandonHomePlanet` source citations
  re-pinned to `202d1ac` with updated line numbers (behavior unchanged).
- `src/fleet.ts`: the `Ship`-enum source citation re-pinned to `202d1ac` (enum unchanged).
- Repo-level `AGENTS.md` §6 updated out-of-band alongside this release.

## [0.9.2] - 2026-09-02

### Docs
- `references/fork-testing.md` gained §12 (round 5) — 13 of the 15 alliance-membership
  functions live-sent on a local Anvil fork, across a full lifecycle (create/invite/
  cancel/accept/request/dismiss/approve/role-change/ownership-transfer/kick/leave) using
  three fresh, alliance-free accounts. Every step confirmed against real on-chain state
  (`allianceOf`/`allianceMembers`/`allianceProfile`/`allianceInvite`/
  `allianceJoinRequests` reads), not just emitted events — including confirming
  `guard._gate_alliance_action`'s Officer-or-above bar for `approveJoinRequest` matches
  the real contract exactly. Only `kickMembers`/`setMembersRole` (the two batch variants)
  weren't sent, being structurally identical to their already-verified singular
  siblings. No code changed; every function behaved exactly as designed.
  `docs/COVERAGE.md`'s Alliances row and `AGENTS.md`'s fork-testing pointer updated to
  match.

## [0.9.1] - 2026-09-01

### Docs
- `SKILL.md`'s "five checks" summary updated to mention the alliance contract's address
  (item 1) and the 15 `ALLIANCE_SIGNATURES` functions' inclusive economy-or-operator
  tier check (item 2), alongside the alliance feature's own doc sweep in
  `veydrift-agent`'s `CHANGELOG.md` `1.15.3` entry.

## [0.9.0] - 2026-09-01

Alliance feature, commit 2/6: an alliance transaction is now buildable and (conditionally)
sendable. Minor bump, same convention as the Colonize/Attack/Missile allowlist widenings
before it — additive and backward compatible (no existing caller's behavior changes unless
they set `policy.actions.allow_alliance: true` themselves), but a real widening of the
transaction allowlist's security surface.

### Added
- `resolveAllowAlliance` + `AllowAllianceResolutionError` (`src/policy.ts`) — the
  alliance-feature counterpart to `resolveAllowCombat`, refactored alongside it into a
  shared `resolveBooleanActionFlag` helper (both are "read one boolean off `actions` in
  `policy.json`, refuse-on-malformed, default-false-on-absent, no CLI-flag/env fallback
  ever" with only the field name and error type differing).
- `ALLIANCE_SIGNATURES`/`allianceSelectorSet()` (`src/allowlist.ts`) — the 15 in-scope
  `VeydriftAllianceSystem` membership functions, resolved against the alliance ABI pinned
  last commit.

### Changed (breaking — allowlist widening)
- `Action` (`src/tx.ts`) gained `contract?: "game" | "alliance"`, defaulting to `"game"` —
  every action JSON written before this feature (including hand-written manual-override
  files) keeps building the exact same transaction it always did. `buildTx` resolves both
  the ABI and the destination address off this field.
- `checkAllowlist`'s address check (item 1) widened to a three-member candidate set
  (`gameContractAddress`/`contractAddress`/`allianceContractAddress`). Its selector check
  (item 2) gained a new branch: `allianceSelectors.has(selector) && (tier === "economy" ||
  tier === "operator")`, calling `resolveAllowAlliance` lazily exactly like Attack's/
  Missile's `allow_combat` checks — but **inclusive of both tiers**, not a single tier,
  since `economy` is alliance's floor, not its ceiling. `checkAllowlist`/`sendTx` gained an
  injectable `resolveAllowAlliance` option, mirroring `resolveAllowCombat`'s.

52 new tests (`tests/policy.test.ts`'s `resolveAllowAlliance` quintet plus an
independence-from-`allow_combat` check, `tests/allowlist.test.ts`'s alliance describe
block, `tests/tx.test.ts`'s `contract` field tests, 15 new `tests/selectors.cast.test.ts`
entries). 216 passed + 2 fork-only skipped = 218 total (164 baseline from `0.8.0` + 52
new); typecheck clean.

Docs: `references/tx-safety.md`'s "Five checks" list (items 1 and 2, now three-way/dual-
conditional) and a new "2026-09-01" section; `references/abi-pinning.md` unchanged this
commit (already covered last commit).

## [0.8.0] - 2026-09-01

### Added
- Alliance feature, commit 1/6: pinned a second contract, `VeydriftAllianceSystem`
  (`abi/VeydriftAllianceSystem.701bed3.json` + `abi/PINNED.alliance.json`, same commit and
  build settings as the game contract's pin). `src/abi.ts`'s loaders/resolvers
  (`loadPinnedArtifact`, `loadPinnedMeta`, `getPinnedAbi`, `findFunctionsByName`,
  `findFunctionBySignature`, `resolveFunctionAbi`, `computePinnedAbiHash`) all gained an
  optional `contract: "game" | "alliance" = "game"` parameter — every pre-existing call site
  is unaffected by the default. `functionsForSelector` (decode/display only) now searches
  both pinned ABIs. `RuntimeConfig` gained `allianceContractAddress`. `verifyAbi()` stays
  game-only, deliberately — no live `allianceAbiHash`/`allianceDeploymentCommit` field
  exists anywhere in `/runtime-config` to check the alliance pin against; see
  `references/abi-pinning.md`'s new "Second contract" section for the permanent limit this
  implies. No behavior change yet — nothing alliance-related is reachable through any live
  path until the next few commits land.

## [0.7.2] - 2026-08-31

### Added
- `license: MIT` in `SKILL.md`'s frontmatter and `package.json` — the repo root now
  carries a `LICENSE` file.

## [0.7.1] - 2026-08-31

### Docs
- **`references/fork-testing.md` gained §11 (round 4, 2026-08-30)** — Attack and Missile,
  commits 6-7's two combat additions, both live-sent on a local Anvil fork for the first
  time. Attack's full launch path (ships/fuel/mission-type/randomness-request) is confirmed
  against real emitted events; its battle resolution remains genuinely out of reach on a
  fork (an off-chain, precommitted randomness reveal this codebase has no part in).
  Missile's complete launch-through-resolution path is confirmed deterministically against
  real before/after on-chain defense counts on both the origin and target planets — the
  strongest verification any allowlisted selector has received in this runbook. No code
  changed; both selectors behaved exactly as designed. `docs/COVERAGE.md`'s Missile and
  Attack rows updated to match.

## [0.7.0] - 2026-08-28

Launch-actions plan, commit 7: `launchInterplanetaryMissileAttack` becomes allowlisted,
conditionally on `policy.actions.allow_combat`, at `operator` tier — a brand-new
selector, sharing nothing with the `launchFleetMission` path.

### Added
- **`COMBAT_SIGNATURES`** (`allowlist.ts`) — `["launchInterplanetaryMissileAttack(uint256,
  uint256,uint8,uint32)"]`, resolved to a selector set (`combatSelectorSet()`) the same
  way `ECONOMY_SIGNATURES`/`LAUNCH_FLEET_MISSION_SIGNATURES` already are, but
  deliberately never merged into `tierSelectors`'s unconditional return value.

### Changed (breaking — allowlist widening)
- `checkAllowlist`'s selector check (item 2 of five) now falls through to
  `combatSelectorSet()` — at `operator` tier only — when the unconditional lookup
  misses, calling `resolveAllowCombat` lazily exactly like Attack's mission-type branch
  does (commit 5): a malformed or absent `actions.allow_combat` must never block an
  unrelated, non-combat transaction. Unlike Attack, there is no calldata argument to
  decode — the selector itself IS the combat action, so its `allow_combat`
  conditionality lives in the selector check directly.
- No changes to `tx.ts`/`abi.ts`/`cli.ts` were needed — `buildTx` already resolves any
  function generically by name/signature via `resolveFunctionAbi`, so a brand-new,
  non-overloaded selector needed no encoder-side special-casing, only the new allowlist
  permission.

8 new tests in `tests/allowlist.test.ts`'s new "launchInterplanetaryMissileAttack" block
(rejects at economy/advisor regardless of `allow_combat`, rejects/allows at operator
based on `allow_combat`, refuses to pass vacuously when `resolveAllowCombat` throws, is
never called outside operator tier, is confirmed absent from `tierSelectors('operator')`'s
own unconditional set) plus a new entry in `tests/selectors.cast.test.ts`'s live `cast
sig` cross-check (confirmed live: selector `0xa72cd29a`, matching the pinned ABI's own
`methodIdentifiers` entry independently). 141 passed + 2 fork-only skipped = 143 total
(135 baseline from 0.6.0 + 8 new; typecheck clean).

Docs: `references/tx-safety.md`'s "Five checks" list (item 2, now conditional) and a new
"commit 7" section; `SKILL.md`'s matching checklist item.

## [0.6.0] - 2026-08-28

The allowlist-widening change below is the same kind of change 0.3.0's Colonize widening
was — a real widening of the transaction allowlist's security surface, flagged with its
own "breaking" section header for visibility, but still a minor bump per this file's own
convention, since it is additive and backward compatible (no existing caller's behavior
changes unless they now set `policy.actions.allow_combat: true` themselves).

### Added
- **`resolveAllowCombat` and `AllowCombatResolutionError`** (`src/policy.ts`), alongside
  the existing `resolveTier`. Resolves `policy.json`'s `actions.allow_combat` from
  `$VEYDRIFT_HOME/policy.json`, same "policy file is authoritative, never a caller-
  supplied value" shape `resolveTier` already uses — but **deliberately without
  `resolveTier`'s no-policy-file fallback to a CLI flag/env var**: there is no
  `--allow-combat` flag and no `VEYDRIFT_ALLOW_COMBAT` env var anywhere in this engine's
  surface, on purpose, since adding one would let a caller that controls its own
  environment simply assert combat is allowed — widening the already-documented `--tier`
  footgun (`references/tx-safety.md`'s residual-limit section) from "assert operator" to
  "assert operator *and* combat." No policy file -> `false`. Malformed/unparseable file,
  or a missing/non-boolean `actions.allow_combat` -> refuses outright (throws), the same
  "a malformed security policy must never fall through to a permissive default" rule
  `resolveTier` already applies to `tier`.
- **`checkAllowlist`'s new `opts.resolveAllowCombat` parameter** (injectable, defaults to
  the real `resolveAllowCombat`), and `sendTx`'s matching `SendOptions.resolveAllowCombat`
  forwarding it through — same injectable-dependency pattern `opts.fetchConfig` already
  uses toward the live `/runtime-config` fetch. Called **lazily**: only once a decoded
  `launchFleetMission` mission type is actually Attack, so a malformed or absent
  `allow_combat` field never blocks an unrelated, non-combat transaction.

### Changed (breaking — allowlist widening)
- **`COMBAT_ALLOWED_MISSION_TYPES = new Set([3])`** — a second, separate mission-type set
  from `OPERATOR_ALLOWED_MISSION_TYPES`, deliberately not merged into it. `checkAllowlist`
  checks it only for a decoded Attack mission type, and only then calls
  `resolveAllowCombat` to decide. Mirrors `veydrift-agent`'s own two-set split
  (`_ALLOWED_MISSION_TYPES` / `_COMBAT_MISSION_TYPES`, `guard.py`), added in the same
  change, never before it — the same "both layers together, never one first" sequencing
  discipline 0.3.0's Colonize widening already established, for the same reason (widening
  one layer alone would reopen the single-layer-enforcement gap the other layer exists to
  close). `OPERATOR_ALLOWED_MISSION_TYPES` itself is unchanged — Attack was never in it
  and still isn't. The remaining five combat mission types (`AcsDefend`/`Intercept`/
  `MissileAttack`/`AcsAttack`/`DefenseHold`) are unaffected — absent from both sets, on
  both sides, unconditionally, regardless of `allow_combat`.
  - `tests/policy.test.ts`: new `resolveAllowCombat` suite (8 tests) mirroring
    `resolveTier`'s own test shape.
  - `tests/allowlist.test.ts`: the `it.each([3, 5, 6, 7, 8, 9])("rejects mission
    type...")` case split — `3` moved into its own "mission type 3 (Attack)" describe
    block (4 new tests: rejects when `allow_combat` resolves false, allows when it
    resolves true, rejects when `resolveAllowCombat` throws, and confirms it is never
    called at all for a non-Attack mission type), `[5, 6, 7, 8, 9]` kept as an
    unconditional-rejection case, now explicitly exercised with `allow_combat=true` to
    prove it doesn't affect them.
- Agent-side `test_tier_map_agrees_with_the_wallet_engines_allowlist` reworked to diff
  both mission-type set *pairs* independently (unconditional and combat-gated) rather
  than one set each — see `veydrift-agent`'s own `CHANGELOG.md` for that half.

### Docs
- `references/tx-safety.md`: new "The same residual limit applies to `allow_combat`"
  subsection (mirroring the existing `resolveTier` one), the "Five checks" list's
  mission-type item updated, and a new dated section entry alongside 0.3.0's Colonize
  one. `SKILL.md`'s mission-type bullet updated to mention Attack's conditional
  reachability.

## [0.5.1] - 2026-08-27

### Fixed

- **Skill self-containment.** `SKILL.md`, `references/abi-pinning.md`,
  `references/fork-testing.md`, and `references/tx-safety.md` no longer cite
  `docs/SPEC.md`/`docs/RESEARCH-ADDENDUM.md`/`docs/COVERAGE.md`/`README.md`/`AGENTS.md` —
  repo-root paths outside this skill's own directory, which aren't guaranteed to travel
  with an installed skill (`npx skills add .`). Where a citation pointed at a dated fix
  also recorded in this file, replaced it with a `CHANGELOG.md` version citation instead;
  otherwise restated the fact inline.
- **`references/fork-testing.md` gained a table of contents.** At 713 lines it's the
  largest reference file across both skills and was the only one over the 300-line
  threshold missing one.
- **`npm run wallet:new` (`scripts/gen-keystore.mjs`) is now documented.** It was never
  referenced from `SKILL.md` or `references/providers.md` — the keystore section documented
  *using* an existing `VEYDRIFT_KEYSTORE` file but never how to create one, a real gap for a
  first install on the default provider. Documented in `providers.md`'s keystore section
  (with the caveat that it mints a *new* address, so it's for a fresh install with no
  settled planet yet, not for migrating an existing key into keystore format) and pointed
  to from `SKILL.md`'s Providers section.

## [0.5.0] - 2026-08-22

### Added
- **`npm run wallet:new`** (`scripts/gen-keystore.mjs`), an interactive keystore-creation
  helper, replacing `PLAYER-GUIDE.md`'s old inline `npx tsx -e '...'` recipe. The old
  recipe took the encryption password as a plaintext literal inside the script itself —
  visible in shell history and, briefly, in a process listing. The new script prompts for
  it interactively instead (`@inquirer/prompts`, masked, typed twice to catch a typo,
  rejected if empty), and prompts for the output directory too (defaulting to
  `~/.veydrift`, matching the `VEYDRIFT_KEYSTORE` convention the rest of the guide already
  assumes). Generates a brand-new random address only — same limitation as the recipe it
  replaces, still cannot import an existing key.

## [0.4.1] - 2026-08-19

### Fixed
- **`simulateTx` (`src/tx.ts`) capped its `eth_call` at the gas that will actually be sent,
  instead of running uncapped against the node's block gas limit.** Previously, `simulate`'s `ok`
  verdict came from an `eth_call` with no `gas` field at all — a separate, fresh `estimateGas()`
  was fetched immediately after, but only ever surfaced as `SimulateResult.gas`/`estimatedCostWei`
  reporting metadata, never validated against or used to cap the call that decided `ok`. So
  `simulate` answered "would this succeed given unlimited gas," not "will the transaction that
  actually gets sent succeed" — and every provider's `signAndSend` passes `tx.gas` through
  verbatim (`providers/keystore.ts`, `envkey.ts`, `fork-impersonate.ts`), so those two questions
  can have different answers.

  Confirmed live on an Anvil fork of Base, real account, planet 664: a `startResearch` call built
  at an estimated gas limit of 465588 (`walletctl build`) simulated `ok: true` (pre-fix, uncapped),
  was sent at that same 465588 limit (`send` always submits `tx.gas` verbatim), and reverted
  `OutOfGas` after genuinely executing most of a `_settleResearchDue` settlement sweep
  (`VeydriftPlanetManagementModule.sol:330`). The identical calldata resent at 931176 (2x) against
  the same fork state succeeded, proving the failure was a pure gas shortfall, not a logic bug.
  `startBuildingUpgrade`, `startShipProduction`, and `startDefenseProduction` all succeeded cleanly
  earlier in the same fork session — not a defect in every selector, but a real one for any call
  whose settlement sweep is unexpectedly wide, which only shows up against real, accumulated
  on-chain state.

  Fixed: `simulateTx` now caps `client.call()` at `tx.gas` when it's known; when it isn't
  (`build` ran without `--from`, or its own estimate failed), it fetches a fresh estimate and
  validates the call against that same figure instead of leaving the call uncapped — a failed
  fallback estimate now propagates as `ok: false` rather than falling through to an unbounded
  call, per this project's fail-closed-on-absent-data rule (`AGENTS.md` §5). The separately-fetched
  `estimateGas()` used for `SimulateResult.gas`/`estimatedCostWei` reporting is unchanged.
  `SimulateResult`'s shape is unchanged — only its accuracy. No Python change was required;
  `veydrift-agent`'s `_walletctl_simulate` (`tick.py`, added `1.1.1`) just parses `simulateTx`'s
  `ok`/`revert reason` output and inherits the fix automatically (confirmed: 484 Python tests
  still pass unmodified).
  See `references/tx-safety.md` and `references/fork-testing.md` §8.4 for the full detail, and
  `AGENTS.md` §10 for how this was found.

## [0.4.0] - 2026-08-17

### Added
- **`fork-impersonate` wallet provider** (`src/providers/fork-impersonate.ts`, landed `dac1050`,
  documented this release). A third provider, registered normally in `providers/index.ts`: runs the
  exact production `sendTx` → `provider.signAndSend` path against a local Anvil fork, using
  `anvil_impersonateAccount` + `anvil_setBalance` + node-trusted `eth_sendTransaction` instead of a
  locally-held key. Reports `capabilities()` honestly as `{ canSign: false, canSimulate: false,
  remotePolicy: false }` — a genuinely new provider category, not a third instance of `keystore`/
  `envkey`'s signing triple. Constructor eagerly calls `refuseIfNotLoopback(getRpcUrl())`, which
  throws unless the resolved RPC host is `127.0.0.1`/`localhost`/`::1`/`[::1]` — this is what makes
  ordinary registry membership safe: production's `VEYDRIFT_RPC_URL` never resolves to loopback, so
  selecting this provider outside a local fork is inert by construction.
- **This is additive, not a new permission.** `fork-impersonate` does not change what any tier is
  allowed to send — `checkAllowlist`'s five checks and the mission-type restriction run unchanged
  regardless of which provider signs. Combat mission types remain unreachable. `--confirm` remains
  unconditionally required; the provider changes *who* signs, never *whether* confirmation is
  needed (`references/tx-safety.md`'s new qualification).
- `references/fork-testing.md` (new) — the execution runbook: starting Anvil, environment setup,
  the per-selector `build`/`simulate`/`send`/`receipt` sequence, the 7 reachable selectors and which
  are planner-reachable, two gotchas (the memoized public client; `/runtime-config` being
  ungoverned by `VEYDRIFT_RPC_URL`), and three verifications beyond a routine sweep (colony-target
  packing, the two fleet-tuple encoders against real contract state, the fuel formula against a
  real balance delta).
- `references/providers.md` — documents `fork-impersonate` as a genuinely new, third provider
  category (node-trusted, unsigned) distinct from both local-signing providers.
- `references/tx-safety.md` — clarifies "never against mainnet" refers to mainnet specifically; a
  local fork is the intended first real exercise of `sendTx`'s send path, not an exception to the
  standing rule.
- `tests/providers/fork-impersonate.test.ts` — unconditional loopback-guard and address-validation
  tests, plus a real-anvil e2e suite skip-gated on `anvil` being installed and
  `VEYDRIFT_FORK_TEST_RPC_URL` being set (absent either, `npm test` stays green and offline).

## [0.3.0] - 2026-08-17

The allowlist-widening change below shipped in the working tree without a version bump of
its own; folded into this release now, alongside a docs-only fix, rather than left
permanently unreleased.

### Fixed (docs)
- Stale mission-type-list comments corrected to mention Colonize (2): `allowlist.ts:57`
  ("types 0/1/4" -> "0 Transport / 1 Deploy / 2 Colonize / 4 Harvest") and
  `allowlist.ts:214-215` (same). No functional change — the actual
  `OPERATOR_ALLOWED_MISSION_TYPES` set already included `2`; only the comments had
  drifted. Found during a judge review of `veydrift-agent`'s general-strategy-engine
  program (finding 5's stale-reference sweep, 2026-08-17).

### Changed (breaking — allowlist widening)
- **`OPERATOR_ALLOWED_MISSION_TYPES` widened to include Colonize (2).** Was withheld in
  0.2.0 (see that version's "Not done this phase" note, kept below for the record)
  pending the matching independent Python-side gate. `veydrift-agent`'s `models.py` has
  since been unfrozen and extended with `ActionKind.FLEET_MISSION` and the `Action`
  fields `launchFleetMission` needs; `guard.py` now has its own `mission_type` gate
  (Phase 5c, an 18th gate, was 17), added in **the same change** as this widening —
  never before it, per the phase 5b brief's own ordering requirement (widening this
  allowlist first would have reopened the single-layer-enforcement gap the new gate
  closes). `test_tier_map_agrees_with_the_wallet_engines_allowlist` (agent-side) now
  also compares the two mission-type sets and fails naming the diff if they ever drift.
  Confirmed as a genuine colonisation entrypoint (not combat-adjacent) — see 0.2.0's
  entry below for the contract evidence, unchanged. See `references/tx-safety.md`'s
  mission-type section for the updated allowed set.
  - `tests/allowlist.test.ts`: the `it.each([2, 3, 5, 6, 7, 8, 9])("rejects mission
    type...")` case moved `2` into the `it.each([0, 1, 4])("allows mission type...")`
    case above it — the one pre-existing test this widening necessarily changes.

## [0.2.0] - 2026-08-17

### Removed (breaking)
- **`settlePlanet(uint256)` removed from `ECONOMY_SIGNATURES`.** Phase 5 of the
  general-strategy-engine program (docs/SPEC.md §5.4/§9). Its body at the pinned
  commit (`701bed3578cff4d134657c714c599dbdb55a4b6a`) is
  `_touchPlayer(msg.sender); _collectPlanetResources(planetId);` — byte-identical to
  `collectResources`, which `abi.ts`'s `NONPAYABLE_READ_FUNCTIONS` already refuses in
  `sendTx` as a disguised read. It was allowlisted here and in `veydrift-agent`'s
  `guard.py` (`_MIN_TIER_FOR_FUNCTION`), with a live `tick.py` encoder branch, but no
  planner rung on the agent side ever produced this action — it was allowlisted
  capacity that could only ever burn gas for zero effect. Removed from all three
  places in the same change (`allowlist.ts`, `guard.py`, `tick.py`'s encoder), so
  `test_tier_map_agrees_with_the_wallet_engines_allowlist` (agent-side) still passes.

### Not done this phase
- **Colonization (`launchFleetMission` mission type 2) was NOT added to
  `OPERATOR_ALLOWED_MISSION_TYPES`.** The brief for this phase asked for it, gated on
  verifying the entrypoint against contract source first. That verification is done —
  `VeydriftGame.sol`'s facade `launchFleetMission` (both overloads) inspects the
  `missionType` argument via inline assembly at calldata offset `0x44` and, when it
  equals `Colonize` (2), delegates to `VeydriftColonizationModule`, whose
  `_launchColonizeFleetMission` calls `_validateColonyCreation`, which calls
  `_requireShips(originPlanetId, Ship.ColonyShip, 1)` — confirming `launchFleetMission`
  is genuinely the entrypoint, not a distinct `launchBodyFleetMission`-style function.
  The widening itself was withheld because the matching independent Python-side
  mission-type gate (this phase's other required half — see `guard.py`'s
  `_MIN_TIER_FOR_FUNCTION` docstring and AGENTS.md §5's two-layer-agreement invariant)
  could not be built: it needs an `ActionKind.FLEET_MISSION` and new `Action` fields on
  the agent's `models.py`, which is frozen for that work package. Widening this
  allowlist without that counterpart would leave the wallet engine as the *sole*
  enforcement layer for mission-type restriction on `launchFleetMission` — a real,
  not merely theoretical, regression of the two-layer design this project deliberately
  keeps. See `docs/SPEC.md` and the veydrift-agent WP report for the full blocker.

## [0.1.1] - 2026-08-15

### Changed
- `SKILL.md` now tells an agent invoking `walletctl` directly (not through
  `veydrift-agent`'s `vd tick`) to check for `node_modules/` and run `npm install` first
  — `npx skills add` copies this skill's source and lockfile but never installs from
  them.

## [0.1.0] - 2026-08-12

### Added
- Initial release: `keystore`/`envkey` providers, the transaction allowlist, ABI pinning
  against the deployed contract, fleet-mission tuple encoding.
