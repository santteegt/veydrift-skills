# Changelog

All notable changes to `veydrift-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface or the
on-disk `policy.json`/`proposals.jsonl`/`actions.jsonl` schema bump major, additive
backward-compatible changes bump minor, fixes and docs-only changes bump patch. This
package's version lives in `pyproject.toml`, independent of `veydrift-wallet`'s — the two
skills are not versioned in lockstep.

## [Unreleased]

### Added
- `vd tick`/`vd tick --readiness` narrow (do not close) the documented "a human
  executing a T1 proposal by hand is invisible to this tool" blind spot: whenever the
  previous tick's proposal was on-chain and unresolved (tier 1, or
  `wallet_engine.require_confirmation` stopped the send), the next tick makes a
  best-effort `/wallet/{addr}/activity` fetch and surfaces whatever raw items come back
  — titles, kinds, transaction hashes — for a human to read. This is **observational
  only**: it never feeds `guard.py`/`Decision`, and it deliberately does **not** classify
  "followed advice" vs. "diverted" — the only `/activity` item ever actually observed by
  this project is a one-time "planet settled" milestone, so the shape of a routine
  building/research-completion item is unconfirmed. A structured match/diverge
  classifier is a deferred follow-up once a real completion-shaped item has been
  observed.

### Fixed
- `tick.py`'s `_run_walletctl` now self-heals a missing `veydrift-wallet/node_modules`
  (installs once from the pinned lockfile, logged visibly, never silently) instead of
  letting a raw `ERR_MODULE_NOT_FOUND` surface as an opaque `walletctl_build` ESCALATE
  detail.
- `vd tick` no longer inflates `tick_count`/`proposals_count`/`logs/proposals.jsonl`/
  `logs/strategy.md` when a repeated invocation produces a content-identical proposal to
  the immediately-previous one (e.g. re-running `vd tick` just to re-inspect output in a
  different `--format`) — this was degrading exactly the promotion evidence
  `vd tick --readiness` reports. Dedup is content-based (a sha256 fingerprint of the full
  proposal record, excluding only `ts`/`tick`), not time-window based, so a genuine
  re-evaluation that happens to recommend the same thing hours later still logs normally.

## [0.1.0] - 2026-08-12

### Added
- Initial release: `read`, `calc`, `plan`, `guard`, `tick`, `log` modules; the tier model
  (advisor/economy/operator); the guardrail set documented in
  `references/guardrails.md`.
