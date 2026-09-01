# Changelog

All notable changes to `veydrift-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/): breaking changes to the CLI surface or the
on-disk `policy.json`/`proposals.jsonl`/`actions.jsonl` schema bump major, additive
backward-compatible changes bump minor, fixes and docs-only changes bump patch. This
package's version lives in `pyproject.toml`, independent of `veydrift-wallet`'s — the two
skills are not versioned in lockstep.

## [Unreleased]

## [1.14.0] - 2026-09-01

Alliance feature, commit 3/6: `guard.py` gains a new, 23rd gate for the 15 membership
functions on `VeydriftAllianceSystem` -- a wholly separate deployed contract, never
touched by this codebase before. Manual-override-only (no `candidates.py` generator, no
`plan.py` ladder rung); gated on a new `policy.actions.allow_alliance` flag, floor
`economy` tier (not `operator` -- membership actions carry no fund/combat risk).

### Added
- New module `alliance_ids.py` -- `AllianceRole` (`None`/`Member`/`Officer`/`Owner`),
  name/id lookups, `meets_min_role()`. Deliberately a sibling to `ids.py`, not folded
  into it: `ids.py`'s own docstring scopes itself to the game contract's six enums;
  `AllianceRole` belongs to a genuinely different deployed contract with its own pinned
  ABI.
- `models.py`: `ActionKind.ALLIANCE`, `ActionsCfg.allow_alliance`, a new banner-commented
  `Action` field block (`alliance_id`, `target_player`, `target_players`, `role`,
  `alliance_tag`/`alliance_name`/`alliance_description`), and a new `AllianceState` model
  group (`AllianceMembership`, `AllianceMember`, `AlliancePendingInvite`,
  `AlliancePendingJoinRequest`, `AllianceJoinRequestForOwner`, `AllianceDirectoryEntry`)
  -- guard-time-only live data from `GET /wallet/{addr}/alliance`, deliberately never
  added to the frozen `Snapshot`.
- `guard.py`: `_MIN_TIER_FOR_FUNCTION` gains all 15 alliance functions at `Tier.ECONOMY`;
  new `_ALLIANCE_FUNCTIONS` frozenset (the cross-layer-test counterpart to
  `_COMBAT_ONLY_FUNCTIONS`); new `_gate_alliance_action` -- one precondition branch per
  function (role floors, membership/invite/request-row lookups, batch-fails-closed for
  `kickMembers`/`setMembersRole`, sole-owner check for `leaveAlliance`,
  Officer-and-not-self check for `transferAllianceOwnership`); `_gate_abi_hash` gained an
  alliance branch that PASSes with an explicit "no live-hash verification path" detail
  (there is no `allianceAbiHash`/`allianceDeploymentCommit` field anywhere in
  `/runtime-config` to compare against, ever -- a permanent limit, not a transitional
  gap). `evaluate_guardrails()` gained a new `alliance_state` keyword parameter.

### Fixed
- `test_tier_map_agrees_with_the_wallet_engines_allowlist` extended: `_ALLIANCE_FUNCTIONS`
  is now subtracted from the unconditional economy-tier diff (mirroring how
  `_COMBAT_ONLY_FUNCTIONS` is subtracted from the operator-tier diff) and compared
  directly against `allowlist.ts`'s new `ALLIANCE_SIGNATURES`.

75 new tests (`tests/test_guard.py`'s `alliance_action` block: one per precondition
branch above, the mandatory `alliance_state is None` missing-data test, and a
deliberately-paired low-/high-stakes tier test). 789 passed (714 baseline + 75 new).

Two known gaps, documented rather than papered over: a third party's own preconditions
(an invitee's home-planet/membership status, a join requester's alliance) cannot be
independently verified from a wallet-scoped `/wallet/{addr}/alliance` read --
`walletctl simulate` remains the real pre-flight backstop; and "caller has a home planet"
is approximated via `snapshot.owned_planet_count > 0`, not a direct
`game.homePlanetOf(player)` read (no existing `read.py` wrapper exposes that view).

## [1.13.2] - 2026-08-31

### Added
- `license: MIT` in `SKILL.md`'s frontmatter and `pyproject.toml` — the repo root now
  carries a `LICENSE` file.
- `AGENTS.md` §4: any new/changed field on `Policy`/`StrategyCfg`/`ActionsCfg` must land
  in `assets/policy.example.json` in the same change.

## [1.13.1] - 2026-08-31

### Fixed
- **`assets/policy.example.json` was missing `strategy.colonize` and
  `strategy.fleet_home_planet_id`** — both added by the launch-actions plan's commit 4
  (`docs/SPEC.md` correction 69) and already documented in `docs/PLAYER-GUIDE.md`'s field
  table and inline JSON example, but never added to the actual bundled example file
  `vd init` copies to `$VEYDRIFT_HOME/policy.json`. Both now present, `false`/`null`
  respectively — the same "off/unset == pre-existing behaviour" default every other
  `strategy` flag in this file already uses.

## [1.13.0] - 2026-08-28

Launch-actions plan, commit 7 (final commit of this plan): Missile becomes
planner-reachable via `launchInterplanetaryMissileAttack` — the second and last combat
action type this plan adds. `guard.py` gains a new, 22nd gate (`missile_target`), and
the shared `attack_protection` gate gains a missile-specific exemption.

### Added
- **`ActionKind.MISSILE_ATTACK`** and **`Action.primary_target: int | None`** (`models.py`)
  — `quantity`/`origin_planet_id`/`target_planet_id`/`target_coordinates` (all already
  existing fields) are reused identically for a missile's count/origin/target; only
  `primary_target` (a `Defense` id) is new.
- **`calc.missile_range(impulse_drive_level)`** and **`calc.missile_system_distance(a,
  b)`** — both read directly from `VeydriftPlanetManagementModule.sol`'s private
  `_interplanetaryMissileRange`/`_systemDistanceForMissiles`, not guessed at. Impulse
  Drive 0 means a real, narrow range of exactly 0 systems (same galaxy, same system
  only), never "no range at all." `missile_system_distance` is deliberately a separate
  function from `distance()` (a fleet-mission travel formula) — the two measure
  genuinely different things, and a caller must separately check galaxy equality.
- **`candidates.generate_missile_candidates`/`select_missile_candidate`** — uses
  `launchInterplanetaryMissileAttack` directly (its own selector; shares nothing with
  `launchFleetMission` — no fleet tuple, no mission-type argument, no fleet slot, no
  travel time, fully synchronous). Gated on `policy.actions.allow_combat` (the same flag
  Attack uses). Sends every owned Interplanetary Missile in one shot, all-or-nothing, at
  the target's single most-numerous eligible defense type among ids 0-7
  (`_choose_missile_primary_target` — ids 8/9, ABM/IPM themselves, are refused as
  targets by the contract). Ranks candidate targets by descending total eligible-defense
  count, falling back to the next-reachable target when the top pick is out of range.
- **New ladder rung `8f:missile`** (`plan.py`, band 8) — placed after Attack (`8e`), the
  most conservative placement in the entire ladder: reached only when every other band,
  Attack included, found nothing at all for any target planet.
- **`tick._missile_targets`** — reads the SAME `/highscores` (economy category) response
  `_attack_targets` already reads, extracting each row's `homePlanet.tactical.
  defenses.units[]` (defense composition) instead of `raidableResources`. Only fetched
  when `policy.actions.allow_combat` is true, the same gating `_attack_targets` has.
- **`guard._gate_missile_target`** — new, 22nd gate. Independently re-derives every
  precondition this codebase's own frozen `Snapshot` can verify: `policy.actions.
  allow_combat` is true (this function has no shared non-combat sibling the way
  `launchFleetMission`/Attack does, so this check lives here rather than in
  `_gate_mission_type`); `primary_target <= Defense.LargeShieldDome` (7); same galaxy and
  in range (`calc.missile_range`/`missile_system_distance`); the origin owns at least
  `quantity` Interplanetary Missiles. Deliberately does NOT check
  `_requireNoPendingMissionResolutionForPlanet` for either planet — not knowable from a
  wallet-scoped read for a foreign target, the documented gap this codebase already
  handles the same way everywhere else (`walletctl simulate` catches it before send).
- **`tick._action_to_walletctl_json`'s new `launchInterplanetaryMissileAttack` branch** —
  not overloaded on the deployed ABI, so resolved by bare name, unlike
  `launchFleetMission`'s two forms.

### Changed
- **`guard._gate_attack_protection`** (the gate Attack already used) gains a missile
  branch: `VeydriftPlanetManagementModule.sol`'s `launchInterplanetaryMissileAttack`
  calls `_enforceAttackProtection(..., countsBashing=false)` — read directly from
  source — so a target whose ONLY `blockedReason` is `"bashing"` is a legal missile
  target even though it would be an illegal fleet Attack. `score_protection`/
  `not_allied` still block a missile exactly like an Attack; only the bashing exemption
  is missile-specific, and only when positively confirmed (a missing `blocked_reason` on
  a `False` result still BLOCKs, even for a missile).
- **`tick._attack_protection_allowed`** now returns `(allowed, blocked_reason)` instead
  of just `allowed` — fetched for both an Attack and a Missile proposal. Every call site
  (guard.py, tick.py's own wiring, tests) updated together.
- **`guard.idempotency_key`** gains a third special case, alongside `FLEET_MISSION` and
  `RESOLVE_MISSION`: `MISSILE_ATTACK`'s `entity_id` is always `None`, so without folding
  in `target_planet_id`/`primary_target`, every missile launched from one planet would
  collapse onto one key/revert-streak counter. Fixed before this action family could
  ever actually be proposed at all — unlike `FLEET_MISSION`'s fix (commit 2), this one
  never had a live-before-fixed window.
- **`guard._gate_health`'s `is_combat_action` check is deliberately Attack-only, not
  "any combat action."** Missile never requests randomness (interception is
  deterministic arithmetic, confirmed by reading `VeydriftPlanetManagementModule.sol`
  directly — no RNG call anywhere in `launchInterplanetaryMissileAttack`), so the
  `combat_only_degradation` health-gate exception genuinely still applies to it. Documented
  explicitly in the gate's own docstring so this reads as a considered exclusion, not a
  gap this gate forgot to extend when Missile was added.

No changes to `veydrift-wallet`'s `tx.ts`/`abi.ts`/`cli.ts` were needed — `buildTx`
already resolves any function generically by name/signature via `resolveFunctionAbi`, so
a brand-new, non-overloaded selector needed no encoder-side special-casing, only a new
allowlist permission (see `veydrift-wallet`'s own `CHANGELOG.md`'s `0.7.0` entry:
`COMBAT_SIGNATURES`, deliberately never merged into `tierSelectors`'s unconditional set
— a genuine, documented shape difference from how Attack's own conditionality is
expressed on that side, since Missile has no shared function to decode a conditional
argument from the way Attack's mission type does).

New tests: `tests/test_calc.py`'s `missile_range`/`missile_system_distance` block (7
tests); `tests/test_guard.py`'s `missile_target` block (18 tests) plus 6 new
`attack_protection` missile-branch tests plus a new health-gate exclusion test;
`tests/test_candidates.py`'s 13-test `generate_missile_candidates`/
`select_missile_candidate` block; `tests/test_tick.py`'s `_missile_targets` unit tests,
`_action_to_walletctl_json`'s new missile-encoding tests, and `_run_tick` wiring tests.
Also reworked: `test_tier_map_agrees_with_the_wallet_engines_allowlist` gained a new
`guard._COMBAT_ONLY_FUNCTIONS` constant + comparison, accounting for the genuine shape
difference between how the two layers express Missile's `allow_combat` conditionality
(see that constant's own docstring). 714 passed (652 baseline + 62 new).

Docs: `docs/SPEC.md` correction 72 and its §1/§5.4/§5.5 updates; `docs/COVERAGE.md`
(moved `launchInterplanetaryMissileAttack` from §1.6's out-of-scope table to §1.1's
implemented table); `docs/RESEARCH-ADDENDUM.md`; `docs/PLAYER-GUIDE.md`/`.html`;
`docs/TECHNICAL-WALKTHROUGH.md`/`.html` (including closing a gap found during this
sweep: the `plan.py`/`candidates.py`/`tick.py` module-table rows in the `.md` file had
drifted stale since before commit 6 and were never caught until now); `README.md`;
`AGENTS.md`; `references/entity-ids.md`, `references/guardrails.md` (new gate row + its
own explanatory section, gate count 21 -> 22), `SKILL.md` (ladder section was stale
since before commit 6 — corrected to the full current eight-band pipeline). Bumped
1.12.0 -> 1.13.0 (additive, minor) per this skill's own CHANGELOG.md convention.

## [1.12.0] - 2026-08-28

Launch-actions plan, commit 6: Attack becomes planner-reachable — the first combat
mission type this codebase has ever proposed on its own. `guard.py` gains a new, 21st
gate (`attack_protection`), and the `health` gate's commit-5-deferred correction is
applied.

### Added
- **`candidates.generate_attack_candidates`/`select_attack_candidate`** — `FleetMissionType.
  Attack` (3) via `launchFleetMission(..., mission_type=Attack, ...)` directly (not the
  `launchAttackMission` wrapper — both dispatch through the identical
  `_launchFleetMission` path, so this reuses every existing encoder/fleet-tuple/
  mission-type-gate untouched, at the cost of only ever using the contract's default
  greedy metal->crystal->deuterium loot order). Gated on `policy.actions.allow_combat`
  and, inside the generator, `snapshot.randomness_readiness.ready` (combat missions
  request VRF at launch and cannot resolve while randomness is degraded). Sends every
  combat-capable ship built on the origin planet (`_ATTACK_SHIP_IDS` — the nine combat
  ships; deliberately excludes haulers, Recycler, Colony Ship, Pathfinder, the same
  "one ship type, one role" discipline `_HAULER_SHIP_IDS` already established for
  Transport). Ranks candidate targets by descending raidable-resource total, picking the
  highest-ranked one the fleet can reach with its own fuel.
- **New ladder rung `8e:attack`** (`plan.py`, band 7) — the most conservative placement
  in the entire ladder, more conservative even than Colonize's `8d`: reached only when
  every other band, Colonize included, found nothing at all for any target planet, since
  committing a fleet to combat risks losing it.
- **`tick._attack_targets`** — reads `/highscores?category=economy&includeAttackProtection=
  true&currentWallet=<own wallet>` (`read.fetch_highscores`, new). Each row's embedded
  `attackProtection.allowed` (an ACCOUNT-level pre-check, no `targetPlanetId`) is used as
  a generation-time courtesy filter only — `None`/unknown or `false` excludes the target
  entirely, fail-closed from the fetch through the generator. Only fetched at all when
  `policy.actions.allow_combat` is true.
- **`tick._attack_protection_allowed`** — the real enforcement: re-fetches
  `/wallet/{addr}/attack-protection?targetPlanetId=N` (`read.fetch_attack_protection`,
  new) fresh, for the SPECIFIC resolved target, at guard-evaluation time — never trusting
  the earlier, coarser `_attack_targets` read. Feeds the new guard gate below. Only
  fetched for an actual Attack proposal.
- **`guard._gate_attack_protection`** — new, 21st gate. `None` (fetch failure,
  unresolvable target, non-boolean response) BLOCKs; `False` BLOCKs; only `True` PASSes.
  PASSes trivially for every non-Attack action.

### Changed
- **`guard._gate_health`** now takes `action` as a parameter and withdraws the
  `combat_only_degradation()` exception specifically for a combat (Attack) action —
  applying the correction commit 5 deferred (`RandomnessReadiness`'s docstring said so
  explicitly). Every non-combat action still gets the exception, unchanged. Belt and
  suspenders with `generate_attack_candidates`'s own generator-level precondition: the
  generator check keeps a degraded-randomness tick from proposing Attack at all, this
  gate is what actually enforces it should that generator check ever be bypassed (e.g. a
  manual `vd tick --action` override).
- `ActionsCfg.allow_combat`, `RandomnessReadiness`, `Action.mission_type`/
  `.target_planet_id`/`.randomness_request_id` docstrings corrected (`models.py`) — no
  longer describe Attack as un-planner-reachable.

No changes to `veydrift-wallet` were needed for this commit — Attack was already
allowlist-permitted and mission-type-gated as of `veydrift-wallet` 0.6.0 (this package's
1.11.0); attack-protection is a game-rule legality concern this package owns end to end,
not a contract-selector/tier/mission-type concern the wallet engine's independent
re-check exists for.

New tests: `tests/test_guard.py`'s `attack_protection` block (4 tests) plus 2 new
`health` tests (withdraws the exception for Attack, still applies it to every non-combat
fleet mission type); `tests/test_candidates.py`'s 14-test `generate_attack_candidates`/
`select_attack_candidate` block; `tests/test_tick.py`'s `_attack_targets`/
`_attack_protection_allowed` unit tests (14) plus 4 `_run_tick` wiring tests. 652 passed
(618 baseline + 34 new).

Docs: `docs/SPEC.md` correction 71 and its §5.4/§5.5/§1 updates, `docs/COVERAGE.md`,
`docs/RESEARCH-ADDENDUM.md`, `docs/PLAYER-GUIDE.md`/`.html`,
`docs/TECHNICAL-WALKTHROUGH.md`/`.html`, `README.md`, `AGENTS.md`, `references/
entity-ids.md`, `references/guardrails.md` (new gate row + explanatory section, gate
count 20 -> 21), `references/api-routes.md` (new §3.21 for `/wallet/{addr}/
attack-protection`, `/highscores`'s `category` non-filtering behavior documented),
`references/manual-action-override.md`, `SKILL.md` (ladder section was stale since
before this commit — corrected to the full current seven-band pipeline), plus every
other narrative doc's stale "no generator proposes Attack" / "not planner-reachable"
claim. Bumped 1.11.0 -> 1.12.0 (additive, minor) per this skill's own CHANGELOG.md
convention.

## [1.11.0] - 2026-08-28

Launch-actions plan, commit 5: `policy.actions.allow_combat` becomes a real,
independently-checked gate for the Attack mission type, at both enforcement layers. The
first change to widen `docs/SPEC.md` §1's combat non-goal since this project's spec was
written — every other combat mission type (`AcsDefend`/`Intercept`/`MissileAttack`/
`AcsAttack`/`DefenseHold`) stays unreachable in code at every tier, regardless of policy,
unchanged.

### Added
- **`guard._COMBAT_MISSION_TYPES = frozenset({ids.FleetMissionType.ATTACK})`** — a
  second, separate set from `_ALLOWED_MISSION_TYPES`, deliberately not merged into it.
  `_gate_mission_type` gains a required `policy` parameter to compute the effective
  allowed set (`_ALLOWED_MISSION_TYPES`, plus `_COMBAT_MISSION_TYPES` when
  `policy.actions.allow_combat` is `true`). Mirrors `veydrift-wallet`'s own
  `allowlist.ts` two-set split (`OPERATOR_ALLOWED_MISSION_TYPES` /
  `COMBAT_ALLOWED_MISSION_TYPES`), added in the same change, never before it — the same
  "both layers together, never one first" sequencing discipline the Colonize widening
  already established (2026-08-17), for the same reason: widening one layer alone would
  reopen the single-layer-enforcement gap the other layer exists to close.
- `Attack` still requires tier `operator` on top of `allow_combat` — the flag widens
  *which* mission type is permitted, never the separate `tier` gate's requirement. No
  `candidates.py` generator produces an Attack `Action` as of this commit — this makes
  Attack launch-encodable and allowlist-permitted, not planner-reachable; that is later
  work.

### Changed
- **`ActionsCfg.allow_combat`'s docstring** — no longer "deliberately ignored by every
  code path." `RandomnessReadiness`'s docstring (`models.py`) similarly corrected:
  `Snapshot.combat_only_degradation()`'s health-gate exception was reasoned about
  assuming combat was unconditionally unreachable; that premise is now narrower, though
  the exception's own behavior is unchanged until a later commit adds an Attack
  generator (its practical effect stays the same in the meantime).

7 new tests (3 in `test_guard.py`'s new "Attack conditionally allowed" block: allows at
operator with `allow_combat=true`, still blocks below operator tier even with the flag
set (confirming `mission_type` and `tier` remain independent gates), still blocks the
other five combat types even with `allow_combat=true`), plus the two pre-existing
combat-blocking tests' docstrings clarified as testing the default-off case, plus
`test_tier_map_agrees_with_the_wallet_engines_allowlist` reworked to diff both
mission-type set *pairs* independently rather than one set each. 618 passed (615
baseline from the docs-sync commit + 3 new — most of this commit's 7 new
assertions land inside the reworked cross-layer test and the three new functions
above, not as separately-counted new test functions).

Docs: `docs/SPEC.md` correction 70 and its tier-table/§1 updates,
`skills/veydrift-wallet/references/tx-safety.md`'s new residual-limit subsection (the
wallet skill's own security-relevant threat-model claims, updated in the same commit as
the code change per this project's own discipline — see this skill's `CHANGELOG.md`),
`references/entity-ids.md`, `references/guardrails.md`, `SKILL.md`, plus every other
narrative doc's stale "allow_combat is ignored everywhere" / "combat unreachable at
every tier" claim across `docs/PLAYER-GUIDE.md`/`.html`, `docs/RESEARCH-ADDENDUM.md`,
`docs/TECHNICAL-WALKTHROUGH.md`/`.html`, `docs/COVERAGE.md`, `README.md`, and
`AGENTS.md` §5's own invariant. Bumped 1.10.0 -> 1.11.0 (additive, minor) per this
skill's own CHANGELOG.md convention.

## [1.10.0] - 2026-08-28

### Added

- **Colonize and Deploy — commit 4 of the launch-actions plan.** Both were allowlisted
  at both enforcement layers and fully encodable since Phase 5b (2026-08-17); nothing
  proposed either until now.
  - `candidates.generate_colonize_candidates`/`select_colonize_candidate`, gated on new
    `policy.strategy.colonize` (default `false`). Every precondition mirrors a real
    contract check in `VeydriftColonizationModule.sol` — exactly one Colony Ship and
    nothing else in the mission tuple, empty cargo (`CargoNotAllowed()` otherwise),
    `randomness_request_id` left `None` (coerced to `0`; anything else reverts
    `InvalidId`), and the colony cap. Target selection reads
    `/universe/galaxies/{g}/systems/{s}` (`tick._colonize_targets`), scoped to the same
    systems the wallet's own planets are in, requiring both `occupiedBy` and
    `migrationReservation` to be `null`; ranked by descending live
    `deuteriumMultiplierBps`, falling back to the next-reachable target when the
    top-ranked one exceeds the Colony Ship's own fuel range. New ladder rung `8d`, the
    most conservative placement in the ladder — reached only when every other band,
    including logistics, found nothing at all.
  - `candidates.generate_deploy_candidates`, gated on `policy.actions.
    allow_fleet_noncombat` **and** new `policy.strategy.fleet_home_planet_id`. Moves an
    entire flyable fleet (not just cargo ships) to a declared home planet — contract-
    identical to Transport at launch, but ships are credited to the target and the fleet
    slot releases at arrival instead of at return. Folded into the existing logistics
    rung (`8c:logistics-deploy`), ranked second among its four families (after Transport,
    ahead of both Harvest generators) — a declared destination is an explicit intent
    signal, the same precedence `building_priority` already uses elsewhere.
  - Both new `StrategyCfg` fields are additive; `schemas/policy.schema.json`
    regenerated. No `Action` schema change this commit.

### Fixed

- **`guard._colony_cap_violation` closes an in-flight-Colonize blind spot.** The cap
  check keyed off `Snapshot.owned_planet_count` alone, which only reflects planets that
  have already resolved — and `VeydriftColonizationModule.sol:255-260` does not revert
  when a Colonize mission's re-checked preconditions fail at resolution, it silently
  flips the mission to `Returning` instead. Two Colonize proposals on consecutive ticks
  could therefore both pass the pre-flight cap check. New `outgoing_colonize_count`
  parameter (`tick._outgoing_colonize_count`, reading `/wallet/{addr}/fleet-visibility`
  for still-`Outbound` Colonize missions) folds in-flight missions into the projected
  count before launch; `None` fails closed exactly like `owned_planet_count is None`
  already does. **Not closed**: post-resolve verification that a `resolveFleetMission`
  receipt for a Colonize mission actually produced a new planet — a real, documented gap,
  not attempted this commit; see `references/strategy-playbook.md` §10.

35 new tests (17 in `test_candidates.py` for the two new generators and their ranking in
`select_logistics_candidate`, 15 in `test_tick.py` for `_colonize_targets`/
`_outgoing_colonize_count` and their wiring, 3 in `test_guard.py` for `outgoing_
colonize_count`'s dimension of `_colony_cap_violation` — plus 4 pre-existing Colonize-cap
tests updated to supply it explicitly, not counted as new). 615 passed (580 baseline from
commit 3 + 35 new). Verified live against a scratch-home dry-run tick, both with the
default policy and with `strategy.colonize: true`, and `vd doctor`.

Docs: `docs/SPEC.md` correction 69, `docs/COVERAGE.md`'s `max_planets` row and the
`launchFleetMission` overload rows, `references/strategy-playbook.md`'s ladder
description (now six bands / ten rungs) and §10's verification-status table (also
corrected a genuinely stale, pre-existing claim found in passing: rung 3
(`resolveFleetMission`) was described as "will never fire from a real tick", which
predates this session — it was actually wired live back in Phase 5, 2026-08-17),
`references/guardrails.md`'s parameter table, `references/entity-ids.md`,
`references/contract-writes.md`'s colonisation note (previously said "not yet
planner-proposed"), and `docs/PLAYER-GUIDE.md`/`.html`'s operator-tier section and field
reference table. Bumped 1.9.0 -> 1.10.0 (additive, minor) per this skill's own
CHANGELOG.md convention.

## [1.9.0] - 2026-08-28

### Added

- **Foreign Harvest — commit 3 of the launch-actions plan.** New
  `candidates.generate_foreign_harvest_candidates`, the foreign-target sibling of
  `generate_harvest_candidates`: the contract does not restrict Harvest to
  `origin == target` — that was this codebase's own prior scope, not a contract rule.
  `_launchFleetMission` only special-cases the *distance* for a local harvest; a foreign
  target gets the real `calc.distance` formula, same as every other mission type.
  Sourced from a new `tick._foreign_debris_targets`, reading `/raid-finder/debris`
  (`read.fetch_raid_finder_debris`, new) — a discovery index, confirmed incomplete (its
  own `indexer.indexedDebrisFields` outnumbers its `targets` array), acceptable here
  because a missed candidate is a missed opportunity, never a wrong answer (unlike
  `_own_planet_debris`, which deliberately avoids this same route for the wallet's own
  planets — see commit 1's entry). Filters out any entry whose `owner` matches the
  wallet, case-insensitively, as an extra defense-in-depth check. `select_logistics_
  candidate` ranks foreign Harvest last among the three logistics families (Transport,
  local Harvest, foreign Harvest) — a closer/simpler own-planet opportunity always wins
  first.
- **`Action.target_planet_id`** (new, optional, `None` default — schema change,
  `schemas/action.schema.json` regenerated). Carries the real on-chain planet id for a
  foreign target, since a foreign planet is never in `Snapshot.planets` for
  `tick._resolve_target_planet_id`'s existing coordinate lookup to find. Set alongside
  the existing `target_coordinates` (still needed for `guard._derive_fleet_mission_spend`'s
  distance re-derivation and for display) — using the local-harvest fixed distance for a
  foreign target would have understated its real fuel cost, the "silent wrong outcome"
  class this codebase specifically guards against.
- New API route reference: `references/api-routes.md` §3.20 `/raid-finder/debris`,
  moved out of the "undocumented-but-live, not wired" table now that it has a live
  caller.

29 new tests (11 in `test_candidates.py` for the new generator and its ranking, 7 in
`test_tick.py` for `_foreign_debris_targets` and its wiring — plus 11 already landed with
commit 1's own Harvest tests, unaffected here). 580 passed (562 baseline from commit 2 +
18 new this commit). Verified live against a scratch-home dry-run tick and `vd doctor`.
Docs: `docs/SPEC.md` correction 68 and its §1/§5.4 updates, `docs/COVERAGE.md`'s
"Debris fields & recycling" row, `references/strategy-playbook.md` §8c,
`references/entity-ids.md`, `docs/PLAYER-GUIDE.md`/`.html`'s operator-tier section.
Bumped 1.8.0 -> 1.9.0 (additive, minor) per this skill's own CHANGELOG.md convention.

## [1.8.0] - 2026-08-28

### Added

- **`fleet_slots` guard gate — commit 2 of the launch-actions plan.** Every
  `launchFleetMission` path on the deployed contract reverts `FleetSlotLimitReached(1 +
  ComputerTechnology)` when no fleet slot is free; `guard.py` gains an independent
  re-derivation of that check (`Snapshot.fleet_slots_active`/`.fleet_slots_limit`, already
  sourced from `/wallet/{addr}/shipyard` — no new fetch), scoped to `FLEET_MISSION`
  actions only and PASSing trivially for everything else. Fails closed on missing data.
  Now 20 gates total (was 19).

### Fixed

- **`guard.idempotency_key` no longer collapses distinct fleet/resolve actions onto one
  key.** `entity_id` is always `None` for both `FLEET_MISSION` and `RESOLVE_MISSION`
  actions, so the base `(planet, function, entity)` triple alone collapsed every fleet
  mission launched from one planet — Transport, Deploy, Colonize, Harvest, and (once later
  commits add them) Attack and Missile — onto a single key and a single
  `AgentState.revert_counts` streak; separately, *every* `resolveFleetMission` action
  collapsed onto one global key regardless of which mission was being resolved, since
  `plan.py`'s rung 3 never sets `planet_id`. Both were live before any mission type beyond
  Transport/Harvest could actually be proposed. Fixed by folding `mission_type` + target
  into the key for fleet missions, and `mission_id` for resolve actions. No migration
  needed for the format change: confirmed directly against this project's own
  `agent-state.json` that no account has ever accumulated fleet-mission or resolve-mission
  state under the old key (`revert_counts: {}`, `executions_count: 0` at the time of this
  change).

7 new tests pin the gate's boundary and missing-data cases (mirroring every other gate's
own convention — happy path, at-the-limit, past-the-limit, and three separate missing-data
parametrizations); 4 more pin the idempotency-key fix directly. 562 passed (551 baseline
from commit 1 + 11 new). Docs:
`references/guardrails.md`'s gate table/count, `references/manual-action-override.md`,
`README.md`/`docs/PLAYER-GUIDE.md`/`docs/SPEC.md`/`docs/TECHNICAL-WALKTHROUGH.md`'s
worked examples (`16/19` → `17/20`), and their `.html` mirrors. Bumped 1.7.0 -> 1.8.0
(additive, minor) per this skill's own CHANGELOG.md convention.

## [1.7.0] - 2026-08-28

### Added

- **Harvest goes live — commit 1 of the launch-actions plan.**
  `candidates.generate_harvest_candidates`'s `own_planet_debris` parameter has been
  logic-complete and unit-tested since Phase 5c but had no live caller, so band 8c's
  Harvest half never fired. `tick.py` gains `_own_planet_debris()`, sourcing a planet's
  own `debrisField` from `/universe/galaxies/{g}/systems/{s}` (`read.fetch_universe_system`,
  new this change — the same route already fetched for `PlanetSnapshot.archetype`, now
  factored out of `_universe_archetype_for_planet` into a general-purpose fetcher).
  Confirmed live-populated (`{"metal": "2400", "crystal": "2400"}` at a real occupied
  slot, probed 2026-08-27) — closing the "populated shape has never actually been seen"
  gap the generator's own docstring previously flagged. Deliberately **not** sourced from
  `/raid-finder/debris`: that route takes no wallet parameter, is independently confirmed
  to omit at least one indexed debris field, and its filtering criteria are undocumented —
  using it risked the exact vacuous-pass-on-absent-data failure mode this skill's own
  guardrails are built to avoid, if it turns out to exclude the caller's own planets.
  `_own_planet_debris` groups owned planets by `(galaxy, system)` so a multi-planet wallet
  sharing a system fetches it once, and degrades to `{}` best-effort on any fetch failure
  — matching `_resolvable_mission_ids`'s existing contract exactly, never aborting the
  tick. `plan_next_action` gains a new `own_planet_debris` keyword parameter (default
  `None`, backward compatible) threaded through to `select_logistics_candidate`. Verified
  against the live API end-to-end via a scratch-home dry-run tick.
  Only local Harvest (a planet's own debris field) is covered by this change — foreign
  Harvest (a third party's field) remains a separate, not-yet-built capability.

## [1.6.5] - 2026-08-27

### Fixed

- **`references/strategy-playbook.md` §10, further generalized per feedback on `1.6.4`'s
  own fix.** Dropped the "Correction — this section originally described..." narrative
  paragraph entirely rather than explaining what used to be wrong; and stripped every
  reference to one specific account's on-chain history (exact building/technology
  levels, "Metal Mine 10 → 11", the specific settlement duration) from both the prose
  and the "what's tested against live state" table. This playbook is meant to
  generalize to any planet — anchoring its own verification-status section to one
  account's particular play history worked against that, even where the underlying
  facts were accurate. The table's cells now describe untested categories ("a live
  account at genuinely progressed levels," "a live account genuinely approaching a
  storage cap") instead of one account's specific circumstances. `§6`/`§7`'s own worked
  examples are unaffected — those are explicitly framed as worked examples using a real
  fixture, not claims about this document's own verification scope.

## [1.6.4] - 2026-08-27

### Fixed

- **`references/strategy-playbook.md` brought up to date with `PLAYER-GUIDE.md` and the
  current codebase.** Six real gameplay-decision behaviors documented for human players
  but absent from the agent-facing playbook, each verified directly against
  `candidates.py`/`techtree.py` before being added: (1) `resource_weights` was never
  named anywhere in the file — added a consolidated note listing the exact three places
  it picks a winner (the existing mine tie-break, a new explanation of the
  multiple-simultaneously-locked-targets tie-break in the unlock-chain rung, and
  Crawler-vs-Satellite once `enable_crawler` is on); (2) `enable_crawler`'s gating was
  undocumented — the Crawler bullet read as though that family were always live; (3) the
  "does not round-robin" behavior of `research_priority`/`building_priority` (first
  reachable declared name wins forever, never advances) wasn't stated anywhere; (4)
  `building_priority`'s asymmetric footgun — a correctly-spelled non-infrastructure name
  silently produces nothing, unlike the other three declared-name fields, which hard-error
  on any unrecognized name; (5) the negative-`count` silent-no-op footgun on
  `ship_targets`/`defense_targets`; (6) a minor `max_alternatives` mention in the
  sanity-check checklist.
- **§10 ("What is unobserved") corrected — it was describing a zero-state account that
  hasn't been zero-state for a while.** The account has since been played by hand through
  the game UI (non-zero on-chain levels), and this codebase has since submitted real
  transactions to it via its own tier 2/3 send path — both already established elsewhere
  in this project's own records, just never propagated into this file. Rewrote the
  opening premise and the three "never observed" items to reflect what's actually still
  unverified (cost-scaling factor, still genuinely unverified) versus what's now been
  observed at least once but not generally (queue behavior under load, lazy settlement —
  both confirmed for one selector via a fork run, not a blanket claim). Reframed the
  per-rung table's "untested against" column as "not independently reconfirmed by this
  document" rather than implying nothing has happened since, and corrected one now-false
  specific claim within it (this account's production is no longer necessarily 0/hr).

## [1.6.3] - 2026-08-27

### Fixed

- **Skill self-containment.** `references/contract-writes.md`, `references/api-routes.md`,
  `references/guardrails.md`, and `assets/com.veydrift.agent.plist.template` no longer
  cite `docs/SPEC.md`/`docs/COVERAGE.md`/`AGENTS.md` — repo-root paths outside this skill's
  own directory, which aren't guaranteed to travel with an installed skill (`npx skills add
  .`). Two spots in `api-routes.md` were structurally built around comparing this skill's
  own probe findings to an external doc's specific claims (a whole section titled "where
  this probe contradicts `RESEARCH-ADDENDUM.md` §2") and needed rewriting to state the same
  facts standalone, not just a citation swap. Where a citation pointed at a dated fix also
  recorded in this file, replaced it with a `CHANGELOG.md` version citation instead.
  `scripts/generate_schemas.py`'s docstring got the same treatment, extending the rule to
  `scripts/` (grouped with `references/`/`assets/` as bundled resources, not with `src/`).
  Caught one incidental stale-doc bug along the way: `api-routes.md` was still describing an
  `except ImportError` fallback in `read.py` that an earlier cleanup had already removed.

## [1.6.2] - 2026-08-27

### Fixed

- `candidates._cheapest_energy_choice` — the comparison `select_building_candidate` uses
  to pick the energy-first *substitute* when a mine upgrade is energy-unsafe — is now a
  three-way comparison (Solar Plant / Solar Satellite / Fusion Reactor) instead of
  two-way. Fusion Reactor was previously excluded from this specific comparison even
  though it was already an ordinary scored `energy`-family candidate elsewhere, and that
  other path alone consistently undersold it (raising future energy capacity doesn't move
  current `production_per_hour` unless the planet is already throttled, and Fusion
  Reactor's own deuterium upkeep makes the delta strictly negative otherwise). Reproduced
  live against `tests/fixtures/planet_hot.json`: the pre-fix code already mis-picked
  Solar Satellite over a ~2x-cheaper Fusion Reactor on its own canonical fixture.
  Fusion Reactor's cost is amortized over a new, documented constant,
  `_ENERGY_UPKEEP_AMORTIZATION_HOURS = 24`, before comparison, since it's the only one of
  the three with an ongoing operating cost. `docs/SPEC.md`'s correction 66 has the full
  before/after numbers and the deliberate test-fixture consequence this uncovered
  (`allow_ships` no longer isolates the Satellite-vs-Solar-Plant path on the unmodified
  hot-planet fixture, since Fusion Reactor is a building and wins there regardless).

## [1.6.1] - 2026-08-27

### Fixed

- `select_building_candidate` (`candidates.py`) no longer gets stuck re-proposing a
  top-ranked mine/energy/declared-`building_priority` pick that the planet simply can't
  afford *yet* (as opposed to `_resolve_storage_precondition`'s existing "can never
  afford, ever" storage-cap check). Before this fix, current holdings were never checked
  at this layer at all -- only `guard.py`'s `_gate_affordability` checked them, and by
  then the ladder had already committed to the pick for the tick, with no path back to
  try the next-ranked candidate. A planet with a real resource shortfall (e.g. crystal
  needed downstream for research/infra, but not yet accumulated) would have its
  highest-density mine BLOCKed by guard.py every single tick, forever, even when a
  cheaper, fully affordable mine sat right below it in priority order. New
  `_resolve_affordability_precondition`, composed with the existing storage-cap check by
  `_resolve_building_preconditions`, makes falling through to the next candidate the
  default -- for a mine walk ordered by value density this naturally tends to land on
  whichever mine produces the resource actually in short supply, without inventing a
  "bottleneck resource" concept. No schema change; `guard.py`'s `_gate_affordability`
  remains the unchanged, authoritative final check. `policy.strategy.resource_weights`
  is unaffected -- it still only ever tie-breaks (docs/PLAYER-GUIDE.md, dated
  2026-08-26), never picks a family; this fix does not widen its scope.

## [1.6.0] - 2026-08-26

### Added

- `vd tick --action <file>`: lets an operator/agent supply their own `Action` instead of
  `plan_next_action`'s own choice, gated behind a new policy field
  `strategy.allow_agent_action_override` (default `false`, refused outright without it) --
  alongside `ship_targets`/`research_priority`/etc, since this is a strategic-override
  lever, not a wallet-engine or top-level account setting. Only the
  planner's choice is substituted -- every other rung of `_run_tick` (all ~19 `guard.py`
  gates, `wallet_engine.require_confirmation`, the tier ceiling, `tick_lock()`, and full
  audit logging) runs exactly as it does for a planner-chosen action. `Action` gained a
  `source: "planner" | "manual_override"` field (default `"planner"`), forcibly set by
  the CLI path so a hand-written file can't spoof it. Whenever the override fires,
  `plan_next_action` is also called for comparison (never executed) and both choices are
  reported together in `logs/strategy.md`, the tick's own printed report, and
  `proposals.jsonl`'s new `"override"` key -- the disagreement with the planner is
  captured automatically, never left to the operator's own rationale text. See
  `references/manual-action-override.md`.
  This closes the gap an ad hoc "override pattern" (an agent calling `walletctl` directly,
  entirely bypassing `vd tick`) exposed in an earlier session: that pattern preserved only
  `walletctl`'s own signing-layer allowlist re-check, not any of `guard.py`'s game-state
  gates, the lockfile, or the audit trail.

## [1.5.0] - 2026-08-26

### Added

- `guard.py`'s `mission_type` gate now BLOCKs a Colonize `launchFleetMission` before send
  when the account is already at (or above) `calc.max_planets`'s colony cap (`1 +
  astrophysicsLevel`) — previously `calc.max_planets` was computed nowhere in this
  codebase, so an already-at-cap Colonize would only be discovered by a real, gas-spending
  on-chain `PlanetLimitReached` revert. Fails closed (`BLOCK`) when the new
  `Snapshot.owned_planet_count` field is unconfirmed, never assumes "not yet at the cap."
- `models.Snapshot` gained `owned_planet_count: int | None`, sourced from the *full*
  `/wallet/{addr}/planets` response — deliberately not `len(Snapshot.planets)`, which can
  be a single-planet subset when `tick.py`'s `_fetch_snapshot` takes its single-planet
  fast path. `read.py`'s `snapshot` command now always fetches `/wallet/{addr}/planets`,
  even with `--planet-id` set, to populate this field correctly.

## [1.4.1] - 2026-08-26

### Fixed

- `read.py` dropped the dead `except ImportError` fallback for the entity ID -> name
  tables (`BUILDING_NAMES`/`TECHNOLOGY_NAMES`/`SHIP_NAMES`/`DEFENSE_NAMES`), a leftover
  from the original multi-agent build where `ids.py` might not have landed yet from a
  concurrent work package. That build finished long ago and `ids.py` is unconditionally
  present now — the fallback was `pragma: no cover` (never exercised, never tested) and
  had already silently drifted from `ids.py` itself (`"Dreadstar"` where the real enum is
  `DEATHSTAR`). Replaced with a plain top-level import; no behavior change.
- `http.py` dropped the same kind of dead `except ImportError` fallback for
  `veydrift_home()`, a leftover from before `state.py` had landed from its own concurrent
  work package. `state.py` is unconditionally present now, the fallback was `pragma: no
  cover`, and no test depended on it. Replaced with a plain top-level import from
  `state.py`; the now-unused `os` import was dropped too. No behavior change.
- **`_mine_priority_order`'s exact-tie handling now breaks by ascending payback hours,
  not dict-declaration order.** An exact density tie between two mines (real, recurring
  case — not hypothetical) previously always favored Metal Mine by accident of Python
  dict ordering, and could force an energy-substitute proposal over a tied-but-safe mine
  when the dict-order favorite was itself energy-blocked. New optional keyword-only
  `_mine_priority_order(planet, *, tie_break=None)`; every call site except
  `select_building_candidate`'s leaves it `None`, reproducing today's exact output
  byte-for-byte (checked directly against every existing fixture — none reaches an exact
  tie). See `docs/SPEC.md`'s dated correction (criterion 65) for the full writeup,
  including the two consequences accepted deliberately rather than engineered around.

## [1.4.0] - 2026-08-22

### Added

- **`research_priority`/`building_priority`'s undeclared fallback tail is now ranked by
  what it unlocks, not by level.** Following up on this session's round-robin finding
  (both fields stick on their first declared entry forever, no completion criterion):
  the question of whether the planner could consider *all* research/infrastructure
  options instead of only the defaults or a hand-maintained list turned out to have a
  cheap, low-risk answer that needed no new formulas and no economic exchange rate --
  `techtree.py`'s already-transcribed, already-tested requirement tables already carry
  everything needed to answer "how many other things does leveling this one directly
  unlock," a structural fact rather than a value judgement.
  - `techtree.py`: new `unlock_breadth(family, entity_id, *, building_levels,
    technology_levels)` — `(fully_unlocked_count, partially_advanced_count)` if the
    entity's level were +1, computed by re-calling the existing `unmet()` against every
    known building/ship/defense/research id (never a hand-built reverse index that could
    drift from the forward tables). Direct unlocks only, one hop, mirroring
    `next_step_toward`'s own one-hop-at-a-time backward walk in the opposite direction.
  - `candidates.py`: `_infrastructure_priority_order`/`_research_priority_order`'s
    fallback-tail sort now uses `unlock_breadth` descending (fully-unlocked count first,
    partially-advanced count as tiebreak, current level ascending, id ascending as the
    final tiebreak) instead of a flat default order / pure lowest-level-first. Nothing
    else moves: `select_building_candidate`/`select_research_candidate`'s
    first-unlocked-wins selection, `Candidate.score`, and `rank_candidates` are all
    untouched, and a declared priority list's own entries still take precedence exactly
    as before. This deliberately stops short of scoring research/infrastructure against
    mines on one axis (`calc.production_per_hour` doesn't model most of them, and
    inventing a resources/hour-vs-unlock-count exchange rate would be exactly the kind
    of invented doctrine this codebase's own docstrings have refused three times already
    — see `candidates.py`'s `generate_unlock_chain_candidates`) — this only reorders
    candidates *within* the already-existing research/infrastructure families.
  - **Dated correction**: this changes the empty-`research_priority` default's exact
    output, previously pinned as reproducing Phase 2 byte-for-byte (`docs/SPEC.md`
    AC25-31) — a deliberate, scoped break of that one guarantee, documented at AC64.
  - `tests/test_techtree.py`: five new tests for `unlock_breadth` itself, including a
    full-real-graph smoke test. `tests/test_candidates.py`: fallback-ordering tests for
    both families (the infrastructure one via a controlled monkeypatch, isolating the
    sort key from the real graph's specific content — hand-picking a clean two-candidate
    example in the real, fully-interconnected graph turned out to be unreliable, since
    Shipyard's implicit `shipyardLevel>=1` floor on every ship/defense dominates almost
    any other comparison from a bare account).

## [1.3.0] - 2026-08-22

### Added

- **A combat-only `/health` degradation no longer blocks the peaceful ladder.** Live,
  during this session's planning: `/health` returned HTTP 503, persistently, with a
  well-formed JSON body reporting `ok: false` while `readiness.ready: true`,
  `readiness.degradationReasons: []`, `gameMaintenance.paused: false`, and
  `randomnessReadiness.ready: false` (a combat-only subsystem — "New attacks are
  temporarily paused"). Before this fix, `vd tick` aborted entirely, indefinitely, for a
  reason that can never affect this codebase's own behaviour: `allow_combat` is
  read-and-ignored everywhere (`ActionsCfg.allow_combat`'s own docstring), so combat is
  unconditionally unreachable regardless of policy.
  - `models.py`: new `RandomnessReadiness` model, `Snapshot.readiness_ready` /
    `randomness_readiness` fields, and `Snapshot.combat_only_degradation()` — a
    structural, fail-closed positive-confirmation check (readiness.ready True, no other
    degradation reasons, game not paused, randomnessReadiness positively confirmed
    not-ready), never an allowlist of known-safe reason text.
  - `read.py`: new `_recover_health_body()`, narrowly scoped to `/health` — a 5xx that
    survives retries has its captured error body defensively parsed and, if it's a real
    health-response shape, evaluated exactly like a normal 200 instead of aborting.
    Every other route's 5xx behaviour through `_fetch_or_exit` is unaffected. New
    `_randomness_readiness()` parser, wired into `snapshot()` alongside the existing
    `game_maintenance`/`degradation_reasons` parsing. Diagnostic message for a recovered
    5xx routed to a new `_stderr_console` so it never corrupts `--json`/`--out`'s
    stdout contract (a real bug caught by this feature's own new snapshot test).
  - `tick.py`: `_fetch_health_only()` (killswitch path) gets the same recovery and grows
    to a 6-tuple, mirroring how it grew for `game_paused` previously — functionally
    inert under `killswitch_active=True` (rung 0 always wins), audit-record honesty.
  - `plan.py` rung 1 / `guard.py`'s `health` gate: both fall through / PASS instead of
    NO-OP / BLOCK when `Snapshot.combat_only_degradation()` is positively confirmed,
    independently re-deriving the same shared fields (mirrors the `game_paused` two-layer
    shape). Rung 1's still-blocking rationale also now surfaces `degradation_reasons`
    instead of a fixed, detail-free string.
  - Verified live end-to-end against the real, currently-degraded API: `vd tick
    --dry-run` now builds a full snapshot and reaches the ordinary decision ladder
    (NOOP: queues busy) instead of aborting with "could not fetch a snapshot" — and
    `vd tick`'s own guard report shows `health: pass` with the combat-only-degradation
    detail message, `game_paused: pass`.
  - `tests/fixtures/health_randomness_degraded.json`: a live capture (2026-08-22, not
    synthesized), reused directly as the mocked 503 body in the new tests.

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
