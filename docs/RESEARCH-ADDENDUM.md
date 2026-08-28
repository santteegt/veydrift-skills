# Veydrift — Research Addendum (2026-08-11)

Corrections and additions to `NOTES.md`, `veydrift-agent-resources.md`, `veydrift-agent-prompt.md`
and `veydrift-briefing.html`, derived from the **contract source** and the **backend source**, not
from probing.

Repo clone: `/Users/santteegt/GitRepositories/clones/veydrift`
Deployed commit: `701bed3578cff4d134657c714c599dbdb55a4b6a` · main HEAD at clone time: `84e468f`

---

## 1. ABI hash — verified, exact match

`/runtime-config` → `backend.build.deploymentAbiHash` =
`sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99`

Reproduced locally. The derivation (from `scripts/veydrift-deployment-manifest.mjs:129-135`) is:

```
sha256( JSON.stringify( out/VeydriftGame.sol/VeydriftGame.json .abi ) )
```

i.e. compact JSON, no whitespace, key order as emitted by `forge build`.

| Build | ABI hash | Matches live? |
| --- | --- | --- |
| `701bed3` (deployment commit) | `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99` | **yes** |
| `84e468f` (main HEAD) | `sha256:361b1c94bf532b97b9971ad41c5be1b4d952710f7c56f046f3999b520179d2a8` | no |

**Consequence: building the ABI from `main` gives you the wrong ABI.** Pin to the
`deploymentCommit` that `/runtime-config` reports, rebuild, and verify the hash before any write.
Foundry settings that matter for reproducibility: `solc 0.8.28`, `optimizer_runs = 1`, `via_ir = true`,
`cbor_metadata = false`, `bytecode_hash = "none"` (`packages/contracts/foundry.toml`).

### 1.1 main has already drifted from the deployed contract

| Only on `main` (does **not** exist on the deployed contract) | Only on deployed (deleted on main) |
| --- | --- |
| `playerScore(address)` | `firstPlanetOf(address)` |
| `settleProductionUntil(uint256,uint64)` | `hasFirstPlanet(address)` |
| `settleAllianceMembershipBoundary(address)` | `previewFirstPlanet(address)` |
| `depositPaidAllianceInviteFee()` | `FLEET_RECALL_COST_BPS()` |
| `startPlanetWithAllianceInvite(bytes32,uint64,uint8,bytes32,bytes32)` | 3 × `SafeCast*` errors |
| event `AllianceBonusCreditedToPlanet(...)` | |

> **Correction to `NOTES.md` §13.5.** It lists `playerScore` among "useful read functions for an
> agent (all public views on the game proxy)". `playerScore` is **not on the deployed
> implementation**. A call to it reverts. Use `GET /wallet/{addr}/highscore` instead.

---

## 2. The full API route list (from `apps/backend/src/server.ts`)

`NOTES.md` §1 and §11 flag "a defense endpoint exists under a name I didn't guess" and "extract the
route list from the frontend bundle" as open work. Both are resolved — the route table is in the
backend source at `server.ts:141-152` and `:2841-2857`. All probed live and returning `200`.

### Wallet routes — `/wallet/{addr}/...`

| Route | Query params | Status | In prior docs? |
| --- | --- | --- | --- |
| `settlement` | — | 200 | yes |
| `queues` | `planetId` | 200 | yes |
| `infrastructure` | `planetId` | 200 | yes (param was not) |
| `research` | `planetId` | 200 | yes (param was not) |
| `shipyard` | `planetId` | 200 | yes (param was not) |
| **`defenses`** | `planetId` | 200 | **no — this is the missing one** |
| **`overview`** | `planetId` | 200 | **no** |
| **`planets`** | — | 200 | **no** |
| **`highscore`** | — | 200 | **no** |
| **`moon`** | `planetId` | 200 | **no** |
| **`activity`** | `includeProjected`,`page`,`pageSize`,`since` | 200 | **no** |
| **`fleet-visibility`** | `archive` | 200 | **no** |
| **`missile-attacks`** | `page`,`pageSize`,`planetId` | 200 | **no** |
| `missions` | `filter`,`missionNumber`,`missionType`,`planetId`,`status`,`page`,`pageSize` | 200 | partially |
| `referrals/history` | `page`,`pageSize` | 200 | no |

