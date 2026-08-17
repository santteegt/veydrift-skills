# Coverage ledger

**What this is.** A standing record of what this codebase can actually do today, what is
planned, what is deferred, and what is deliberately out of scope — reconstructed once so
nobody has to re-derive it from `docs/SPEC.md` §1's non-goals, `AGENTS.md` §10's known gaps,
and scattered source comments again. Not a spec (`docs/SPEC.md` is that) and not a tutorial
(`docs/PLAYER-GUIDE.md`/`docs/TECHNICAL-WALKTHROUGH.md` are that) — a ledger, meant to be kept
current as phases land.

**How Part 1 is derived — regenerate it, don't hand-maintain it.** The function inventory
comes straight from the pinned ABI, not from memory:

```bash
jq -r '.abi[] | select(.type=="function" and (.stateMutability=="nonpayable" or .stateMutability=="payable")) | .name' \
  skills/veydrift-wallet/abi/VeydriftGame.701bed3.json | sort -u | wc -l
```

Run 2026-08-16 against `skills/veydrift-wallet/abi/VeydriftGame.701bed3.json`: **61 ABI
entries, 60 unique function names.** The gap is `launchFleetMission`, which is overloaded on
the deployed ABI (a 7-arg and a 6-arg form — see `AGENTS.md` §7, trap #2); both forms are
listed as separate rows below. Every claim in Part 1 traces to one of: `guard.py`'s
`_MIN_TIER_FOR_FUNCTION` (`guard.py:72-85`), `allowlist.ts`'s `ECONOMY_SIGNATURES` /
`LAUNCH_FLEET_MISSION_SIGNATURES` (`allowlist.ts:38-58`), a grep of `plan.py` and (as of
Phase 2, 2026-08-16) `candidates.py` for `Action(function=...)`, `tick.py`'s
`_action_to_walletctl_json` (`tick.py:265-280`), or the
deployed contract source at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(`/Users/santteegt/GitRepositories/clones/veydrift`).

**Correction to an earlier estimate**: this ledger was scoped assuming 5 payable functions.
The pinned ABI actually has **6** (§1.5 below) — verified directly, not assumed.

**Architecture note that explains a lot of Part 1's "deferred" rows**: the deployed
`VeydriftGame.sol` is a thin facade — `contract VeydriftGame is VeydriftResourceReserves`
(`VeydriftGame.sol:14`) — that delegates most gameplay entrypoints to separate module
contracts via `_delegateToXModule()` calls, and says so directly in its own doc-comment:
*"Advanced gameplay entrypoints stay in the ABI and fail explicitly until they are split into
modules"* (`VeydriftGame.sol:12-13`). A long tail of ABI entries being untouched by any layer
of this codebase is partly a reflection of that upstream architecture, not purely a gap in
this repo.

**A note on `startResearch`'s planner logic** (see §1.1 below): the research rung is
lowest-level-account-wide, tie-broken by ascending id — deliberately simple, not a
tech-tree strategy. **Phase 1 landed** (`techtree.py` plus a guard `prerequisites` gate):
a locked candidate is skipped in favour of the next unlocked one, on both sides
independently. **Phase 2 landed 2026-08-16** (the general-strategy-engine program): the
entity-selection logic for rungs 5-9, including this research rung, moved out of `plan.py`
into a new `candidates.py` generate/filter/score/select pipeline — see `docs/SPEC.md`
§5.4. Function names below are updated accordingly; behaviour is unchanged (Phase 2's own
acceptance criterion). **Phase 3 landed 2026-08-16**: every planet-local entity is now
reachable, driven by declared `policy.strategy` targets — `ship_targets`/`defense_targets`
(stock-keeping), `research_priority` (ordering override), `building_priority` (the new
"infrastructure" family), plus scored Crawler and proactive-storage candidates. Empty
targets reproduce Phase 2's behaviour exactly (Phase 3's own acceptance criterion). See
`docs/SPEC.md` §5.4/§5.6 and this ledger's Part 1.1/Part 3 rows for what moved. **Phase 4
landed 2026-08-16**: a locked `ship_targets`/`defense_targets`/`research_priority` entry
is no longer a dead end — new `techtree.next_step_toward` walks the requirement tables
backwards to find the shallowest currently-buildable prerequisite, and new
`candidates.generate_unlock_chain_candidates`/`select_unlock_chain_candidate` propose it
as a new, last-precedence ladder rung (`8b`, `plan.py`). Emits only `startBuildingUpgrade`/
`startResearch` — no new write entrypoint, no ABI/allowlist change. Empty targets
reproduce Phase 3's behaviour exactly (Phase 4's own acceptance criterion).

