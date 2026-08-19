# Changelog

All notable changes to `veydrift-wallet` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface, the ABI
pin, or the transaction allowlist bump major, additive backward-compatible changes bump
minor, fixes and docs-only changes bump patch. This package's version lives in
`package.json`, independent of `veydrift-agent`'s — the two skills are not versioned in
lockstep.

## [Unreleased]

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
