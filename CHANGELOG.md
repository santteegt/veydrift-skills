# Changelog

All notable changes to this project are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). An entry is added per tier
promotion (see `AGENTS.md` §9) or per capability change (a new action reaching the
decision ladder, a new wallet provider, a new guardrail) — not per commit.

## [Unreleased]

### Added

- Initial build of the Veydrift agent infrastructure: two installable skills,
  `veydrift-agent` (Python/uv — reads the game API, runs deterministic calculators,
  proposes zero or one action per tick) and `veydrift-wallet` (TypeScript/Node — the sole
  path in this codebase that ever builds, allowlists, simulates or submits a transaction).
- Three-tier agent model (`advisor` → `economy` → `operator`), gated exclusively by a
  human edit of `policy.json`; no code path advances it. Combat mission types are
  unreachable in code at every tier.
- `vd read` (17 targets across the wallet, universe and top-level API surface, plus a
  composed `snapshot`), `vd calc` (deterministic formulas, no cost-scaling — costs are
  always read live), and `vd plan run` (the decision ladder, energy-first invariant,
  planet-trait-derived build order — verified against planet 664's real, current
  zero-state data and against a synthetic hot-planet fixture that inverts the energy-source
  choice).
- `walletctl` (`status`, `verify-abi`, `build`, `simulate`, `send`, `receipt`): two working
  wallet providers (`keystore` default, `envkey` testing-only) proven to derive the same
  address from the same key material; a live-`/runtime-config`-backed allowlist independent
  of the agent skill; ABI pinned to the deployed contract at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
  and verified live-matching; the 14-slot fleet-tuple index-shift conversion and the
  `launchFleetMission` overload-disambiguation, both dedicated functions with dedicated
  tests.
- Documentation: `AGENTS.md` (primary operating doc), `CLAUDE.md` (pointer), this file,
  both skills' `SKILL.md`, and `references/` covering the API routes, formulas, canonical
  entity-id enums, the strategy derivation, contract write entrypoints and their traps,
  wallet providers, ABI pinning, and transaction safety.
- `docs/wallet-provider-research.md`: every wallet-provider candidate beyond the two
  shipped (EIP-7702 delegation, Web3Signer, HashiCorp Vault, Cobo, Coinbase CDP, Turnkey,
  OKX OnchainOS) evaluated against the address-binding constraint — a Veydrift planet is
  permanently bound to the EOA that settled it, verified directly against the deployed
  contract. Recommendation: keep the encrypted keystore; EIP-7702 to Base's audited
  `EIP7702Proxy` is the one path worth prototyping later.

### Known gaps at this release

- **No transaction has ever been submitted to Veydrift from this codebase.** The write
  path is built, allowlisted and fixture-tested — never executed against mainnet. See
  `AGENTS.md` §8.
- Guardrail evaluation, the tick loop, structured logging and state management
  (`guard.py`/`tick.py`/`log.py`/`state.py`, generated JSON schemas, `policy.example.json`,
  and the launchd plist template) were a separate, concurrently-built work package and may
  not be present in every checkout of this history — run `vd doctor` to see what's wired
  in the copy you have.
- `docs/SPEC.md`'s tier table does not allocate `startShipProduction` to any tier's
  submittable set, even though the decision ladder can propose it when
  `policy.actions.allow_ships` is enabled. Do not enable `allow_ships` in a real policy
  file until this is resolved — see `skills/veydrift-agent/references/contract-writes.md`
  §8.

## Tier promotion log

No promotion has occurred. The account remains at tier 1 (`advisor`) since this project's
inception; every entry above describes the tier-1 build only.