---

## Part 1 — Write entrypoints (from the pinned ABI)

### 1.1 Implemented (5 functions, 5 rows)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `startBuildingUpgrade` | Yes — `candidates.select_building_candidate` / `select_storage_candidate` (moved from `plan.py`'s `_next_building_action` / `_storage_overflow_action` in Phase 2). **Phase 3 (2026-08-16)** widens the entities this can target: `generate_infrastructure_candidates` (Robotics Factory/Nanite Factory/Shipyard/Research Lab/Terraformer/Missile Silo, ordered by `building_priority`), Fusion Reactor (scored, in `generate_energy_candidates`), and proactive storage (`generate_proactive_storage_candidates`, Band 2). **Phase 4** adds a fourth source: `generate_unlock_chain_candidates`, when the shallowest unmet prerequisite toward a locked declared target resolves to a building | ECONOMY (`guard.py:73`) | ECONOMY (`allowlist.ts:39`) | implemented | — |
| `startResearch` | Yes — `candidates.select_research_candidate` (moved from `plan.py`'s `_next_research_action` in Phase 2), lowest-level-account-wide tie-break, filtered through `techtree.unmet()` (Phase 1). **Phase 3** adds `Policy.strategy.research_priority`: named technologies first, then the same lowest-level-first fallback (now labelled `"default: ..."` in the losing/fallback rationale). **Phase 4** adds a second source: `generate_unlock_chain_candidates`, when the shallowest unmet prerequisite toward a locked declared target resolves to a technology | ECONOMY (`guard.py:74`) | ECONOMY (`allowlist.ts:40`) | implemented | — |
| `startShipProduction` | Yes — `candidates.select_building_candidate`'s energy-fallback branch and `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_next_building_action`/`_shipyard_action` in Phase 2). **Phase 3** adds `generate_crawler_candidates` (scored) and `generate_ship_target_candidates` (stock-keeping toward `Policy.strategy.ship_targets`, any of the 16 ships) — Solar Satellite's separate energy-driven path is unchanged and untouched by either addition | ECONOMY (`guard.py:83`, added 2026-08-12 — see comment there for the dead-config history) | ECONOMY (`allowlist.ts:49`) | implemented | — |
| `startDefenseProduction` | Yes — `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_shipyard_action`, `allow_defense`, in Phase 2). **Phase 3** adds `generate_defense_target_candidates` (stock-keeping toward `Policy.strategy.defense_targets`, any of the 10 defenses, respecting the shield-dome/missile-silo caps via a new independent `candidates._defense_capacity_reason`) — declaring `defense_targets` supersedes the pre-Phase-3 hardcoded Rocket-Launcher-only default; an empty list reproduces it exactly | ECONOMY (`guard.py:77`) | ECONOMY (`allowlist.ts:43`) | implemented | — |
| `resolveFleetMission` | **Live since 2026-08-17 (Phase 5, docs/SPEC.md §5.4).** `tick.py`'s wired caller (`_run_tick`) now computes `resolvable_mission_ids` via a new `_resolvable_mission_ids()` — reads `/wallet/{addr}/fleet-visibility` directly (raw dict, bypassing `models.py`, same posture `_maybe_check_human_activity` already takes toward `/activity`) and finds the player's own `outgoing` missions still `Outbound`, `needsResolution`, and >60s past `arrivalAt` — and passes it into `plan_next_action`, so rung 3 fires from the real entrypoint. Verified against the live API (`vd tick --dry-run`, scratch `VEYDRIFT_HOME`) 2026-08-17. Root cause of the prior dormancy stands as documented below (`Snapshot`, frozen, carries no mission list) — this fix works *around* that constraint rather than through it. | ECONOMY (`guard.py:75`) | ECONOMY (`allowlist.ts:41`) | **implemented (live)** | — |

