# Entity IDs — the six canonical enums

**Source of truth:** the *deployed* contract, read directly at commit
`701bed3578cff4d134657c714c599dbdb55a4b6a` — not `docs.md` (Veydrift's own published docs,
which get two of these wrong), not OGame convention, not cost-fingerprinting against a live
account. Every table below is a direct transcription of an `enum` declaration at that exact
commit. `main` on the contracts repo has drifted from what's deployed — every citation here
is pinned to the deployment commit specifically, not to `main`.

Verified against this skill's source repository and its own further research as of
2026-08-12; where a fact needs deeper justification than fits here, that repository's own
docs carry the full derivation.

If you are about to hand-write one of these tables from `docs.md` or from memory: don't.
Two of the six enums below are provably wrong in Veydrift's own published docs, and the
failure mode is silent — a plausible-looking table that sends `startDefenseProduction` for
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
- [9. What Veydrift's own docs get wrong, summarized](#9-what-veydrifts-own-docs-get-wrong-summarized)

---

## 1. `Building`

Source: [`packages/contracts/src/libraries/VeydriftTypes.sol:4-21`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L4-L21) (commit `701bed35`).

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

Id 15's contract name is `InterdimensionalRiftStabilizer`; this codebase's display name
shortens it to "Rift Stabilizer". Hard-capped at level 1; mechanics otherwise unpublished
(`docs.md` mentions "Rift resource movement" with no formula).

There is also a separate 4-member `MoonBuilding` enum
([VeydriftTypes.sol:23-28](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L23-L28): `LunarBase, RoboticsFactory, JumpGate, Shipyard`) for moon
construction, out of scope for this codebase. Not reproduced in `ids.py` — add it there
first if a future pass needs it, rather than reusing `Building`'s ids (they are a different
enum entirely and only partially overlap by coincidence: `RoboticsFactory` and `Shipyard`
share the same *contract function* for base cost, [VeydriftCatalog.sol:75-77](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftCatalog.sol#L75-L77), but are
still distinct enum types).

## 2. `Technology`

Source: [`packages/contracts/src/libraries/VeydriftTypes.sol:62-78`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L62-L78) (commit `701bed35`).

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
(Weapons/Shielding/Armor, 5-7) and Hyperspace (8) — confirmed directly against the enum
declaration.

## 3. `Ship`

Source: [`packages/contracts/src/libraries/VeydriftTypes.sol:43-60`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L43-L60) (commit `701bed35`).

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
([`VeydriftTypes.sol:53`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L53): `Deathstar,`) and appears directly in [VeydriftFleetFuel.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFleetFuel.sol)'s
`_missionShipQuantity` (`if (ship == Ship.Deathstar) return ships.deathstar;`). Every
rapidfire table in `docs.md` ("Reaper -> Deathstar x10", "Deathstar -> any defense x200")
does say Deathstar — but the same unit gets called **Dreadstar** in some project research
notes that predate reading the contract source. Confirmed straight from source.

`ids.py` resolves this by making `"Deathstar"` the canonical display name (it's what the
contract calls it) and accepting `"Dreadstar"` as an alias in `ship_id()`:

```python
>>> from veydrift_agent import ids
>>> ids.ship_name(11)
'Deathstar'
>>> ids.ship_id("Dreadstar"), ids.ship_id("Deathstar")
(11, 11)
```

Pathfinder (14) is confirmed real and mission-capable (`Ship.Pathfinder` is a genuine enum
member), despite `docs.md` omitting it from the ship catalog table and mentioning it only
once, in the rapidfire table.

## 4. `Defense`

Source: [`packages/contracts/src/libraries/VeydriftTypes.sol:30-41`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L30-L41) (commit `701bed35`).

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
sorts *before* the cannon — and `IonCannon` follows immediately at id 5. Read straight
from the enum declaration, not inferred.

## 5. `FleetMissionType`

Source: [`packages/contracts/src/VeydriftGameStorage.sol:166-177`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGameStorage.sol#L166-L177) (commit `701bed35`,
inside `abstract contract VeydriftGameStorage`, not [VeydriftTypes.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol) — the one enum of
the six that lives in the storage contract rather than the shared types library).

| id | Mission | id | Mission |
| --: | --- | --: | --- |
| 0 | Transport | 5 | ACS Defend |
| 1 | Deploy | 6 | Intercept |
| 2 | Colonize | 7 | Missile Attack |
| 3 | Attack | 8 | ACS Attack |
| 4 | Harvest | 9 | Defense Hold |

`Intercept` (6) and `DefenseHold` (9) appear in neither `docs.md` nor any prior project
research — genuinely new, found only by reading the enum. Both are combat-adjacent
counterplay mechanics ([VeydriftGameplayModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGameplayModule.sol)'s `_isCounterplayMissionType` groups
`AcsAttack`, `AcsDefend` and `Intercept` together) and their detailed mechanics are
undocumented anywhere.

**`AcsAttack` (8), `MissileAttack` (7), `Intercept` (6), `AcsDefend` (5) and
`DefenseHold` (9) are unreachable from this codebase at every tier, regardless of
policy** — each requires an actual code change to reach, not a `policy.json` edit.
`Attack` (3) is the one exception, since the launch-actions plan's commit 5
(2026-08-28): `policy.actions.allow_combat` is a real, independently-checked gate for it
at `operator` tier, at both enforcement layers (`guard.py`'s `mission_type` gate,
`allowlist.ts`'s calldata-level check). **Since commit 6 (same date),
`candidates.generate_attack_candidates` does construct an Attack `Action`** — the
ladder's most conservative rung (`8e:attack`), reached only once every other rung has
found nothing at all for any target planet. A separate, new `attack_protection` guard
gate re-checks the specific target's live attack-protection status fresh at
guard-evaluation time, never trusting the generator's own, earlier read.

**Since 2026-08-17 (Phase 5c/5b), `plan.py` can construct a `launchFleetMission`
`Action`** for every one of the four non-combat mission types — all gated on
`policy.actions.allow_fleet_noncombat` (default `false`) except Colonize, gated on
`policy.strategy.colonize` (also default `false`): `candidates.
generate_transport_candidates` (Transport), `generate_deploy_candidates` (Deploy, added
2026-08-28, commit 4 of the launch-actions plan — permanently repositions an entire
flyable fleet toward a declared `policy.strategy.fleet_home_planet_id`),
`generate_harvest_candidates`/`generate_foreign_harvest_candidates` (Harvest against a
local or foreign debris field — live as of 2026-08-28, commits 1 and 3), and
`generate_colonize_candidates` (Colonize, added commit 4 — target selection reads
`/universe/galaxies/{g}/systems/{s}` for free slots in the wallet's own systems).
`resolveFleetMission` (ladder rung 3) remains the only *permissionless* fleet-adjacent
function.

## 6. `Resource`

Source: [`packages/contracts/src/libraries/VeydriftTypes.sol:80-85`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L80-L85) (commit `701bed35`).

| id | Resource |
| --: | --- |
| 0 | Metal |
| 1 | Crystal |
| 2 | Deuterium |
| 3 | Energy |

The `uint8` used by the undocumented market-bridge functions
`depositMarketResource(uint256, Resource, uint128)`,
`requestMarketResourceWithdrawal(...)` and `finishMarketResourceWithdrawal(Resource)` —
resources become the three ERC-20 proxies listed in `/runtime-config`'s
`resourceTokenAddresses`. Not otherwise used by any function this codebase's ladder can
reach; included for completeness because `ids.py` owns all six enums, not because
`plan.py` calls the market bridge.

## 7. The 14-slot fleet-mission tuple is a *different* ordering from `Ship`

Every fleet entrypoint (`launchFleetMission`, both overloads) takes a fixed
`(uint32 x 14)` tuple, not the 16-member `Ship` enum directly. Two ships cannot fly and
are omitted from the tuple entirely ([VeydriftFleetFuel.sol:73-87](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFleetFuel.sol#L73-L87), `_missionShipQuantity`;
both non-flyable ships simply `return 0` for any input): **Solar Satellite (9)** and
**Crawler (15)**. Because both omissions are followed by real, flyable ship ids, every
tuple slot from index 9 onward is shifted down by one relative to the `Ship` enum id:

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
documentation, but **does not implement the conversion function itself** — `tick.py`'s
`_ship_counts_to_fleet_tuple` (Python, added Phase 5c) and `veydrift-wallet`'s
`shipCountsToFleetTuple()` (TypeScript) each do, independently, both built from this same
table, each with its own dedicated test pinning "Destroyer lands at tuple index 9, not
10" (`tests/test_tick.py`'s `test_fleet_mission_ship_tuple_pins_destroyer_at_index_nine_
not_ten` and `fleet.test.ts`'s equivalent). This module only owns the enums; the two
places that must not get the conversion wrong are `tick.py` and `fleet.ts`, not this one
— and as of Phase 5c, both are live, not just one.

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

## 9. What Veydrift's own docs get wrong, summarized

| Claim in `docs.md` (or genre convention) | Correction | Evidence |
| --- | --- | --- |
| Defense enum follows OGame order (never actually asserted, just an easy default to assume) | `SmallShieldDome`=3, `GaussCannon`=4, `IonCannon`=5 | [VeydriftTypes.sol:30-41](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L30-L41) |
| Ship 11 is "Dreadstar" | Contract enum member is `Deathstar`; "Dreadstar" is an alias, not the canonical name | [VeydriftTypes.sol:53](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L53), [VeydriftFleetFuel.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFleetFuel.sol) (`Ship.Deathstar`) |
| `playerScore` is a useful read function | Not on the deployed implementation; reverts. Use `/wallet/{addr}/highscore` | deployed ABI, verified live |
| Pathfinder missing from ship catalog | Real enum member, id 14, mission-capable | [VeydriftTypes.sol:56](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L56) |
| `Intercept`/`DefenseHold` mission types | Not mentioned anywhere in `docs.md`; real enum members, ids 6 and 9, mechanics undocumented | [VeydriftGameStorage.sol:166-177](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGameStorage.sol#L166-L177) |
