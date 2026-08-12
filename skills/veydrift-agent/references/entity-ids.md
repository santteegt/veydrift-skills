# Entity IDs — the six canonical enums

**Owned by:** WP2 (`ids.py`). **Source of truth:** the *deployed* contract, read directly
with `git show 701bed3578cff4d134657c714c599dbdb55a4b6a:<path>` against
`/Users/santteegt/GitRepositories/clones/veydrift` — not `docs.md`, not OGame convention,
not cost-fingerprinting against a live account (the method `docs/NOTES.md` §12.3 had to
fall back to before the contract source was available). Every table below is a direct
transcription of an `enum` declaration at that exact commit. `main` at this repo has
drifted from the deployed contract (`docs/RESEARCH-ADDENDUM.md` §1.1); every citation
here is pinned to the deployment commit specifically, not to `main`.

If you are about to hand-write one of these tables from `docs.md` or from memory: don't.
Two of the six enums below are provably wrong in prior docs, and the failure mode is
silent — a plausible-looking table that sends `startDefenseProduction` for
`SmallShieldDome` (3) when you meant `GaussCannon` (4) reverts or, worse, succeeds and
builds the wrong thing.

## Table of contents

- [1. `Building`](#1-building)
- [2. `Technology`](#2-technology)
- [3. `Ship`](#3-ship) — the Deathstar/Dreadstar naming trap
- [4. `Defense`](#4-defense) — not OGame order
- [5. `FleetMissionType`](#5-fleetmissiontype)
- [6. `Resource`](#6-resource)
- [7. The 14-slot fleet-mission tuple is a *different* ordering from `Ship`](#7-the-14-slot-fleet-mission-tuple-is-a-different-ordering-from-ship)
- [8. How to use these from Python](#8-how-to-use-these-from-python)
- [9. What prior docs got wrong, summarized](#9-what-prior-docs-got-wrong-summarized)

---

## 1. `Building`

Source: `packages/contracts/src/libraries/VeydriftTypes.sol:4-21` (commit `701bed35`).

| id | Building | id | Building |
| --: | --- | --: | --- |
| 0 | Metal Mine | 8 | Crystal Storage |
| 1 | Crystal Mine | 9 | Deuterium Tank |
| 2 | Deuterium Synthesizer | 10 | Fusion Reactor |
| 3 | Solar Plant | 11 | Nanite Factory |
| 4 | Robotics Factory | 12 | Terraformer |
| 5 | Shipyard | 13 | Alliance Depot |
| 6 | Research Lab | 14 | Missile Silo |
| 7 | Metal Storage | 15 | Rift Stabilizer |

Id 15's contract name is `InterdimensionalRiftStabilizer`; `docs/NOTES.md` §13.5 and this
codebase's display name shorten it to "Rift Stabilizer". Hard-capped at level 1;
mechanics otherwise unpublished (`docs/RESEARCH-ADDENDUM.md` §6, `docs.md` mentions "Rift
resource movement" with no formula).

Confirms `docs/NOTES.md` §2's table exactly — this enum was already right, just not
previously confirmed against source.

There is also a separate 4-member `MoonBuilding` enum
(`VeydriftTypes.sol:23-28`: `LunarBase, RoboticsFactory, JumpGate, Shipyard`) for moon
construction, out of scope for this pass (`docs/SPEC.md` §1 non-goals list moons only via
`docs/wallet-provider-research.md`'s address-binding discussion, not gameplay). Not
reproduced in `ids.py` — add it there first if a future pass needs it, rather than
reusing `Building`'s ids (they are a different enum entirely and only partially overlap
by coincidence: `RoboticsFactory` and `Shipyard` share the same *contract function* for
base cost, `VeydriftCatalog.sol:75-77`, but are still distinct enum types).

## 2. `Technology`

Source: `packages/contracts/src/libraries/VeydriftTypes.sol:62-78` (commit `701bed35`).

| id | Technology | id | Technology |
| --: | --- | --: | --- |
| 0 | Energy Technology | 8 | Hyperspace Technology |
| 1 | Laser Technology | 9 | Impulse Drive |
| 2 | Ion Technology | 10 | Hyperspace Drive |
| 3 | Combustion Drive | 11 | Plasma Technology |
| 4 | Computer Technology | 12 | Astrophysics |
| 5 | Weapons Technology | 13 | Intergalactic Research Network |
| 6 | Shielding Technology | 14 | Graviton Technology |
| 7 | Armor Technology | | |

**Not `docs.md`'s table order.** Impulse Drive is id 9, sitting *after* the combat techs
(Weapons/Shielding/Armor, 5-7) and Hyperspace (8) — `docs/NOTES.md` §2 flagged this by
cost-fingerprinting before the contract source was available; this table confirms it
against the enum declaration itself.

## 3. `Ship`

Source: `packages/contracts/src/libraries/VeydriftTypes.sol:43-60` (commit `701bed35`).

| id | Ship | id | Ship |
| --: | --- | --: | --- |
| 0 | Small Cargo | 8 | Bomber |
| 1 | Light Fighter | 9 | Solar Satellite |
| 2 | Recycler | 10 | Destroyer |
| 3 | Colony Ship | 11 | **Deathstar** |
| 4 | Large Cargo | 12 | Battlecruiser |
| 5 | Heavy Fighter | 13 | Reaper |
| 6 | Cruiser | 14 | Pathfinder |
| 7 | Battleship | 15 | Crawler |

### The Deathstar/Dreadstar trap

The enum member at id 11 is literally spelled `Deathstar`
(`VeydriftTypes.sol:53: Deathstar,`). Every rapidfire table in `docs.md` ("Reaper ->
Deathstar x10", "Deathstar -> any defense x200" — those two entries actually *do* say
Deathstar) and yet the same unit is called **Dreadstar** in `docs/NOTES.md` §2's
cost-fingerprinted table and in `veydrift-agent-resources.md`. Confirmed straight from
source (`docs/NOTES.md` §13.5: `Ship.Deathstar` appears directly in
`VeydriftFleetFuel.sol`'s `_missionShipQuantity`, e.g. `if (ship == Ship.Deathstar) return
ships.deathstar;`).

`ids.py` resolves this by making `"Deathstar"` the canonical display name (it's what the
contract calls it) and accepting `"Dreadstar"` as an alias in `ship_id()`:

```python
>>> from veydrift_agent import ids
>>> ids.ship_name(11)
'Deathstar'
>>> ids.ship_id("Dreadstar"), ids.ship_id("Deathstar")
(11, 11)
```

Pathfinder (14) is confirmed real and mission-capable (`docs/NOTES.md` §13.5:
`Ship.Pathfinder` is a genuine enum member), despite `docs.md` omitting it from the ship
catalog table and mentioning it only once, in the rapidfire table.

## 4. `Defense`

Source: `packages/contracts/src/libraries/VeydriftTypes.sol:30-41` (commit `701bed35`).

| id | Defense | id | Defense |
| --: | --- | --: | --- |
| 0 | Rocket Launcher | 5 | Ion Cannon |
| 1 | Light Laser | 6 | Plasma Turret |
| 2 | Heavy Laser | 7 | Large Shield Dome |
| 3 | **Small Shield Dome** | 8 | Anti-Ballistic Missile |
| 4 | **Gauss Cannon** | 9 | Interplanetary Missile |

**This is not OGame order**, and it is the one enum in this file most likely to be
hand-typed wrong from memory or genre convention. In OGame, Gauss Cannon sits before the
shield domes; here, `SmallShieldDome` is id 3 and `GaussCannon` is id 4 — the shield dome
sorts *before* the cannon — and `IonCannon` follows immediately at id 5.
`docs/RESEARCH-ADDENDUM.md` §3 calls this out explicitly as a correction to earlier
inferred tables, and this is read straight from the enum declaration, not inferred.

Confirming this against the live `/wallet/{addr}/defenses` response is the one enum table
that *cannot* be cost-fingerprinted the way `docs/NOTES.md` §12.3 fingerprinted the
others: this account has built zero defenses, and defense base costs are close enough in
places (e.g. Rocket Launcher 2,000M and Small Shield Dome 10,000M/10,000C both have
distinctive triples, so fingerprinting *would* work here in principle) that the contract
source is simply the more direct check, and the one this module actually used.

## 5. `FleetMissionType`

Source: `packages/contracts/src/VeydriftGameStorage.sol:166-177` (commit `701bed35`,
inside `abstract contract VeydriftGameStorage`, not `VeydriftTypes.sol` — the one enum of
the six that lives in the storage contract rather than the shared types library).

| id | Mission | id | Mission |
| --: | --- | --: | --- |
| 0 | Transport | 5 | ACS Defend |
| 1 | Deploy | 6 | Intercept |
| 2 | Colonize | 7 | Missile Attack |
| 3 | Attack | 8 | ACS Attack |
| 4 | Harvest | 9 | Defense Hold |

`Intercept` (6) and `DefenseHold` (9) appear in neither `docs.md` nor
`docs/NOTES.md`/`veydrift-agent-resources.md` — genuinely new, found only by reading the
enum. Both are combat-adjacent counterplay mechanics
(`VeydriftGameplayModule.sol`'s `_isCounterplayMissionType` groups `AcsAttack`,
`AcsDefend` and `Intercept` together) and their detailed mechanics are undocumented
anywhere (`docs/RESEARCH-ADDENDUM.md` §6.3).

**Every combat mission type is unreachable from this codebase at every tier** —
`Attack` (3), `AcsAttack` (8), `MissileAttack` (7) and `Intercept` (6) require a code
change to reach, not a `policy.json` edit (`docs/SPEC.md` §4: `allow_combat` is
deliberately ignored by every code path). `DefenseHold` (9) is stationing, not an attack,
but is likewise out of scope for this pass (`docs/SPEC.md` §1 non-goals: "Combat,
alliances, ACS"). `plan.py` never constructs an `Action` with `function=
"launchFleetMission"` at all in this pass — the only fleet-adjacent function it can
propose is the permissionless `resolveFleetMission` (ladder rung 3).

## 6. `Resource`

Source: `packages/contracts/src/libraries/VeydriftTypes.sol:80-85` (commit `701bed35`).

| id | Resource |
| --: | --- |
| 0 | Metal |
| 1 | Crystal |
| 2 | Deuterium |
| 3 | Energy |

The `uint8` used by the undocumented market-bridge functions
`depositMarketResource(uint256, Resource, uint128)`,
`requestMarketResourceWithdrawal(...)` and `finishMarketResourceWithdrawal(Resource)`
(`docs/NOTES.md` §13.3 — resources become the three ERC-20 proxies listed in
`/runtime-config`'s `resourceTokenAddresses`). Not otherwise used by any function this
codebase's ladder can reach; included for completeness because `ids.py` owns all six
enums, not because `plan.py` calls the market bridge.

## 7. The 14-slot fleet-mission tuple is a *different* ordering from `Ship`

Every fleet entrypoint (`launchFleetMission`, both overloads — see
`docs/RESEARCH-ADDENDUM.md` §4.2) takes a fixed `(uint32 x 14)` tuple, not the 16-member
`Ship` enum directly. Two ships cannot fly and are omitted from the tuple entirely
(`VeydriftFleetFuel.sol:73-87`, `_missionShipQuantity`; both non-flyable ships simply
`return 0` for any input): **Solar Satellite (9)** and **Crawler (15)**. Because both
omissions are followed by real, flyable ship ids, every tuple slot from index 9 onward is
shifted down by one relative to the `Ship` enum id:

| tuple index | Ship id | Ship name |
| --: | --: | --- |
| 0 | 0 | Small Cargo |
| 1 | 1 | Light Fighter |
| 2 | 2 | Recycler |
| 3 | 3 | Colony Ship |
| 4 | 4 | Large Cargo |
| 5 | 5 | Heavy Fighter |
| 6 | 6 | Cruiser |
| 7 | 7 | Battleship |
| 8 | 8 | Bomber |
| 9 | **10** | **Destroyer** |
| 10 | **11** | **Deathstar** |
| 11 | 12 | Battlecruiser |
| 12 | 13 | Reaper |
| 13 | 14 | Pathfinder |
| — | 9 | *(Solar Satellite — no slot)* |
| — | 15 | *(Crawler — no slot)* |

Indexing the tuple directly with a `Ship` id is the trap: `tuple[9]` is **not** Solar
Satellite (it has no slot at all) — it is Destroyer, one id higher than a naive reader
would guess. `ids.py` records this table (`FLEET_TUPLE_ORDER`, `NON_FLYABLE_SHIPS`) for
documentation, but **does not implement the conversion function.** That belongs to
`veydrift-wallet` (WP4a, TypeScript) — `shipCountsToFleetTuple()` — which is also where
`docs/SPEC.md` §6.7's dedicated test lives ("Destroyer lands at tuple index 9, not 10").
This module only owns the enums; the encoder that must not get this wrong is the wallet
skill's, not this one's. `plan.py` in this pass never constructs a fleet-mission action at
all (see §5 above), so the trap is currently unreachable from this codebase's write path
— documented here anyway because the next work package to touch fleet actions will need
it immediately.

## 8. How to use these from Python

```python
from veydrift_agent import ids

ids.Building.SOLAR_PLANT          # 3, an IntEnum member -- compares equal to a bare int
ids.building_name(3)              # "Solar Plant"
ids.building_id("solar plant")    # 3 -- normalized, case/space/hyphen-insensitive

ids.Defense.SMALL_SHIELD_DOME     # 3
ids.Defense.GAUSS_CANNON          # 4 -- not 3; see §4

ids.ship_name(11)                 # "Deathstar" -- canonical
ids.ship_id("Dreadstar")          # 11 -- alias accepted

ids.FleetMissionType.DEFENSE_HOLD # 9
ids.Resource.DEUTERIUM            # 2
```

Every `*_id()` lookup raises `KeyError` on an unknown name rather than guessing — there
is no silent fallback anywhere in this module.

## 9. What prior docs got wrong, summarized

| Prior claim | Where | Correction | Evidence |
| --- | --- | --- | --- |
| Defense enum follows OGame order | (implicit in genre convention; not actually asserted anywhere in this repo's docs, which is itself the risk — an agent or human filling the gap from memory would get it wrong) | `SmallShieldDome`=3, `GaussCannon`=4, `IonCannon`=5 | `VeydriftTypes.sol:30-41` |
| Ship 11 is "Dreadstar" | `docs/NOTES.md` §2, `veydrift-agent-resources.md` | Contract enum member is `Deathstar`; "Dreadstar" is the name used only in `docs.md`'s rapidfire tables and is an alias, not the canonical name | `VeydriftTypes.sol:53`, `VeydriftFleetFuel.sol` (`Ship.Deathstar`) |
| `playerScore` is a useful read function | `docs/NOTES.md` §13.5 | Not on the deployed implementation; reverts. Use `/wallet/{addr}/highscore` | `docs/RESEARCH-ADDENDUM.md` §1.1 |
| Pathfinder missing from ship catalog | `docs.md` (omits it from the table) | Real enum member, id 14, mission-capable | `docs/NOTES.md` §12.3, `VeydriftTypes.sol:56` |
| `Intercept`/`DefenseHold` mission types | not mentioned anywhere prior | Real enum members, ids 6 and 9, mechanics undocumented | `VeydriftGameStorage.sol:166-177` |
