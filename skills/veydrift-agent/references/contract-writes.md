# Contract writes — entrypoints and traps

Every claim below was checked directly against the *deployed* contract source with
`git show 701bed3578cff4d134657c714c599dbdb55a4b6a:<path>` on 2026-08-12 — not transcribed
from an earlier draft's summary of it without re-checking. Where this file adds a nuance
that earlier summary didn't carry, it says so explicitly (§5, §6).

This document is about the **contract**. `veydrift-agent` never encodes calldata or signs
— `plan.py` only ever names a function and its arguments in an `Action`
(`models.py::Action.function`, a plain string). `veydrift-wallet`'s `src/fleet.ts`,
`src/abi.ts` and `src/allowlist.ts` are what actually build, allowlist and submit a
transaction. Read this file to know *what the contract will do*; read
`skills/veydrift-wallet/references/{abi-pinning,tx-safety}.md` for how the wallet skill
encodes and gates it.

## Table of contents

- [1. The write entrypoints this codebase's ladder can reach](#1-the-write-entrypoints-this-codebases-ladder-can-reach)
- [2. Trap: the 14-slot fleet tuple index shift](#2-trap-the-14-slot-fleet-tuple-index-shift)
- [3. Trap: `launchFleetMission` is overloaded](#3-trap-launchfleetmission-is-overloaded)
- [4. Trap: six `nonpayable` functions that are semantically reads](#4-trap-six-nonpayable-functions-that-are-semantically-reads)
- [5. Trap: `finish*` functions — "back-compat no-op" is not quite right for three of the four](#5-trap-finish-functions--back-compat-no-op-is-not-quite-right-for-three-of-the-four)
- [6. Trap: `startBuildingUpgrade` reverts with `ConstructionActive` if a build is already queued](#6-trap-startbuildingupgrade-reverts-with-constructionactive-if-a-build-is-already-queued)
- [7. Trap: `abandonPlanet` reverts with `CannotAbandonHomePlanet` for a home planet](#7-trap-abandonplanet-reverts-with-cannotabandonhomeplanet-for-a-home-planet)
- [8. A gap this pass found: `plan.py` can propose an action no tier's allowlist will ever submit](#8-a-gap-this-pass-found-planpy-can-propose-an-action-no-tiers-allowlist-will-ever-submit)

---

## 1. The write entrypoints this codebase's ladder can reach

This table is the write functions "inside a sane agent mandate" out of 61 total non-view
functions on the deployed contract, cross-checked against
[`packages/contracts/src/VeydriftGame.sol`](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) at the deployment commit and against which
tier's wallet-allowlist selector set (`skills/veydrift-wallet/src/allowlist.ts`) actually
includes each one:

| Action | Signature | [VeydriftGame.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) line | Tier that may submit it |
| --- | --- | --- | --- |
| Building upgrade | `startBuildingUpgrade(uint256,uint8)` | 131 | economy, operator |
| Research | `startResearch(uint256,uint8)` | 220 | economy, operator |
| Defense production | `startDefenseProduction(uint256,uint8,uint32)` | 176 | economy, operator |
| Permissionless resolve | `resolveFleetMission(uint256)` | 425 | economy, operator (permissionless — costs no allowlist-gated capability at all, but still goes through `walletctl` like everything else so it's still logged) |
| Settle production | `settlePlanet(uint256)` | 121 | economy, operator |
| Fleet launch (7-arg) | `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)` | 358 | operator only, and only for mission types Transport(0)/Deploy(1)/Harvest(4) — §3 |
| Fleet launch (6-arg) | `launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)` | 325 | operator only, same mission-type restriction |
| Ship production | `startShipProduction(uint256,uint8,uint32)` | 186 | `economy` — granted 2026-08-12, see §8 |
| Fleet return | `completeFleetMissionReturn(uint256)` | 442 | **none** — not in any tier's table, and not in `allowlist.ts`'s selector sets. `plan.py` never constructs this action |

The tier column is read straight from `allowlist.ts`'s `ECONOMY_SIGNATURES` and
`LAUNCH_FLEET_MISSION_SIGNATURES` constants (`skills/veydrift-wallet/references/tx-safety.md`
documents the allowlist mechanics), which match the project's own tier table exactly for
the first six rows. `advisor` is not a column here because it may build and simulate any
of these, but its selector set is empty by design (`tierSelectors("advisor") === []`) — it
can never submit anything.

## 2. Trap: the 14-slot fleet tuple index shift

Every `launchFleetMission` overload takes a fixed `(uint32 × 14)` ship-count tuple, but
`enum Ship` ([VeydriftTypes.sol:43-60](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftTypes.sol#L43-L60)) has **16** members. Two cannot fly and have no
tuple slot at all — confirmed at [VeydriftFleetFuel.sol:73-87](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFleetFuel.sol#L73-L87), where both ids simply
`return 0` for any input:

- `SolarSatellite` — Ship id **9**
- `Crawler` — Ship id **15**

Because `SolarSatellite` sits in the middle of the enum (not at either end), every
flyable ship with id > 9 is shifted down by exactly one tuple slot:

| tuple index | Ship id | Ship name |
| --: | --: | --- |
| 0-8 | 0-8 | SmallCargo … Bomber (unshifted) |
| **9** | **10** | **Destroyer** ← not id 9; id 9 (SolarSatellite) has no slot |
| 10 | 11 | Deathstar |
| 11 | 12 | Battlecruiser |
| 12 | 13 | Reaper |
| 13 | 14 | Pathfinder |

`shipCountsToFleetTuple()` (`skills/veydrift-wallet/src/fleet.ts`) is the single
conversion function that must never be reimplemented at a call site. It throws — not
silently zeroes — if asked to place `SolarSatellite` or `Crawler` in a fleet, even at
count zero, and `tests/fleet.test.ts` asserts a Destroyer lands at tuple index 9. This
codebase's `plan.py` never constructs a `launchFleetMission` action at all (`ids.py`'s
own reference notes this — `references/entity-ids.md` §7), so the trap is currently
unreachable from the read/plan side; it's load-bearing for whoever adds fleet actions
next, and it's already built and tested on the wallet side today.

## 3. Trap: `launchFleetMission` is overloaded

Both of these live on the deployed ABI simultaneously:

```solidity
launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint16,uint256)   // 7-arg
launchFleetMission(uint256,uint256,uint8,(uint32×14),(uint128,uint128,uint128),uint256)           // 6-arg
```

Confirmed directly: [VeydriftGame.sol:325](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol#L325) (6-arg) and `:358` (7-arg). Selecting by bare
function name is ambiguous — viem and ethers both require the full canonical signature.
`skills/veydrift-wallet/src/abi.ts`'s `resolveFunctionAbi()` takes a full signature string,
never a bare name, specifically because of this function; `allowlist.ts` computes the
operator tier's selector set from both full signatures independently
(`LAUNCH_FLEET_MISSION_SIGNATURES`, a 2-element array).

## 4. Trap: six `nonpayable` functions that are semantically reads

These are declared `external` with no `view`/`pure` modifier — confirmed directly in
[VeydriftGame.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) — because they lazily settle state before returning, not because
they're meant to be sent as transactions:

| Function | [VeydriftGame.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) line |
| --- | --- |
| `protectedResources(uint256)` | 688 |
| `raidableResources(uint256)` | 692 |
| `maxRaidLoot(uint256,uint256)` | 696 |
| `debrisField(uint256)` | 527 |
| `collectResources(uint256)` | 126 |
| `attackProtectionStatus(address,uint256)` | 452 |

`viem.readContract` (and `ethers.callStatic`) will reject or mislead against these —
`simulateContract` / `eth_call` is the only correct way to invoke them. `walletctl
simulate` is the sanctioned path; `walletctl send` refuses all six
unconditionally, at every tier, even with `--confirm` — sending one would mean paying real
gas for what is, semantically, a read (`skills/veydrift-wallet/references/tx-safety.md`).

## 5. Trap: `finish*` functions — "back-compat no-op" is not quite right for three of the four

An earlier draft of this project's research stated plainly: *"Confirmed: `finishBuildingUpgrade` /
`finishResearch` / `finishShipProduction` / `finishDefenseProduction` exist but are
back-compat no-ops... calling them wastes gas."* Reading the actual delegatecall chain
behind each one shows that framing is true for exactly one of the four and needs a real
qualifier for the other three — worth recording precisely rather than repeating the
summary verbatim, since "wastes gas" and "can revert" are different failure modes for
anyone deciding whether it's safe to call one defensively.

- **`finishBuildingUpgrade(uint256)`** ([VeydriftGame.sol:170-173](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol#L170-L173)) really is a harmless
  no-op in the sense that matters: its body is just `_touchPlayer` +
  `_requirePlanetOwner` + `_settleResources(planetId)`, with **no active/ready gate at
  all**. It cannot revert on "nothing to finish" — it just re-runs the same lazy-settle
  that `startBuildingUpgrade` already runs on every call. Calling it costs gas for
  nothing; it never throws.
- **`finishResearch()`, `finishShipProduction(uint256)`, `finishDefenseProduction(uint256)`**
  are different: [VeydriftGame.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol)'s versions (`:181`, `:191`, `:225`) delegatecall
  through [VeydriftColonizationModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftColonizationModule.sol) into [VeydriftPlanetManagementModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftPlanetManagementModule.sol)
  (research, `:368-376`), [VeydriftShipProductionModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftShipProductionModule.sol) (`:51-64`), and
  [VeydriftDefenseProductionModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftDefenseProductionModule.sol) (`:205-`), where the *real* completion logic
  lives — and that logic **reverts**: `QueueInactive()` if nothing is queued,
  `QueueNotReady(readyAt)` if the queue is active but not yet due. These three are
  "back-compat" in the sense that every `start*` call already settles anything that's
  come due (so there's rarely a reason to call `finish*` separately), but calling one
  when there is genuinely nothing to finish **reverts**, it does not silently succeed and
  waste gas quietly. `veydrift-agent`'s ladder never proposes any of the four `finish*`
  functions (`plan.py` has no code path that constructs one), so this is documentation
  for whoever is tempted to add one defensively, not a live bug.

## 6. Trap: `startBuildingUpgrade` reverts with `ConstructionActive` if a build is already queued

```solidity
// packages/contracts/src/VeydriftGameStorage.sol:365 (error declaration)
error ConstructionActive();
```

```solidity
// packages/contracts/src/VeydriftGame.sol:139 (the actual revert site)
if (buildingConstructions[planetId].active) revert ConstructionActive();
```

Both citations independently confirmed at commit `701bed35`. `startBuildingUpgrade`
settles any *ready* construction before this check (so a completed-but-unobserved queue
clears itself and does not falsely trip this), but a **genuinely in-progress** construction
still reverts. The contract allows only one active `BuildingConstruction` per planet —
`plan.py`'s rung 5/6 logic (`references/strategy-playbook.md` §8) is written around this
exactly: if the building queue is busy, it does not propose a second building action at
all, rather than proposing one that's guaranteed to revert.

**Adjacent, not the cited trap but directly relevant if this file is ever extended to
research/ships/defense:** `startResearch` gates on a *differently-named* error,
`QueueActive()` ([VeydriftPlanetManagementModule.sol:331](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftPlanetManagementModule.sol#L331)), not `ConstructionActive` — the
two queue types have separate revert names despite the identical shape of the check. Ship
and defense production behave differently again: `startShipProduction` and
`startDefenseProduction` do **not** revert when a queue is already active — they push the
new order onto a backlog (`_shipQueueBacklogs[planetId]` /
[VeydriftShipProductionModule.sol:47-48](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftShipProductionModule.sol#L47-L48), and the equivalent in
[VeydriftDefenseProductionModule.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftDefenseProductionModule.sol)) instead of reverting. Only building and research
queues are hard-blocking; ship and defense queues are not. This codebase's ladder does not
currently rely on that distinction (rung 8's ship/defense proposals aren't reachable at
default policy settings — `allow_ships`/`allow_defense` both default `false`), but it's
worth knowing before assuming every queue type reverts the same way `ConstructionActive`
does.

## 7. Trap: `abandonPlanet` reverts with `CannotAbandonHomePlanet` for a home planet

```solidity
// packages/contracts/src/VeydriftPlanetManagementModule.sol:146-150 (at 701bed357...)
function abandonPlanet(uint256 planetId) external {
    _requirePlanetOwner(planetId);
    _settleDueCombatArrivals(msg.sender);
    _requireNoPendingMissionResolutionForPlanet(planetId);
    if (homePlanetOf[msg.sender] == planetId) revert CannotAbandonHomePlanet();
```

```solidity
// packages/contracts/src/VeydriftGameStorage.sol:432
error CannotAbandonHomePlanet();
```

Both confirmed directly at commit `701bed35`. `abandonPlanet` is not on the tier table at
all — no tier this codebase implements can submit it — so this is not a live-reachable
trap for anything `vd plan`/`walletctl` do today. It matters for a different reason: **this
is the contract-level proof behind why a Veydrift planet is permanently bound to the EOA
that settled it.** Planet 664 is this wallet's home planet (its only planet), so
`abandonPlanet` is permanently unreachable for it — not merely inadvisable, but reverting
by construction. Combined with there being no `transferPlanet` function anywhere in the
contract (confirmed by `grep -lE "transferPlanet|sellPlanet|giftPlanet|setPlanetOwner"`
across every `.sol` file at this commit), planet 664 cannot leave this EOA by any
contract-level mechanism — only by handing over the private key itself, which is custody
transfer, not a game action.

## 8. A gap this pass found — and fixed on 2026-08-12

> **Resolved.** `startShipProduction` was granted to the `economy` tier in **both** enforcement
> layers (`guard.py`'s `_MIN_TIER_FOR_FUNCTION`, `allowlist.ts`'s `ECONOMY_SIGNATURES`) and in
> the project's own tier-table spec. Producing ships spends resources on your own planet — the same risk profile as
> `startDefenseProduction`, which tier 2 already permitted. Combat remains gated separately, by
> mission type on `launchFleetMission`, and stays unreachable in code. `allow_ships` still defaults
> to `false`, so the fix widened nothing until a human opts in.
>
> The original analysis is preserved below because the *shape* of the defect is worth remembering:
> two components each correctly implemented what they were told, and the error lived in the
> specification that told them. No single-component review would have caught it.

### The original finding (pre-fix)

Not a contract trap — a cross-package inconsistency worth recording plainly rather than
quietly working around. `plan.py`'s ladder rung 8 (`references/strategy-playbook.md` §8)
can construct an `Action` with `function="startShipProduction"` when
`policy.actions.allow_ships` is `true` and a Solar Satellite is currently the cheaper
energy source (`references/formulas.md` §9). But:

- The project's tier table did **not** list `startShipProduction` among what either
  `economy` or `operator` may submit.
- `skills/veydrift-wallet/src/allowlist.ts`'s `ECONOMY_SIGNATURES` (used by both `economy`
  and `operator`, since `operator` is `ECONOMY_SIGNATURES` plus the two
  `launchFleetMission` overloads) does not include it either.

So a `vd plan` proposal with `allow_ships: true` is fully legitimate per the decision
ladder, gets a rendered, ready-to-submit transaction at tier 1 exactly as designed
(`SPEC.md` §4: "Tier 1 still builds calldata"), and would pass `walletctl build` and
`walletctl simulate` — but `walletctl send` would refuse it at **every** tier, forever,
on the `selector` allowlist check alone, since no tier's selector set ever contains
`startShipProduction`'s 4-byte selector. This is not a bug in either `plan.py` or
`allowlist.ts` individually — both correctly implement what they were each told to
implement — it's a gap in `SPEC.md` §4's tier table itself, which never allocated ship
production to a tier. `assets/policy.example.json`'s default (`allow_ships: false`,
per `SPEC.md` §5.6) means this rung does not fire in the shipped default configuration, so
it is not a live-reachable problem today, but flipping `allow_ships` to `true` at any
tier produces proposals that can never be executed through this codebase's own wallet
engine. Worth a `SPEC.md` update (add `startShipProduction` to a tier, or have `plan.py`
gate rung 8's ship branch on tier reachability the same way combat is gated) before
`allow_ships` is ever turned on in a real policy file.
