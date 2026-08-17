# Changelog

All notable changes to `veydrift-wallet` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface, the ABI
pin, or the transaction allowlist bump major, additive backward-compatible changes bump
minor, fixes and docs-only changes bump patch. This package's version lives in
`package.json`, independent of `veydrift-agent`'s — the two skills are not versioned in
lockstep.

## [Unreleased]

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