### 1.2 Planned (2 rows — one function, two overloads)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)` (7-arg) | No — no rung in `plan.py` constructs it | OPERATOR (`guard.py:84`) | OPERATOR, mission types 0/1/4 only (`allowlist.ts:55-58`, decoded from calldata at `allowlist.ts:180-205`) | **still planned P5c — blocked on `models.py`** | `tick.py:277-278`'s `_action_to_walletctl_json` has no branch for it — hits the `else: raise ValueError`. Needs `ActionKind.FLEET_MISSION` + new `Action` fields (`mission_type`, `origin_planet_id`, `target_coordinates`, `ships`, `cargo`, `speed_pct`, `holding_seconds`) on `models.py`, frozen for the Phase 5 work package that attempted this (2026-08-17) — see `veydrift-agent`'s `CHANGELOG.md` `[Unreleased]` entry. Colonize (mission type 2) specifically was investigated and confirmed as this same entrypoint (not a separate function) — see §1.3 below — but is equally blocked. |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)` (6-arg) | No | OPERATOR (`guard.py:84`) | OPERATOR, mission types 0/1/4 only (`allowlist.ts:55-58`) | **still planned P5c — blocked on `models.py`** | Same as above; both overloads are allowlisted together, resolved by full signature never by name (`abi.ts`'s `resolveFunctionAbi`) — see `AGENTS.md` §7 trap #2. |

### 1.3 Removed (1 row)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `settlePlanet` | No — grepped `plan.py` and `candidates.py`, no rung/generator emits it | ~~ECONOMY~~ — **removed 2026-08-17** (was `guard.py:76`) | ~~ECONOMY~~ — **removed 2026-08-17** (was `allowlist.ts:42`) | **removed, Phase 5 (docs/SPEC.md §5.4/§9) — `veydrift-wallet` v0.2.0, breaking** | Was: body identical to `collectResources` at the pinned commit — `VeydriftGame.sol:120-128`: both are exactly `_touchPlayer(msg.sender); _collectPlanetResources(planetId);`. `collectResources` is correctly refused as a disguised read (`abi.ts`'s `NONPAYABLE_READ_FUNCTIONS`, §1.4 below); `settlePlanet` was the same operation but allowlisted at ECONOMY on both sides with a live `tick.py` encoder branch despite no planner rung ever proposing it. Removed from `guard.py`'s `_MIN_TIER_FOR_FUNCTION`, `allowlist.ts`'s `ECONOMY_SIGNATURES`, and `tick.py`'s `_action_to_walletctl_json` together, in the same change — `test_tier_map_agrees_with_the_wallet_engines_allowlist` (agent-side) verifies the first two still agree. Real colonisation — what a human might have expected `settlePlanet` to be — is `launchFleetMission` mission type `Colonize` (2); see §1.2 and §1.5 below. |

### 1.4 Correctly excluded — disguised reads (6 rows)

`abi.ts`'s `NONPAYABLE_READ_FUNCTIONS` (`abi.ts:204-211`) — `nonpayable` in the ABI (confirmed
in the jq dump: none of these six are `view`/`pure`) because each lazily settles state before
returning, but semantically a read. `isNonpayableRead()` makes `sendTx` refuse every one of
them outright (`AGENTS.md` §7).

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `attackProtectionStatus` | No | not present | not present | correctly excluded — route via `simulate` | N/A |
| `collectResources` | No | not present | not present | correctly excluded — route via `simulate` | N/A |
| `debrisField` | No | not present | not present | correctly excluded — route via `simulate` | N/A |
| `maxRaidLoot` | No | not present | not present | correctly excluded — route via `simulate` | N/A |
| `protectedResources` | No | not present | not present | correctly excluded — route via `simulate` | N/A |
| `raidableResources` | No | not present | not present | correctly excluded — route via `simulate` | N/A |

### 1.5 Excluded — payable (6 rows, corrected from an earlier estimate of 5)

