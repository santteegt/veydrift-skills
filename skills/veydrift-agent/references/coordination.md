# ACS defense coordination — AcsDefend/Intercept/launchDefenseHold/openDefenseIntent

Covers the four functions this feature makes real, override-executable actions, plus the
suggestion layer (`coordination.py`) built on top. All four require
`policy.actions.allow_acs_defense: true`, are never planner-produced (no `candidates.py`
generator, no `plan.py` ladder rung), and are reachable only via `vd tick --action` +
`policy.strategy.allow_agent_action_override` — the same manual-override-only posture the
15 `VeydriftAllianceSystem` membership functions already established (see
`manual-action-override.md`).

## Verified against source, pinned commit `202d1acd9e35d815bd66cb9bae744341b1b1cf9e`

Confirmed by direct source read of `VeydriftGameplayModule.sol`, `VeydriftDefenseHoldModule.sol`,
`VeydriftAllianceSystem.sol`, and `libraries/VeydriftAntiRaidPrimitives.sol` — not inferred
from `docs.md` or the backend's own summaries.

### AcsDefend(5) / Intercept(6) — `launchFleetMission`'s counterplay branch

Both reuse the two existing `launchFleetMission` overloads unchanged (same selectors as
Transport/Deploy/Colonize/Harvest/Attack). The differences are entirely inside
`_launchFleetMission`'s own dispatch:

- **The `targetPlanetId` calldata argument is repurposed to mean `hostileMissionId`** for
  these two mission types only — the contract re-derives the real target internally
  (`targetPlanetId = hostile.targetPlanetId`). This is AGENTS.md §7's **third** documented
  silent-corruption trap: `Action.mission_id` carries this id cleanly in this codebase's
  clean model; `tick.py`'s encoder (`_fleet_mission_args`) is the one place the
  repurposing becomes an actual calldata value. `Action.target_planet_id`/
  `target_coordinates` stay unused for these two.
- **The hostile mission's own type must be exactly `Attack`** — not the broader
  Attack/Intercept/MissileAttack triple (`_isHostileMission`, which governs
  `openDefenseIntent`'s own authorization question, not this launch path). Confirmed
  directly: `hostile.missionType != FleetMissionType.Attack` reverts `InvalidMissionType`
  unconditionally. A first draft of this feature's plan mistakenly reused the broader
  triple here — caught by an adversarial review pass before any code shipped.
- **`FleetAlreadyArrived`**: the caller's own computed arrival must be `<=` the hostile
  mission's `arrivalAt` — stronger than, and independent of, the 5-minute join cutoff
  below. `guard._gate_acs_defend_target` re-derives this from `action.ships`/
  `action.speed_pct` and the real target's coordinates (read from the fetched
  `hostile_mission["targetPlanet"]["coordinates"]`, since `action.target_coordinates` is
  unset for these two).
- **The join window**: `VeydriftAntiRaidPrimitives.ACS_DEFEND_JOIN_CUTOFF_SECONDS = 5
  minutes`, `canJoinAcsDefense(now, arrivalAt) = now + 300 < arrivalAt`, enforced by
  `VeydriftAllianceSystem._canCoordinateDefense` (folded into `counterplayDefenseFuelContext`'s
  `canCoordinate` return value).
- **The two-view-function-call shape**: `_launchFleetMission` calls
  `IVeydriftCounterplayAllianceSystem(allianceSystem).counterplayDefenseFuelContext(msg.sender,
  targetPlanetId, hostileMissionId, ships, hostile.arrivalAt - arrivalAt)` and reverts
  `InvalidQuantity` if `canCoordinate` is `false`. The real, contract-computed
  `netHoldingFuelCost` is added on top of the ordinary mission fuel; `depotSupport` (if
  non-zero) is spent from the *defended* planet's resources, not the origin's.

### `launchDefenseHold` — its own entrypoint (`VeydriftDefenseHoldModule.sol`)