### Top-level routes

| Route | Status | Notes |
| --- | --- | --- |
| `/health`, `/runtime-config`, `/highscores`, `/graphql` | 200 | known |
| `/universe/system`, `/universe/systems` | 200 | known |
| **`/battle-reports`** | 200 (~60 KB) | **new** — resolves the `protectedResources` open question in `NOTES.md` §6 |
| **`/raid-finder/debris`** | 200 | **new** — server-side target selection |
| **`/raid-finder/rifters`** | 200 | **new** |
| **`/missions`** | 200 | **new** — global, not wallet-scoped |
| **`/randomness-readiness`** | 200 | **new** |
| **`/chain/events`** | 200 but slow (>2 min uncapped) | **new** — needs paging params; do not call naively |
| `/alliance-invites/*`, `/referrals/*` | — | write-adjacent, out of mandate |
| `/stats` | **410 Gone** | retired |

### Two routes that change the agent design

- **`fleet-visibility`** returns `{ incoming, outgoing, returning, joinableAttacks, completedMissions,
  battleReports }`. `incoming` is the **hostile-fleet detection surface**. The agent prompt escalates
  on "any incoming hostile fleet detected against 664" but had no endpoint to detect it with. Now it does.
- **`overview?planetId=`** bundles `settlement` + `planets` + `queues` + `fleetVisibility` in one call.
  Useful, but it does **not** include `infrastructure`/`research`/`shipyard`/`defenses`, so it does not
  collapse the read loop to a single request.

### Multi-planet is already supported

Every per-planet route takes `?planetId=`. The prior docs assume a single planet. Build the read
layer planet-scoped from day one — `/wallet/{addr}/planets` enumerates them.

---

## 3. Canonical enums — from the contract, not inferred

`NOTES.md` §2 derived the Building / Technology / Ship maps by cost-fingerprinting. All three are
**confirmed correct** against [`packages/contracts/src/libraries/VeydriftTypes.sol`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol). Two enums were
previously unknown.

### `Defense` (new — [VeydriftTypes.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol))

| id | Defense | id | Defense |
| --: | --- | --: | --- |
| 0 | RocketLauncher | 5 | IonCannon |
| 1 | LightLaser | 6 | PlasmaTurret |
| 2 | HeavyLaser | 7 | LargeShieldDome |
| 3 | SmallShieldDome | 8 | AntiBallisticMissile |
| 4 | GaussCannon | 9 | InterplanetaryMissile |

Note **SmallShieldDome (3) sorts before GaussCannon (4), and IonCannon is 5** — not the OGame order.

### `FleetMissionType` (new — [VeydriftGameStorage.sol:174](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGameStorage.sol#L174))

| id | Mission | id | Mission |
| --: | --- | --: | --- |
| 0 | Transport | 5 | AcsDefend |
| 1 | Deploy | 6 | Intercept |
| 2 | Colonize | 7 | MissileAttack |
| 3 | Attack | 8 | AcsAttack |
| 4 | Harvest | 9 | DefenseHold |

`Intercept` and `DefenseHold` appear in neither `docs.md` nor the prior notes.

### `Resource`

`0` Metal · `1` Crystal · `2` Deuterium · `3` Energy — the `uint8` in `depositMarketResource`,
`requestMarketResourceWithdrawal`, `finishMarketResourceWithdrawal`.

### The 14-slot fleet tuple is **not** the Ship enum