`allowlist.ts:130-134` checks `tx.value !== 0n` unconditionally, before the selector check, at
every tier — structurally excludes all six regardless of any selector list ever added to
`ECONOMY_SIGNATURES`/`LAUNCH_FLEET_MISSION_SIGNATURES`.

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `importMigratedState` | No | not present | not present | out of scope — payable + migration (SPEC.md §1 non-goal) | Structural; would also require a migration-model decision, not just a value check relaxation |
| `importMigratedStateWithReferral` | No | not present | not present | out of scope — payable + migration + referrals (SPEC.md §1 non-goal) | Same as above |
| `settleFirstPlanet` | No | not present | not present | out of scope — payable | First-planet settlement is a one-time bootstrap action outside the tick loop's scope |
| `settleFirstPlanetWithReferral` | No | not present | not present | out of scope — payable + referrals | Same, plus referral-code handling |
| `startPlanet` | No | not present | not present | out of scope — payable | **Not the real colonisation path** (a naming trap worth flagging explicitly): verified 2026-08-17 against `VeydriftGame.sol`'s facade — real player colonisation is `launchFleetMission` mission type `Colonize` (2), which dispatches to `VeydriftColonizationModule` (see §1.2 above); `startPlanet` is a separate, `payable` entrypoint, structurally excluded here by `allowlist.ts`'s unconditional `value == 0` check regardless of what this codebase ever builds. Also depends on `maxPlanets` (`calc.max_planets`, §3). |
| `startPlanetWithReferral` | No | not present | not present | out of scope — payable + referrals | Same, plus referral-code handling |

### 1.6 Out of scope — combat (6 rows)

`docs/SPEC.md` §1 non-goal: "Combat, alliances, ACS, migration, referrals, NFT burns, the
ERC-20 market bridge." None of these six appear in `plan.py`, `guard.py`'s tier map, or
`allowlist.ts`'s signature lists (grepped, absent from all three). Combat is unreachable **in
code**, not by config — `AGENTS.md` §5: enabling any of these requires a source change to
`_MIN_TIER_FOR_FUNCTION` and `allowlist.ts`, and combat mission types 3/6/7/8/9 stay outside
`OPERATOR_ALLOWED_MISSION_TYPES = {0, 1, 4}` (`allowlist.ts:61`) even at tier 3.

| Function | Contract location (pinned commit) | Status | What it would take |
| --- | --- | --- | --- |
| `launchAttackMission` | declared `VeydriftGame.sol:381`, impl `VeydriftGameplayModule.sol:90` | out of scope — combat | Source change across `plan.py`, `guard.py`, `allowlist.ts`, four documents, and `test_tier_map_agrees_with_the_wallet_engines_allowlist` — deliberate friction, per `AGENTS.md` §5 |
| `joinAttackMission` | declared `VeydriftGame.sol:394`, impl `VeydriftGameplayModule.sol:127` | out of scope — combat | Same |
| `launchInterplanetaryMissileAttack` | declared `VeydriftGame.sol:447`, impl `VeydriftPlanetManagementModule.sol:76` | out of scope — combat | Same |
| `launchDefenseHold` | declared `VeydriftGame.sol:404`, impl `VeydriftDefenseHoldModule.sol:58` | out of scope — combat | Same |
| `completeAttackTargetSnapshotQueues` | `VeydriftColonizationModule.sol:94` | out of scope — combat | Settlement/bookkeeping helper tied to attack-target queues; same friction applies since it only has meaning once combat is reachable |
| `settleDuePlayerCombatArrivals` | `VeydriftGame.sol:210` | out of scope — combat | Settles arrivals of already-launched combat missions; same reasoning as above — meaningless without combat itself being reachable |

**Combat is a policy exclusion, not a technical one.** `packages/contracts/src/libraries/VeydriftCatalog.sol`
at the pinned commit has the battle-resolution formulas extractable the same way the tech
tree already is elsewhere in this codebase: `shipBattleAttack` (line 261), `shipBattleShield`
(line 281), `shipBattleHull` (line 301), `shipRapidfireAgainstShip` (line 335),
`shipRapidfireAgainstDefense` (line 357) — all `public pure`. Nothing here is unpublished; it
is simply not read.

### 1.7 Owner-only (13 rows)

Not player-callable at any tier — `onlyOwner` or an OZ `initializer`/`Initializable` guard at
the pinned commit, spot-verified directly against source (not assumed from naming):

| Function | Modifier / contract location (pinned commit) |
| --- | --- |
| `initialize` | `initializer` — `VeydriftGame.sol:50` |
| `transferOwnership` | inherited via the `VeydriftResourceReserves` → ... → OZ `Ownable` chain |
| `setGamePaused` | `onlyOwner` — `VeydriftGame.sol:244` |
| `setRandomnessEngine` | `onlyOwner` — `VeydriftGame.sol:234` |
| `setMoonSystem` | `onlyOwner` — `VeydriftGame.sol:230` |
| `setMigrationSettlement` | `onlyOwner` — `VeydriftGame.sol:240` |
| `setAllianceSystem` | `onlyOwner` — `VeydriftGame.sol:291` |
| `setResourceToken` | `onlyOwner` — `VeydriftResourceReserves.sol:241` |
| `setResourceTokens` | `onlyOwner` — `VeydriftResourceReserves.sol:225-227` |
| `setStartPrice` | `onlyOwner` — `VeydriftGameStorage.sol:814` |
| `setAttackProtectionExemption` | `onlyOwner` — `VeydriftGameStorage.sol:820-823` |
| `withdrawFees` | `onlyOwner` — `VeydriftGameStorage.sol:1157` |
| `depositResourceReserves` | `onlyOwner` — `VeydriftResourceReserves.sol:250` |

