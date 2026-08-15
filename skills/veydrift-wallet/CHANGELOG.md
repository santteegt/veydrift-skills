# Changelog

All notable changes to `veydrift-wallet` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface, the ABI
pin, or the transaction allowlist bump major, additive backward-compatible changes bump
minor, fixes and docs-only changes bump patch. This package's version lives in
`package.json`, independent of `veydrift-agent`'s — the two skills are not versioned in
lockstep.

## [Unreleased]

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
