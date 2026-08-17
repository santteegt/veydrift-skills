# Changelog

All notable changes to `veydrift-wallet` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface, the ABI
pin, or the transaction allowlist bump major, additive backward-compatible changes bump
minor, fixes and docs-only changes bump patch. This package's version lives in
`package.json`, independent of `veydrift-agent`'s — the two skills are not versioned in
lockstep.

## [Unreleased]

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