`depositResourceReserves` was found during verification and added here — not itself named in
the original scoping pass, but it is `onlyOwner` at the cited line, so it belongs here rather
than in "deferred — other" below.

### 1.8 Deferred — other (19 rows)

Player-callable (no `onlyOwner`), not payable, not a disguised read, not combat — genuinely
untouched by every layer of this codebase, mostly because they belong to a game surface this
project hasn't built a planner/guard/wallet path for yet. See Part 2 for the surface each one
belongs to.

| Function | Contract location (pinned commit) | Belongs to (Part 2 surface) |
| --- | --- | --- |
| `abandonPlanet` | `VeydriftGame.sol:320` | planet lifecycle — reverts for a home planet per `README.md`'s key-custody section; a single-planet account can never call this meaningfully |
| `clearMoonState` | `VeydriftColonizationModule.sol:74` | moon acquisition & jump gates |
| `completeFleetMissionReturn` | `VeydriftGame.sol:442` | fleet/mission bookkeeping (adjacent to §1.1's dormant `resolveFleetMission`) |
| `finishBuildingUpgrade` | `VeydriftGame.sol:170` | queue-completion helper — the contract-side "finish" call for a construction whose `readyAt` has elapsed; not modelled as a distinct planner action anywhere |
| `finishDefenseProduction` | `VeydriftColonizationModule.sol:132` | queue-completion helper, same shape |
| `finishResearch` | `VeydriftGame.sol:225` | queue-completion helper, same shape |
| `finishShipProduction` | `VeydriftColonizationModule.sol:90` | queue-completion helper, same shape |
| `grantMoonResources` | `VeydriftColonizationModule.sol:62` | moon acquisition & jump gates |
| `launchBodyFleetMission` | declared `VeydriftGame.sol:344`, impl `VeydriftDefenseHoldModule.sol:197` | moon acquisition & jump gates — a body-targeted fleet mission distinct from planet-to-planet `launchFleetMission`; likely moon/gate-related given its home module, not independently confirmed beyond the signature |
| `moveMoonGateShips` | `VeydriftColonizationModule.sol:70` | moon acquisition & jump gates |
| `recallFleetMission` | `VeydriftDefenseHoldModule.sol:331` | fleet/mission bookkeeping |
| `releaseExcessResourceReserves` | `VeydriftGame.sol:278`, delegates to `_delegateToStateMigrationModule()` | referrals & migration |
| `renamePlanet` | `VeydriftGame.sol:315` | cosmetic player action, no planner/guard reason to exclude it beyond nobody having built it |
| `reserveMigrationCoordinates` | `VeydriftGame.sol:248` | referrals & migration |
| `setMoonShipCount` | `VeydriftColonizationModule.sol:66` | moon acquisition & jump gates — plain `external`, **not** `onlyOwner` despite the "set" naming (verified directly; do not assume owner-only from the name alone) |
| `setSpaceDockSystem` | `VeydriftGame.sol:287` | space dock repair — plain `external`, **not** `onlyOwner`. The contract's own doc-comment at `VeydriftGame.sol:283-286` reads verbatim: *"UNUSED / DORMANT: SpaceDock is never set on the live deployment, so `_spaceDockSystem` stays `address(0)` and combat wreckage recording no-ops."* This function is dormant on the live deployment itself, upstream of anything this repo does. |
| `settleDuePlayerColonizeArrivals` | `VeydriftColonizationModule.sol:112` | moon acquisition & jump gates / colonization arrival settlement |
| `spendMoonResources` | `VeydriftColonizationModule.sol:58` | moon acquisition & jump gates |
| `untrackResolvedFleetMission` | `VeydriftColonizationModule.sol:78` | fleet/mission bookkeeping |

### 1.9 Out of scope — ERC-20 market bridge (3 rows)

`docs/SPEC.md` §1 non-goal, listed explicitly: "the ERC-20 market bridge."

| Function | Contract location (pinned commit) |
| --- | --- |
| `depositMarketResource` | `VeydriftGame.sol:459` |
| `requestMarketResourceWithdrawal` | `VeydriftGame.sol:464` |
| `finishMarketResourceWithdrawal` | `VeydriftGame.sol:469` |

**Row count check**: 5 (§1.1) + 1 (§1.2, one unique name — `launchFleetMission` — spread across
2 overload rows) + 1 (§1.3) + 6 (§1.4) + 6 (§1.5) + 6 (§1.6) + 13 (§1.7) + 19 (§1.8) + 3 (§1.9)
= **60 unique function names**, matching the `jq -u` count above. Counting table *rows* instead
(§1.2 contributing 2 rows for its 2 overloads) gives **61**, matching the raw ABI entry count.

---

## Part 2 — Game surfaces

Surfaces not reducible to a single ABI entrypoint. Same status/what-it-would-take treatment.

| Surface | Status | What it would take / notes |
| --- | --- | --- |
| Fusion Reactor as an energy-first *substitute* | **partial (Phase 3)** | Fusion Reactor is a scored candidate in `generate_energy_candidates`, so it can win the economic band on its own merits. It is deliberately **not** wired into `candidates._cheapest_energy_choice`, the comparison that picks the substitute when a mine upgrade is energy-blocked — that comparison is still Solar Plant vs. Solar Satellite only, as it was pre-Phase-3, because it is pinned by the hot-planet counterfactual fixture where Fusion Reactor happens to be unlocked. Consequence: on an energy-blocked planet the substitute proposed can be a Solar Plant even where a Fusion Reactor would be the better buy. Closing it means extending `_cheapest_energy_choice` to a three-way comparison (it must account for Fusion's deuterium upkeep via `calc.fusion_deuterium_upkeep`, which the two-way comparison never had to) and re-pinning that fixture deliberately. |
| Combat & battle resolution | out of scope — policy, not technical (§1.6 above) | The formulas (`shipBattleAttack`/`Shield`/`Hull`, rapidfire tables) are already extractable from `VeydriftCatalog.sol` the same way the tech tree is. What's missing is the deliberate friction removal across `plan.py`/`guard.py`/`allowlist.ts` described in `AGENTS.md` §5, not a research problem. |
| Espionage | not found | No espionage/spy-probe entrypoint, building, or technology found anywhere in the pinned ABI or contract source (`VeydriftTypes.sol`, `VeydriftCatalog.sol` — searched for `probe`/`scan`/`espionage`/`recon`, no matches). Recorded as "not found" rather than "deferred" — it may simply not exist as a mechanic in this version of Veydrift, unlike classic OGame-likes. |
| Raid/loot model | blocked | `protectedResources` semantics are unconfirmed — `docs/NOTES.md` §6 (`docs/NOTES.md:121-133`): raidable resources observed as exactly 50% of held while `protectedResources` independently read 0, meaning it tracks something other than a simple floor. `docs/SPEC.md` §1 lists "a raid-profitability model" as an explicit non-goal for this reason. `models.py`'s `PlanetSnapshot.protected_resources` field comment says the same: "Semantics UNCONFIRMED... Do not build a loot model on this." |
| Debris fields & recycling | deferred | `debrisField(uint256)` is a `NONPAYABLE_READ_FUNCTIONS` entry (§1.4) with a real implementation at `VeydriftPlanetManagementModule.sol:281-284` returning `(metal, crystal)` per planet — readable today via `simulate`, but nothing in `read.py`'s `snapshot` composition or `plan.py` consumes it. |
| Moon acquisition & jump gates | deferred | Seven entrypoints belong here — `grantMoonResources`, `spendMoonResources`, `clearMoonState`, `moveMoonGateShips`, `setMoonShipCount`, `settleDuePlayerColonizeArrivals`, `launchBodyFleetMission` (all in §1.8) — plus `read.py`'s standalone `moon` command (`read.py:486-498`), which `Snapshot`'s composition never calls (see the data-surfaces subsection below). A moon-aware planner phase would need to both read this data into `Snapshot` and build the write paths. |
| Space dock repair | deferred, and upstream-dormant | `setSpaceDockSystem` (§1.8) carries the contract's own doc-comment that the live deployment never sets a Space Dock system contract at all, so ship-repair mechanics are inert **on the deployed contract itself** — not just unbuilt in this repo (`VeydriftGame.sol:283-286`). |
| Interdimensional Rift Stabilizer | mechanics unpublished, hard-capped at level 1 | `Building.InterdimensionalRiftStabilizer` exists in the catalog (id 15, cost tuple `(8_000, 8_000, 4_000)` at `VeydriftCatalog.sol:30`); the level-1 hard cap is documented at `docs/NOTES.md:540`, not independently re-derived from a level-cap function in this pass — no such function was found by name in `VeydriftCatalog.sol` during this ledger's research. |
| Terraformer field gain | deferred | `Building.Terraformer` exists in the catalog with a cost tuple `(0, 50_000, 100_000)` at `VeydriftCatalog.sol:27`; the fields-gained-per-level formula was not traced in this pass. |
| Expeditions | not found | No expedition-related entrypoint or type found in the pinned contract source during this pass (searched `VeydriftTypes.sol`/`VeydriftGame.sol`/module contracts for "expedition", no matches). Recorded as "not found," same posture as espionage above. |
| Alliances | out of scope — separate contract, separate non-goal | `VeydriftAllianceSystem.sol` is its own deployed contract (`createAlliance`, `inviteMember`, `acceptInvite`, etc. starting at `VeydriftAllianceSystem.sol:339`) with its own ABI — not part of `VeydriftGame.701bed3.json`, and not pinned or read anywhere in this repo. `docs/SPEC.md` §1 lists alliances as an explicit non-goal. |
| Highscore/score model | API-only, no contract entrypoint | `/highscores` is a backend API route (`skills/veydrift-agent/references/api-routes.md:123`, `read.py`'s `highscores` command at `read.py:631-647`) — 1-2+ MB, `--out`-mandatory, never composed into `Snapshot`. No `playerScore`/highscore function was found in the pinned `VeydriftGame` ABI or contract source. |
| Referrals & migration | out of scope — explicit non-goal | `docs/SPEC.md` §1: "migration, referrals" listed by name. Covers `reserveMigrationCoordinates`, `releaseExcessResourceReserves` (§1.8), `importMigratedState(WithReferral)`, `settleFirstPlanetWithReferral`, `startPlanetWithReferral` (§1.5). |

### Data surfaces `read.py` fetches and then discards

`vd read snapshot`'s composition (`read.py:739+`) calls only health + overview +
infrastructure + research + shipyard + defenses. `read.py` has standalone commands for other
routes that `snapshot` never touches, so `Snapshot` (the frozen model every downstream module
reads) never carries this data even though it is one CLI call away:

| Data | Where it's fetched (unused by `Snapshot`) | Why it matters |
| --- | --- | --- |
| Moon state | `read.py`'s `moon` command, `read.py:486-498` | Needed for any moon-acquisition planner path (Part 2 above) |
| The player's own outgoing/returning missions | `read.py`'s `missions` command, `read.py:536-548`; also `fetch_fleet_visibility()` (new 2026-08-17) | **No longer the reason rung 3 is dead** — `tick.py`'s `_resolvable_mission_ids` now reads `outgoing` from `/wallet/{addr}/fleet-visibility` directly and feeds rung 3 (see §1.1 above). Still true that this data has no home on `Snapshot` (`models.py` frozen) — the fix works around that, not through it; a future `models.py` change could still promote this to a first-class field if a planner rung ever needs the *full* mission list (not just resolvable ids). |
| Universe/neighbourhood data (other players, debris, moons, migration reservations near a planet) | `read.py`'s `universe` command, `read.py:584-610` | **Partially closed 2026-08-17**: `PlanetSnapshot.archetype` is now populated for a planet's *own* slot, opt-in via `read.snapshot`'s new `universe_cadence_hours` (wired from `policy.cadence.universe_hours`, cadence-gated via `http.py`'s existing disk cache). The rest of this row stands: `occupiedBy`/`debrisField`/`hasMoon` for *other* slots in the neighbourhood still have no home on `Snapshot`/`PlanetSnapshot` (frozen `models.py`) and are not surfaced anywhere the planner can see them — still relevant for a future colonisation-target-selection planner (see §1.2/§1.5 above). |
| Moon state (buildings, resources, ship counts) | `read.py`'s `moon` command, `read.py:486-498` | **Not closed.** Moon buildings carry `key`/`label` unlike every other surface (per this phase's own brief) and there is no field on `PlanetSnapshot`/`Snapshot` (frozen `models.py`) to carry that differently-shaped data without forcing it into `Entity` and losing the distinction. Investigated 2026-08-17 (Phase 5) and left undone for the same `models.py`-frozen reason as §1.2's `launchFleetMission` rows. |
| Tactical/combat data from `/planets` | `read.py`'s `planets`-adjacent commands; `highscores`' "full planet+tactical payload" note at `read.py:638` | Would be needed for any combat-adjacent planning, itself out of scope (Part 1 §1.6) |
| `launchableShips` | Present in raw `/shipyard` response (`docs/NOTES.md:30`), never read into `PlanetSnapshot` | Only matters once a fleet-mission planner (P5c, §1.2) exists |

**`crawlerProduction` and `missileSiloLevel` moved out of this table, 2026-08-16 (Phase 3 of
the general-strategy-engine program).** Both are now read into `PlanetSnapshot`
(`crawler_production`, `missile_silo_level`) from the exact same routes `snapshot` already
calls — no new HTTP call — and both are live consumers, not dead data: `crawler_production`
feeds `candidates.generate_crawler_candidates` (preferring the live `capped` flag over
recomputing), and `missile_silo_level` feeds `candidates._defense_capacity_reason`'s
independent missile-silo-slot cap check. Both default `None` and are treated as
unverifiable, never `0`, everywhere they're consumed.

---

## Part 3 — Verified-but-unused `calc.py` functions

Every function below is contract-derived and covered by `tests/test_calc.py`, but grepping
`plan.py`/`candidates.py` for each name found no call site beyond what's already wired —
`candidates.py` (which now owns this logic, moved from `plan.py` in Phase 2) imports
`calc.energy_balance`, `calc.scaled_level` and `calc.production_per_hour` (new in Phase 2,
for `score_payback`), and calls `calc.build_seconds` once (for the informational
build-time-savings note, not a decision). None of the functions below participate in any
ladder rung today.

| Function | What it computes | Plausible future consumer |
| --- | --- | --- |
| `production_per_hour` | Full per-hour Metal/Crystal/Deuterium output, applying multiplier → crawler boost → fusion upkeep → energy throttle in the contract's own order | A full economic simulator / lookahead planner (note: this row predates Phase 2/3 and is stale — `candidates.py` already calls this directly, both for `score_payback`'s before/after delta since Phase 2 and for `generate_crawler_candidates` since Phase 3; not corrected here, out of this pass's scope) |
| `ship_seconds` | Ship-production queue duration | A fleet-mission / shipyard planning phase (P5c and beyond) |
| `research_seconds` | Research queue duration | Already covered live by `vd calc verify`'s duration cross-check; not used for planning decisions |
| `distance` | Coordinate-pair distance (`"G:S:P"` or tuple) | Any planner path that ranks candidate mission targets — colonization, P5c fleet missions |
| `travel_seconds` | Mission travel time from distance + speed | P5c fleet-mission planning |
| `mission_fuel` | Deuterium fuel cost for a mission | P5c fleet-mission planning (affordability of the mission itself, not just the ships) |
| `available_cargo` | Cargo capacity minus fuel cost | P5c fleet-mission planning |
| `max_planets` | `1 + astrophysics_level` colony cap | A colonization-planning phase (`startPlanet`, §1.5 — itself blocked structurally on `value == 0` until a payable-action design exists) |
| `solar_crossover_table` | Smallest Solar Plant level whose energy alone covers same-level mines | Currently used only by the standalone `vd calc crossover` CLI command, not by the planner — `candidates._cheapest_energy_choice` (moved from `plan.py`'s `_energy_candidate` in Phase 2) already does a live, per-planet version of this comparison directly |
| `deuterium_multiplier_bps` | Temperature-derived deuterium multiplier | Used only via `candidates.py`'s live-multiplier reads today (moved from `plan.py` in Phase 2; `PlanetSnapshot.deuterium_multiplier_bps`, sourced from the API); this pure recomputation isn't called because the live value is already provided (docs/SPEC.md §5.4: prefer live data over recomputing it) |
| `max_temp_from_bps` | Inverse of `deuterium_multiplier_bps`, diagnostic only per its own docstring | Cross-checking a reported multiplier against a reported `temperature`, not planning |
