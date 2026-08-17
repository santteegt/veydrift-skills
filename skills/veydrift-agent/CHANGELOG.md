# Changelog

All notable changes to `veydrift-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface or the
on-disk `policy.json`/`proposals.jsonl`/`actions.jsonl` schema bump major, additive
backward-compatible changes bump minor, fixes and docs-only changes bump patch. This
package's version lives in `pyproject.toml`, independent of `veydrift-wallet`'s — the two
skills are not versioned in lockstep.

## [Unreleased]

Phase 5 of the general-strategy-engine program (docs/SPEC.md §5.4/§9). **Left under
`[Unreleased]` deliberately**: this phase's brief asked for veydrift-agent 1.0.0, but
`pyproject.toml` (and `models.py`) are frozen for this work package (see AGENTS.md §4 and
the WP report) and a version bump without the models.py-dependent half of this phase
landing would overstate what shipped. A maintainer who can edit those two files should
finish 5c/5b (below) and cut 1.0.0 in one change, not bump the version here first.

### Added
- **`PlanetSnapshot.archetype` is now populated** (was permanently `None` before this).
  `read.snapshot` gained an opt-in `--universe-cadence-hours` flag (default: unset, no
  new network call) that fetches `/universe/galaxies/{g}/systems/{s}` for each planet's
  own archetype; `vd tick` wires it automatically from `policy.cadence.universe_hours`
  (default 24h — a previously dead policy field). Cadence-gating reuses `http.py`'s
  existing disk cache rather than adding new state: the fetch is attempted every tick,
  but only reaches the network once per `universe_hours` window.
- **`plan.py`'s rung 3 (`resolveFleetMission`) is revived.** It has accepted
  `resolvable_mission_ids` since Phase 1, but nothing ever computed the argument —
  `tick.py` now does, via a new `_resolvable_mission_ids()` that reads
  `/wallet/{addr}/fleet-visibility` directly (bypassing `models.py`, the same posture
  `_maybe_check_human_activity` already takes toward `/activity`) and finds the
  player's own `outgoing` missions that are still `"Outbound"`, `needsResolution`, and
  more than 60s past `arrivalAt`.
- `read.fetch_fleet_visibility()` — a new CLI-bypassing helper mirroring
  `fetch_activity()`, used by the above.

### Fixed
- **`read._parse_datetime` no longer silently drops the live API's real timestamp
  format.** Confirmed live 2026-08-17: `arrivalAt`/`returnAt`/`readyAt`/`occurredAt` all
  arrive as a **decimal string of unix seconds** (e.g. `"1786947731"`), not ISO 8601 —
  `wallet_activity.json`'s own real (non-synthetic) fixture already carried this shape
  in `transactionAt`/`occurredAt`, but nothing had generalised the parser to match it.
  A decimal-string epoch previously fell through to `datetime.fromisoformat`, raised,
  and silently became `None` — indistinguishable from "the API didn't report this."
  `QueueEntry.ready_at` and `IncomingFleet.arrives_at` were the two fields this
  silently affected on real (non-synthetic-fixture) data. The two synthetic fixtures
  (`wallet_infrastructure_active_queue.json`, `wallet_overview_incoming.json`) guessed
  ISO instead of probing; both shapes now parse.

### Removed (breaking)
- **`settlePlanet` removed from `guard._MIN_TIER_FOR_FUNCTION` and `tick.py`'s
  `_action_to_walletctl_json` encoder.** Its body at the pinned commit is
  byte-identical to `collectResources`, a disguised read `veydrift-wallet`'s `abi.ts`
  already refuses to send. No planner rung ever produced this action. Mirrors the
  removal from `ECONOMY_SIGNATURES` in `veydrift-wallet`'s `allowlist.ts` (v0.2.0) —
  see that package's changelog for the full writeup and the contract evidence for why
  real colonisation (`launchFleetMission` mission type 2) is a different entrypoint
  entirely, not a `settlePlanet` variant.

### Not done this phase (blocked on `models.py`)
- **Non-combat fleet-mission planning (5c) and real colonisation (5b) are NOT
  implemented.** Both require `ActionKind.FLEET_MISSION` and new `Action` fields
  (`mission_type`, `origin_planet_id`, `target_coordinates`, `ships`, `cargo`,
  `speed_pct`, `holding_seconds`) on `models.py`, which is this work package's frozen
  interface (AGENTS.md §4). Everything downstream of that — `guard.py`'s mission-type
  gate, `tick.py`'s `launchFleetMission` overload resolution and 14-slot fleet-tuple
  encoding, the planner's logistics/colonisation generators, and the extension of
  `test_tier_map_agrees_with_the_wallet_engines_allowlist` to compare mission-type sets
  — was left undone rather than built against a workaround that doesn't actually touch
  the frozen contract. See the WP report for the colonisation-entrypoint contract
  evidence gathered in the course of this phase (also recorded in
  `veydrift-wallet`'s CHANGELOG.md v0.2.0 entry), which a maintainer who can edit
  `models.py` can use directly rather than re-deriving it.

## [0.6.0] - 2026-08-16

### Added
- **A locked declared target now drives its own build-up** (Phase 4 of the
  general-strategy-engine program). Before this change, a `ship_targets`/
  `defense_targets`/`research_priority` entry the account could not build *yet* (e.g. a
  Small Cargo target on a fresh planet, which needs Shipyard 2 and Combustion Drive 2)
  was declared, legal to want, and permanently unreachable — every generator correctly
  refused to propose the locked entity itself, but nothing ever proposed the
  *prerequisite* that would unlock it.
  - `techtree.next_step_toward(family, entity_id, *, building_levels, technology_levels)
    -> UnlockStep | None` — new pure function. Walks `unmet()`'s output backwards,
    breadth-first, to find the shallowest requirement in the chain that is itself
    buildable right now (its own `unmet()` is empty *and* its own current level is
    known — an `UnmetRequirement(have=None)` never becomes a confidently-chosen step).
    Cycle-safe (a `visited` node set) and depth-bounded (`_MAX_UNLOCK_DEPTH = 32`)
    defensively, though the real requirement tables are asserted acyclic by test.
    Returns `None` when the target is already unlocked or the chain bottoms out
    unresolvable. No cost math — levels only, same discipline `unmet()` follows.
  - `candidates.generate_unlock_chain_candidates` / `select_unlock_chain_candidate` —
    new family, new ladder rung `8b` in `plan.py`. For every locked entry in
    `ship_targets`/`defense_targets`/`research_priority` (not `building_priority`, which
    already has its own reachability path), proposes the shallowest buildable
    prerequisite, `score=None` always. Gated on the matching `allow_building`/
    `allow_research` flag and the matching queue being idle. When more than one locked
    target resolves to a different step, ordered by weighted cost ascending
    (`policy.strategy.resource_weights`, live `Entity.cost` only); unknown cost sorts
    last, never guessed. Reached only when every earlier rung (deadline-driven storage
    overflow, economically-scored building/infrastructure, policy-declared
    research/ships/defense) found nothing at all — deliberately the *last* rung, not
    folded into `building_priority`'s precedence, so it can never outrank the
    storage-overflow deadline and can never displace a scored economic or
    policy-declared candidate.
  - `Action.expected_effect` carries the *remaining* chain after this step, so
    `strategy.md`/`proposals.jsonl` show the multi-tick plan implied by a declared
    target without ever committing to it — every tick re-derives from live state from
    scratch.
  - `guard.py`'s `prerequisites` gate required no change: it derives legality from
    `Action.kind`, not from which `candidates.py` generator produced the action, so it
    already independently re-verifies an unlock-chain step — confirmed by a new test,
    not merely assumed.
  - **What this is not**: not an ROI calculation (`score` is always `None` for this
    family — an unlock step's value is entirely in what it eventually enables, not
    something this codebase computes a payback number for); not a commitment to the
    rest of the chain (each tick re-derives from live state; nothing is queued in
    advance); not a change to `building_priority`'s own reachability path or
    precedence.
  - Empty `ship_targets`/`defense_targets`/`research_priority` (the default) reproduces
    Phase 3's planner output exactly — every pre-existing test passes unmodified.

## [0.5.0] - 2026-08-16

### Added
- **Every planet-local entity is now reachable, driven by declared policy targets +
  `techtree.py` + the contract's caps** (Phase 3 of the general-strategy-engine program).
  Before this change the planner could only ever propose 13 of the 51 entities in
  `ids.py`. `candidates.py` gains:
  - `generate_ship_target_candidates` / `generate_defense_target_candidates` — stock-
    keeping toward new `Policy.strategy.ship_targets` / `.defense_targets`
    (`list[EntityTarget]`, each `{name|id, count}`): the first declared target below its
    live `Entity.count`, filtered through `techtree.unmet()`. **Solar Satellite's
    separate energy-driven scored path is untouched** — `ship_targets` never merges with
    it. Declaring `defense_targets` supersedes the pre-Phase-3 hardcoded
    Rocket-Launcher-only default entirely; an empty list reproduces that default exactly.
  - `generate_crawler_candidates` — Crawler (Ship id 15, non-flyable), scored via
    `calc.crawler_boost_bps` (previously dead). The formula's own internal caps (8 per
    combined mine level, 5,000 bps) make an already-saturated crawler count score `None`
    automatically; the live `PlanetSnapshot.crawler_production.capped` flag short-
    circuits the same conclusion without recomputing, when present.
  - `generate_infrastructure_candidates` — the "infrastructure" family reserved, unused,
    in 0.4.0: Robotics Factory, Nanite Factory, Shipyard, Research Lab, Terraformer,
    Missile Silo, always `score=None`, ordered by new `Policy.strategy.building_priority`
    — the family's sole reachability switch; empty means it never fires. Fusion Reactor
    does **not** live here — it moves `production_per_hour`, so it is a scored
    `generate_energy_candidates` candidate instead, deliberately without touching the
    pinned `_cheapest_energy_choice` substitution comparison (Solar Plant vs. Solar
    Satellite only, per the hot-planet counterfactual test).
  - `generate_proactive_storage_candidates` — storage as a Band-2 candidate (always
    `score=None`), activating `calc.storage_cap` (previously dead) so headroom is visible
    before the reactive overflow trigger fires. Additive to `alternatives` only; never
    changes which candidate wins Band 2.
  - `generate_research_candidates` now orders by new `Policy.strategy.research_priority`
    (technology names) first, falling back to the pre-existing lowest-level-first order
    for everything not named — and that fallback's `score_basis` is explicitly prefixed
    `"default: ..."` so it reads as a fallback, not a derived recommendation.
- New `Policy.strategy` fields: `ship_targets`, `defense_targets`: `list[EntityTarget]`
  (new model: `{name: str | None, id: int | None, count: int}`); `research_priority`,
  `building_priority`: `list[str]`. All default to `[]`; a typo'd name raises `ValueError`
  at generation time rather than silently proposing nothing (`ids.py`'s existing
  `KeyError`-on-unknown-name convention, re-raised with the offending name).
- `PlanetSnapshot` gains `missile_silo_level` (← `/defenses`'s `missileSiloLevel`) and
  `crawler_production` (← `/infrastructure`'s `crawlerProduction` block, new
  `CrawlerProduction` model) — both sourced from routes `read.py`'s `snapshot` command
  already fetches, no new HTTP call. Both default `None`; `None` means unverifiable, never
  `0`, for every consumer.
- Two new independent shield-dome/missile-silo cap checks in `candidates.py`
  (`_defense_capacity_reason`), deliberately not shared code with `guard.py`'s existing
  `_defense_cap_violation` — the same defense-in-depth posture `_gate_energy` already
  takes toward `plan.py`'s energy invariant.
- `schemas/policy.schema.json` regenerated for `EntityTarget`/the four new `StrategyCfg`
  fields.

### This phase is explicitly NOT
- **Not a fleet doctrine.** `ship_targets`/`defense_targets` stock-keep toward a declared
  count; nothing in this change flies a ship, launches a fleet mission, or reasons about
  combat. Ships/defenses become *producible*, never *flyable* — that stays a future phase.
- **Not a threat model.** Which defenses to build, and how many, is entirely the
  operator's declared `defense_targets` — the engine still only enforces legality
  (`techtree.unmet()`), affordability (`guard.py`, unchanged) and, where a number is
  genuinely comparable, economics (`score_payback`). It never invents a doctrine.

### Verified unaffected
- **Zero behaviour change with empty `strategy` targets** — this phase's own acceptance
  criterion, pinned directly (`tests/test_candidates.py`,
  `tests/test_plan.py::test_empty_strategy_targets_reproduce_phase_2_planner_output_exactly`
  / `::test_empty_strategy_targets_reproduce_phase_2_hot_planet_output_exactly`) and by
  every pre-existing test in `test_plan.py`/`test_candidates.py`/`test_guard.py` passing
  unmodified.
- **`guard.py`'s `prerequisites` gate already generalizes to `Action.quantity > 1` and the
  missile-silo slot arithmetic** — verified with two new tests, no code change needed
  (`test_prerequisites_blocks_a_multi_unit_shield_dome_request_even_at_zero_built`,
  `test_prerequisites_blocks_a_multi_unit_missile_request_over_remaining_silo_capacity`).
- **No cost-scaling function was added anywhere** — every new candidate's cost is a live
  `Entity.cost`, never recomputed.
- **Combat stays unreachable.** No `_MIN_TIER_FOR_FUNCTION` entry, no `allowlist.ts`
  change, nothing proposing `launchFleetMission`.
- 386 tests passing, up from the pre-Phase-3 baseline of 357 (29 new: 23 in
  `test_candidates.py`, 3 in `test_guard.py`, 2 in `test_plan.py`, 1 in `test_read.py` —
  all additions, none replacing an existing assertion).

## [0.4.0] - 2026-08-16

### Changed
- **Rungs 5-9 of `plan.py`'s decision ladder are now a generate/filter/score/select
  candidate pipeline** (new module `candidates.py`), replacing the old scheme where each
  rung both decided the action *family* and hardcoded *which entity* in one function.
  Rungs 0-4 (killswitch, health, pending-tx, mission-resolving, hostile-fleet — vetoes,
  not strategy) are untouched. `candidates.py` provides one generator per family (`mine`,
  `energy`, `storage`, `research`, `ship`, `defense`), a `score_payback` scorer (weighted
  cost ÷ weighted marginal `calc.production_per_hour` delta, in payback hours — scored
  iff the level change actually moves that function's output; a storage building, a
  locked entity, and every research/ship/defense pick are `score=None`), and a `select_*`
  function per rung that replays the exact priority order the pre-Phase-2 ladder used —
  the energy-first invariant is still a **hard filter**, not a score: an energy-unsafe
  mine is never generated as a candidate at all, and the cheaper of Solar Plant / Solar
  Satellite is generated in its place, identical semantics to before. **This phase's own
  acceptance criterion is zero behaviour change**: every pre-existing `test_plan.py`,
  `test_guard.py` and `test_tick.py` test passes unmodified (342 -> 357, the 15 new ones
  all additions — see `tests/test_candidates.py` and the new alternatives/dedup cases in
  `tests/test_tick.py`).
- `Action` gains `alternatives: list[AlternativeNote]` — the runner-up candidates from
  the same pipeline pass that produced the winning action, ranked (scored ascending by
  payback hours, unscored last), capped at `policy.strategy.max_alternatives` (default
  5), each carrying a `why_not` ("payback 47.3h vs winner's 12.0h", or
  `techtree.describe()`'s "locked: needs Shipyard 2 (have 0)" for a locked one). Wired
  into the printed/`--format json` report and `proposals.jsonl`, same as `expected_effect`
  got in 0.2.0. **`alternatives` participates in `_fingerprint_proposal`'s dedup hash** —
  deliberately *not* added to `_FINGERPRINT_EXCLUDED_KEYS` — so two content-identical
  ticks (alternatives included) still dedup to one logged proposal, and a tick whose only
  real change is a different runner-up is correctly logged as new evidence, not
  suppressed. Getting this backwards would have silently defeated dedup on nearly every
  tick, the same bug class the 0.2.0 dedup fix (`fa06252`) closed.
- New `Policy.strategy: StrategyCfg` (`resource_weights: Resources`, default 1:1:1;
  `max_alternatives: int`, default 5). `resource_weights` is the exchange rate
  `score_payback` uses to collapse a metal/crystal/deuterium cost triple to a scalar —
  1:1:1 preserves the assumption `_energy_candidate` already made implicitly (it summed
  the three unweighted) before this field existed. **Additive for existing policy
  files** (absent `strategy` key -> default) but, because `Policy` is `extra="forbid"`, a
  new policy file that sets `strategy` will not load on an agent build predating this
  field.
- **Disclaimer, stated once here rather than repeated at every call site**: `alternatives`
  is informational only. It is never an ROI verdict (no "you should have built X
  instead"), it does not add a new entity family or new proposable behaviour (Phase 3's
  job), and it is never read by `guard.py` or any `Decision` logic — the winning `Action`
  is decided exactly the way it always was; `alternatives` only explains what else was
  considered and why it lost.
- `schemas/policy.schema.json` / `schemas/action.schema.json` regenerated
  (`scripts/generate_schemas.py`) for the new `StrategyCfg`/`AlternativeNote` fields.

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
