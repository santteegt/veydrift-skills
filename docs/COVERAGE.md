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
acceptance criterion).

---

## Part 1 — Write entrypoints (from the pinned ABI)

### 1.1 Implemented (5 functions, 5 rows)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `startBuildingUpgrade` | Yes — `candidates.select_building_candidate` / `select_storage_candidate` (moved from `plan.py`'s `_next_building_action` / `_storage_overflow_action` in Phase 2) | ECONOMY (`guard.py:73`) | ECONOMY (`allowlist.ts:39`) | implemented | — |
| `startResearch` | Yes — `candidates.select_research_candidate` (moved from `plan.py`'s `_next_research_action` in Phase 2), lowest-level-account-wide tie-break, filtered through `techtree.unmet()` (Phase 1) | ECONOMY (`guard.py:74`) | ECONOMY (`allowlist.ts:40`) | implemented | — |
| `startShipProduction` | Yes — `candidates.select_building_candidate`'s energy-fallback branch and `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_next_building_action`/`_shipyard_action` in Phase 2) | ECONOMY (`guard.py:83`, added 2026-08-12 — see comment there for the dead-config history) | ECONOMY (`allowlist.ts:49`) | implemented | — |
| `startDefenseProduction` | Yes — `candidates.select_shipyard_candidate` (moved from `plan.py`'s `_shipyard_action`, `allow_defense`, in Phase 2) | ECONOMY (`guard.py:77`) | ECONOMY (`allowlist.ts:43`) | implemented | — |
| `resolveFleetMission` | Yes in code (`plan_next_action` rung 3, unchanged by Phase 2 — a veto rung, not part of the candidate pipeline), **but dormant** — `tick.py`'s wired caller (`_run_tick`, ~line 773) never passes `resolvable_mission_ids` to `plan_next_action`, so the parameter defaults to `[]` and rung 3 never fires from the real entrypoint. Root cause: `Snapshot` (frozen, `models.py`) carries no list of the player's own fleet missions, so there is nothing to check "Resolving > 60s" against — see `plan.py`'s module docstring. | ECONOMY (`guard.py:75`) | ECONOMY (`allowlist.ts:41`) | **implemented (dormant)** | Populate `Snapshot`/a caller-supplied list from `/wallet/{addr}/missions` and wire it into `tick.py`'s `plan_next_action` call — tracked informally as the natural "Phase 5a" alongside 5b/5c below. |

### 1.2 Planned (2 rows — one function, two overloads)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)` (7-arg) | No — no rung in `plan.py` constructs it | OPERATOR (`guard.py:84`) | OPERATOR, mission types 0/1/4 only (`allowlist.ts:55-58`, decoded from calldata at `allowlist.ts:180-205`) | planned P5c | `tick.py:277-278`'s `_action_to_walletctl_json` has no branch for it — hits the `else: raise ValueError`. A fleet-mission planner rung plus the encoder branch is P5c. |
| `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)` (6-arg) | No | OPERATOR (`guard.py:84`) | OPERATOR, mission types 0/1/4 only (`allowlist.ts:55-58`) | planned P5c | Same as above; both overloads are allowlisted together, resolved by full signature never by name (`abi.ts`'s `resolveFunctionAbi`) — see `AGENTS.md` §7 trap #2. |

### 1.3 To remove (1 row)

| Function | Planner | Guard tier map | Wallet allowlist | Status | What it would take |
| --- | --- | --- | --- | --- | --- |
| `settlePlanet` | No — grepped `plan.py` and `candidates.py`, no rung/generator emits it | ECONOMY (`guard.py:76`) | ECONOMY (`allowlist.ts:42`) | **to remove, P5b** | Body is identical to `collectResources` at the pinned commit — `VeydriftGame.sol:120-128`: both are exactly `_touchPlayer(msg.sender); _collectPlanetResources(planetId);`. `collectResources` is correctly refused as a disguised read (`abi.ts`'s `NONPAYABLE_READ_FUNCTIONS`, §1.4 below); `settlePlanet` is the same operation but is allowlisted at ECONOMY on both sides and has a live `tick.py` encoder branch (`tick.py:276`) despite no planner rung ever proposing it. Remove the guard/allowlist/encoder entries together (keeping either without the others reopens the tier-map-agreement gap `test_tier_map_agrees_with_the_wallet_engines_allowlist` exists to catch). |

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
| `startPlanet` | No | not present | not present | out of scope — payable | Additional-planet colonization; also depends on `maxPlanets` (`calc.max_planets`, §3) and a colonization-target planner nobody has built |
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
| The player's own outgoing/returning missions | `read.py`'s `missions` command, `read.py:536-548` | This is *exactly* why `plan.py`'s rung 3 (`resolveFleetMission`) is dead in practice — see §1.1 above and `plan.py:19-25` |
| Universe/neighbourhood data (other players, debris, moons, migration reservations near a planet) | `read.py`'s `universe` command, `read.py:584-610` | `PlanetSnapshot.archetype` is set to `None` unconditionally in `_planet_snapshot()` (`read.py:689,709`) with the comment "not present on any route `snapshot` composes" — this is the reason why |
| Tactical/combat data from `/planets` | `read.py`'s `planets`-adjacent commands; `highscores`' "full planet+tactical payload" note at `read.py:638` | Would be needed for any combat-adjacent planning, itself out of scope (Part 1 §1.6) |
| `crawlerProduction` | Present in raw `/infrastructure` response (`docs/NOTES.md:28`), never read into `PlanetSnapshot` by `_planet_snapshot()` (`read.py:656-727`) | `calc.crawler_boost_bps` (Part 3) exists and is tested but has no live input path into the planner |
| `missileSiloLevel` | Documented as part of `/wallet/{addr}/defenses` (`skills/veydrift-agent/references/api-routes.md:275`), never read into `PlanetSnapshot` | Only matters once missile-attack/defense planning exists (out of scope, §1.6) |
| `launchableShips` | Present in raw `/shipyard` response (`docs/NOTES.md:30`), never read into `PlanetSnapshot` | Only matters once a fleet-mission planner (P5c, §1.2) exists |

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
| `production_per_hour` | Full per-hour Metal/Crystal/Deuterium output, applying multiplier → crawler boost → fusion upkeep → energy throttle in the contract's own order | A full economic simulator / lookahead planner |
| `crawler_boost_bps` | Crawler production boost in bps, capped per mine level and at 5,000 bps total | Same — needs live `crawlerProduction` data, which `Snapshot` doesn't carry (Part 2 above) |
| `storage_cap` | Per-level storage ceiling from the contract's literal lookup table (levels 0-50) | Independent verification of the API's own `storageCaps` field, or a fallback when it's missing |
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
