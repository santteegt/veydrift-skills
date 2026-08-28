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
`_MIN_TIER_FOR_FUNCTION` (`guard.py:83-103`, re-verified 2026-08-17), `allowlist.ts`'s
`ECONOMY_SIGNATURES` (`allowlist.ts:38-60`) / `LAUNCH_FLEET_MISSION_SIGNATURES`
(`allowlist.ts:65-68`), a grep of `plan.py` and (as of
Phase 2, 2026-08-16) `candidates.py` for `Action(function=...)`, `tick.py`'s
`_action_to_walletctl_json` (`tick.py:449-482`, re-verified 2026-08-17), or the
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

### 1.1 Implemented (6 functions, 7 rows — `launchFleetMission`'s two overloads added 2026-08-17, Phase 5c/5b)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `startBuildingUpgrade` | Yes — `candidates.select_building_candidate` / `select_storage_candidate` (moved from `plan.py`'s `_next_building_action` / `_storage_overflow_action` in Phase 2). **Phase 3 (2026-08-16)** widens the entities this can target: `generate_infrastructure_candidates` (Robotics Factory/Nanite Factory/Shipyard/Research Lab/Terraformer/Missile Silo, ordered by `building_priority`), Fusion Reactor (scored, in `generate_energy_candidates`), and proactive storage (`generate_proactive_storage_candidates`, Band 2). **Phase 4** adds a fourth source: `generate_unlock_chain_candidates`, when the shallowest unmet prerequisite toward a locked declared target resolves to a building | ECONOMY (`guard.py:84`) | ECONOMY (`allowlist.ts:39`) | **implemented — the one selector verified against a real chain state.** `tick.py`'s full `build → simulate → send → await receipt → await indexed` path ran end-to-end against a local Anvil fork of Base (`status: "success"`, Metal Mine 10 → 11 on planet 664) — the first time this codebase's own send path, not a human through the game UI, resolved above level 0. This codebase has since run this selector against mainnet itself, for real, at tier 2/3 — see `README.md`'s Status section (this ledger doesn't itself catalog which mainnet selectors that specifically exercised). | That same fork run is what surfaced `veydrift-agent` 1.1.1's fix: `_send_and_await` sent without ever calling `walletctl simulate` first (`AGENTS.md` §10, `skills/veydrift-agent/CHANGELOG.md`'s `1.1.1` entry) |
| `startResearch` | Yes — `candidates.select_research_candidate` (moved from `plan.py`'s `_next_research_action` in Phase 2), lowest-level-account-wide tie-break, filtered through `techtree.unmet()` (Phase 1). **Phase 3** adds `Policy.strategy.research_priority`: named technologies first, then the same lowest-level-first fallback (now labelled `"default: ..."` in the losing/fallback rationale). **Phase 4** adds a second source: `generate_unlock_chain_candidates`, when the shallowest unmet prerequisite toward a locked declared target resolves to a technology | ECONOMY (`guard.py:85`) | ECONOMY (`allowlist.ts:40`) | implemented | A `startResearch` call on real accumulated state (planet 664) is what surfaced `simulateTx`'s uncapped-`eth_call` defect on the Anvil fork: `simulate` reported `ok: true` at gas limit 465588, `send` submitted it at that same limit, and it reverted `OutOfGas`. Fixed — `simulate` now caps its call at `tx.gas` (`skills/veydrift-wallet/references/tx-safety.md`, `references/fork-testing.md` §8.4, `CHANGELOG.md`) — this was a `simulate`-mechanism defect, not specific to `startResearch`'s reachability or gas cost. |
| `startShipProduction` | Yes — `candidates.select_building_candidate`'s energy-fallback branch and `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_next_building_action`/`_shipyard_action` in Phase 2). **Phase 3** adds `generate_crawler_candidates` (scored, gated behind `Policy.strategy.enable_crawler` — default `false`, added 2026-08-17 by judge finding 4; before that fix an unlocked Crawler could silently outrank Solar Satellite with no explicit opt-in) and `generate_ship_target_candidates` (stock-keeping toward `Policy.strategy.ship_targets`, any of the 16 ships, unaffected by `enable_crawler`) — Solar Satellite's separate energy-driven path is unchanged and untouched by either addition | ECONOMY (`guard.py:101`, added 2026-08-12 — see comment there for the dead-config history) | ECONOMY (`allowlist.ts:59`) | **implemented — now verified against a real chain state.** Solar Satellite (id 9) qty 1 live-sent on a local Anvil fork of Base from the project's own account (planet 664), `status: "success"` (round 2, 2026-08-19, `skills/veydrift-wallet/references/fork-testing.md` §9.1) | — |
| `startDefenseProduction` | Yes — `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_shipyard_action`, `allow_defense`, in Phase 2). **Phase 3** adds `generate_defense_target_candidates` (stock-keeping toward `Policy.strategy.defense_targets`, any of the 10 defenses, respecting the shield-dome/missile-silo caps via a new independent `candidates._defense_capacity_reason`) — declaring `defense_targets` supersedes the pre-Phase-3 hardcoded Rocket-Launcher-only default; an empty list reproduces it exactly | ECONOMY (`guard.py:95`) | ECONOMY (`allowlist.ts:52`) | **implemented — now verified against a real chain state.** Rocket Launcher (id 0) qty 1 live-sent on the same fork/account, `status: "success"` (round 2, 2026-08-19, `fork-testing.md` §9.1) | — |
| `resolveFleetMission` | **Live since 2026-08-17 (Phase 5, docs/SPEC.md §5.4).** `tick.py`'s wired caller (`_run_tick`) now computes `resolvable_mission_ids` via a new `_resolvable_mission_ids()` — reads `/wallet/{addr}/fleet-visibility` directly (raw dict, bypassing `models.py`, same posture `_maybe_check_human_activity` already takes toward `/activity`) and finds the player's own `outgoing` missions still `Outbound`, `needsResolution`, and >60s past `arrivalAt` — and passes it into `plan_next_action`, so rung 3 fires from the real entrypoint. Verified against the live API (`vd tick --dry-run`, scratch `VEYDRIFT_HOME`) 2026-08-17. Root cause of the prior dormancy stands as documented below (`Snapshot`, frozen, carries no mission list) — this fix works *around* that constraint rather than through it. | ECONOMY (`guard.py:86`) | ECONOMY (`allowlist.ts:41`) | **implemented (live) — now fork-live-sent (round 3, 2026-08-19).** Round 2 could only confirm this selector by reading `VeydriftColonizationModule.sol:237-240` (an invalid/nonexistent mission id silently no-ops rather than reverting, by design), since neither test account had an unresolved mission. Round 3 produced a Colony Ship, launched a real Colonize mission (id `26480`) on the same impersonated multi-planet account, and resolved it through the exact production `walletctl build → simulate → send` path: `status: "success"`, tx `0xb409b6a34413a60fe0ced28a4778ed69d99c6eccde94047d23c3c1b3553002ff`. The source read and the live send are complementary — source explains why an *invalid* id is safe, the send confirms a *valid* one resolves correctly (`fork-testing.md` §10.4) | — |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)` (7-arg) | **Encodable and planner-reachable only if `Action.speed_pct` is hand-set — no generator sets it.** `candidates.generate_transport_candidates`/`generate_deploy_candidates`/`generate_harvest_candidates`/`generate_foreign_harvest_candidates`/`generate_colonize_candidates` (the first four gated on `policy.actions.allow_fleet_noncombat`, Colonize on `policy.strategy.colonize`, all default `false`) construct the `Action`; none of these generators ever sets `Action.speed_pct` — `grep -rn "speed_pct" skills/veydrift-agent/src/` returns only `models.py`'s field declaration and `tick.py`'s own encoder, and `tick.py`'s `_action_to_walletctl_json` (`tick.py:442`) selects this 7-arg overload only when `speed_pct is not None`. Transport and Deploy specifically are additionally gated at the **contract level**, not just the planner's: `VeydriftGameplayModule.sol`'s `_launchFleetMission` requires `_requirePlanetOwner(targetPlanetId)` for both — the mission target must itself be a planet the sender owns, confirmed live by a reverted `NotPlanetOwner()` send (round 2, `docs/RESEARCH-ADDENDUM.md` §4.3) — which is why `generate_transport_candidates`'s ≥2-owned-planets precondition (`candidates.py:1913-1930`) and `generate_deploy_candidates`'s own-planet check are the literal contract rule, not an overcautious heuristic. **Round 2 (2026-08-19) live-sent this exact overload**, hand-writing the action JSON (origin planet 23 → target planet 184, both owned by a temporarily-impersonated real 10-planet account, `{smallCargo: 1}`, `{deuterium: 1000}`, `speedPercent: 50`), `status: "success"` (`fork-testing.md` §9.2) — so this is now confirmed live *by hand-writing the action*, still correctly not planner-reachable | OPERATOR (`guard.py`'s `_MIN_TIER_FOR_FUNCTION`) **+** `mission_type` gate, default-deny (`guard.py`'s `_ALLOWED_MISSION_TYPES = {0, 1, 2, 4}` unconditionally, new in this change; **plus `_COMBAT_MISSION_TYPES = {3}` when `policy.actions.allow_combat=true`, added 2026-08-28, launch-actions plan commit 5**) | OPERATOR, mission types **0/1/2/4** unconditionally (Colonize added this change, `allowlist.ts`'s `OPERATOR_ALLOWED_MISSION_TYPES`, decoded from calldata at `allowlist.ts`'s calldata-level check), **plus Attack(3) via `COMBAT_ALLOWED_MISSION_TYPES` when `resolveAllowCombat` resolves true (commit 5)** | **implemented — encodable and live-confirmed by hand-written action, but still not planner-reachable; see the 6-arg row for the overload a live plan actually selects. Colonize is allowlisted+gated at both layers and shares this overload's encoding path; round 2 sent this exact 7-arg form for Transport only (no Colony Ship owned by either test account that round), but round 3 (2026-08-19) sent a real Colonize mission on the 6-arg form (see that row) and confirmed the slot-claiming behavior live — see Part 3's `max_planets` row below and `fork-testing.md` §10** | Wiring `Action.speed_pct` from a planner rung that wants non-default mission speed |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)` (6-arg) | Same generators; `tick.py` selects this overload when `Action.speed_pct` is `None` (the contract's own 100%-speed default) — since no generator ever sets `speed_pct`, this is the branch every live `launchFleetMission` proposal actually takes today, including Colonize and Deploy since 2026-08-28 (`docs/SPEC.md` correction 69), and Attack since the same date's commit 6 (`generate_attack_candidates` doesn't set `speed_pct` either — `docs/SPEC.md` correction 71). Same Transport/Deploy target-ownership contract rule as the 7-arg row above applies here identically | Same as above | Same as above | **implemented (live) — this is the overload the planner actually reaches, and round 2 (2026-08-19) confirmed it end-to-end on a fork**: origin planet 23 (`2:477:7`) → target planet 184 (`2:477:3`), both owned by the same impersonated 10-planet account as the 7-arg row, `{smallCargo: 2, largeCargo: 1}`, `{deuterium: 5000}`, `status: "success"` — the first real fleet mission ever launched by this codebase (`fork-testing.md` §9.2). The fuel this mission actually cost (`FleetMissionCargo` event, `fuelCost = 10`) matched `calc.mission_fuel`'s prediction exactly (`fork-testing.md` §8.3). **Round 3 (2026-08-19) sent this same overload for Colonize (mission type 2)**: the same account produced a Colony Ship (home planet already met the Shipyard ≥4/Impulse Drive ≥3 production thresholds, no unlock chain needed), sent a Colonize mission to a scanned-available coordinate (`2:477:9`), and `resolveFleetMission` resolved it — `status: "success"` on both, and `isCoordinateAvailable`/`planetCountOf` confirmed the exact targeted slot flipped `true`→`false` / `10`→`11` (`fork-testing.md` §10) | — |

### 1.2 Planned (0 rows — was 2; both moved to §1.1, 2026-08-17)

Empty as of this change. `launchFleetMission`'s two overloads (the only rows this section ever
held) moved to §1.1 once `models.py` was unfrozen and `guard.py`'s `mission_type` gate /
`tick.py`'s encoder / `candidates.py`'s logistics generators landed — see that section and
`veydrift-agent`'s `CHANGELOG.md` `[Unreleased]` entry for the full writeup.

### 1.3 Removed (1 row)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `settlePlanet` | No — grepped `plan.py` and `candidates.py`, no rung/generator emits it | ~~ECONOMY~~ — **removed 2026-08-17** (was `guard.py:76`) | ~~ECONOMY~~ — **removed 2026-08-17** (was `allowlist.ts:42`) | **removed, Phase 5 (docs/SPEC.md §5.4/§9) — `veydrift-wallet` v0.2.0, breaking** | Was: body identical to `collectResources` at the pinned commit — `VeydriftGame.sol:120-128`: both are exactly `_touchPlayer(msg.sender); _collectPlanetResources(planetId);`. `collectResources` is correctly refused as a disguised read (`abi.ts`'s `NONPAYABLE_READ_FUNCTIONS`, §1.4 below); `settlePlanet` was the same operation but allowlisted at ECONOMY on both sides with a live `tick.py` encoder branch despite no planner rung ever proposing it. Removed from `guard.py`'s `_MIN_TIER_FOR_FUNCTION`, `allowlist.ts`'s `ECONOMY_SIGNATURES`, and `tick.py`'s `_action_to_walletctl_json` together, in the same change — `test_tier_map_agrees_with_the_wallet_engines_allowlist` (agent-side) verifies the first two still agree. Real colonisation — what a human might have expected `settlePlanet` to be — is `launchFleetMission` mission type `Colonize` (2), live and allowlisted since 2026-08-17 (§1.1's `launchFleetMission` rows). |

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

`allowlist.ts:164-169` checks `tx.value !== 0n` unconditionally, before the selector check, at
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

### 1.6 Out of scope — combat entrypoints other than `launchFleetMission` (6 rows)

`docs/SPEC.md` §1 non-goal, narrowed by the launch-actions plan's commit 5 (2026-08-28) to
exclude one specific case: "Combat, alliances, ACS, migration, referrals, NFT burns, the
ERC-20 market bridge" remains the non-goal for everything below, since none of these six
*separate contract entrypoints* are reached via `launchFleetMission`'s mission-type
argument at all — each is its own selector, and none of the six appear in `plan.py`,
`guard.py`'s tier map, or `allowlist.ts`'s signature lists (grepped, absent from all
three), unaffected by commit 5. Enabling any of these six requires a source change to
`_MIN_TIER_FOR_FUNCTION` and `allowlist.ts`, unconditionally — no policy flag reaches
them.

**This is now distinct from `launchFleetMission`'s own mission-type restriction**, which
commit 5 did partially widen: mission type 3 (Attack) is reachable via
`launchFleetMission` itself when `policy.actions.allow_combat` resolves `true` at tier 3
(`guard.py`'s `_COMBAT_MISSION_TYPES`, `allowlist.ts`'s `COMBAT_ALLOWED_MISSION_TYPES` —
see §1.1's `launchFleetMission` rows and `docs/SPEC.md` correction 70). Mission types 5
(AcsDefend) / 6 (Intercept) / 8 (AcsAttack) / 9 (DefenseHold) stay outside both mission-
type sets unconditionally, even at tier 3 — `launchAttackMission`/`joinAttackMission`
below are the entrypoints for AcsAttack specifically (via `joinAttackMission`) and remain
fully out of scope regardless of `allow_combat`, since `allow_combat` only widens
`launchFleetMission`'s own Attack (3) branch, not these separate selectors.

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

**Row count check** (updated 2026-08-17, Phase 5c/5b — `launchFleetMission` moved from §1.2 to
§1.1): 6 (§1.1, unique names — `launchFleetMission` counted once despite its 2 overload rows) + 0
(§1.2, now empty) + 1 (§1.3) + 6 (§1.4) + 6 (§1.5) + 6 (§1.6) + 13 (§1.7) + 19 (§1.8) + 3 (§1.9)
= **60 unique function names**, matching the `jq -u` count above. Counting table *rows* instead
(§1.1 contributing 7 rows — 5 single-row functions plus `launchFleetMission`'s 2 overloads) gives
6 - 1 + 2 = 7 for §1.1, so 7 + 0 + 1 + 6 + 6 + 6 + 13 + 19 + 3 = **61**, matching the raw ABI
entry count.

---

## Part 2 — Game surfaces

Surfaces not reducible to a single ABI entrypoint. Same status/what-it-would-take treatment.

| Surface | Status | What it would take / notes |
| --- | --- | --- |
| Mine selection ignores the payback score it computes | **known inconsistency (since Phase 2); the exact-tie case closed 2026-08-26 (`docs/SPEC.md` AC65)** | `select_building_candidate` picks the winning mine by walking `_mine_priority_order` — the pre-Phase-2 heuristic, `(level + 1) / (base_rate × multiplier)`, lower first — and applies `rank_candidates` (the payback scorer) only to the *leftovers*, which become `Action.alternatives`. So a tick can display an economic ranking that disagrees with the decision it just made, without saying which one drove the choice. Preserving the old walk was Phase 2's explicit acceptance criterion, so making payback the *primary* selector remains deliberately out of scope — that would need its own phase and a re-pin of the mine fixtures, not an incidental change. One consequence of the heuristic itself is still worth knowing: metal's base rate (30) is 1.5× crystal's (20), so mine levels converge on roughly 3:2 metal:crystal. The other former consequence — an **exact** density tie breaking by dict-declaration order, which always favored `METAL_MINE` — is no longer accurate as written: `_mine_priority_order` now takes an optional `tie_break: Mapping[int, float]` (`select_building_candidate` supplies each tied mine's already-computed payback score), and breaks an exact tie by ascending payback instead, only falling back to dict order when *neither* tied mine has a real payback score to compare. This doesn't touch the broader inconsistency this row describes (payback still never drives a *non-tied* pick) — it only replaces one incidental tie-break rule with a deliberate one, using a number already computed for the same family, the same move `generate_unlock_chain_candidates` already makes for unlock-step ties. The preservation criterion this row cites is asserted in `docs/SPEC.md` §9 and this module's own top-of-file docstring (`candidates.py:19-24`) — not `select_building_candidate`'s own docstring, which doesn't exist; that citation in an earlier version of this row was wrong. |
| No lever biases mine upgrades against research | **not planned** | Mines are ladder rung 6 (building queue), research is rung 7 (research queue) — independent queues, so a mine proposal never starves research: the next tick's building queue is busy, rung 6 does not fire, and research is proposed. The real constraint is one action per tick. But there is no policy field that shifts precedence between the two, and none that re-ranks mines: `research_priority` orders *which* technology once rung 7 fires, and `building_priority` biases infrastructure over mines (with the starvation caveat noted below). An operator who wants research favoured over mine upgrades has no knob today. |
| `building_priority` can starve the economic band | **known footgun (since Phase 3)** | When `policy.strategy.building_priority` is set, the first *unlocked* infrastructure building on the list wins the building band outright every tick, affordability notwithstanding (`candidates.select_building_candidate`). Intent-wins is the deliberate design — a declared priority is meant to beat a scored mine — but the consequence is not obvious: a declared priority that stays unaffordable indefinitely blocks every economic pick for as long as it stays unaffordable, rather than yielding to the next candidate. The affordability ETA in the guard's BLOCK detail is the only signal an operator gets. Raised by the 2026-08-17 judge pass as a design gap, not a defect; recorded rather than changed, since changing it would invert the intent-wins rule the field exists for. |
| Fusion Reactor as an energy-first *substitute* | **closed (docs/SPEC.md correction 66)** | Fusion Reactor is a scored candidate in `generate_energy_candidates` (wins the economic band on its own merits) **and** is now wired into `candidates._cheapest_energy_choice`, the comparison that picks the substitute when a mine upgrade is energy-blocked — a three-way comparison against Solar Plant and Solar Satellite by cost per energy point, with Fusion Reactor's cost amortized over a fixed `_ENERGY_UPKEEP_AMORTIZATION_HOURS = 24` window of its own ongoing deuterium upkeep first (the only one of the three with a recurring cost, via `calc.fusion_deuterium_upkeep`). On `tests/fixtures/planet_hot.json` — the fixture this comparison is pinned against — Fusion Reactor now wins outright (51.64/energy point amortized, versus Satellite's 83.33 and Solar Plant's 211.88). |
| Combat & battle resolution | out of scope — policy, not technical (§1.6 above) | The formulas (`shipBattleAttack`/`Shield`/`Hull`, rapidfire tables) are already extractable from `VeydriftCatalog.sol` the same way the tech tree is. What's missing is the deliberate friction removal across `plan.py`/`guard.py`/`allowlist.ts` described in `AGENTS.md` §5, not a research problem. |
| Espionage | not found | No espionage/spy-probe entrypoint, building, or technology found anywhere in the pinned ABI or contract source (`VeydriftTypes.sol`, `VeydriftCatalog.sol` — searched for `probe`/`scan`/`espionage`/`recon`, no matches). Recorded as "not found" rather than "deferred" — it may simply not exist as a mechanic in this version of Veydrift, unlike classic OGame-likes. |
| Raid/loot model | blocked | `protectedResources` semantics are unconfirmed — `docs/NOTES.md` §6 (`docs/NOTES.md:121-133`): raidable resources observed as exactly 50% of held while `protectedResources` independently read 0, meaning it tracks something other than a simple floor. `docs/SPEC.md` §1 lists "a raid-profitability model" as an explicit non-goal for this reason. `models.py`'s `PlanetSnapshot.protected_resources` field comment says the same: "Semantics UNCONFIRMED... Do not build a loot model on this." |
| Debris fields & recycling | **recycling closed 2026-08-28; the ABI's own read function stays unused** | Harvest is now live both locally (own planet, 2026-08-28) and against a foreign target (same date) — `candidates.generate_harvest_candidates`/`generate_foreign_harvest_candidates`, sourced from `/universe/galaxies/{g}/systems/{s}`'s and `/raid-finder/debris`'s backend-indexed `debrisField`/`debris` data respectively, never a live contract call. `debrisField(uint256)` — the ABI's own `NONPAYABLE_READ_FUNCTIONS` entry (§1.4), implemented at `VeydriftPlanetManagementModule.sol:281-284`, returning `(metal, crystal)` per planet via `simulate` — remains genuinely unused: the backend-indexed routes already serve the same data without a live call, so nothing in this codebase has needed it. |
| Moon acquisition & jump gates | deferred | Seven entrypoints belong here — `grantMoonResources`, `spendMoonResources`, `clearMoonState`, `moveMoonGateShips`, `setMoonShipCount`, `settleDuePlayerColonizeArrivals`, `launchBodyFleetMission` (all in §1.8) — plus `read.py`'s standalone `moon` command (`read.py:506-519`), which `Snapshot`'s composition never calls (see the data-surfaces subsection below). A moon-aware planner phase would need to both read this data into `Snapshot` and build the write paths. |
| Space dock repair | deferred, and upstream-dormant | `setSpaceDockSystem` (§1.8) carries the contract's own doc-comment that the live deployment never sets a Space Dock system contract at all, so ship-repair mechanics are inert **on the deployed contract itself** — not just unbuilt in this repo (`VeydriftGame.sol:283-286`). |
| Interdimensional Rift Stabilizer | mechanics unpublished, hard-capped at level 1 | `Building.InterdimensionalRiftStabilizer` exists in the catalog (id 15, cost tuple `(8_000, 8_000, 4_000)` at `VeydriftCatalog.sol:30`); the level-1 hard cap is documented at `docs/NOTES.md:540`, not independently re-derived from a level-cap function in this pass — no such function was found by name in `VeydriftCatalog.sol` during this ledger's research. |
| Terraformer field gain | deferred | `Building.Terraformer` exists in the catalog with a cost tuple `(0, 50_000, 100_000)` at `VeydriftCatalog.sol:27`; the fields-gained-per-level formula was not traced in this pass. |
| Expeditions | not found | No expedition-related entrypoint or type found in the pinned contract source during this pass (searched `VeydriftTypes.sol`/`VeydriftGame.sol`/module contracts for "expedition", no matches). Recorded as "not found," same posture as espionage above. |
| Alliances | out of scope — separate contract, separate non-goal | `VeydriftAllianceSystem.sol` is its own deployed contract (`createAlliance`, `inviteMember`, `acceptInvite`, etc. starting at `VeydriftAllianceSystem.sol:339`) with its own ABI — not part of `VeydriftGame.701bed3.json`, and not pinned or read anywhere in this repo. `docs/SPEC.md` §1 lists alliances as an explicit non-goal. |
| Highscore/score model | API-only, no contract entrypoint | `/highscores` is a backend API route (`skills/veydrift-agent/references/api-routes.md:123`, `read.py`'s `highscores` command at `read.py:713-729`) — 1-2+ MB, `--out`-mandatory, never composed into `Snapshot`. No `playerScore`/highscore function was found in the pinned `VeydriftGame` ABI or contract source. |
| Referrals & migration | out of scope — explicit non-goal | `docs/SPEC.md` §1: "migration, referrals" listed by name. Covers `reserveMigrationCoordinates`, `releaseExcessResourceReserves` (§1.8), `importMigratedState(WithReferral)`, `settleFirstPlanetWithReferral`, `startPlanetWithReferral` (§1.5). |

### Data surfaces `read.py` fetches and then discards

`vd read snapshot`'s composition (`read.py:833+`) calls only health + overview +
infrastructure + research + shipyard + defenses. `read.py` has standalone commands for other
routes that `snapshot` never touches, so `Snapshot` (the frozen model every downstream module
reads) never carries this data even though it is one CLI call away:

| Data | Where it's fetched (unused by `Snapshot`) | Why it matters |
| --- | --- | --- |
| Moon state | `read.py`'s `moon` command, `read.py:506-519` | Needed for any moon-acquisition planner path (Part 2 above) |
| The player's own outgoing/returning missions | `read.py`'s `missions` command, `read.py:556-569`; also `fetch_fleet_visibility()` (new 2026-08-17) | **No longer the reason rung 3 is dead** — `tick.py`'s `_resolvable_mission_ids` now reads `outgoing` from `/wallet/{addr}/fleet-visibility` directly and feeds rung 3 (see §1.1 above). Still true that this data has no home on `Snapshot` (`models.py` frozen) — the fix works around that, not through it; a future `models.py` change could still promote this to a first-class field if a planner rung ever needs the *full* mission list (not just resolvable ids). |
| Universe/neighbourhood data (other players, debris, moons, migration reservations near a planet) | `read.py`'s `universe` command, `read.py:666-692` | **Partially closed 2026-08-17, further narrowed 2026-08-28**: `PlanetSnapshot.archetype` is now populated for a planet's *own* slot, opt-in via `read.snapshot`'s new `universe_cadence_hours` (wired from `policy.cadence.universe_hours`, cadence-gated via `http.py`'s existing disk cache). `debrisField` for a planet's *own* slot is now also read, but out-of-band like fleet-visibility/resolvable-mission-ids — `tick._own_planet_debris` fetches it directly (`read.fetch_universe_system`, new 2026-08-28) and passes it into `plan_next_action` as an explicit parameter, never landing on `Snapshot`/`PlanetSnapshot` itself (still frozen). The rest of this row stands: `occupiedBy`/`debrisField`/`hasMoon`/`migrationReservation` for *other* slots in the neighbourhood still have no home anywhere the planner can see them — still relevant for a future colonisation-target-selection planner (see §1.2/§1.5 above). |
| Moon state (buildings, resources, ship counts) | `read.py`'s `moon` command, `read.py:506-519` | **Not closed.** Moon buildings carry `key`/`label` unlike every other surface (per this phase's own brief) and there is no field on `PlanetSnapshot`/`Snapshot` (frozen `models.py`) to carry that differently-shaped data without forcing it into `Entity` and losing the distinction. Investigated 2026-08-17 (Phase 5) and left undone for the same `models.py`-frozen reason as §1.2's `launchFleetMission` rows. |
| Tactical/combat data from `/planets` | `read.py`'s `planets`-adjacent commands; `highscores`' "full planet+tactical payload" note at `read.py:720` | Would be needed for any combat-adjacent planning, itself out of scope (Part 1 §1.6) |
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
| ~~`distance`~~ | Coordinate-pair distance (`"G:S:P"` or tuple) | **Live since 2026-08-17 (Phase 5c, this change)** — `candidates.generate_transport_candidates` calls it to compute the origin→destination distance for `mission_fuel`/`travel_seconds`. Moved to §1.1's `launchFleetMission` rows; kept struck through here rather than deleted, matching this table's own "reconstructed once" convention. |
| ~~`travel_seconds`~~ | Mission travel time from distance + speed | **Live since 2026-08-17** — `candidates.generate_transport_candidates`, informational-only (feeds `Action.rationale`, not a guard/plan decision). |
| ~~`mission_fuel`~~ | Deuterium fuel cost for a mission | **Live since 2026-08-17** — both logistics generators (`generate_transport_candidates`/`generate_harvest_candidates`), to bound cargo via `available_cargo`. |
| ~~`available_cargo`~~ | Cargo capacity minus fuel cost | **Live since 2026-08-17** — same two generators; the actual cap on `Action.cargo`. |
| ~~`max_planets`~~ | `1 + astrophysics_level` colony cap | **Used by the planner and the guard layer both, as of 2026-08-28.** `candidates.generate_colonize_candidates` (commit 4 of the launch-actions plan, gated on `policy.strategy.colonize`) now proposes a colonisation `Action` — target selection reads `/universe/galaxies/{g}/systems/{s}` for free slots in the wallet's own systems, ranked by live `deuteriumMultiplierBps`. `guard.py`'s `_gate_mission_type` calls `calc.max_planets` directly (`_colony_cap_violation`) to `BLOCK` a Colonize mission before send when `Snapshot.owned_planet_count` (now also folding in in-flight `Outbound` Colonize missions, closing a real race-condition gap — see `docs/SPEC.md` correction 69) is already at or above the cap, independent of whatever proposed the action (manually or otherwise). Its formula is independently confirmed live: round 3's `PlanetLimitReached(uint256)` revert (`VeydriftColonizationModule.sol:289-301`, `limit = 1 + astrophysicsLevel`) fired at exactly `limit = 10` for an account with Astrophysics level 9 and 10 owned planets — matching this function's formula exactly. **Round 2 (2026-08-19)** verified the *encoding* half of Colonize: `tick.py`'s `_encode_colony_target` was round-trip tested in Python against `VeydriftColonizationModule.sol:472-490`'s actual `_encodeColonyTarget`/`_decodeColonyTarget` for four coordinates including a boundary case, all exact matches (`skills/veydrift-wallet/references/fork-testing.md` §8.1). **Round 3 (2026-08-19) closed the remaining slot-claiming half**: a real Colony Ship was produced, a Colonize `launchFleetMission` was sent to a scanned-available coordinate (`2:477:9`), and `resolveFleetMission` resolved it — `isCoordinateAvailable`/`planetCountOf` confirmed the exact targeted slot flipped `true`→`false` / `10`→`11` (`fork-testing.md` §10.9). Packing math and slot-claim behavior are now both confirmed live, and the planner-side "where to colonise" logic is no longer unbuilt — though the *silent-resolve-failure* hazard both rounds' own findings imply (a resolve receipt reading `success` even when the slot-claim fails at arrival) has only its pre-flight half closed; post-resolve verification remains a documented, not-yet-built gap (`docs/SPEC.md` correction 69). |

**New in Phase 5c, not in this table because they're used, not unused**: `calc.py` gained
`SHIP_CARGO_CAPACITY` (a fixed lookup table), `ship_fuel_consumption`, `ship_speed`,
`ship_movement_stats` — all called by both logistics generators to size a mission's fleet, fuel
and cargo. Sourced directly from `VeydriftCatalog.sol` at the pinned commit, not the banned
"cost-scaling function" category (see `calc.py`'s own comment on the distinction, and
`docs/SPEC.md` §5.4's Phase 5c note).
| `solar_crossover_table` | Smallest Solar Plant level whose energy alone covers same-level mines | Currently used only by the standalone `vd calc crossover` CLI command, not by the planner — `candidates._cheapest_energy_choice` (moved from `plan.py`'s `_energy_candidate` in Phase 2) already does a live, per-planet version of this comparison directly |
| `deuterium_multiplier_bps` | Temperature-derived deuterium multiplier | Used only via `candidates.py`'s live-multiplier reads today (moved from `plan.py` in Phase 2; `PlanetSnapshot.deuterium_multiplier_bps`, sourced from the API); this pure recomputation isn't called because the live value is already provided (docs/SPEC.md §5.4: prefer live data over recomputing it) |
| `max_temp_from_bps` | Inverse of `deuterium_multiplier_bps`, diagnostic only per its own docstring | Cross-checking a reported multiplier against a reported `temperature`, not planning |
