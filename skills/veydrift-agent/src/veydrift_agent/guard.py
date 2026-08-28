"""`vd guard` — the 19-gate guardrail evaluator (docs/SPEC.md §5.5).

`evaluate_guardrails()` is the pure core: given an `Action`, the `Snapshot` it was
planned from, the `Policy`, the persisted `AgentState`, and a handful of caller-supplied
facts that don't live on any of those frozen/local models (live contract addresses, the
live ABI hash, a built `UnsignedTx` + gas estimate, the wallet's ETH balance), it returns
a `GuardReport` with **all 19 gates evaluated, never short-circuited** — the full
`GuardReport.verdicts` list is the audit artifact (docs/SPEC.md §5.5), so a passing tick
is exactly as informative as a blocked one.

Kept deliberately network-free and side-effect-free: `tick.py` gathers the "caller-
supplied facts" (a live `/runtime-config` fetch via `http.py`, a `walletctl build` shell-
out, etc.) and hands them in as plain parameters. That is what makes this module testable
purely from fixtures, the same posture `plan.py` takes with `killswitch_active` /
`pending_tx_unreconciled`.

**The rule this module is built around (the brief's own words):** a gate must not pass
vacuously when the data it needs is absent. `energy` is `None` because the API omitted
it is not "the energy check passed" — it is "the energy check could not run", and that
must resolve to `BLOCK`/`ESCALATE`, never `PASS`. Every gate below that depends on
optional data follows this rule explicitly; see `references/guardrails.md` for the
gate-by-gate rationale and `tests/test_guard.py` for a dedicated missing-data test per
gate where the risk is real.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from veydrift_agent import calc, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    Decision,
    GuardReport,
    GuardStatus,
    GuardVerdict,
    PlanetSnapshot,
    Policy,
    QueueKind,
    Resources,
    Snapshot,
    Tier,
    UnsignedTx,
)
from veydrift_agent.state import AgentState
from veydrift_agent.techtree import (
    MAX_DEFENSE_PER_PLANET,
    MISSILE_SLOTS,
    EntityFamily,
    describe,
    missile_silo_capacity,
    unmet,
)

app = typer.Typer(no_args_is_help=True, help="Evaluate guardrails against a proposed action.")

# --------------------------------------------------------------------------------------
# Contract-derived constants. Duplicated here (rather than imported from
# skills/veydrift-wallet, a separate TypeScript project this package must never import
# from -- SPEC.md §5.5/§9 acceptance criterion 15) but sourced from the exact same pin:
# skills/veydrift-wallet/abi/PINNED.json, verified against the deployed commit
# 701bed3578cff4d134657c714c599dbdb55a4b6a (docs/SPEC.md §6.6).
# --------------------------------------------------------------------------------------

#: sha256(JSON.stringify(pinned.abi)) at the deployed commit. Mirrors
#: skills/veydrift-wallet/abi/PINNED.json's `abiHash` byte-for-byte; if that file is ever
#: re-pinned, update this constant in the same change.
PINNED_ABI_HASH = "sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99"

#: Contract function name -> the lowest tier allowed to *submit* it. Mirrors
#: skills/veydrift-wallet/src/allowlist.ts's ECONOMY_SIGNATURES /
#: LAUNCH_FLEET_MISSION_SIGNATURES sets at the tier level (this module checks function
#: name only; the wallet engine independently re-checks the full selector + decoded
#: mission-type restriction on the actual calldata — defense in depth, not a single point
#: of truth). `advisor` (tier 1) intentionally allows nothing here: it may propose, never
#: submit (docs/SPEC.md §4).
_MIN_TIER_FOR_FUNCTION: dict[str, Tier] = {
    "startBuildingUpgrade": Tier.ECONOMY,
    "startResearch": Tier.ECONOMY,
    "resolveFleetMission": Tier.ECONOMY,
    # `settlePlanet` removed 2026-08-17 (Phase 5, docs/SPEC.md §5.4/§9 -- a breaking
    # allowlist change). Its body at the pinned commit is byte-identical to
    # `collectResources`, a disguised read `veydrift-wallet`'s `abi.ts` already refuses in
    # `sendTx` -- and no planner rung ever produced this action, so it was allowlisted
    # capacity that could only ever burn gas for zero effect. Mirrors the removal from
    # `ECONOMY_SIGNATURES` in veydrift-wallet/src/allowlist.ts and the encoder branch in
    # tick.py's `_action_to_walletctl_json` -- all three together, or
    # `test_tier_map_agrees_with_the_wallet_engines_allowlist` fails.
    "startDefenseProduction": Tier.ECONOMY,
    # Added 2026-08-12. plan.py rung 8 proposes ships when policy.actions.allow_ships is
    # set, but no tier granted the function, so the knob was dead config: every proposal
    # blocked here forever. Ships are a resource spend on your own planet -- the same risk
    # profile as defense, already at ECONOMY. Combat stays gated at the mission-type level
    # on launchFleetMission. Mirrors ECONOMY_SIGNATURES in veydrift-wallet/src/allowlist.ts.
    "startShipProduction": Tier.ECONOMY,
    "launchFleetMission": Tier.OPERATOR,
}

_TIER_ORDER: dict[Tier, int] = {Tier.ADVISOR: 1, Tier.ECONOMY: 2, Tier.OPERATOR: 3}

#: `FleetMissionType` values `launchFleetMission` may submit unconditionally -- no
#: policy flag affects this set (Phase 5c, docs/SPEC.md §5.5). Default-deny: any
#: `mission_type` not in this set, and not in `_COMBAT_MISSION_TYPES` below with
#: `policy.actions.allow_combat` true, BLOCKs at the `mission_type` gate,
#: independently of the `tier` gate above. Mirrors `allowlist.ts`'s
#: `OPERATOR_ALLOWED_MISSION_TYPES` constant exactly --
#: `test_tier_map_agrees_with_the_wallet_engines_allowlist` parses both and fails naming
#: the diff if they ever drift (AGENTS.md §5: "the two tier-enforcement layers must
#: agree"). Before this gate existed, `allowlist.ts`'s set was the *only* place this
#: restriction was enforced at all -- harmless only because nothing ever proposed
#: `launchFleetMission` (docs/COVERAGE.md); Phase 5c's planner rungs change that, so a
#: second, independent enforcement point is no longer optional.
#:
#: Colonize (2) added 2026-08-17 (Phase 5b, docs/SPEC.md §9). `VeydriftGame.sol`'s
#: `launchFleetMission` facade dispatches `missionType == Colonize` to
#: `VeydriftColonizationModule`; `_launchColonizeFleetMission` ->
#: `_validateColonyCreation` -> `_requireShips(originPlanetId, Ship.ColonyShip, 1)`
#: confirms it as a real colonisation entrypoint, not combat-adjacent (docs/
#: RESEARCH-ADDENDUM.md §4, `references/contract-writes.md` §1).
_ALLOWED_MISSION_TYPES: frozenset[int] = frozenset(
    {
        ids.FleetMissionType.TRANSPORT,
        ids.FleetMissionType.DEPLOY,
        ids.FleetMissionType.COLONIZE,
        ids.FleetMissionType.HARVEST,
    }
)

#: `FleetMissionType` values permitted only when `policy.actions.allow_combat` is
#: `true` (launch-actions plan, commit 5, 2026-08-28). Deliberately a separate set from
#: `_ALLOWED_MISSION_TYPES` above, not merged into it -- mirrors `allowlist.ts`'s
#: `COMBAT_ALLOWED_MISSION_TYPES` constant exactly, and keeps the
#: unconditional-vs-conditional distinction visible at a glance for both the reader and
#: the cross-layer test, which diffs both halves independently.
#:
#: Only **3 Attack**. `5 AcsDefend`, `6 Intercept`, `8 AcsAttack`, `9 DefenseHold` stay
#: out of both sets, at every tier, regardless of `allow_combat` -- AGENTS.md §5:
#: "combat stays unreachable by code, not by config" still governs every combat type
#: this flag does *not* name. All four are alliance-coordination mission types this
#: codebase has no other write path for either (no `joinAttackMission`/
#: `launchDefenseHold` allowlisting exists); enabling any of them requires an actual
#: source change here AND in `allowlist.ts`, never a policy flag alone.
_COMBAT_MISSION_TYPES: frozenset[int] = frozenset({ids.FleetMissionType.ATTACK})

_MINE_ENTITY_IDS = {ids.Building.METAL_MINE, ids.Building.CRYSTAL_MINE, ids.Building.DEUTERIUM_SYNTHESIZER}
_ENERGY_FIX_BUILDINGS = {ids.Building.SOLAR_PLANT, ids.Building.FUSION_REACTOR}

#: Which `techtree` table an `Action.kind` maps to. `RESOLVE_MISSION`/`NOOP`/`ESCALATE`/
#: `HALT` have no entry -- `_gate_prerequisites` treats those as "nothing to check," the
#: same posture `_gate_energy`/`_gate_affordability` take toward an action with no target.
_FAMILY_FOR_ACTION_KIND: dict[ActionKind, EntityFamily] = {
    ActionKind.BUILD: EntityFamily.BUILDING,
    ActionKind.RESEARCH: EntityFamily.RESEARCH,
    ActionKind.SHIP: EntityFamily.SHIP,
    ActionKind.DEFENSE: EntityFamily.DEFENSE,
}


def _verdict(gate: str, status: GuardStatus, detail: str) -> GuardVerdict:
    return GuardVerdict(gate=gate, status=status, detail=detail)


def _format_eta_hm(hours: float) -> str:
    """`1.6333` -> `"1h 38m"`. Rounds to the nearest minute; `"0h 0m"` for a near-miss ETA
    rounding down to zero -- an already-affordable resource never reaches this formatter
    at all (see `_gate_affordability`'s `covers()` short-circuit)."""
    total_minutes = round(hours * 60)
    h, m = divmod(total_minutes, 60)
    return f"{h}h {m}m"


def idempotency_key(action: Action) -> str:
    """`(planet, action, entity)` per docs/SPEC.md §5.5's `idempotency` row -- except for
    two action kinds where that base triple collapses distinct actions onto one key and
    one `AgentState.revert_counts` streak, both fixed together here 2026-08-28 (commit 2
    of the launch-actions plan):

    - **`FLEET_MISSION`** (`launchFleetMission`): `entity_id` is always `None` (a fleet
      mission carries a `ships` map, not a single entity), so every fleet mission
      launched from one planet -- Transport, Deploy, Colonize, Harvest, and (once later
      commits add them) Attack and Missile -- would otherwise share one key. Fixed by
      folding in `mission_type` and the target (`action.target_coordinates`).
    - **`RESOLVE_MISSION`** (`resolveFleetMission`): `plan.py`'s rung 3 never sets
      `planet_id` (only `mission_id`), so *every* resolve action collapsed onto the
      single global key `"None:resolveFleetMission:None"` regardless of which mission was
      being resolved -- a second real bug in the same base formula, found while fixing
      the first. Fixed by folding in `mission_id` instead.

    Both were live before any mission type beyond Transport/Harvest could actually be
    proposed, so this closes the gap before the wider mission-type surface this plan adds
    could turn a latent collision into a routine one.

    Shared with `state.PendingTx.key` / `AgentState.revert_counts` so `idempotency` and
    `revert_streak` key off the exact same identity. No migration needed for the format
    change: confirmed directly against this project's own `agent-state.json` that no
    account has ever accumulated fleet-mission or resolve-mission state under the old key
    (`revert_counts: {}`, `executions_count: 0` at the time of this change) -- see this
    package's `CHANGELOG.md`'s `1.8.0` entry."""
    key = f"{action.planet_id}:{action.function}:{action.entity_id}"
    if action.kind is ActionKind.FLEET_MISSION:
        key = f"{key}:{action.mission_type}:{action.target_coordinates}"
    elif action.kind is ActionKind.RESOLVE_MISSION:
        key = f"{key}:{action.mission_id}"
    return key


#: `gas`/`eth_floor` ESCALATE (never BLOCK) whenever their live data simply isn't
#: available yet -- and at tier 1, `tick.py` never even calls `walletctl status` for a
#: balance (`eth_floor` is unconditionally unknown there), and `gas` is unknown too
#: whenever no wallet provider is configured to produce a build-time gas estimate (the
#: normal pre-promotion state). Both are already documented in
#: `references/guardrails.md` as "expected, not alarming" at tier 1 -- this constant is
#: what makes `is_structural_tier_block` agree with that document rather than contradict
#: it. Only the ESCALATE (missing-data) form counts as noise here: a `gas`/`eth_floor`
#: verdict that is a BLOCK (a real ceiling/floor breach, not missing data) is always
#: substantive, at any tier.
_TIER1_EXPECTED_ESCALATIONS = {("gas", "escalate"), ("eth_floor", "escalate")}


def is_structural_tier_block(non_passing_gates: list[tuple[str, str]]) -> bool:
    """True when the *entire* reason decision != ALLOW is exactly the noise cluster
    tier 1 produces on every single onchain proposal before a wallet is configured: the
    `tier` gate BLOCKing (mandatory below a function's minimum tier -- docs/SPEC.md §4),
    optionally alongside `gas`/`eth_floor` ESCALATEing purely because no gas estimate or
    ETH balance is available yet.

    This is not a guess: a routine tier-1 proposal on an unlocked entity (the
    `prerequisites` gate PASSes -- nothing about a plain mine upgrade is locked) shows
    exactly `guards: 16/19 pass (block)`, and the 3 gates that don't pass there are
    precisely `tier` (BLOCK), `gas` (ESCALATE, no estimate), `eth_floor` (ESCALATE,
    balance never checked at tier 1) -- this predicate is written to recognise exactly
    that cluster as carrying zero promotion-relevant information, matching what
    `references/guardrails.md` already says about `gas`/`eth_floor` missing data being
    routine and non-alarming at tier 1.

    `tier` BLOCKing is required for a result to count as structural at all -- `non_passing_gates`
    without it (e.g. only `gas`/`eth_floor` escalating on an otherwise-ALLOWed action)
    is a different situation entirely and must not be silently absorbed here.

    Any *other* non-passing gate -- affordability/energy/storage/fields/reserve/
    idempotency/revert_streak/etc, or a `gas`/`eth_floor` verdict that is itself a BLOCK
    (a real ceiling/floor breach, not missing data) -- means real information fired too,
    so the whole proposal is NOT purely structural even though `tier` is still one of the
    reasons decision != ALLOW.

    `non_passing_gates` is `[(gate, status), ...]` for every verdict that isn't `"pass"`
    (accepts plain strings, not the `GuardStatus`/enum objects, so this same predicate
    works both on a live `GuardReport.verdicts` list and on `proposals.jsonl`'s
    already-serialised dicts -- see `tick.py`'s two callers).
    """
    if ("tier", "block") not in non_passing_gates:
        return False
    return all(gate_status == ("tier", "block") or gate_status in _TIER1_EXPECTED_ESCALATIONS for gate_status in non_passing_gates)


# --------------------------------------------------------------------------------------
# Individual gates. Each takes exactly the inputs it needs and returns one GuardVerdict.
# Grouped in the table order of docs/SPEC.md §5.5.
# --------------------------------------------------------------------------------------


def _gate_killswitch(*, killswitch_active: bool) -> GuardVerdict:
    if killswitch_active:
        return _verdict("killswitch", GuardStatus.BLOCK, "$VEYDRIFT_HOME/KILLSWITCH is present")
    return _verdict("killswitch", GuardStatus.PASS, "KILLSWITCH absent")


def _gate_tier(action: Action, policy: Policy) -> GuardVerdict:
    if action.function is None:
        return _verdict("tier", GuardStatus.PASS, "action has no on-chain function (noop/escalate/halt)")
    min_tier = _MIN_TIER_FOR_FUNCTION.get(action.function)
    if min_tier is None:
        return _verdict("tier", GuardStatus.BLOCK, f"{action.function} is not in any tier's allowed set")
    if _TIER_ORDER[policy.tier] < _TIER_ORDER[min_tier]:
        return _verdict(
            "tier",
            GuardStatus.BLOCK,
            f"{action.function} requires tier >= {min_tier.value}; policy tier is {policy.tier.value}",
        )
    return _verdict("tier", GuardStatus.PASS, f"{action.function} allowed at tier {policy.tier.value}")


#: Field widths `_decodeColonyTarget` masks against
#: (`VeydriftColonizationModule.sol:42-46,482-492`, pinned commit 701bed35):
#: ``COLONIZATION_COORDINATE_MASK = 0xffff`` for both galaxy and system (each packed as
#: a `uint16`), ``COLONIZATION_POSITION_MASK = 0xff`` for position (packed as a `uint8`
#: occupying the low byte directly, not shifted). Verified directly against the pinned
#: source, not merely trusted from a brief. Duplicated from `tick.py`'s
#: `_encode_colony_target` bounds, not imported -- this module never imports from
#: `tick.py` (the same "duplicated here" posture every other contract-derived constant
#: in this module already takes).
_COLONIZATION_GALAXY_MAX = 0xFFFF
_COLONIZATION_SYSTEM_MAX = 0xFFFF
_COLONIZATION_POSITION_MAX = 0xFF


def _colony_target_range_violation(coordinates: str | None) -> str | None:
    """`None` when `coordinates` packs cleanly into `_encode_colony_target`'s on-chain
    representation; otherwise a detail string. Judge finding 2 (2026-08-17): a
    galaxy/system/position value outside these masks does not raise on-chain at encode
    time -- there is no Solidity-side call in this codebase's write path, `tick.py`'s
    Python function IS the encoder -- it silently collides with an adjacent field's bits
    during the `|` pack, producing a *different, still-valid-looking* target
    (`"1:2:300"` decodes as galaxy 1, system 3, position 44: position's low-byte overflow
    adds 1 to system). This is `guard.py`'s independent re-check of `tick.py`'s own bounds
    check, the same defense-in-depth posture every other duplicated check in this module
    takes."""
    if coordinates is None:
        return "no target_coordinates set for a Colonize mission"
    parts = coordinates.split(":")
    if len(parts) != 3:
        return f"{coordinates!r} is not a 'G:S:P' coordinate string"
    try:
        galaxy, system, position = (int(p) for p in parts)
    except ValueError:
        return f"{coordinates!r} is not a 'G:S:P' coordinate string"
    if not (0 <= galaxy <= _COLONIZATION_GALAXY_MAX):
        return f"galaxy {galaxy} out of range [0, {_COLONIZATION_GALAXY_MAX}]"
    if not (0 <= system <= _COLONIZATION_SYSTEM_MAX):
        return f"system {system} out of range [0, {_COLONIZATION_SYSTEM_MAX}]"
    if not (0 <= position <= _COLONIZATION_POSITION_MAX):
        return f"position {position} out of range [0, {_COLONIZATION_POSITION_MAX}]"
    return None


def _astrophysics_level(snapshot: Snapshot) -> int:
    """Absent-from-`technologies` (never researched) and present-with-`level=None` both
    mean level 0 -- same convention `candidates.py`'s `_energy_technology_level` already
    uses for this exact shape of data; not "unknown," since `/research` always reports
    every technology it knows about."""
    entity = next((t for t in snapshot.technologies if t.id == ids.Technology.ASTROPHYSICS), None)
    return entity.level if entity is not None and entity.level is not None else 0


def _colony_cap_violation(snapshot: Snapshot, *, outgoing_colonize_count: int | None) -> str | None:
    """`None` when a Colonize mission would not exceed
    `VeydriftColonizationModule.sol:289-301`'s per-account colony cap (`limit = 1 +
    astrophysicsLevel`, :func:`calc.max_planets`); otherwise a detail string.

    Fails closed on `snapshot.owned_planet_count is None` rather than assuming "not yet
    at the cap" -- AGENTS.md §5's "a guardrail must never pass vacuously on absent data."
    Deliberately keys off `owned_planet_count`, not `len(snapshot.planets)`: the latter
    can be a single-planet subset of the account's real holdings (see
    `Snapshot.owned_planet_count`'s own docstring for why), which would let this check
    silently under-count and pass an already-at-cap account.

    **`outgoing_colonize_count` (commit 4 of the launch-actions plan)**: `owned_planet_
    count` alone only reflects planets that have already resolved. Colonize's own
    `resolveFleetMission` re-checks the cap at arrival but does **not** revert on
    failure -- `VeydriftColonizationModule.sol:255-260` silently flips the mission to
    `Returning` instead, so two Colonize proposals on consecutive ticks could both pass
    this check under the old (single-field) formula, and the second would silently
    bounce home at arrival with a `status: "success"` resolve receipt and no colony
    created. `outgoing_colonize_count` -- the wallet's own still-`Outbound` Colonize
    missions, supplied by `tick._outgoing_colonize_count` -- closes that gap by folding
    in-flight missions into the projected count. **`None` here fails closed exactly like
    `owned_planet_count is None` does** -- an unfetchable in-flight count is not "assume
    zero in flight," the same AGENTS.md §5 rule this function already applies to its
    other input."""
    if snapshot.owned_planet_count is None:
        return "owned planet count is unknown -- cannot verify the colony cap"
    if outgoing_colonize_count is None:
        return "in-flight Colonize mission count is unknown -- cannot verify the colony cap accounts for them"
    limit = calc.max_planets(_astrophysics_level(snapshot))
    projected = snapshot.owned_planet_count + outgoing_colonize_count
    if projected >= limit:
        in_flight_note = f" + {outgoing_colonize_count} in-flight Colonize" if outgoing_colonize_count else ""
        return (
            f"already at or would exceed the colony cap ({snapshot.owned_planet_count} owned"
            f"{in_flight_note} >= {limit}; limit = 1 + astrophysicsLevel) -- "
            "VeydriftColonizationModule reverts PlanetLimitReached rather than silently "
            "declining the mission"
        )
    return None


def _gate_mission_type(
    action: Action, snapshot: Snapshot, policy: Policy, *, outgoing_colonize_count: int | None = None
) -> GuardVerdict:
    """Phase 5c (docs/SPEC.md §5.5): default-deny gate for `launchFleetMission`'s
    `mission_type` argument, independent of and in addition to the `tier` gate above.
    Mirrors `allowlist.ts`'s calldata-level mission-type check -- defense in depth, not a
    single point of truth (same posture every other duplicated check in this module
    takes).

    Fails closed on `action.mission_type is None`: a `launchFleetMission` action with no
    mission type set is not "nothing to check" (the `prerequisites`/`energy`/etc. gates'
    "no target" PASS), it is a malformed action that must never reach the wallet engine --
    AGENTS.md §5's "a guardrail must never pass vacuously on absent data," applied to this
    gate's own input rather than to snapshot data. Any action that is not
    `launchFleetMission` PASSes trivially, same as every other function-specific gate in
    this module (`address`/`abi_hash`/`gas`/`eth_floor` all take this shape toward
    `action.is_onchain()`).

    **`policy` parameter, launch-actions plan commit 5**: the allowed set is
    `_ALLOWED_MISSION_TYPES`, plus `_COMBAT_MISSION_TYPES` (Attack only) when
    `policy.actions.allow_combat` is `true`. Combat requires tier `operator` on top of
    this, exactly like every other `launchFleetMission` mission type -- `allow_combat`
    widens *which* mission type is permitted, never the tier requirement itself.

    Colonize additionally goes through `_colony_target_range_violation` (below) and, new
    here, `_colony_cap_violation` -- a Colonize that would exceed `calc.max_planets`'s cap
    is `BLOCK`ed the same way an out-of-range target is: both are cases where the contract
    call would revert or corrupt state rather than the wallet-engine boundary catching it
    first, so this gate catches them before send instead of after.
    """
    if action.function != "launchFleetMission":
        return _verdict("mission_type", GuardStatus.PASS, "action is not launchFleetMission")
    if action.mission_type is None:
        return _verdict(
            "mission_type",
            GuardStatus.BLOCK,
            "launchFleetMission action has no mission_type set -- cannot verify it against "
            "the allowed set; a malformed action, not nothing to check",
        )
    allowed = _ALLOWED_MISSION_TYPES | (_COMBAT_MISSION_TYPES if policy.actions.allow_combat else frozenset())
    if action.mission_type not in allowed:
        name = ids.mission_type_name(action.mission_type)
        return _verdict(
            "mission_type",
            GuardStatus.BLOCK,
            f"mission_type {action.mission_type} ({name}) is not in the allowed set {sorted(allowed)} "
            "(Transport/Deploy/Colonize/Harvest always; Attack only with "
            f"policy.actions.allow_combat=true, currently {policy.actions.allow_combat})",
        )
    if action.mission_type == ids.FleetMissionType.COLONIZE:
        # Judge finding 2: independently re-check tick.py's own colony-target bounds --
        # an out-of-range coordinate doesn't fail on-chain, it silently corrupts the
        # packed target (see _colony_target_range_violation's docstring).
        violation = _colony_target_range_violation(action.target_coordinates)
        if violation is not None:
            return _verdict(
                "mission_type",
                GuardStatus.BLOCK,
                f"Colonize target_coordinates invalid: {violation} -- an out-of-range "
                "value would silently corrupt the packed on-chain colony target rather "
                "than raise",
            )
        cap_violation = _colony_cap_violation(snapshot, outgoing_colonize_count=outgoing_colonize_count)
        if cap_violation is not None:
            return _verdict("mission_type", GuardStatus.BLOCK, f"Colonize mission blocked: {cap_violation}")
    return _verdict(
        "mission_type",
        GuardStatus.PASS,
        f"mission_type {action.mission_type} ({ids.mission_type_name(action.mission_type)}) is allowed",
    )


def _defense_count(planet: PlanetSnapshot, defense_id: int) -> int | None:
    entity = next((d for d in planet.defenses if d.id == defense_id), None)
    return entity.count if entity is not None else None


def _queued_defense_quantity(planet: PlanetSnapshot, defense_id: int) -> int:
    """Best-effort re-derivation of `_queuedDefenseQuantity`
    (`VeydriftDefenseProductionModule.sol:398-`) from the single `QueueEntry`
    `PlanetSnapshot` carries per queue kind -- there is no backlog list in the frozen
    `models.py` (`techtree.py`'s own docstring on `MAX_DEFENSE_PER_PLANET` flags this).
    Returns `0` when nothing matching this defense id is currently queued; a caller
    already-built + this always undercounts rather than overcounts a real backlog deeper
    than one entry, which is the safe-to-under-restrict-yourself direction for a queue
    quantity feeding a cap check, not the safe-to-vacuously-pass one."""
    entry = planet.queues.get(QueueKind.DEFENSE)
    if entry is None or entry.entity_id != defense_id:
        return 0
    return entry.quantity or 0


def _defense_cap_violation(action: Action, planet: PlanetSnapshot) -> str | None:
    """Re-derives `_requireDefenseCapacity` (`VeydriftDefenseProductionModule.sol:352-380`)
    independently from the snapshot: the shield-dome per-planet cap
    (`techtree.MAX_DEFENSE_PER_PLANET`) and the missile-silo slot cap
    (`techtree.MISSILE_SLOTS` / `missile_silo_capacity`). Returns `None` when nothing is
    violated, else a detail string for the `prerequisites` gate's `BLOCK`. Fails closed:
    a defense/missile count the snapshot didn't report BLOCKs rather than being treated
    as zero."""
    defense_id = action.entity_id
    quantity = action.quantity if action.quantity is not None else 1

    cap = MAX_DEFENSE_PER_PLANET.get(defense_id)
    if cap is not None:
        built = _defense_count(planet, defense_id)
        if built is None:
            return (
                f"{action.entity_name or f'defense {defense_id}'} count not reported for "
                f"planet {planet.planet_id}; cannot verify the {cap}-per-planet cap"
            )
        queued = _queued_defense_quantity(planet, defense_id)
        projected = built + queued + quantity
        if projected > cap:
            return (
                f"{action.entity_name or f'defense {defense_id}'} is capped at {cap} per "
                f"planet (built {built} + queued {queued} + this action {quantity} = "
                f"{projected})"
            )

    slots_per_unit = MISSILE_SLOTS.get(defense_id, 0)
    if slots_per_unit:
        silo_entity = next((b for b in planet.buildings if b.id == ids.Building.MISSILE_SILO), None)
        silo_level = silo_entity.level if silo_entity is not None else None
        if silo_level is None:
            return (
                f"Missile Silo level not reported for planet {planet.planet_id}; cannot "
                "verify missile slot capacity"
            )
        capacity = missile_silo_capacity(silo_level)
        used = 0
        for missile_id, slots in MISSILE_SLOTS.items():
            count = _defense_count(planet, missile_id)
            if count is None:
                return (
                    f"{ids.defense_name(missile_id)} count not reported for planet "
                    f"{planet.planet_id}; cannot verify missile slot capacity"
                )
            used += slots * count
            used += slots * _queued_defense_quantity(planet, missile_id)
        requested = slots_per_unit * quantity
        if used + requested > capacity:
            return (
                f"{action.entity_name or f'defense {defense_id}'} would use {requested} "
                f"missile silo slot(s); {used} already used/queued against a capacity of "
                f"{capacity} (Missile Silo level {silo_level})"
            )
    return None


def _gate_fleet_ship_availability(action: Action, snapshot: Snapshot) -> GuardVerdict:
    """`prerequisites`' `FLEET_MISSION`-specific branch (also-worth-fixing #2, judge
    review 2026-08-17): does the origin planet actually own enough of every ship
    `action.ships` commits? Before this, `FLEET_MISSION` had no entry in
    `_FAMILY_FOR_ACTION_KIND`, so `_gate_prerequisites` PASSed it trivially -- correct for
    "does this entity have its on-chain unlock prerequisites" (a fleet mission proposes no
    new building/tech/ship/defense entity), but it left "does the origin planet actually
    own these ships" unchecked anywhere in this module. `candidates.py`'s generators only
    ever commit already-built ships, but this is `guard.py`'s own independent re-check of
    that invariant -- the same defense-in-depth posture every other gate here takes toward
    the planner, and the one Finding 1 showed is not optional for this action family.
    Fails closed: a ship count the snapshot didn't report is unverifiable, not "0 built"."""
    if not action.ships:
        return _verdict("prerequisites", GuardStatus.PASS, "fleet mission commits no ships to check")
    if action.origin_planet_id is None:
        return _verdict(
            "prerequisites", GuardStatus.BLOCK, "fleet mission has no origin_planet_id to check ship availability against"
        )
    planet = snapshot.planet(action.origin_planet_id)
    if planet is None:
        return _verdict("prerequisites", GuardStatus.BLOCK, f"planet {action.origin_planet_id} not found in snapshot")
    owned = {e.id: e.count for e in planet.ships}
    shortfalls: list[str] = []
    for ship_id, requested in action.ships.items():
        if requested <= 0:
            continue
        built = owned.get(ship_id)
        if built is None:
            shortfalls.append(f"{ids.ship_name(ship_id)} count not reported for planet {planet.planet_id}")
        elif built < requested:
            shortfalls.append(f"{ids.ship_name(ship_id)} requests {requested}, only {built} built")
    if shortfalls:
        return _verdict(
            "prerequisites", GuardStatus.BLOCK, "fleet mission ship availability unverified/insufficient: " + "; ".join(shortfalls)
        )
    return _verdict("prerequisites", GuardStatus.PASS, f"origin planet {planet.planet_id} has every ship this mission commits")


def _gate_fleet_slots(action: Action, snapshot: Snapshot) -> GuardVerdict:
    """New gate, commit 2 of the launch-actions plan. Every `_launchFleetMission` path on
    the deployed contract reverts `FleetSlotLimitReached(1 + ComputerTechnology)` when
    `activeFleetMissionCount[msg.sender] >= fleetSlotLimit` -- confirmed at four separate
    call sites in `VeydriftGameplayModule.sol`/`VeydriftColonizationModule.sol`/
    `VeydriftDefenseHoldModule.sol`, all the same formula. `Snapshot` already carries both
    halves (`fleet_slots_active`/`fleet_slots_limit`, sourced from `/wallet/{addr}/
    shipyard`'s live `fleetSlots` block -- no new fetch needed), so this is a pure
    re-derivation, the same defense-in-depth posture every other gate here takes.

    Scoped to `FLEET_MISSION` only (`launchFleetMission`) -- `resolveFleetMission`
    *frees* a slot rather than consuming one, and `launchInterplanetaryMissileAttack`
    (a later commit) is fully synchronous and consumes no fleet slot at all, confirmed
    directly against its contract implementation.

    Fails closed on missing data, never PASSes vacuously: either field being `None` means
    "unverifiable this tick," not "assume a slot is free" -- the same posture every other
    gate here takes toward absent data (AGENTS.md §5)."""
    if action.kind is not ActionKind.FLEET_MISSION:
        return _verdict("fleet_slots", GuardStatus.PASS, "action is not launchFleetMission")
    if snapshot.fleet_slots_active is None or snapshot.fleet_slots_limit is None:
        return _verdict(
            "fleet_slots", GuardStatus.BLOCK, "fleet slot usage/limit is unknown -- cannot verify a slot is free"
        )
    if snapshot.fleet_slots_active >= snapshot.fleet_slots_limit:
        return _verdict(
            "fleet_slots",
            GuardStatus.BLOCK,
            f"no free fleet slot ({snapshot.fleet_slots_active}/{snapshot.fleet_slots_limit} active; "
            "limit = 1 + ComputerTechnology) -- the contract reverts FleetSlotLimitReached",
        )
    free = snapshot.fleet_slots_limit - snapshot.fleet_slots_active
    return _verdict(
        "fleet_slots", GuardStatus.PASS, f"{free} fleet slot(s) free ({snapshot.fleet_slots_active}/{snapshot.fleet_slots_limit})"
    )


def _gate_prerequisites(action: Action, snapshot: Snapshot) -> GuardVerdict:
    """New gate (docs/SPEC.md §5.5), slotted immediately after `tier` and before
    `address`. Independently re-derives the planet's building/technology level vectors
    from `snapshot` -- never trusts `plan.py`'s own filtering, exactly as `_gate_energy`
    already re-derives the energy invariant rather than calling `plan.py`. BLOCKs on any
    unmet `techtree` requirement, on any `have=None` (a level the snapshot didn't report
    -- fail closed, never PASS on absent data), and on a shield-dome/missile-slot cap
    violation.

    `FLEET_MISSION` gets its own branch (`_gate_fleet_ship_availability`) -- see that
    function's docstring.

    Actions with no entity to check (`resolve_mission`/`noop`/`escalate`/`halt`, or any
    action missing `entity_id`) PASS trivially -- there is nothing here for this gate to
    say anything about, the same posture `_gate_energy`/`_gate_affordability` take toward
    an action with no target planet.
    """
    if action.kind is ActionKind.FLEET_MISSION:
        return _gate_fleet_ship_availability(action, snapshot)

    family = _FAMILY_FOR_ACTION_KIND.get(action.kind)
    if family is None or action.entity_id is None:
        return _verdict("prerequisites", GuardStatus.PASS, "action has no entity to check prerequisites for")
    if action.planet_id is None:
        return _verdict(
            "prerequisites",
            GuardStatus.BLOCK,
            f"{action.entity_name or action.entity_id} has no target planet to derive levels from",
        )
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("prerequisites", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")

    building_levels: dict[int, int | None] = {b.id: b.level for b in planet.buildings}
    technology_levels: dict[int, int | None] = {t.id: t.level for t in snapshot.technologies}

    unmet_reqs = unmet(family, action.entity_id, building_levels=building_levels, technology_levels=technology_levels)
    if unmet_reqs:
        detail = "; ".join(describe(u) for u in unmet_reqs)
        return _verdict(
            "prerequisites",
            GuardStatus.BLOCK,
            f"{action.entity_name or action.entity_id} on-chain prerequisites unmet: {detail}",
        )

    if family is EntityFamily.DEFENSE:
        cap_violation = _defense_cap_violation(action, planet)
        if cap_violation is not None:
            return _verdict("prerequisites", GuardStatus.BLOCK, cap_violation)

    return _verdict(
        "prerequisites",
        GuardStatus.PASS,
        f"{action.entity_name or action.entity_id} on-chain prerequisites satisfied",
    )


def _gate_address(action: Action, *, live_addresses: set[str] | None, unsigned_tx: UnsignedTx | None) -> GuardVerdict:
    if not action.is_onchain():
        return _verdict("address", GuardStatus.PASS, "action has no destination to check")
    if not live_addresses:
        # Missing data, not "nothing to check" -- an onchain action with no live address
        # set to check against must not pass vacuously.
        return _verdict("address", GuardStatus.BLOCK, "could not fetch a live /runtime-config address set")
    if unsigned_tx is None:
        return _verdict("address", GuardStatus.BLOCK, "no built transaction available to check a destination on")
    to = unsigned_tx.to.lower()
    if to not in {a.lower() for a in live_addresses}:
        return _verdict("address", GuardStatus.BLOCK, f"{unsigned_tx.to} is not in the live contract address set")
    return _verdict("address", GuardStatus.PASS, f"{unsigned_tx.to} is a live Veydrift contract address")


def _gate_abi_hash(action: Action, snapshot: Snapshot) -> GuardVerdict:
    if not action.is_onchain():
        return _verdict("abi_hash", GuardStatus.PASS, "action has no calldata to pin-check")
    live_hash = snapshot.deployment_abi_hash
    if not live_hash:
        return _verdict("abi_hash", GuardStatus.BLOCK, "live deploymentAbiHash missing from snapshot; blocking all writes")
    if live_hash != PINNED_ABI_HASH:
        return _verdict(
            "abi_hash",
            GuardStatus.BLOCK,
            f"live {live_hash} != pinned {PINNED_ABI_HASH} -- contract upgraded, blocking all writes",
        )
    return _verdict("abi_hash", GuardStatus.PASS, "live deploymentAbiHash matches the pinned commit")


def _gate_health(snapshot: Snapshot) -> GuardVerdict:
    """`plan.py`'s rung 1 is the first line of defense for the same distinction below --
    this is the second, independent one (same two-layer shape as `_gate_game_paused`):
    a proposal reaching `guard.py` must still be re-checked, not trusted from upstream."""
    if snapshot.health_ok:
        return _verdict("health", GuardStatus.PASS, "/health ok and ready")
    if snapshot.combat_only_degradation():
        return _verdict(
            "health",
            GuardStatus.PASS,
            "/health reported ok=false, but positively confirmed as a randomness/combat-"
            "readiness-only degradation (readiness.ready=true, no other degradation "
            "reasons, game not paused) -- irrelevant to this codebase, which never "
            "proposes combat regardless of policy",
        )
    return _verdict("health", GuardStatus.BLOCK, "/health reported not ok / not ready")


def _gate_game_paused(snapshot: Snapshot) -> GuardVerdict:
    """Second, independent line of defense -- plan.py's rung 1b is the first. BLOCKs
    unconditionally (not ESCALATE): by the time a proposal reaches guard.py, a confirmed
    pause is a hard safety fact, not the discretionary call plan.py already made.
    Fail-closed like _gate_energy: game_maintenance is None means unconfirmed, not
    confirmed-clear.

    Takes only `snapshot` (like `_gate_health`), not `action` -- a pause blocks every
    write universally, it isn't planet/entity-scoped like `_gate_energy`."""
    if snapshot.game_maintenance is None:
        return _verdict(
            "game_paused", GuardStatus.BLOCK,
            "gameMaintenance missing from /health; cannot confirm the game is not "
            "paused -- this is a check that could not run, not one that passed",
        )
    if snapshot.game_maintenance.paused:
        reasons = ", ".join(snapshot.degradation_reasons) or "game_paused"
        return _verdict(
            "game_paused", GuardStatus.BLOCK,
            f"gameMaintenance.paused is true ({reasons}); any write would revert "
            "on-chain during a chain-side maintenance pause",
        )
    return _verdict("game_paused", GuardStatus.PASS, "gameMaintenance.paused is false")


def _gate_index_lag(policy: Policy, agent_state: AgentState, *, now) -> GuardVerdict:
    pending = agent_state.pending
    if pending is None:
        return _verdict("index_lag", GuardStatus.PASS, "no pending receipt awaiting indexing")
    if pending.indexed_at is not None:
        return _verdict("index_lag", GuardStatus.PASS, f"{pending.key} indexed at {pending.indexed_at.isoformat()}")
    if pending.receipt_at is None:
        return _verdict("index_lag", GuardStatus.WARN, f"{pending.key} sent but no receipt recorded yet")
    elapsed = (now - pending.receipt_at).total_seconds()
    if elapsed >= policy.limits.max_index_wait_s:
        return _verdict(
            "index_lag",
            GuardStatus.BLOCK,
            f"{pending.key} receipt is {elapsed:.0f}s old, exceeding max_index_wait_s="
            f"{policy.limits.max_index_wait_s}s; halting rather than act on stale indexed state",
        )
    return _verdict("index_lag", GuardStatus.WARN, f"{pending.key} awaiting index, {elapsed:.0f}s elapsed")


#: `VeydriftGameStorage.sol:52` (`LOCAL_HARVEST_DISTANCE`) -- duplicated from
#: `candidates.py`'s own constant of the same name and value, not imported: this module
#: never imports from `candidates.py` (the module docstring's "duplicated here, sourced
#: from the exact same pin" convention, already applied to `PINNED_ABI_HASH` and
#: `_MIN_TIER_FOR_FUNCTION`/`_ALLOWED_MISSION_TYPES` above -- two independent
#: implementations of the same contract rule, so a bug in one is unlikely to also be in
#: the other). A same-planet Harvest (`origin_planet_id == planet_id`,
#: `target_coordinates` unset) uses this fixed distance instead of `calc.distance`, which
#: is undefined for two identical coordinates in the sense the contract means here.
_LOCAL_HARVEST_DISTANCE = 5


def _drive_tech_levels(snapshot: Snapshot) -> tuple[int, int, int]:
    """`(combustion_drive_level, impulse_drive_level, hyperspace_drive_level)` -- `0` for
    any technology the snapshot doesn't report. Duplicated from `candidates.py`'s
    function of the same name and behaviour, not imported, for the same reason every
    other contract-derived value in this module is duplicated rather than shared."""

    def _level(tech_id: int) -> int:
        entity = next((t for t in snapshot.technologies if t.id == tech_id), None)
        return entity.level if entity is not None and entity.level is not None else 0

    return (
        _level(ids.Technology.COMBUSTION_DRIVE),
        _level(ids.Technology.IMPULSE_DRIVE),
        _level(ids.Technology.HYPERSPACE_DRIVE),
    )


def _derive_fleet_mission_spend(action: Action, snapshot: Snapshot) -> Resources | None:
    """Independently re-derive a `FLEET_MISSION` action's true launch spend -- cargo plus
    fuel, fuel counted as deuterium (`VeydriftGameplayModule.sol:246-260`, pinned commit
    701bed35: ``_spend(origin, {..., deuterium: cargo.deuterium + fuelCost})``) -- from
    `action.ships` / `action.origin_planet_id` / `action.target_coordinates` alone,
    **never** from `action.cost`.

    This is `guard.py`'s own defense-in-depth check for the one action family whose true
    cost previously lived off `Action.cost` entirely (judge finding 1, 2026-08-17):
    `candidates.py`'s logistics generators built a `FLEET_MISSION` `Action` without ever
    setting `cost`, so `affordability`/`reserve`/`value_ceiling` all evaluated a real
    resource spend as zero and passed vacuously -- defeating this module's own defense-in-
    depth claim for exactly the one action family that moves resources off-planet. A
    planner that forgets to populate `cost` again must still be caught here, the same
    posture `_gate_energy` already takes toward `plan.py`'s energy invariant: an
    independent re-derivation, never a call into the planner's own code.

    For every other `ActionKind` this is just `action.cost`, unchanged.

    Returns `None` when the spend cannot be verified (unknown route, absent ship/route/
    technology data) -- **unverifiable, never zero** (AGENTS.md §5's "a guardrail must
    never pass vacuously on absent data," applied to this derivation's own inputs, not
    just to snapshot data)."""
    if action.kind is not ActionKind.FLEET_MISSION:
        return action.cost
    if not action.ships or action.origin_planet_id is None:
        return None
    origin = snapshot.planet(action.origin_planet_id)
    if origin is None or origin.coordinates is None:
        return None

    if action.target_coordinates is None:
        # Local Harvest special case: target IS origin (candidates.py's own convention;
        # tick.py's encoder resolves it the same way). Any other target_coordinates-unset
        # mission is malformed, not "nothing to check" -- fail closed.
        if action.mission_type != ids.FleetMissionType.HARVEST:
            return None
        distance = _LOCAL_HARVEST_DISTANCE
    else:
        try:
            distance = calc.distance(origin.coordinates, action.target_coordinates)
        except (ValueError, TypeError):
            return None

    combustion, impulse, hyperspace = _drive_tech_levels(snapshot)
    ship_stats: list[tuple[int, int, int]] = []  # (fuel_consumption, count, speed)
    for ship_id, count in action.ships.items():
        if count <= 0:
            continue
        try:
            _, fuel_consumption, speed = calc.ship_movement_stats(ship_id, combustion, impulse, hyperspace)
        except (KeyError, ValueError):
            return None
        ship_stats.append((fuel_consumption, count, speed))
    if not ship_stats:
        return None

    slowest_speed = min(speed for _, _, speed in ship_stats)
    fuel = calc.mission_fuel(ship_stats, distance, slowest_speed)
    return Resources(
        metal=action.cargo.metal,
        crystal=action.cargo.crystal,
        deuterium=action.cargo.deuterium + fuel,
    )


def _gate_affordability(action: Action, snapshot: Snapshot) -> GuardVerdict:
    if action.planet_id is None:
        return _verdict("affordability", GuardStatus.PASS, "action has no target planet to check cost against")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("affordability", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    spend = _derive_fleet_mission_spend(action, snapshot)
    if spend is None:
        return _verdict(
            "affordability",
            GuardStatus.BLOCK,
            "fleet-mission spend could not be independently verified (missing ships/route/"
            "technology data) -- refusing to treat an unverifiable cost as zero",
        )
    if planet.resources_as_of_now.covers(spend):
        return _verdict("affordability", GuardStatus.PASS, "resourcesAsOfNow covers the proposed cost")

    # Best-effort, informational only -- never changes this gate's BLOCK decision, which
    # is already fixed by the covers() check above. Each short resource gets its own
    # clause rather than a single collapsed number: a reader takes the max (every
    # resource must clear at once) by inspection, and collapsing would hide which
    # resource is actually the bottleneck.
    eta_bits: list[str] = []
    for label, cost, current, per_hour, cap in (
        ("Metal", spend.metal, planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
        ("Crystal", spend.crystal, planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
        (
            "Deuterium",
            spend.deuterium,
            planet.resources_as_of_now.deuterium,
            planet.production_per_hour.deuterium,
            planet.storage_caps.deuterium,
        ),
    ):
        if current >= cost:
            continue  # this resource isn't the short one
        shortfall = cost - current
        hours = calc.hours_to_afford(current, per_hour, cost, cap)
        if hours is None:
            reason = "cost exceeds storage cap" if cost > cap else "no production"
            eta_bits.append(f"{shortfall} more {label} (never affordable: {reason})")
        else:
            eta_bits.append(f"{shortfall} more {label} (affordable in ~{_format_eta_hm(hours)})")
    eta_text = "; ".join(eta_bits)

    return _verdict(
        "affordability",
        GuardStatus.BLOCK,
        f"resourcesAsOfNow does not cover cost (need M{spend.metal} C{spend.crystal} "
        f"D{spend.deuterium}; have M{planet.resources_as_of_now.metal} "
        f"C{planet.resources_as_of_now.crystal} D{planet.resources_as_of_now.deuterium}) -- {eta_text}",
    )


def _gate_energy(action: Action, snapshot: Snapshot) -> GuardVerdict:
    """The gate the brief specifically calls out: `planet.energy is None` must never read
    as "the energy check passed" -- it means the check could not run, so it BLOCKs. When
    energy data *is* present, this independently re-derives the post-upgrade requirement
    for a mine action (mirroring, not calling, `plan._next_building_action`'s own
    invariant -- a second, independent look at the same number, the same posture
    `veydrift-wallet`'s allowlist takes toward re-validating a tx it didn't build)."""
    if action.planet_id is None:
        return _verdict("energy", GuardStatus.PASS, "action has no target planet")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("energy", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    if planet.energy is None:
        return _verdict(
            "energy",
            GuardStatus.BLOCK,
            "planet.energy is missing from the snapshot -- cannot verify energy balance; "
            "this is a check that could not run, not one that passed",
        )

    energy_fixing = (action.kind is ActionKind.BUILD and action.entity_id in _ENERGY_FIX_BUILDINGS) or (
        action.kind is ActionKind.SHIP and action.entity_id == ids.Ship.SOLAR_SATELLITE
    )
    if energy_fixing:
        return _verdict("energy", GuardStatus.PASS, "action increases energy supply (Solar Plant/Fusion/Satellite)")

    if action.kind is ActionKind.BUILD and action.entity_id in _MINE_ENTITY_IDS and action.target_level is not None:
        # Independent re-derivation of the post-upgrade requirement, using only what the
        # snapshot itself reports for the OTHER two mines/solar/fusion (never recomputing
        # cost -- only the duration/energy formulas calc.py already owns).
        def level_of(building_id: int) -> int:
            entity = next((b for b in planet.buildings if b.id == building_id), None)
            return entity.level if entity is not None and entity.level is not None else 0

        levels = {
            ids.Building.METAL_MINE: level_of(ids.Building.METAL_MINE),
            ids.Building.CRYSTAL_MINE: level_of(ids.Building.CRYSTAL_MINE),
            ids.Building.DEUTERIUM_SYNTHESIZER: level_of(ids.Building.DEUTERIUM_SYNTHESIZER),
        }
        levels[action.entity_id] = action.target_level
        satellite = next((s for s in planet.ships if s.id == ids.Ship.SOLAR_SATELLITE), None)
        required_post = calc.energy_balance(
            levels[ids.Building.METAL_MINE],
            levels[ids.Building.CRYSTAL_MINE],
            levels[ids.Building.DEUTERIUM_SYNTHESIZER],
            level_of(ids.Building.SOLAR_PLANT),
            level_of(ids.Building.FUSION_REACTOR),
            # Energy technology is not modelled independently here. This is NOT a
            # conservative "higher required" simplification -- energy technology scales
            # Fusion Reactor's energy *produced*, not the energy *required* by mines, so
            # omitting it (treating it as level 0) is a produced-side simplification. It
            # can only ever make `required_post > produced` MORE likely to trip (fewer
            # fusion-tech-boosted watts on the produced side), which is the safe direction
            # for a gate that must not vacuously pass -- but "conservative (higher
            # required)" as previously written here was simply wrong about which side of
            # the equation this affects.
            0,
            satellite.count if satellite is not None and satellite.count else 0,
            planet.energy.solar_satellite_energy or 0,
        ).required
        if required_post > planet.energy.produced:
            return _verdict(
                "energy",
                GuardStatus.BLOCK,
                f"post-upgrade required {required_post} > produced {planet.energy.produced} "
                "-- energy-first invariant violated",
            )

    if planet.energy.scale_bps != 10_000:
        return _verdict("energy", GuardStatus.WARN, f"planet already throttled at scaleBps={planet.energy.scale_bps}")
    if planet.energy.produced < planet.energy.required:
        return _verdict(
            "energy",
            GuardStatus.WARN,
            f"planet already energy-negative (produced {planet.energy.produced} < required "
            f"{planet.energy.required}) independent of this action",
        )
    return _verdict("energy", GuardStatus.PASS, f"produced {planet.energy.produced} >= required {planet.energy.required}, scaleBps=10000")


def _gate_storage_overflow(action: Action, snapshot: Snapshot, policy: Policy) -> GuardVerdict:
    """`PlanetSnapshot`'s resource fields default to zero when the API omits them, so a
    cap of 0 is ambiguous: it may mean "the API did not report a cap" or "the cap really
    is zero".

    A gate must never resolve that ambiguity by passing. A resource that is *producing*
    while its cap is unknown is precisely the overflow case this gate exists to catch, so
    an unverifiable cap escalates to a human instead. When production is zero, nothing can
    overflow regardless of the cap, and PASS is genuinely correct rather than vacuous.

    (Live `/infrastructure` does populate `storageCaps` -- 10,000 per resource at storage
    level 0 -- so this path is a defensive one, not the normal case.)"""
    trigger = policy.storage.hours_to_cap_trigger
    at_risk: list[str] = []
    unverifiable: list[str] = []
    for planet in snapshot.planets:
        if action.planet_id is not None and planet.planet_id != action.planet_id:
            continue
        triples = (
            ("metal", planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
            ("crystal", planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
            ("deuterium", planet.resources_as_of_now.deuterium, planet.production_per_hour.deuterium, planet.storage_caps.deuterium),
        )
        for label, current, per_hour, cap in triples:
            if cap <= 0:
                if per_hour > 0:
                    unverifiable.append(f"planet {planet.planet_id} {label}")
                continue
            hours = calc.hours_to_cap(current, per_hour, cap)
            if hours is not None and hours <= trigger:
                at_risk.append(f"planet {planet.planet_id} {label} ({hours:.1f}h)")
    if unverifiable:
        return _verdict(
            "storage_overflow",
            GuardStatus.ESCALATE,
            "cannot verify overflow: producing with no reported storage cap for "
            + ", ".join(unverifiable),
        )
    if not at_risk:
        return _verdict("storage_overflow", GuardStatus.PASS, "no resource within the overflow trigger window")
    addresses_it = action.kind in (ActionKind.BUILD,) or action.rule.startswith("5:")
    status = GuardStatus.PASS if addresses_it else GuardStatus.WARN
    return _verdict("storage_overflow", status, f"at risk: {', '.join(at_risk)}" + ("" if addresses_it else " -- action does not address it"))


def _gate_fields(action: Action, snapshot: Snapshot, policy: Policy) -> GuardVerdict:
    if action.planet_id is None:
        return _verdict("fields", GuardStatus.PASS, "action has no target planet")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("fields", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    if planet.fields_total is None or planet.fields_used is None:
        return _verdict("fields", GuardStatus.BLOCK, "fields_used/fields_total missing from snapshot; cannot verify capacity")
    if planet.fields_total <= 0:
        return _verdict("fields", GuardStatus.BLOCK, "fields_total is 0; cannot verify capacity")
    pct = planet.fields_used / planet.fields_total * 100
    if pct >= 100:
        return _verdict("fields", GuardStatus.BLOCK, f"fields at {pct:.0f}% -- no capacity for a new building")
    if pct >= policy.limits.field_warn_pct:
        return _verdict("fields", GuardStatus.WARN, f"fields at {pct:.0f}% (warn threshold {policy.limits.field_warn_pct}%)")
    return _verdict("fields", GuardStatus.PASS, f"fields at {pct:.0f}%")


def _gate_reserve(action: Action, snapshot: Snapshot, policy: Policy) -> GuardVerdict:
    if action.planet_id is None:
        return _verdict("reserve", GuardStatus.PASS, "action has no target planet / no spend")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("reserve", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    spend = _derive_fleet_mission_spend(action, snapshot)
    if spend is None:
        return _verdict(
            "reserve",
            GuardStatus.BLOCK,
            "fleet-mission spend could not be independently verified (missing ships/route/"
            "technology data) -- refusing to treat an unverifiable cost as zero",
        )
    holdings = planet.resources_as_of_now
    projected = (
        holdings.metal - spend.metal,
        holdings.crystal - spend.crystal,
        holdings.deuterium - spend.deuterium,
    )
    floors = (policy.reserves.metal, policy.reserves.crystal, policy.reserves.deuterium)
    breaches = [
        label
        for label, value, floor in zip(("metal", "crystal", "deuterium"), projected, floors)
        if value < floor
    ]
    if breaches:
        return _verdict("reserve", GuardStatus.BLOCK, f"spend would breach the reserve floor for: {', '.join(breaches)}")
    return _verdict("reserve", GuardStatus.PASS, "spend preserves configured reserve floors")


def _gate_gas(
    action: Action, policy: Policy, agent_state: AgentState, *, gas_cost_wei: int | None, now
) -> GuardVerdict:
    """`gas_cost_wei` must be a **wei** quantity (gas units * gas price), never raw gas
    units -- see `tick.py`'s `_walletctl_build`, which is the only production caller and
    sources this from `walletctl build`'s `estimatedCostWei` field, not its `gas` field.
    Comparing gas *units* (~1e5 on Base) against these wei-scale ceilings (~1e15-1e16)
    would make both ceilings permanently inert; that was a confirmed defect (fixed) --
    see `tests/test_tick.py::test_walletctl_build_cost_crosses_the_unit_boundary_into_the_gas_gate`
    for the boundary-crossing regression test."""
    if not action.is_onchain():
        return _verdict("gas", GuardStatus.PASS, "action spends no gas")
    if gas_cost_wei is None:
        return _verdict(
            "gas",
            GuardStatus.ESCALATE,
            "no gas cost estimate (wei) available for this action; cannot verify gas_per_tx_wei/gas_per_day_wei ceilings",
        )
    if gas_cost_wei > policy.limits.gas_per_tx_wei:
        return _verdict("gas", GuardStatus.BLOCK, f"{gas_cost_wei} wei > gas_per_tx_wei ceiling {policy.limits.gas_per_tx_wei}")
    spent_today = agent_state.gas_spent_today(now=now)
    if spent_today + gas_cost_wei > policy.limits.gas_per_day_wei:
        return _verdict(
            "gas",
            GuardStatus.BLOCK,
            f"{spent_today} + {gas_cost_wei} wei would exceed gas_per_day_wei ceiling {policy.limits.gas_per_day_wei}",
        )
    return _verdict("gas", GuardStatus.PASS, f"{gas_cost_wei} wei within per-tx and per-day ceilings")


def _gate_eth_floor(action: Action, policy: Policy, *, eth_balance_wei: int | None) -> GuardVerdict:
    """The other gate the brief calls out by construction: `Snapshot.eth_balance_wei` is
    **always** `None` from `read.py`'s `snapshot` command (no read route reports it --
    `models.py`'s own comment says so; it's `walletctl`'s job). If this gate simply read
    `snapshot.eth_balance_wei` it would vacuously pass on every tick forever. Instead it
    takes `eth_balance_wei` as an explicit, separately-sourced parameter (`tick.py` best-
    effort-parses it from `walletctl status`) and BLOCKs/ESCALATEs rather than assume."""
    if not action.is_onchain():
        return _verdict("eth_floor", GuardStatus.PASS, "action spends no gas")
    if eth_balance_wei is None:
        return _verdict(
            "eth_floor",
            GuardStatus.ESCALATE,
            "wallet ETH balance unknown -- the read API never reports it (see models.py); "
            "`walletctl status` was not consulted or failed",
        )
    if eth_balance_wei < policy.limits.eth_gas_floor_wei:
        return _verdict("eth_floor", GuardStatus.BLOCK, f"{eth_balance_wei} wei < eth_gas_floor_wei {policy.limits.eth_gas_floor_wei}")
    return _verdict("eth_floor", GuardStatus.PASS, f"{eth_balance_wei} wei >= eth_gas_floor_wei")


def _gate_value_ceiling(action: Action, snapshot: Snapshot, policy: Policy) -> GuardVerdict:
    spend = _derive_fleet_mission_spend(action, snapshot)
    if spend is None:
        return _verdict(
            "value_ceiling",
            GuardStatus.BLOCK,
            "fleet-mission spend could not be independently verified (missing ships/route/"
            "technology data) -- refusing to treat an unverifiable cost as zero",
        )
    total_cost = spend.metal + spend.crystal + spend.deuterium
    if total_cost == 0:
        return _verdict("value_ceiling", GuardStatus.PASS, "action has no resource cost")
    if action.planet_id is None:
        return _verdict("value_ceiling", GuardStatus.BLOCK, "action has a cost but no target planet to compute % of holdings")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("value_ceiling", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    holdings = planet.resources_as_of_now
    total_holdings = holdings.metal + holdings.crystal + holdings.deuterium
    if total_holdings <= 0:
        return _verdict("value_ceiling", GuardStatus.ESCALATE, "holdings are zero; cannot compute cost as a % of holdings")
    pct = total_cost / total_holdings * 100
    if pct > policy.limits.escalate_above_pct_of_resources:
        return _verdict(
            "value_ceiling",
            GuardStatus.ESCALATE,
            f"cost is {pct:.1f}% of holdings, above escalate_above_pct_of_resources="
            f"{policy.limits.escalate_above_pct_of_resources}%",
        )
    return _verdict("value_ceiling", GuardStatus.PASS, f"cost is {pct:.1f}% of holdings")


def _gate_idempotency(action: Action, agent_state: AgentState) -> GuardVerdict:
    if not action.is_onchain():
        return _verdict("idempotency", GuardStatus.PASS, "action has no on-chain identity")
    pending = agent_state.pending
    key = idempotency_key(action)
    if pending is not None and pending.key == key and pending.indexed_at is None:
        return _verdict("idempotency", GuardStatus.BLOCK, f"a pending tx already exists for {key}")
    return _verdict("idempotency", GuardStatus.PASS, f"no pending tx for {key}")


def _gate_revert_streak(action: Action, agent_state: AgentState, policy: Policy) -> GuardVerdict:
    if not action.is_onchain():
        return _verdict("revert_streak", GuardStatus.PASS, "action has no on-chain identity")
    key = idempotency_key(action)
    count = agent_state.revert_counts.get(key, 0)
    if count >= policy.escalation.on_revert_count:
        return _verdict("revert_streak", GuardStatus.ESCALATE, f"{key} has reverted {count} time(s) (threshold {policy.escalation.on_revert_count})")
    return _verdict("revert_streak", GuardStatus.PASS, f"{key} has reverted {count} time(s)")


# --------------------------------------------------------------------------------------
# The full 20-gate evaluation.
# --------------------------------------------------------------------------------------


def evaluate_guardrails(
    action: Action,
    snapshot: Snapshot,
    policy: Policy,
    agent_state: AgentState,
    *,
    killswitch_active: bool = False,
    live_addresses: set[str] | None = None,
    unsigned_tx: UnsignedTx | None = None,
    gas_cost_wei: int | None = None,
    eth_balance_wei: int | None = None,
    outgoing_colonize_count: int | None = None,
    now=None,
) -> GuardReport:
    """Evaluate all 20 gates and return the full `GuardReport`. Never short-circuits: even
    once one gate has already BLOCKed, every remaining gate still runs, because the
    report -- not just the final decision -- is the audit artifact.

    `now` defaults to `datetime.now(UTC)`; accepted as a parameter purely so tests can
    freeze time for `index_lag`. `outgoing_colonize_count` (commit 4 of the launch-
    actions plan) is `tick.py`'s live count of the wallet's own still-`Outbound` Colonize
    missions, only meaningful for a Colonize action -- see `_colony_cap_violation`'s
    docstring for why this closes an in-flight-mission blind spot the cap check
    otherwise has, and why `None` fails closed rather than assuming zero.
    """
    from datetime import UTC
    from datetime import datetime as _datetime

    now = now or _datetime.now(UTC)

    verdicts = [
        _gate_killswitch(killswitch_active=killswitch_active),
        _gate_tier(action, policy),
        _gate_mission_type(action, snapshot, policy, outgoing_colonize_count=outgoing_colonize_count),
        _gate_prerequisites(action, snapshot),
        _gate_fleet_slots(action, snapshot),
        _gate_address(action, live_addresses=live_addresses, unsigned_tx=unsigned_tx),
        _gate_abi_hash(action, snapshot),
        _gate_health(snapshot),
        _gate_game_paused(snapshot),
        _gate_index_lag(policy, agent_state, now=now),
        _gate_affordability(action, snapshot),
        _gate_energy(action, snapshot),
        _gate_storage_overflow(action, snapshot, policy),
        _gate_fields(action, snapshot, policy),
        _gate_reserve(action, snapshot, policy),
        _gate_gas(action, policy, agent_state, gas_cost_wei=gas_cost_wei, now=now),
        _gate_eth_floor(action, policy, eth_balance_wei=eth_balance_wei),
        _gate_value_ceiling(action, snapshot, policy),
        _gate_idempotency(action, agent_state),
        _gate_revert_streak(action, agent_state, policy),
    ]

    if any(v.status is GuardStatus.BLOCK for v in verdicts):
        decision = Decision.BLOCK
    elif any(v.status is GuardStatus.ESCALATE for v in verdicts):
        decision = Decision.ESCALATE
    else:
        decision = Decision.ALLOW

    return GuardReport(decision=decision, verdicts=verdicts)


# --------------------------------------------------------------------------------------
# CLI — offline: reads Action/Snapshot/Policy from files, prints the verdict table.
# `tick.py` (this WP's own wired, online caller) supplies the live-only parameters this
# offline entrypoint cannot (live addresses, a built tx, gas/ETH balance) -- so a `vd
# guard run` invocation always evaluates the gates whose data it lacks as the honest
# BLOCK/ESCALATE their missing-data path produces, which is itself a useful way to see
# that path exercised by hand.
# --------------------------------------------------------------------------------------


_STATUS_COLOR = {
    GuardStatus.PASS: "green",
    GuardStatus.WARN: "yellow",
    GuardStatus.BLOCK: "red",
    GuardStatus.ESCALATE: "magenta",
}


def render_report(report: GuardReport) -> Table:
    table = Table(title=f"guard: {report.decision.value.upper()} ({report.passed}/{report.total} pass)")
    table.add_column("gate")
    table.add_column("status")
    table.add_column("detail")
    for v in report.verdicts:
        color = _STATUS_COLOR[v.status]
        table.add_row(v.gate, f"[{color}]{v.status.value}[/{color}]", v.detail)
    return table


@app.command()
def run(
    action: Path = typer.Option(..., "--action", help="Path to an Action JSON file."),  # noqa: B008
    snapshot: Path = typer.Option(..., "--snapshot", help="Path to a Snapshot JSON file."),  # noqa: B008
    policy: Path = typer.Option(..., "--policy", help="Path to a Policy JSON file."),  # noqa: B008
    killswitch: bool = typer.Option(False, help="Simulate a present KILLSWITCH file."),
    json_output: bool = typer.Option(False, "--json", help="Print the GuardReport as JSON instead of a table."),
) -> None:
    """Evaluate guardrails offline from Action/Snapshot/Policy files. No network calls,
    no live address/ABI/gas/ETH data -- those gates report their honest missing-data
    verdict. `tick.py` is the online, fully-supplied caller."""
    from veydrift_agent.state import load_agent_state

    console = Console()
    try:
        action_model = Action.model_validate(json.loads(action.read_text()))
        snapshot_model = Snapshot.model_validate(json.loads(snapshot.read_text()))
        policy_model = Policy.model_validate(json.loads(policy.read_text()))
    except (OSError, ValueError) as exc:
        console.print(f"[red]failed to load action/snapshot/policy: {exc}[/red]")
        raise typer.Exit(code=4) from exc

    report = evaluate_guardrails(
        action_model,
        snapshot_model,
        policy_model,
        load_agent_state(),
        killswitch_active=killswitch,
    )

    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console.print(render_report(report))

    if report.decision is Decision.BLOCK:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