`launchDefenseHold(originPlanetId, targetPlanetId, ships, cargo, uint16 speedPercent,
uint256 holdSeconds) -> uint256`, `nonpayable`, selector `d3ad415f` (the ABI's inputs are
unnamed; the module source is the ordering authority). A **wholly separate contract
entrypoint**, not a `launchFleetMission` overload — no `mission_type` argument at all,
confirmed by reading the module directly. Same "wholly separate entrypoint, own
`ActionKind`" precedent `MISSILE_ATTACK` already set — `ActionKind.DEFENSE_HOLD`, not
`FLEET_MISSION`.

- **Bounds**: `MIN_DEFENSE_HOLD_SECONDS = 1 hour` (3600s), `MAX_DEFENSE_HOLD_SECONDS = 32
  hours` (115200s), both inclusive — `InvalidHoldWindow` otherwise.
- **`SamePlanet()`** reverts on `originPlanetId == targetPlanetId`; requires the caller
  own `originPlanetId` (`_requirePlanetOwner`) — `targetPlanetId` may be the caller's own
  planet or a same-alliance member's.
- **Real, non-repurposed target**: unlike AcsDefend/Intercept, `targetPlanetId` here is a
  genuine named argument — `Action.target_planet_id`/`target_coordinates` are used exactly
  as they are for a normal `FLEET_MISSION`.
- **`defenseHoldFuelContext(viewer, defenderPlanetId, ships, holdSeconds) -> (canCoordinate,
  netHoldingFuelCost, depotSupport)`** is called the same way `counterplayDefenseFuelContext`
  is for AcsDefend/Intercept, and its `netHoldingFuelCost` is added on top of the ordinary
  point-to-point travel fuel (confirmed directly: `fuelCost = ordinaryFuelCost +
  netHoldingFuelCost`) — the ordinary component is fully computable here (both endpoints'
  coordinates are real, non-repurposed fields), unlike AcsDefend/Intercept.
- Still consumes a fleet slot and real ships — `guard._gate_fleet_slots`/
  `_gate_fleet_ship_availability` are widened to trigger on `ActionKind.DEFENSE_HOLD` too,
  not just `FLEET_MISSION` (a real gap this feature closed: before the fix,
  `_gate_prerequisites`'s own dispatch condition fell through to a `None`-returning family
  lookup for this new `ActionKind` and silently PASSed ship availability for every
  DefenseHold action).

### `openDefenseIntent` — `VeydriftAllianceSystem`'s 16th in-scope function

`openDefenseIntent(defenderPlanetId, hostileMissionId) -> intentId`. **Callable only by
`defenderPlanetId`'s own owner, unconditionally** — `VeydriftAllianceSystem.sol:682`:
`if (target.owner != msg.sender) revert NotPlanetOwner(...)`. This is a coordinator
*announcing* their own planet is under attack and inviting alliance help, not a mechanism
for an ally to open an intent on a teammate's behalf — confirmed both by source and by a
live `NotPlanetOwner` revert path during fork verification (round 6). `guard._gate_
alliance_action`'s `openDefenseIntent` branch independently re-derives this ownership
check from `Snapshot` (`snapshot.planet(action.planet_id) is not None`), the same live-
ownership check `_gate_acs_defend_target`/`_gate_defense_hold_target` already use for
origin ownership — added after this round surfaced that the first implementation
verified every other precondition but not this one. **Optional, not a prerequisite** for
AcsDefend/Intercept — its `intentId` is never consumed elsewhere on-chain; its only value
is the `AllianceDefenseIntentOpened` event, a coordination signal for allies to see and
act on (via AcsDefend/Intercept/`launchDefenseHold` themselves, which remain independently
callable by any alliance member or self-owner regardless of whether an intent was ever
opened). Gated on `allow_acs_defense` (not `allow_alliance`) at `economy` tier (it moves no
fleet, spends no resource — same floor the 15 membership functions use), kept out of
`_ALLIANCE_FUNCTIONS`/`ALLIANCE_SIGNATURES` deliberately (folding it in would break the
existing `guard._ALLIANCE_FUNCTIONS == ts_alliance_signature_names` cross-layer equality).
On-chain validation is entirely inline in `openDefenseIntent` itself — it does **not** call
`canCoordinateDefense`/`_canCoordinateDefense` at all; it re-fetches `game.fleetMission(
hostileMissionId)` directly and checks `status == Outbound`, `targetPlanetId ==
defenderPlanetId`, `_isHostileMission(missionType)` (the broader Attack/Intercept/
MissileAttack triple — genuinely broader than AcsDefend/Intercept's own exactly-`Attack`
launch-path requirement, confirmed by reading both code paths side by side), and the same
5-minute join cutoff, reverting `InvalidAlliance` on any failure.