Every fleet entrypoint takes `(uint32 × 14)`, but there are **16** ships. `SolarSatellite (9)` and
`Crawler (15)` cannot fly and are omitted ([VeydriftFleetFuel.sol:73-87](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFleetFuel.sol#L73-L87)). Tuple order:

```
0 SmallCargo   1 LightFighter  2 Recycler    3 ColonyShip   4 LargeCargo
5 HeavyFighter 6 Cruiser       7 Battleship  8 Bomber       9 Destroyer
10 Deathstar   11 Battlecruiser 12 Reaper    13 Pathfinder
```

**Indices 9-13 are shifted by one relative to the Ship enum.** Building a fleet tuple by indexing
with the Ship id silently sends Destroyers where Solar Satellites were meant. This must be a
single tested conversion function, never hand-written at a call site.

---

## 4. Write entrypoints on the deployed contract

61 non-view functions. The ones inside a sane agent mandate:

| Action | Signature |
| --- | --- |
| Building upgrade | `startBuildingUpgrade(uint256 planetId, uint8 building)` |
| Research | `startResearch(uint256 planetId, uint8 technology)` |
| Ships | `startShipProduction(uint256 planetId, uint8 ship, uint32 quantity)` |
| Defense | `startDefenseProduction(uint256 planetId, uint8 defense, uint32 quantity)` |
| Permissionless resolve | `resolveFleetMission(uint256 missionId)` |
| Settle production | `settlePlanet(uint256 planetId)` |
| Fleet return | `completeFleetMissionReturn(uint256 missionId)` |

**Correction (2026-08-12).** An earlier draft of this section, and `NOTES.md` §13.5, state that all
four `finish*` functions are back-compat no-ops. Only one is. Verified against
`git show 701bed35:packages/contracts/src/VeydriftGame.sol`
([or the same source, browsable](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol)):

| Function | Body | Behaviour |
| --- | --- | --- |
| `finishBuildingUpgrade(uint256)` | `_touchPlayer` · `_requirePlanetOwner` · `_settleResources` | genuine no-op — settles and returns |
| `finishResearch()` | `_touchPlayer` · `_delegateToPlanetManagementModule()` | **delegates; can revert** |
| `finishShipProduction(uint256)` | `_touchPlayer` · `_delegateToColonizationModule()` | **delegates; can revert** |
| `finishDefenseProduction(uint256)` | `_touchPlayer` · `_delegateToColonizationModule()` | **delegates; can revert** |

The three delegating forms revert with `QueueInactive` / `QueueNotReady` rather than silently
succeeding. So the practical advice is unchanged — an agent should not call any of them, because
lazy settlement means `startBuildingUpgrade` completes a finished upgrade on its own — but the
*reason* differs: three of the four are not harmless, they are a wasted transaction that reverts.

### 4.1 Trap — "read" functions that are not `view`

These are `nonpayable` in the ABI because they lazily settle before returning:

```
attackProtectionStatus  collectResources  debrisField  maxRaidLoot
protectedResources      raidableResources
```

`viem.readContract` / `ethers.callStatic` will reject or mislead. Use `simulateContract` /
`eth_call` explicitly. **Never send them as transactions** — you pay gas for a read.

### 4.1a Planet 664 cannot be transferred *or* abandoned

`NOTES.md` §13.2 records that `abandonPlanet(uint256)` exists and describes it as a way to "give a
planet up" — the one exit path the game offers. There is a guard it does not mention:

```solidity
// VeydriftPlanetManagementModule.sol:150
if (homePlanetOf[msg.sender] == planetId) revert CannotAbandonHomePlanet();
```

Planet 664 is this wallet's home planet and its only planet. So it can be neither transferred (no
transfer function exists at all) nor abandoned. It is unconditionally bound to
`0x224a…fa0f` for as long as the account exists. This tightens, rather than merely restates,
the custody conclusion in `NOTES.md` §13.4 — and it is the governing constraint on wallet-provider
choice, since any provider issuing a new address cannot hold the planet. See
`wallet-provider-research.md`.

### 4.2 Trap — `launchFleetMission` is overloaded

```solidity
launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)
launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)
```

Both live on the deployed ABI. viem and ethers both require explicit signature disambiguation;
selecting by name alone is ambiguous and will throw or pick the wrong one.

### 4.3 Trap — Transport and Deploy require the target to also be an owned planet

New (2026-08-19, fork-testing round 2 — `skills/veydrift-wallet/references/fork-testing.md` §9).
Not previously documented anywhere in this repo. Read directly from source,
`VeydriftGameplayModule.sol`'s `_launchFleetMission`, immediately after the `_missionMovement`
call:

```solidity
if (missionType == FleetMissionType.Transport || missionType == FleetMissionType.Deploy) {
    _requirePlanetOwner(targetPlanetId);
}
```

Confirmed by reproduction, not just by reading: building a Transport `launchFleetMission` from the
project's own planet 664 to a real, populated third-party-owned planet (id 23) reverted
`NotPlanetOwner()` (selector `0xab2bcfd3`) — both at `build`'s gas-estimation step and at
`simulate`. This is **not a bug and not a testing limitation** — it is the actual contract rule:
Transport and Deploy are intra-empire logistics only, never point-to-point cargo between arbitrary
players. Harvest and Colonize carry **no** such check — the `if` above is scoped to exactly those
two mission types, confirmed by the same source read.

**Direct, permanent consequence**: the project's own reference account has exactly one planet
(`homePlanetId: "664"`, confirmed via `GET /wallet/{addr}/planets`), so it can **never** exercise
Transport or Deploy, structurally, regardless of ship inventory or resources — not until it
colonizes a second planet. This also retroactively validates
`skills/veydrift-agent/src/veydrift_agent/candidates.py`'s `generate_transport_candidates`
requiring ≥2 owned planets before proposing Transport (`candidates.py:1913-1930`) — that
precondition is not overcautious, it is the literal contract requirement, now confirmed
independently rather than just inferred from the planner's own docstring.

To exercise selectors 6/7 (`launchFleetMission`'s two overloads) live at all, round 2 impersonated
a different real, multi-planet player (`0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1`, 10 owned
planets, found via `GET /highscores`) instead — the same no-real-key impersonation technique used
throughout this fork-testing effort, harmless to the impersonated account since nothing leaves the
local fork. Both a 6-arg Transport and a 7-arg Transport (explicit `speedPercent`) sent between two
of that account's own planets (23 → 184) succeeded, `status: "success"` each.

**Round 3 (2026-08-19) exercised Colonize on the same 6-arg overload, with the same account,
closing the one mission type §4.3/§4.4 hadn't yet reached live.** A Colony Ship was produced
(planet 23 already had Shipyard 10 and Impulse Drive 6, both above the production thresholds — no
unlock chain needed for this account specifically), then a Colonize `launchFleetMission` was sent
to a scanned-available coordinate (`2:477:9`), and `resolveFleetMission` resolved it — the first
time that selector has been live-sent by this codebase rather than confirmed by source alone.
`isCoordinateAvailable(2,477,9)`/`planetCountOf` read `true`/`10` before the send and `false`/`11`
after, confirming the exact targeted slot was claimed. One real game rule was hit and worked
around along the way (the account was already at its Astrophysics-derived colony cap,
`PlanetLimitReached`; a single `anvil_setStorageAt` write bumped the on-chain Astrophysics level by
one to unblock the test — scaffolding for that unrelated precondition, not a change to the
Colonize logic itself). Full sequence: `skills/veydrift-wallet/references/fork-testing.md` §10.

### 4.4 Fuel formula, distance, and ship-movement-stats — confirmed against a real chain-emitted event

New (2026-08-19, fork-testing round 2). `calc.distance`, `calc.ship_movement_stats`, and
`calc.mission_fuel` had previously only been derived from `docs.md`'s published formula set (§5
below) and cross-checked against each other — never against a real transaction. `docs.md` gives no
mechanism for observing the true fuel cost of a sent mission directly; the naive approach
(deuterium balance before/after) turned out to be unreliable — it's contaminated by production
accruing in the real-time gap between the two reads, so a first attempt at that method produced a
noisy, non-matching result (~1 deuterium observed vs. ~8 predicted) and should not be used for this
check. The reliable, authoritative source is the transaction's own event:
`event FleetMissionCargo(uint256 indexed missionId, uint128 metal, uint128 crystal, uint128
deuterium, uint128 fuelCost)` (`VeydriftGameStorage.sol:602-608`), decoded directly from the
receipt's logs.

For the 6-arg Transport in §4.3 above (origin `2:477:7` → target `2:477:3`, `{smallCargo: 2,
largeCargo: 1}`, distance 1020, using the impersonated player's real drive-tech levels —
Combustion Drive 6, Impulse Drive 6, Hyperspace Drive 7, read live via
`technologyLevel(address,uint8)`): **event `fuelCost = 10`**, `calc.mission_fuel`'s prediction
using those same inputs: **10**. Exact match — the first time this codebase's fuel/distance/speed
formulas have been confirmed against a real chain observation rather than merely derived from
contract source. Full command sequence:
`skills/veydrift-wallet/references/fork-testing.md` §8.3.

### 4.5 Colony-target packing — now round-tripped through a real send, not just Python-side math

`tick.py`'s `_encode_colony_target` was previously verified only by reimplementing
`VeydriftColonizationModule.sol:472-490`'s `_encodeColonyTarget`/`_decodeColonyTarget` shifts/masks
in Python and round-tripping four coordinates against that reimplementation
(`skills/veydrift-wallet/references/fork-testing.md` §8.1) — correct against source, but never
exercised against the actual contract. Round 3 (2026-08-19,
`skills/veydrift-wallet/references/fork-testing.md` §10.5) sent the packed value
`_encode_colony_target("2:477:9")` produces
(`57896044618658097711785492504343953926634992332820282019728792003956598496521`) as the real
`targetPlanetId` argument of a live Colonize `launchFleetMission`, and confirmed via before/after
`isCoordinateAvailable`/`planetCountOf` reads that it claimed exactly the `2:477:9` slot. This
strengthens the existing verification rather than superseding it — the Python-side round-trip
against source still stands as the check for coordinates never actually sent on a fork.

### 4.6 `gameMaintenance` / `pausedSince` — first observed live, 2026-08-20

`GET /health` carries a `gameMaintenance` block (`{paused, observedAt, pausedSince,
pauseAgeSeconds}`) and a `readiness.degradationReasons` array, neither previously
documented anywhere in this project. An agent session checking live status on 2026-08-20
found the game genuinely paused for chain-side maintenance
(`gameMaintenance.paused: true`, a real `pausedSince`, `readiness.degradationReasons:
["game_paused"]`) — a one-off, hand-read observation, not something any codepath in
`veydrift-agent` acted on before this. A follow-up live fetch the same day/next (game no
longer paused) confirmed the field's normal shape:

```json
"readiness": { "ready": true, "degraded": false, "degradationReasons": [],
  "gamePaused": false, "gamePauseAgeSeconds": 0 },
"gameMaintenance": { "paused": false, "observedAt": "2026-08-20T22:59:26.727Z",
  "pausedSince": null, "pauseAgeSeconds": 0 }
```

`gameMaintenance` is **always present**, not absent when not paused — the not-paused
shape above, not an omitted key, is the normal case. `readiness` separately carries its
own flattened `gamePaused`/`gamePauseAgeSeconds`, redundant with `gameMaintenance`.
`degradationReasons` is confirmed genuinely free-form: `tests/fixtures/
health_unhealthy.json` already carries a different real reason ("Upstream RPC unfinished
requests are growing or stale."), captured independently before this observation.

Also notable: the same 2026-08-20 check found `/health`'s top-level `ok: false` for a
reason unrelated to any pause (`randomnessReadiness.ready: false`), while
`readiness.ready`/`degraded` were both fine — the existing `ok`/`readiness.ready` health
check is a broad, multi-cause signal, and a game-pause fact needs its own dedicated
signal rather than folding into it. This is a single-capture observation (one real pause,
one real not-paused response) — `skills/veydrift-agent/tests/fixtures/health_paused.json`
is a hand-synthesized fixture built from the not-paused capture, not a second independent
live pause. `skills/veydrift-agent/references/api-routes.md` §3.1 has the full field-level
writeup; `skills/veydrift-agent/references/guardrails.md`'s `game_paused` gate and
`plan.py`'s rung `1b` are the two consumers.

**2026-08-22 follow-up: the `randomnessReadiness`-only degradation above turned out to be
persistent, not a single-capture blip, and served via HTTP 503 specifically.** A `vd tick`
report claimed `/health` returning 503 with `ok:false` meant "the CLI's health gate trips
on the top-level `ok:false` regardless" of cause — tracing the actual code path showed
this was a misdiagnosis: `http.fetch()` raises on any HTTP ≥500 status **before the JSON
body is ever parsed**, so `_health_ok()` (the function that actually checks
`ok`/`readiness.ready`) was never invoked in that failure mode; the abort happened at the
HTTP-status layer. A direct live re-check (two `curl`s, moments apart) confirmed the
underlying condition is real and ongoing, though: `/health` returning HTTP 503 with a
full, well-formed JSON body — `ok: false`, `readiness.ready: true`,
`readiness.degradationReasons: []`, `configurationReady: true`,
`gameMaintenance.paused: false`, `randomnessReadiness.ready: false`. At the time this was
written, `allow_combat` was read-and-ignored everywhere in this codebase (combat
unconditionally unreachable regardless of policy), so this degradation could never
affect what this codebase would propose — `veydrift-agent` 1.3.0 added
`Snapshot.combat_only_degradation()` (a structural, fail-closed positive-confirmation
check, not a reason-text allowlist) and `read._recover_health_body()` (narrow,
`/health`-only 5xx body recovery) so `plan.py`'s rung 1 and `guard.py`'s `health` gate
both proceed past this specific, verified-safe condition instead of blocking
indefinitely. **Correction, 2026-08-28 (launch-actions plan, commit 5):** `allow_combat`
is no longer unconditionally ignored — it now gates the Attack mission type at both
enforcement layers. **Further correction, 2026-08-28 (commit 6, same plan):**
`combat_only_degradation()`'s own exception logic is no longer unchanged either —
`guard._gate_health` now takes the action as a parameter and withdraws the exception
specifically for a combat (Attack) action (which requests VRF at launch and cannot
resolve while randomness is degraded), while every non-combat action still gets the
exception unchanged; a `generate_attack_candidates` generator now exists too, gated on
this same flag plus `snapshot.randomness_readiness.ready`. See `veydrift-agent`'s own
`models.py` (`RandomnessReadiness`'s docstring) for the precise, current framing.
Verified live end-to-end: `vd tick --dry-run`
against the still-degraded real API now builds a full snapshot and reaches the ordinary
ladder (NOOP: queues busy) instead of aborting. Full design:
`skills/veydrift-agent/references/guardrails.md`'s `health` gate section;
`skills/veydrift-agent/references/api-routes.md` §3.1 has the field-level writeup and the
exact `randomnessReadiness` shape (its own `reasons` array, distinct from
`readiness.degradationReasons`).

---

## 5. Formulas confirmed against `docs.md`

`docs.md` publishes the full formula set (fetched 2026-08-11). Everything `NOTES.md` §12.4 verified
(universe speed = 1, the three duration divisors) still holds. Formulas the prior docs did not carry:

```
fusion reactor energy   = floor(30 * L * (105 + energyTech)^L / 100^L)
fusion deut upkeep      = ceil(10 * L * 11^L / 10^L)
crawler boost bps       = min(effectiveCrawlers * 2, 5000)
effective crawlers      = min(crawlerCount, 8 * (metalL + crystalL + deutL))
travel seconds          = 10 + floor(floor(350 * sqrt(dist * 10 / slowestSpeed)) * 100 / (speedPct * universeSpeed))
mission fuel            = 1 + floor(sum(qty * shipFuel * dist * (1 + eff/100)^2) / 35000 + 0.5)
available cargo         = total ship cargo - mission fuel
moon chance bps         = min(floor((metalDebris + crystalDebris) / 100000) * 100, 2000)
```

Combat is a 6-round loop with `no loss if effectiveAttack <= effectiveShield / 100`. Debris is
**30%** of combined losses. These belong in a calculator script, not in prose an agent re-derives.

---

## 6. Open questions that remain open

1. **`protectedResources` semantics** (`NOTES.md` §6) — still unconfirmed, but `/battle-reports` now
   gives a corpus to settle it against. Worth one analysis pass; do not model loot until then.
2. **`/chain/events` paging contract** — the route exists but an unparameterised call did not return
   within 2 minutes. Find its params in `server.ts` before using it.
3. `Intercept` and `DefenseHold` mission mechanics are undocumented anywhere.
4. Everything in `NOTES.md` §12.9 about single-account/zero-state observation still stands. The
   account has taken **no actions**: all queues `null`, all levels 0, 1,000 M / 1,000 C / 0 D,
   unchanged since settlement at block 49666196.
