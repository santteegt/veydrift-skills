# Changelog

All notable changes to this project are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). An entry is added per tier
promotion (see `README.md`'s promotion procedure) or per capability change (a new action
reaching the decision ladder, a new wallet provider, a new guardrail) — not per commit.

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
- Documentation: `README.md` (product overview, install, usage, safety contract, key
  custody), `AGENTS.md` (dev-agent build/test commands and invariants, per the
  [agents.md](https://agents.md) convention), `CLAUDE.md` (pointer to both), this file,
  both skills' `SKILL.md`, and `references/` covering the API routes, formulas, canonical
  entity-id enums, the strategy derivation, contract write entrypoints and their traps,
  wallet providers, ABI pinning, and transaction safety.
- `docs/wallet-provider-research.md`: every wallet-provider candidate beyond the two
  shipped (EIP-7702 delegation, Web3Signer, HashiCorp Vault, Cobo, Coinbase CDP, Turnkey,
  OKX OnchainOS) evaluated against the address-binding constraint — a Veydrift planet is
  permanently bound to the EOA that settled it, verified directly against the deployed
  contract. Recommendation: keep the encrypted keystore; EIP-7702 to Base's audited
  `EIP7702Proxy` is the one path worth prototyping later.

### Changed

- Documentation restructured to match the [agents.md](https://agents.md) convention:
  `AGENTS.md` is now scoped to what a coding agent needs to *continue developing* this
  repo (setup/test commands, code invariants, the ABI-pinning procedure, known gaps in the
  code); the product overview, tier model, install/usage instructions, safety contract and
  key-custody explanation moved to a new `README.md`. Every cross-reference between skills'
  `SKILL.md`/`references/` files and the old single `AGENTS.md` was repointed to whichever
  file now actually holds that content.
- `skills-lock.json` removed from version control and added to `.gitignore`. It's an
  artifact `npx skills add` writes to whatever directory the install is *run from* (the
  consumer's working directory), not something this source repo should ship — it landed in
  git only because an install was once tested from the repo root itself.

### Corrected

- **The `startShipProduction`-not-in-any-tier gap this changelog previously listed under
  "Known gaps" was already fixed before that entry was written** — `docs/SPEC.md` §4 has
  granted it to the `economy` tier, in both enforcement layers, since the first judge-review
  fix pass. The stale bullet is removed rather than left to mislead; see `git log` for the
  fix commits and `skills/veydrift-agent/references/contract-writes.md` §8 for the
  after-the-fact writeup.
- **A claimed `files:` frontmatter exclusion mechanism does not exist.** SKILL.md
  frontmatter has no field that filters what `npx skills add` copies — verified empirically
  by installing a probe skill with `files: ["**/*", "!.venv/", "!node_modules/"]` in its
  frontmatter and observing both directories copied anyway. The installer's only exclusions
  are hardcoded: dotfiles/dotdirs plus `__pycache__`/`__pypackages__`/`metadata.json` (as of
  `skills@1.5.22`) — `.venv`, `node_modules`, `dist`, and `build` are **not** excluded by
  default. `README.md`'s Install section and `AGENTS.md` §3 both now say to clean those out
  of `skills/*/` before installing, rather than relying on frontmatter that has no effect.

### Known gaps at this release

- **No transaction has ever been submitted to Veydrift from this codebase.** The write
  path is built, allowlisted and fixture-tested — never executed against mainnet. See
  `README.md`'s "What this does not verify" section.
- Guardrail evaluation, the tick loop, structured logging and state management
  (`guard.py`/`tick.py`/`log.py`/`state.py`, generated JSON schemas, `policy.example.json`,
  and the launchd plist template) were a separate, concurrently-built work package and may
  not be present in every checkout of this history — run `vd doctor` to see what's wired
  in the copy you have.
- `walletctl`'s tier check falls back to a caller-supplied `--tier` when no policy file
  exists at `$VEYDRIFT_HOME`, so it defends against a misconfigured caller rather than a
  hostile one that controls its own environment. Documented, not fixed — see
  `skills/veydrift-wallet/references/tx-safety.md`'s residual-limit section and `AGENTS.md`
  §10.

## Tier promotion log

No promotion has occurred. The account remains at tier 1 (`advisor`) since this project's
inception; every entry above describes the tier-1 build only.