### The `_canCoordinateDefense` self-owned-planet short-circuit — read this before touching either new gate

```solidity
function _canCoordinateDefense(address viewer, uint256 defenderPlanetId, uint256 hostileMissionId)
    private view returns (bool)
{
    if (game.planet(defenderPlanetId).owner == viewer) return true;   // <-- short-circuit
    ... alliance-membership / status / hostility / target-match / cutoff checks ...
}
```

**`canCoordinate=true` alone is never sufficient** for a self-owned defended planet — it
answers "am I authorized to help this planet" (trivially yes, it's your own), not "is this
a live, valid, still-joinable hostile mission" (a completely separate question the
short-circuit skips entirely). This is why `guard._gate_acs_defend_target`'s independent
`GET /mission/{id}` re-check (`_hostile_mission_coordination_defect`) stays load-bearing
even when the live `coordination_allowed` probe reports `true` — the two checks are
complementary, not redundant. A future reader "simplifying" the gate by trusting
`coordination_allowed` alone would silently reopen the exact gap this shape exists to
close. `openDefenseIntent`'s own `_gate_alliance_action` branch reuses the identical
`_hostile_mission_coordination_defect` helper for the same reason.

`defenseHoldFuelContext` has no such short-circuit subtlety — its own `canCoordinate` *is*
sufficient for the target-planet authorization question, since it isn't tied to any
specific hostile mission at all (any same-alliance planet, any chosen window). What it
structurally cannot check is `SamePlanet`/origin ownership (no `originPlanetId` argument)
— `guard._gate_defense_hold_target` covers both independently.

## Live verification status — honest, not overclaimed

