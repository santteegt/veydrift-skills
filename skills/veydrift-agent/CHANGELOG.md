# Changelog

All notable changes to `veydrift-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface or the
on-disk `policy.json`/`proposals.jsonl`/`actions.jsonl` schema bump major, additive
backward-compatible changes bump minor, fixes and docs-only changes bump patch. This
package's version lives in `pyproject.toml`, independent of `veydrift-wallet`'s — the two
skills are not versioned in lockstep.

## [Unreleased]

## [0.3.0] - 2026-08-16

### Added
- New module `techtree.py`: the full on-chain prerequisite table for buildings, ships,
  defenses and research (transcribed from `VeydriftDependencies.sol`/`VeydriftCatalog.sol`
  at the pinned commit `701bed3578cff4d134657c714c599dbdb55a4b6a`), plus the shield-dome
  per-planet cap and missile-silo slot capacity. `unmet()` fails closed on absent data: a
  building/technology level the snapshot didn't report is treated as *not satisfying* a
  requirement, never assumed high enough — the same no-vacuous-pass posture as every
  existing guard. A new `prerequisites` gate in `guard.py`, slotted immediately after
  `tier` and before `address`, independently re-derives the same check from `Snapshot`
  rather than trusting `plan.py`'s own filtering (the same defense-in-depth posture
  `_gate_energy` already takes toward the energy invariant) and additionally BLOCKs a
  shield-dome/missile-silo cap violation. `GuardReport.verdicts` is now a fixed 17-entry
  list, up from 16. `unmet()` reports only the strictest unmet requirement per target: the
  contract genuinely checks some targets twice (every defense carries a blanket
  `Shipyard >= 1` on top of its own `Shipyard >= N`), and reporting both rendered as
  `"needs Shipyard 1 (have 0); needs Shipyard 8 (have 0)"` — two clauses for one problem.
  The tables still mirror the contract verbatim; the collapse happens at the output, and
  never turns a locked entity into an empty result.
- `plan.py` now filters every building/ship/defense/research candidate through
  `techtree.unmet()` before returning it as a proposal. Where the ladder's first-choice
  candidate is locked but a lower-priority one is not (e.g. Laser Technology locked on
  Energy tech level, Combustion Drive available at the same Research Lab level), the
  planner skips to the next unlocked candidate rather than silently falling through to a
  rung-9 NOOP.

### Fixed
- **The live bug this phase exists to close**: `plan.py`'s rung 7
  (`_next_research_action`) picked `min(snapshot.technologies, key=lambda t: ((t.level or
  0), t.id))` with no regard for whether the pick's prerequisites were met — on a fresh
  planet this resolved to Energy Technology (id 0), which requires Research Lab ≥ 1. On a
  fresh planet at tier ≥ 2 that was a guaranteed on-chain revert, paid in real gas, the
  first time the ladder's own default pick was ever submitted. The same hole let rung 8
  propose a Rocket Launcher on a planet with no Shipyard (`requireDefense`'s unconditional
  `shipyardLevel == 0` revert). Both are now filtered through `techtree.unmet()` before a
  proposal can be returned; `tests/test_plan.py::test_research_not_proposed_when_research_lab_is_zero_the_bug_this_wp_fixes`
  pins the fix down directly, and
  `tests/test_plan.py::test_rocket_launcher_not_proposed_without_a_shipyard` pins the
  Rocket Launcher case.

### Not covered by this change
- **`techtree.py`'s table is transcribed from contract source and has never been
  validated against a live revert.** This account has taken zero on-chain actions, so no
  proposal this table declares "unlocked" has ever actually been submitted and observed
  to succeed, and none it declares "locked" has been confirmed to revert for exactly the
  stated reason. See `docs/SPEC.md` §11.
- **No cost-scaling formula was added anywhere.** `techtree.py` is a *requirements*
  (level-comparison) table only — it never computes or scales a cost. Live cost stays the
  API's `cost` object, unchanged from every prior release; `calc.py`'s "no cost-scaling
  function" constraint is untouched by this change.
- **The shield-dome/missile-silo cap check can undercount a real queue backlog.**
  `PlanetSnapshot` carries a single `QueueEntry` per queue kind, not a backlog list
  (`models.py` is frozen); the cap check accounts for at most one queued item.

## [0.2.0] - 2026-08-15

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
- `vd plan`'s mine/building-upgrade proposals now carry a plain-text `expected_effect`
  note showing how much faster the exact same build would complete at Robotics Factory
  level+1 (e.g. "at Robotics Factory 4, this build takes 3600s; at level 5, it would take
  1800s (50% faster)"), using the already-verified `calc.build_seconds` formula. `guard`'s
  `affordability` gate's BLOCK detail now also states an estimated "affordable in ~Xh Ym"
  per resource that's short, based on current `production_per_hour`, or explicitly "never
  affordable" when a resource's cost exceeds its storage cap or its production rate is
  zero. Both are **informational only** — plain computed facts appended to existing text
  fields, never a verdict, never a new guard behavior, never a `Decision` input;
  deliberately not an ROI/opportunity-cost calculator, since that would require assuming
  an unbounded, unknowable future build plan. `Action.expected_effect` — previously
  written by `plan.py` but read by nothing — is now also surfaced in `vd tick`'s printed/
  `--format json` report and in `logs/proposals.jsonl`.

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
