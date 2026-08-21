# Changelog

All notable changes to `veydrift-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface or the
on-disk `policy.json`/`proposals.jsonl`/`actions.jsonl` schema bump major, additive
backward-compatible changes bump minor, fixes and docs-only changes bump patch. This
package's version lives in `pyproject.toml`, independent of `veydrift-wallet`'s — the two
skills are not versioned in lockstep.

## [Unreleased]

## [1.2.1] - 2026-08-21

### Fixed

- **Storage-cap precondition on the winning building pick.** `select_building_candidate`
  (Band 2, `candidates.py`) could crown a mine/energy pick, or a declared
  `building_priority` target, whose cost exceeded the planet's *current* storage cap for
  a resource it needed — not merely "not affordable yet" (`guard.py`'s
  `_gate_affordability` already covers that and BLOCKs it at execution time) but "not
  affordable ever" until storage is raised, since production stops accumulating past cap.
  `generate_proactive_storage_candidates` already existed for exactly this situation, but
  only ever appeared as an informational `alternatives` entry — by design, per its own
  module comment, it could "never outrank a scored mine/energy pick." So the ladder kept
  re-proposing the same guard.py-doomed pick every tick, with the actual fix (raise the
  matching storage building) demoted to an alternative note instead of surfacing as the
  next step.
  - `candidates.py`: new `_exceeds_storage_cap` / `_resolve_storage_precondition` helpers,
    applied to every tentative winner `select_building_candidate` produces — the scored
    mine, the energy substitute, and a declared `building_priority` target alike.
    Mirrors the existing energy-first hard-filter pattern: a capped pick with a matching
    storage candidate available is replaced by it; a capped pick with none available
    falls through to the next candidate instead of getting stuck (next mine in priority
    order, or the next declared `building_priority` entry).
  - No `guard.py` change — `_gate_affordability`'s BLOCK/ETA behavior is unchanged; this
    fix reduces how often that BLOCK is reached by fixing the upstream proposal, not by
    touching the gate itself.
  - `tests/test_candidates.py`: three new tests — a capped mine winner replaced by its
    matching storage candidate, the same case with no storage candidate available
    (falls through to the next mine), and a capped `building_priority` winner replaced
    the same way. `tests/test_plan.py`'s
    `test_matched_building_levels_isolate_temperature_as_the_only_variable` fixture
    also had its synthetic planet's storage caps bumped to match `planet_hot.json`'s —
    its stock 10,000 caps were a level-0-ish leftover that this fix correctly started
    tripping on a 32,842-metal Solar Plant cost, for a storage reason unrelated to the
    test's actual (temperature) point.

## [1.2.0] - 2026-08-20

### Added

- **Game-pause detection**, a new safety feature following this codebase's existing
  two-layer defense-in-depth pattern for `health`/`tier`/`mission_type`: `/health`'s
  `gameMaintenance` block (`paused`, `pausedSince`, `pauseAgeSeconds`) and
  `readiness.degradationReasons` were observed live for the first time this session (a
  real chain-side maintenance pause), and neither was previously parsed anywhere in this
  codebase — nothing distinguished "the game is deliberately halted, any write would
  revert" from any other reason a tick produced nothing.
  - `models.py`: new `GameMaintenance` model, `Snapshot.game_paused` /
    `game_maintenance` / `degradation_reasons` fields, `EscalationCfg.on_game_paused`
    (default `true`).
  - `read.py`: new shared `_game_maintenance()` parser (fail-closed — `gameMaintenance`
    missing from the response means "unconfirmed," never "confirmed not paused"), wired
    into `snapshot`. `tick.py`'s killswitch-only `_fetch_health_only()` now shares this
    same parser (previously a second, independent `ok`/`readiness.ready` implementation)
    and returns a 4-tuple instead of a bare bool.
  - `plan.py`: new veto rung `1b` (right after rung 1's health check, before rung 2's
    pending-tx check) — ESCALATE by default, or NO-OP if `escalation.on_game_paused` is
    `false`; either way a confirmed pause always halts proposing.
  - `guard.py`: new 19th gate, `game_paused` — the second, independent line of defense.
    Unlike rung `1b` it BLOCKs unconditionally (not ESCALATE, and not opt-out-able): by
    the time a proposal reaches `guard.py`, a confirmed pause is a hard safety fact, and
    a stale/racy proposal built just before a pause began must still be caught. Fail-
    closed like `energy`: `Snapshot.game_maintenance is None` BLOCKs as "could not run,"
    never passes vacuously.
  - Agent-side only, by design — no `veydrift-wallet` changes. The new `guard.py` gate
    blocks the proposal before it ever reaches the wallet skill; `walletctl simulate`
    (mandatory before every send since 1.1.1) independently catches any would-revert
    transaction that somehow got built anyway.

## [1.1.1] - 2026-08-19

### Fixed

- **`tick.py` never called `walletctl simulate` before sending.** `_run_walletctl(...)`
  was only ever invoked with `build`, `status`, `receipt` and `send` — the string
  `simulate` appeared nowhere in `src/`, despite `SKILL.md`, `AGENTS.md` and
  `docs/SPEC.md` all documenting a `build -> simulate -> send` sequence. A tier>=2 send
  went straight from `build` to `send` with no free `eth_call`/`estimateGas` pre-flight,
  so a transaction that would revert burned real gas to find that out instead of costing
  nothing. Reproduced on a local Anvil fork of Base: `startResearch(664, 0)` simulated as
  `ok: false` / `InsufficientResources(6798, 1874, 4444)`, then `send` submitted it anyway
  and the receipt came back `status: "reverted"`.

  **Why the existing 473 unit tests (and two prior adversarial judge passes) didn't catch
  this:** the tests that exercise `_send_and_await` and the full `_run_tick` send path all
  monkeypatch the `walletctl` subprocess boundary and assert on the calls that *are*
  made — a *missing* call is invisible to that style of test unless something explicitly
  asserts the call sequence. Fixed alongside a regression test
  (`test_full_tick_sequence_is_build_then_simulate_then_send`) that records and asserts
  the call order itself, specifically so this class of gap surfaces again if reintroduced.

  Added `_walletctl_simulate` (`tick.py`), wired into `_send_and_await` between writing
  the tx file and calling `_walletctl_send`. `walletctl simulate --tx <file> --from
  <address>` has no `--provider` flag (confirmed against the live CLI and the fork) and
  its `--from` is mandatory — without it, simulate runs against a default address and
  fails `NotPlanetOwner()` rather than reflecting the real sender. Output is plain text
  (`ok:`/`revert reason:` lines), parsed defensively like `walletctl status` already is.
  The wallet address now comes from a new `_walletctl_status` helper that parses both the
  `balance:` and `address:` lines from the *same* `walletctl status` call `_run_tick`
  already made for the `eth_floor` gate, rather than a second subprocess call per tick.

  **Fail-closed, matching AGENTS.md §5's rule for absent guardrail data:** a simulate
  result that could not be obtained or parsed at all (`walletctl` unreachable, timed out,
  no wallet address, non-zero exit with no `ok:` line, or unparseable output) is treated
  identically to a genuine simulated revert — both block the send. Neither is logged to
  `actions.jsonl` or counted via `record_revert`/`executions_count` (nothing was
  submitted, so there is no on-chain outcome to record) — this matches how a `walletctl
  build` failure is already handled, not how a real on-chain revert is. The revert reason
  (or the unusable-result error) is threaded into `guard_report` as a new
  `walletctl_simulate` `GuardVerdict`, the same mechanism `build_error` already uses, so
  it reaches both `proposals.jsonl`'s `guard_verdicts` and the printed tick report (a new
  `!! SIMULATION FAILED` line), not just `logs/strategy.md`.

  **What this is not:** it does not change tier-1 behaviour (tier 1 never reaches
  `_send_and_await`), the allowlist, `--confirm`'s unconditional requirement, or combat
  reachability. `AGENTS.md` §10 is updated separately to record that the
  `build -> simulate -> send` sequence, including this fix, has now run end-to-end
  against a local Anvil fork of Base (`startBuildingUpgrade`, `status: "success"`) — it
  has still never run against mainnet.

## [1.1.0] - 2026-08-17

Judge review of the just-completed general-strategy-engine program (`b00d8ca..f6a7c56`). Minor
rather than patch: `StrategyCfg` gains a new additive field (`enable_crawler`), and `guard.py`
gains an independent defense-in-depth check for fleet-mission spend — both backward-compatible
(an old `policy.json` with no `strategy.enable_crawler` key still loads, defaulting to the
pre-existing behaviour), not a breaking CLI/schema/ABI change.

### Fixed

- **Finding 1 (most severe) — fleet-mission actions bypassed every resource gate.**
  `generate_transport_candidates`/`generate_harvest_candidates` (`candidates.py`) built a
  `FLEET_MISSION` `Action` without ever setting `Action.cost` — exactly what `guard.py`'s
  `affordability`/`reserve`/`value_ceiling` gates read. A planet holding 50,000 deuterium with a
  40,000 reserve floor could propose a Transport of the 10,000 surplus plus fuel with the
  `reserve` gate PASSing (final holdings 39,929 — floor breached, no gate fired). Fixed on two
  independent layers: both generators now populate `Action.cost = cargo + fuel` (fuel counted as
  deuterium, `VeydriftGameplayModule.sol:246-260`); `guard.py` gained
  `_derive_fleet_mission_spend`, which independently re-derives the true spend from
  `Action.ships`/`Action.cargo`/route rather than trusting `Action.cost` at all, the same
  defense-in-depth posture `_gate_energy` already takes toward `plan.py`'s energy invariant — a
  planner that forgets `cost` again is still caught. Unverifiable spend (missing ships/route/
  technology data) resolves to `BLOCK`, never a silent zero.
- **Finding 2 — `_encode_colony_target` silently corrupted out-of-range coordinates.**
  `tick.py:_encode_colony_target` had no bounds check; `"1:2:300"` decoded on-chain as galaxy 1,
  system 3, position 44 (position's low-byte overflow spilling into the system field) — a
  corrupted but still in-range-looking target that would launch a real Colony Ship at the wrong
  slot with no error anywhere in the pipeline. Now raises on any galaxy/system value outside
  `[0, 0xffff]` or position outside `[0, 0xff]` (verified against `_decodeColonyTarget`'s own
  masks, `VeydriftColonizationModule.sol:42-46,482-492`), or a malformed `"G:S:P"` string.
  `guard.py`'s `mission_type` gate gained an independent re-check of the same bounds for Colonize
  actions.
- **Finding 3 — Transport committed the entire fleet, not cargo ships.** `_cargo_ships` filtered
  on nonzero `SHIP_CARGO_CAPACITY`, true for all 14 flyable ships including combat ships and the
  Deathstar — a Transport could strip a planet's defence fleet for the round trip and pay
  combat-ship fuel rates. Restricted to genuine haulers only (Small Cargo, Large Cargo — see
  `_HAULER_SHIP_IDS`'s docstring for why Recycler/Pathfinder/Colony Ship and every combat ship are
  excluded), and a new `_select_haulers_for_cargo` picks the smallest hauler fleet that covers the
  proposed cargo rather than committing every owned hauler regardless of need.
- **Finding 4 — Phase 3's "reproduces Phase 2 output when nothing new is configured" claim was
  false for the Crawler.** `generate_crawler_candidates` was gated only on `allow_ships`, so an
  unlocked, scoreable Crawler could outrank Solar Satellite in `select_shipyard_candidate`'s
  ranked winner pick with an entirely empty `policy.strategy` — latent only because the Crawler
  happened to be locked on the account this codebase was tested against. Gated behind a new
  `policy.strategy.enable_crawler` field (default `false`), following `ship_targets`/
  `building_priority`'s own "empty/off == old behaviour" convention. Audited the same shape in
  `generate_proactive_storage_candidates`, `generate_infrastructure_candidates`, and the Fusion
  Reactor branch of `generate_energy_candidates`: all three are structurally confined to
  `alternatives` in their current call graphs (never reachable as a rung's winner), so none needed
  the same fix — see the WP report for the full trace.
- **`_gate_prerequisites` now checks ship availability at the origin for `FLEET_MISSION`
  actions** (previously PASSed trivially — `FLEET_MISSION` had no entry in
  `_FAMILY_FOR_ACTION_KIND`). Fails closed on a ship count the snapshot didn't report.
- **`_ship_counts_to_fleet_tuple` now rejects a negative ship count**, matching
  `veydrift-wallet`'s `fleet.ts` (the two encoders could previously disagree on the same
  malformed input).
- Dead code removed: `select_logistics_candidate`'s trailing `alternatives.extend(transports/
  harvests)` was unreachable (both generators return at most one `Candidate`, and both branches
  above already return whenever either is non-empty).
- Doc corrections (stale `settlePlanet`-is-allowlisted / mission-type-list / gate-count
  references): `docs/SPEC.md` (tier table, Phase 5 status note), `docs/COVERAGE.md`,
  `skills/veydrift-agent/SKILL.md`, `docs/PLAYER-GUIDE.md`, `docs/TECHNICAL-WALKTHROUGH.md`,
  `AGENTS.md` §8 (a dry-run tick's `strategy.md` entry is conditional, not guaranteed), and
  `tests/test_guard.py`/`tests/test_tick.py`'s own "17-gate"/"17 gates" docstrings (stale since
  the `mission_type` gate landed at 18).

### Added

- `Policy.strategy.enable_crawler: bool = False` — see Finding 4 above.

## [1.0.0] - 2026-08-17

Phase 5 of the general-strategy-engine program (docs/SPEC.md §5.4/§9), and the release
that closes it. Major rather than minor because of two breaking changes: `settlePlanet`
is removed from both enforcement layers, and `OPERATOR_ALLOWED_MISSION_TYPES` is widened
to admit Colonize (2) — the only allowlist widening in the entire program.

### Added — 5c/5b: non-combat fleet missions and colonisation (this change)

The `models.py` block below was the reason a prior pass of this phase stopped
short (see "Not done this phase" further down, kept for history): the orchestrator
has since unfrozen and extended `models.py` with `ActionKind.FLEET_MISSION` and
`Action.mission_type`/`.origin_planet_id`/`.target_coordinates`/`.ships`/`.cargo`/
`.speed_pct`/`.randomness_request_id`. This change is everything downstream of that.

- **`guard.py` gains an 18th gate, `mission_type`** (was 17) — a default-deny check on
  `launchFleetMission`'s mission-type argument, independent of and in addition to the
  `tier` gate. Fails closed (`BLOCK`) on `mission_type is None`. Mirrors
  `veydrift-wallet`'s `allowlist.ts` `OPERATOR_ALLOWED_MISSION_TYPES` exactly —
  `test_tier_map_agrees_with_the_wallet_engines_allowlist` now also compares the two
  mission-type sets, not just the function-name sets, and fails naming the diff if they
  ever drift. Allowed: Transport (0), Deploy (1), Colonize (2, new — see below),
  Harvest (4). Combat types (3, 5, 6, 7, 8, 9) are never added, by design (AGENTS.md
  §5's "combat stays unreachable by code, not by config").
- **`tick.py`'s `_action_to_walletctl_json` gains a `launchFleetMission` branch.**
  Resolves the overload by **full canonical signature**, never by name (AGENTS.md §7
  trap #2): the 7-arg form (explicit `speedPercent`) is used when `Action.speed_pct` is
  set; the 6-arg form (contract-side default 100% speed) is used when it is `None` —
  chosen *by overload*, never by fabricating a speed value at the encoder. The 14-slot
  fleet tuple is built via a new `_ship_counts_to_fleet_tuple`, mirroring `ids.
  FLEET_TUPLE_ORDER` / `ids.NON_FLYABLE_SHIPS` (AGENTS.md §7 trap #1 — Destroyer at
  tuple index 9, not 10; raises on a non-flyable ship id even at count 0). Colonize's
  `targetPlanetId` argument is a packed `(galaxy, system, position)` coordinate
  (`_encode_colony_target`, confirmed against `VeydriftColonizationModule.sol:472-479`),
  never a real planet id; every other mission type's target is resolved to a real
  on-chain planet id by matching `target_coordinates` against the wallet's own planets
  in the snapshot (the only planets this codebase's planner ever targets).
  **Correction to this phase's own docs/COVERAGE.md row**: the trailing `uint256` both
  overloads share is `randomnessRequestId` in the deployed source, not a "holding
  duration" — confirmed directly (`VeydriftGameplayModule.sol`/
  `VeydriftColonizationModule.sol`); it is meaningfully set by the contract only for
  `Attack` and the two counterplay mission types, none reachable here, and
  Colonize hard-reverts (`InvalidId`) unless it is exactly `0`. `Action.randomness_request_id`
  is encoded as-is (default `0`, never fabricated) despite its name.
- **`candidates.py` gains a logistics family**: `generate_transport_candidates` (move a
  planet's surplus above `policy.reserves` to whichever other own planet holds the
  least of it, using already-built cargo-capable ships only) and
  `generate_harvest_candidates` (local harvest of a planet's own debris field only —
  the contract's `originPlanetId == targetPlanetId` special case; the frozen `Snapshot`
  carries no debris data at all, so this generator takes an explicit
  `own_planet_debris` parameter rather than guessing the unconfirmed live shape of
  `/universe/...`'s `debrisField` field — see the generator's own docstring; no caller
  wires a live source yet). Both gated on `policy.actions.allow_fleet_noncombat`
  (**defaults `false`**), wired into `plan.py` as a new band 5 (`8c:logistics-*`),
  reached only after bands 1-4 produce nothing. `calc.py` gained the ship-movement-stats
  formula layer this needed (`SHIP_CARGO_CAPACITY`, `ship_fuel_consumption`,
  `ship_speed`, `ship_movement_stats`) — a fixed, fully-published lookup table from
  `VeydriftCatalog.sol`, not the banned "cost-scaling" category (see calc.py's own
  comment on the distinction).
- **`allowlist.ts`'s `OPERATOR_ALLOWED_MISSION_TYPES` widened to include Colonize (2)**
  — the only widening in this program, added only in the same change as `guard.py`'s
  `mission_type` gate (never before it, per this phase's own brief: widening the
  allowlist first would have reopened the single-layer-enforcement gap the new gate
  closes). See `veydrift-wallet`'s own `[Unreleased]` entry.

### Added — prior pass (kept)
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

### Changed
- **`_warn_dead_policy_keys` no longer warns on `actions.allow_fleet_noncombat=true`.**
  The key stopped being dead config in this change (see "5c/5b" above) — the "no
  effect" warning would now be false. The function is kept as a hook for a future dead
  key, per its own docstring.

### Historical note — "not done this phase" (superseded, kept for the record)
> Non-combat fleet-mission planning (5c) and real colonisation (5b) were NOT
> implemented in an earlier pass of this phase. Both required `ActionKind.FLEET_MISSION`
> and new `Action` fields (`mission_type`, `origin_planet_id`, `target_coordinates`,
> `ships`, `cargo`, `speed_pct`, `holding_seconds`) on `models.py`, which was this work
> package's frozen interface at the time (AGENTS.md §4). Everything downstream of that
> — `guard.py`'s mission-type gate, `tick.py`'s `launchFleetMission` overload
> resolution and 14-slot fleet-tuple encoding, the planner's logistics/colonisation
> generators, and the extension of `test_tier_map_agrees_with_the_wallet_engines_
> allowlist` to compare mission-type sets — was left undone rather than built against a
> workaround that doesn't actually touch the frozen contract. The orchestrator has since
> unfrozen `models.py` for exactly this purpose; the "5c/5b" entry above is the result.

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
