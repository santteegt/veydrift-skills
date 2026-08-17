"""`vd guard` — the 17-gate guardrail evaluator (docs/SPEC.md §5.5).

`evaluate_guardrails()` is the pure core: given an `Action`, the `Snapshot` it was
planned from, the `Policy`, the persisted `AgentState`, and a handful of caller-supplied
facts that don't live on any of those frozen/local models (live contract addresses, the
live ABI hash, a built `UnsignedTx` + gas estimate, the wallet's ETH balance), it returns
a `GuardReport` with **all 17 gates evaluated, never short-circuited** — the full
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
    """`(planet, action, entity)` per docs/SPEC.md §5.5's `idempotency` row. Shared with
    `state.PendingTx.key` / `AgentState.revert_counts` so `idempotency` and
    `revert_streak` key off the exact same identity."""
    return f"{action.planet_id}:{action.function}:{action.entity_id}"


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
    exactly `guards: 14/17 pass (block)`, and the 3 gates that don't pass there are
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


def _gate_prerequisites(action: Action, snapshot: Snapshot) -> GuardVerdict:
    """New gate (docs/SPEC.md §5.5), slotted immediately after `tier` and before
    `address`. Independently re-derives the planet's building/technology level vectors
    from `snapshot` -- never trusts `plan.py`'s own filtering, exactly as `_gate_energy`
    already re-derives the energy invariant rather than calling `plan.py`. BLOCKs on any
    unmet `techtree` requirement, on any `have=None` (a level the snapshot didn't report
    -- fail closed, never PASS on absent data), and on a shield-dome/missile-slot cap
    violation.

    Actions with no entity to check (`resolve_mission`/`noop`/`escalate`/`halt`, or any
    action missing `entity_id`) PASS trivially -- there is nothing here for this gate to
    say anything about, the same posture `_gate_energy`/`_gate_affordability` take toward
    an action with no target planet.
    """
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
    if not snapshot.health_ok:
        return _verdict("health", GuardStatus.BLOCK, "/health reported not ok / not ready")
    return _verdict("health", GuardStatus.PASS, "/health ok and ready")


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


def _gate_affordability(action: Action, snapshot: Snapshot) -> GuardVerdict:
    if action.planet_id is None:
        return _verdict("affordability", GuardStatus.PASS, "action has no target planet to check cost against")
    planet = snapshot.planet(action.planet_id)
    if planet is None:
        return _verdict("affordability", GuardStatus.BLOCK, f"planet {action.planet_id} not found in snapshot")
    if planet.resources_as_of_now.covers(action.cost):
        return _verdict("affordability", GuardStatus.PASS, "resourcesAsOfNow covers the proposed cost")

    # Best-effort, informational only -- never changes this gate's BLOCK decision, which
    # is already fixed by the covers() check above. Each short resource gets its own
    # clause rather than a single collapsed number: a reader takes the max (every
    # resource must clear at once) by inspection, and collapsing would hide which
    # resource is actually the bottleneck.
    eta_bits: list[str] = []
    for label, cost, current, per_hour, cap in (
        ("Metal", action.cost.metal, planet.resources_as_of_now.metal, planet.production_per_hour.metal, planet.storage_caps.metal),
        ("Crystal", action.cost.crystal, planet.resources_as_of_now.crystal, planet.production_per_hour.crystal, planet.storage_caps.crystal),
        (
            "Deuterium",
            action.cost.deuterium,
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
        f"resourcesAsOfNow does not cover cost (need M{action.cost.metal} C{action.cost.crystal} "
        f"D{action.cost.deuterium}; have M{planet.resources_as_of_now.metal} "
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
    holdings = planet.resources_as_of_now
    projected = (
        holdings.metal - action.cost.metal,
        holdings.crystal - action.cost.crystal,
        holdings.deuterium - action.cost.deuterium,
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
    total_cost = action.cost.metal + action.cost.crystal + action.cost.deuterium
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
# The full 17-gate evaluation.
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
    now=None,
) -> GuardReport:
    """Evaluate all 17 gates and return the full `GuardReport`. Never short-circuits: even
    once one gate has already BLOCKed, every remaining gate still runs, because the
    report -- not just the final decision -- is the audit artifact.

    `now` defaults to `datetime.now(UTC)`; accepted as a parameter purely so tests can
    freeze time for `index_lag`.
    """
    from datetime import UTC
    from datetime import datetime as _datetime

    now = now or _datetime.now(UTC)

    verdicts = [
        _gate_killswitch(killswitch_active=killswitch_active),
        _gate_tier(action, policy),
        _gate_prerequisites(action, snapshot),
        _gate_address(action, live_addresses=live_addresses, unsigned_tx=unsigned_tx),
        _gate_abi_hash(action, snapshot),
        _gate_health(snapshot),
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