- **`GET /mission/{id}`'s shape** was confirmed live against a real (already-`Returned`)
  mission during this feature's development: `{"mission": {missionId, status,
  missionType, originPlanetId, targetPlanetId, arrivalAt, originPlanet, targetPlanet,
  ...}}`, server-side cache TTL `0` (always live), confirmed unauthenticated/public.
- **The still-`Outbound`-and-hostile case specifically remains unobserved against the
  real, live backend** — no account probed during this feature's development had a
  genuinely live incoming Attack the real indexer could report on. A fork-created hostile
  mission (round 6, below) is invisible to the real backend by construction (it never
  happened on real mainnet), so this specific route shape is still typed-from-source for
  that one case, the same "spot-checked once, not exhaustively" honesty `read.
  fetch_mission_by_id`'s own docstring states.
- **All four functions have now been live-sent on a local Anvil fork of Base, round 6
  (2026-09-08)** — see `skills/veydrift-wallet/references/fork-testing.md` §13 for the
  full runbook. `launchDefenseHold`, AcsDefend, Intercept, and `openDefenseIntent` all
  produced real, decoded on-chain events and real before/after ship-count/fleet-slot
  state changes; the exactly-`Attack` requirement was confirmed by a live `InvalidMissionType`
  revert, not just by source; and this round is what surfaced the `openDefenseIntent`
  ownership-check gap fixed above. Scope is "launch confirmed," same honest ceiling every
  prior round states for combat: full combined-defense *resolution* (whether a joined
  fleet's help actually changes a battle's outcome) sits behind the same off-chain
  randomness reveal Attack's own resolution already does (AGENTS.md §11) and was not, and
  could not be, exercised.

## `tick.py`'s live pre-check probes

`counterplayDefenseFuelContext`/`defenseHoldFuelContext`/`canCoordinateDefense` are all
`view` functions on `VeydriftAllianceSystem`, resolved via the same `buildTx`/`simulateTx`
pipeline every write already uses (`contract: "alliance"`) — reading a decoded return
value needed a new `walletctl simulate --json` capability (`veydrift-wallet` `1.1.0`),
since `simulate`'s plain-text output has no channel for one. `tick._walletctl_simulate`
(the pre-send gate `_send_and_await` already calls) is untouched — a deliberately separate
function, `_walletctl_simulate_probe`, handles the JSON channel, so this feature's new
code never touches the existing, well-tested send pre-flight path.

`_acs_defend_coordination_probe`'s `holdSeconds` argument is a deliberate, stated
approximation: the FULL remaining window until the hostile mission's `arrivalAt`
(`hostile.arrivalAt - now`), not `hostile.arrivalAt` minus the caller's own computed
arrival the way the real contract call does it — this avoids duplicating the distance/
speed formula a third time in `tick.py`. Because the caller's own arrival is always `>=
now`, this is a safe-direction OVER-estimate of the true `holdSeconds`, which can only
ever over-, never under-, state the probed `netHoldingFuelCost`.
`guard._gate_acs_defend_target`'s own independent `FleetAlreadyArrived` re-check (using
the real formula) is what actually gates whether the action is allowed at all — the probe
exists only to surface a live cost and a coarser authorization signal.

A transient RPC failure in any of these three probes fails the corresponding action
closed (BLOCK, never a guessed `True`/`0`) — including inside the 5-minute join window.
Accepted, stated tradeoff, not a silent one: correctness over availability, the same
posture every other live-data gate in this codebase takes.

## Worked `--action` examples

```json
{
  "kind": "fleet_mission",
  "function": "launchFleetMission",
  "planet_id": 664,
  "mission_type": 5,
  "origin_planet_id": 664,
  "mission_id": 99001,
  "ships": {"6": 5},
  "rule": "operator override",
  "rationale": "AcsDefend against incoming Attack (hostile mission 99001)"
}
```

```json
{
  "kind": "defense_hold",
  "function": "launchDefenseHold",
  "planet_id": 664,
  "origin_planet_id": 664,
  "target_planet_id": 665,
  "target_coordinates": "7:181:15",
  "ships": {"6": 5},
  "speed_pct": 100,
  "hold_seconds": 3600,
  "rule": "operator override",
  "rationale": "Station a fleet at 665 for 1 hour"
}
```

Both require `policy.actions.allow_acs_defense: true`, `policy.strategy.
allow_agent_action_override: true`, and tier `operator`. `vd radar check --alliance-id
<id>` (or `vd tick`'s own report when `policy.actions.allow_alliance` is also on) surfaces
the real, live `hostile_mission_id` to reference — see `coordination.py`.

## The suggestion layer (`coordination.py`)

Derives `CoordinationSuggestion`s from `radar.py`'s own `incoming_fleet` findings — zero
new network calls, zero new persisted state, zero effect on `plan.py`'s ladder or
`guard.py`'s gates (same shape decision `opportunities.py` already made for its own four
families). Filters to `mission_type_name == "Attack"` exactly, the same requirement
`guard._gate_acs_defend_target`'s own launch-path re-check enforces.

**Known limitation, stated plainly**: this module cannot detect whether an
`openDefenseIntent` has already been opened for a given hostile mission — that needs a
direct read of the `AllianceDefenseIntentOpened` event or an equivalent indexed route,
which this codebase does not have. A suggestion is re-surfaced every tick the hostile
mission stays live and reachable, identically, regardless of whether it was already acted
on.
