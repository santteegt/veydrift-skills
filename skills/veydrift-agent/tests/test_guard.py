"""Tests for veydrift_agent.guard — the 16-gate guardrail evaluator.

The most important tests here are the "missing data must not vacuously pass" ones (one
per gate where that risk is real: `address`, `abi_hash`, `affordability`, `energy`,
`fields`, `reserve`, `gas`, `eth_floor`, `value_ceiling`). Each constructs a snapshot/
policy/action where the relevant field is `None` (or otherwise absent) and asserts the
gate resolves to `BLOCK` or `ESCALATE`, never `PASS` — the exact defect the work package
brief calls out as the most likely real bug in this package.
"""

from __future__ import annotations

from datetime import UTC, datetime

from veydrift_agent import guard, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    Decision,
    EnergyBalance,
    Entity,
    EscalationCfg,
    GuardStatus,
    Limits,
    PlanetSnapshot,
    Policy,
    Resources,
    Snapshot,
    Tier,
)
from veydrift_agent.state import AgentState, PendingTx

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"


def make_policy(**overrides) -> Policy:
    base = dict(
        wallet=WALLET,
        planets=[664],
        limits=Limits(
            gas_per_tx_wei=3_000_000_000_000_000,
            gas_per_day_wei=20_000_000_000_000_000,
            eth_gas_floor_wei=2_000_000_000_000_000,
            escalate_above_pct_of_resources=25,
            max_index_wait_s=300,
            field_warn_pct=80,
        ),
        actions=ActionsCfg(),
        escalation=EscalationCfg(),
    )
    base.update(overrides)
    return Policy(**base)


def make_planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=664,
        coordinates="7:181:14",
        temperature=10,
        fields_used=7,
        fields_total=174,
        resources=Resources(metal=1000, crystal=1000, deuterium=0),
        resources_as_of_now=Resources(metal=1000, crystal=1000, deuterium=0),
        storage_caps=Resources(metal=10000, crystal=10000, deuterium=10000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=100, required=50, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=60, crystal=15)),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=1, cost=Resources(metal=105, crystal=25)),
        ],
        ships=[],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def make_snapshot(*, planets=None, health_ok=True, abi_hash=guard.PINNED_ABI_HASH, **overrides) -> Snapshot:
    base = dict(
        taken_at=NOW,
        wallet=WALLET,
        health_ok=health_ok,
        deployment_abi_hash=abi_hash,
        planets=planets if planets is not None else [make_planet()],
    )
    base.update(overrides)
    return Snapshot(**base)


def make_build_action(**overrides) -> Action:
    base = dict(
        kind=ActionKind.BUILD,
        function="startBuildingUpgrade",
        planet_id=664,
        entity_id=ids.Building.METAL_MINE,
        entity_name="Metal Mine",
        target_level=1,
        cost=Resources(metal=60, crystal=15, deuterium=0),
        rule="6:building-queue-empty",
        rationale="test",
    )
    base.update(overrides)
    return Action(**base)


LIVE_ADDR = "0xf397910F005151b09644228573a4353818D3755d"


def make_unsigned_tx(**overrides):
    from veydrift_agent.models import UnsignedTx

    base = dict(to=LIVE_ADDR, data="0x165715e3" + "00" * 64, value=0, chain_id=8453, gas=100_000)
    base.update(overrides)
    return UnsignedTx(**base)


def evaluate(action, snapshot, policy, agent_state=None, **kwargs):
    kwargs.setdefault("now", NOW)
    return guard.evaluate_guardrails(action, snapshot, policy, agent_state or AgentState(), **kwargs)


def verdict(report, gate: str):
    return next(v for v in report.verdicts if v.gate == gate)


# --------------------------------------------------------------------------------------
# All 16 gates are always present, never short-circuited.
# --------------------------------------------------------------------------------------


def test_all_sixteen_gates_always_present_even_when_blocked():
    action = make_build_action()
    report = evaluate(action, make_snapshot(health_ok=False), make_policy())
    assert report.total == 16
    gates = {v.gate for v in report.verdicts}
    assert gates == {
        "killswitch", "tier", "address", "abi_hash", "health", "index_lag",
        "affordability", "energy", "storage_overflow", "fields", "reserve",
        "gas", "eth_floor", "value_ceiling", "idempotency", "revert_streak",
    }
    assert report.decision is Decision.BLOCK
    # health failing does not stop e.g. affordability from also being evaluated
    assert verdict(report, "affordability").status is GuardStatus.PASS


def test_full_allow_at_economy_tier_with_all_live_data_supplied():
    action = make_build_action(
        kind=ActionKind.RESEARCH, function="startResearch", entity_id=ids.Technology.ENERGY, target_level=1, cost=Resources(metal=100, crystal=50)
    )
    policy = make_policy(tier=Tier.ECONOMY)
    report = evaluate(
        action,
        make_snapshot(),
        policy,
        live_addresses={LIVE_ADDR},
        unsigned_tx=make_unsigned_tx(data="0x1234567800" + "00" * 62),
        gas_cost_wei=500_000,
        eth_balance_wei=5_000_000_000_000_000,
    )
    assert report.decision is Decision.ALLOW
    assert all(v.status is GuardStatus.PASS for v in report.verdicts)


# --------------------------------------------------------------------------------------
# killswitch
# --------------------------------------------------------------------------------------


def test_killswitch_blocks_when_active():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), killswitch_active=True)
    assert verdict(report, "killswitch").status is GuardStatus.BLOCK
    assert report.decision is Decision.BLOCK


def test_killswitch_passes_when_absent():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), killswitch_active=False)
    assert verdict(report, "killswitch").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# tier
# --------------------------------------------------------------------------------------


def test_tier_blocks_advisor_from_ever_submitting():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(tier=Tier.ADVISOR))
    assert verdict(report, "tier").status is GuardStatus.BLOCK


def test_tier_allows_economy_action_at_economy_tier():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(tier=Tier.ECONOMY))
    assert verdict(report, "tier").status is GuardStatus.PASS


def test_tier_blocks_launch_fleet_mission_below_operator():
    action = make_build_action(function="launchFleetMission", entity_id=None)
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.ECONOMY))
    assert verdict(report, "tier").status is GuardStatus.BLOCK


def test_tier_passes_noop_trivially():
    action = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="nothing to do")
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.ADVISOR))
    assert verdict(report, "tier").status is GuardStatus.PASS


def test_tier_allows_start_ship_production_from_economy_up():
    """Ship production is submittable from tier 2, and blocked at tier 1.

    This test previously asserted a BLOCK at *every* tier, which faithfully encoded a
    defect in docs/SPEC.md v2.0: rung 8 of plan.py proposes ships when
    policy.actions.allow_ships is set, but the §4 tier table granted the function to no
    tier, so the knob was dead config. Fixed 2026-08-12 by granting it at ECONOMY --
    producing ships spends resources on your own planet, the same risk profile as
    startDefenseProduction. Mirrors ECONOMY_SIGNATURES in veydrift-wallet/src/allowlist.ts."""
    action = make_build_action(
        kind=ActionKind.SHIP, function="startShipProduction", entity_id=ids.Ship.SOLAR_SATELLITE, quantity=1
    )
    snapshot = make_snapshot()
    assert verdict(evaluate(action, snapshot, make_policy(tier=Tier.ADVISOR)), "tier").status is GuardStatus.BLOCK
    assert verdict(evaluate(action, snapshot, make_policy(tier=Tier.ECONOMY)), "tier").status is GuardStatus.PASS
    assert verdict(evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR)), "tier").status is GuardStatus.PASS


def test_storage_overflow_escalates_when_producing_with_no_reported_cap():
    """A zero cap is ambiguous -- "API omitted it" or "genuinely zero". Resolving that
    ambiguity by passing would be a vacuous pass on the exact case the gate exists to
    catch, so a resource that is producing with no reported cap escalates instead."""
    snapshot = make_snapshot()
    planet = snapshot.planets[0]
    planet.storage_caps = Resources(metal=0, crystal=0, deuterium=0)
    planet.production_per_hour = Resources(metal=120, crystal=0, deuterium=0)
    report = evaluate(make_build_action(), snapshot, make_policy(tier=Tier.ECONOMY))
    v = verdict(report, "storage_overflow")
    assert v.status is GuardStatus.ESCALATE
    assert "cannot verify" in v.detail

    # Zero production means nothing can overflow regardless of cap: PASS is genuine here.
    planet.production_per_hour = Resources(metal=0, crystal=0, deuterium=0)
    assert verdict(
        evaluate(make_build_action(), snapshot, make_policy(tier=Tier.ECONOMY)), "storage_overflow"
    ).status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# address — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_address_blocks_when_live_addresses_unavailable():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), live_addresses=None, unsigned_tx=make_unsigned_tx())
    assert verdict(report, "address").status is GuardStatus.BLOCK


def test_address_blocks_when_no_unsigned_tx_built():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), live_addresses={LIVE_ADDR}, unsigned_tx=None)
    assert verdict(report, "address").status is GuardStatus.BLOCK


def test_address_blocks_destination_outside_live_set():
    tx = make_unsigned_tx(to="0x0000000000000000000000000000000000dEaD")
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), live_addresses={LIVE_ADDR}, unsigned_tx=tx)
    assert verdict(report, "address").status is GuardStatus.BLOCK


def test_address_passes_for_a_live_destination():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), live_addresses={LIVE_ADDR}, unsigned_tx=make_unsigned_tx())
    assert verdict(report, "address").status is GuardStatus.PASS


def test_address_trivially_passes_for_offchain_action():
    action = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="x")
    report = evaluate(action, make_snapshot(), make_policy(), live_addresses=None, unsigned_tx=None)
    assert verdict(report, "address").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# abi_hash — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_abi_hash_blocks_when_snapshot_hash_missing():
    report = evaluate(make_build_action(), make_snapshot(abi_hash=None), make_policy())
    assert verdict(report, "abi_hash").status is GuardStatus.BLOCK


def test_abi_hash_blocks_on_drift():
    report = evaluate(make_build_action(), make_snapshot(abi_hash="sha256:deadbeef"), make_policy())
    assert verdict(report, "abi_hash").status is GuardStatus.BLOCK


def test_abi_hash_passes_when_pinned_matches_live():
    report = evaluate(make_build_action(), make_snapshot(), make_policy())
    assert verdict(report, "abi_hash").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------


def test_health_blocks_when_not_ok():
    report = evaluate(make_build_action(), make_snapshot(health_ok=False), make_policy())
    assert verdict(report, "health").status is GuardStatus.BLOCK


def test_health_passes_when_ok():
    report = evaluate(make_build_action(), make_snapshot(health_ok=True), make_policy())
    assert verdict(report, "health").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# index_lag
# --------------------------------------------------------------------------------------


def test_index_lag_passes_with_no_pending_tx():
    report = evaluate(make_build_action(), make_snapshot(), make_policy())
    assert verdict(report, "index_lag").status is GuardStatus.PASS


def test_index_lag_blocks_past_max_wait():
    from datetime import timedelta

    agent_state = AgentState(
        pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, receipt_at=NOW - timedelta(seconds=1000))
    )
    policy = make_policy()
    report = evaluate(make_build_action(), make_snapshot(), policy, agent_state)
    assert verdict(report, "index_lag").status is GuardStatus.BLOCK


def test_index_lag_warns_within_budget():
    from datetime import timedelta

    agent_state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, receipt_at=NOW - timedelta(seconds=5)))
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), agent_state)
    assert verdict(report, "index_lag").status is GuardStatus.WARN


def test_index_lag_passes_once_indexed():
    agent_state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, receipt_at=NOW, indexed_at=NOW))
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), agent_state)
    assert verdict(report, "index_lag").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# affordability — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_affordability_blocks_when_planet_not_in_snapshot():
    report = evaluate(make_build_action(planet_id=999), make_snapshot(), make_policy())
    assert verdict(report, "affordability").status is GuardStatus.BLOCK


def test_affordability_blocks_when_cost_exceeds_holdings():
    action = make_build_action(cost=Resources(metal=999_999))
    report = evaluate(action, make_snapshot(), make_policy())
    assert verdict(report, "affordability").status is GuardStatus.BLOCK


def test_affordability_passes_when_cost_covered():
    report = evaluate(make_build_action(cost=Resources(metal=100)), make_snapshot(), make_policy())
    assert verdict(report, "affordability").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# energy — the gate the brief calls out by name. MISSING DATA must not vacuously pass.
# --------------------------------------------------------------------------------------


def test_energy_blocks_when_planet_energy_is_none():
    planet = make_planet(energy=None)
    action = make_build_action()
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    v = verdict(report, "energy")
    assert v.status is GuardStatus.BLOCK
    assert "could not run" in v.detail


def test_energy_blocks_mine_upgrade_that_would_exceed_produced():
    planet = make_planet(
        energy=EnergyBalance(produced=5, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=60, crystal=15))],
    )
    action = make_build_action(target_level=1)  # Metal Mine 0->1 needs 11 energy, only 5 produced
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "energy").status is GuardStatus.BLOCK


def test_energy_passes_mine_upgrade_within_energy_budget():
    planet = make_planet(
        energy=EnergyBalance(produced=1000, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=60, crystal=15))],
    )
    action = make_build_action(target_level=1)
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "energy").status is GuardStatus.PASS


def test_energy_passes_trivially_for_solar_plant_build_even_if_energy_negative():
    planet = make_planet(energy=EnergyBalance(produced=0, required=50, scale_bps=8000))
    action = make_build_action(entity_id=ids.Building.SOLAR_PLANT, target_level=2)
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "energy").status is GuardStatus.PASS


def test_energy_warns_when_already_negative_independent_of_this_action():
    planet = make_planet(energy=EnergyBalance(produced=10, required=50, scale_bps=8000))
    action = make_build_action(kind=ActionKind.RESEARCH, function="startResearch", entity_id=ids.Technology.ENERGY, target_level=1)
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "energy").status is GuardStatus.WARN


def test_energy_trivially_passes_when_action_has_no_planet():
    action = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="x")
    report = evaluate(action, make_snapshot(), make_policy())
    assert verdict(report, "energy").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# storage_overflow
# --------------------------------------------------------------------------------------


def test_storage_overflow_passes_when_nothing_near_cap():
    report = evaluate(make_build_action(), make_snapshot(), make_policy())
    assert verdict(report, "storage_overflow").status is GuardStatus.PASS


def test_storage_overflow_warns_when_a_resource_is_at_risk_and_action_does_not_address_it():
    planet = make_planet(
        resources_as_of_now=Resources(metal=9900, crystal=0, deuterium=0),
        production_per_hour=Resources(metal=1000, crystal=0, deuterium=0),
        storage_caps=Resources(metal=10000, crystal=10000, deuterium=10000),
    )
    action = make_build_action(kind=ActionKind.RESEARCH, function="startResearch", entity_id=ids.Technology.ENERGY, target_level=1)
    policy = make_policy()
    policy.storage.hours_to_cap_trigger = 2.0
    report = evaluate(action, make_snapshot(planets=[planet]), policy)
    assert verdict(report, "storage_overflow").status is GuardStatus.WARN


def test_storage_overflow_ignores_a_zero_cap_resource_documented_limitation():
    """Documented limitation: a storage cap of exactly 0 is indistinguishable from
    "the API omitted it", so this gate skips it rather than false-blocking a genuine
    zero-state planet (e.g. the real planet 664 fixture)."""
    planet = make_planet(storage_caps=Resources(metal=0, crystal=0, deuterium=0))
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "storage_overflow").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# fields — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_fields_blocks_when_data_missing():
    planet = make_planet(fields_used=None, fields_total=None)
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "fields").status is GuardStatus.BLOCK


def test_fields_blocks_when_total_is_zero():
    planet = make_planet(fields_used=0, fields_total=0)
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "fields").status is GuardStatus.BLOCK


def test_fields_blocks_at_full_capacity():
    planet = make_planet(fields_used=174, fields_total=174)
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "fields").status is GuardStatus.BLOCK


def test_fields_warns_above_warn_threshold():
    planet = make_planet(fields_used=150, fields_total=174)  # ~86%
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "fields").status is GuardStatus.WARN


def test_fields_passes_below_warn_threshold():
    planet = make_planet(fields_used=7, fields_total=174)
    report = evaluate(make_build_action(), make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "fields").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# reserve — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_reserve_blocks_when_planet_missing():
    report = evaluate(make_build_action(planet_id=999, cost=Resources(metal=10)), make_snapshot(), make_policy())
    assert verdict(report, "reserve").status is GuardStatus.BLOCK


def test_reserve_blocks_spend_that_breaches_floor():
    planet = make_planet(resources_as_of_now=Resources(metal=100, crystal=0, deuterium=0))
    policy = make_policy(reserves=Resources(metal=50, crystal=0, deuterium=0))
    action = make_build_action(cost=Resources(metal=60))
    report = evaluate(action, make_snapshot(planets=[planet]), policy)
    assert verdict(report, "reserve").status is GuardStatus.BLOCK


def test_reserve_passes_spend_within_floor():
    planet = make_planet(resources_as_of_now=Resources(metal=1000, crystal=0, deuterium=0))
    policy = make_policy(reserves=Resources(metal=50, crystal=0, deuterium=0))
    action = make_build_action(cost=Resources(metal=60))
    report = evaluate(action, make_snapshot(planets=[planet]), policy)
    assert verdict(report, "reserve").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# gas — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_gas_escalates_when_no_estimate_available():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), gas_cost_wei=None)
    assert verdict(report, "gas").status is GuardStatus.ESCALATE


def test_gas_blocks_over_per_tx_ceiling():
    policy = make_policy()
    report = evaluate(make_build_action(), make_snapshot(), policy, gas_cost_wei=policy.limits.gas_per_tx_wei + 1)
    assert verdict(report, "gas").status is GuardStatus.BLOCK


def test_gas_blocks_over_per_day_ceiling_when_combined_with_prior_spend():
    policy = make_policy(limits=Limits(gas_per_tx_wei=10**18, gas_per_day_wei=1000, eth_gas_floor_wei=0))
    agent_state = AgentState()
    agent_state.record_gas_spent(900, now=NOW)
    report = evaluate(make_build_action(), make_snapshot(), policy, agent_state, gas_cost_wei=200)
    assert verdict(report, "gas").status is GuardStatus.BLOCK


def test_gas_passes_within_ceilings():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), gas_cost_wei=500_000)
    assert verdict(report, "gas").status is GuardStatus.PASS


def test_gas_trivially_passes_offchain_action():
    action = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="x")
    report = evaluate(action, make_snapshot(), make_policy(), gas_cost_wei=None)
    assert verdict(report, "gas").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# eth_floor — the other gate the brief calls out by name. MISSING DATA must not
# vacuously pass. Snapshot.eth_balance_wei is ALWAYS None from read.py (see models.py's
# own comment) -- this gate must never read that field, only the explicit parameter.
# --------------------------------------------------------------------------------------


def test_eth_floor_escalates_when_balance_unknown():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), eth_balance_wei=None)
    assert verdict(report, "eth_floor").status is GuardStatus.ESCALATE


def test_eth_floor_blocks_below_floor():
    policy = make_policy()
    report = evaluate(make_build_action(), make_snapshot(), policy, eth_balance_wei=policy.limits.eth_gas_floor_wei - 1)
    assert verdict(report, "eth_floor").status is GuardStatus.BLOCK


def test_eth_floor_passes_above_floor():
    policy = make_policy()
    report = evaluate(make_build_action(), make_snapshot(), policy, eth_balance_wei=policy.limits.eth_gas_floor_wei + 1)
    assert verdict(report, "eth_floor").status is GuardStatus.PASS


def test_eth_floor_never_reads_snapshot_eth_balance_field():
    """Even if a caller populated Snapshot.eth_balance_wei directly (bypassing the
    explicit parameter), this gate must ignore it -- proving there is no accidental
    vacuous-pass path through the snapshot field."""
    snap = make_snapshot(eth_balance_wei=999_999_999_999_999_999)
    report = evaluate(make_build_action(), snap, make_policy(), eth_balance_wei=None)
    assert verdict(report, "eth_floor").status is GuardStatus.ESCALATE


def test_eth_floor_trivially_passes_offchain_action():
    action = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="x")
    report = evaluate(action, make_snapshot(), make_policy(), eth_balance_wei=None)
    assert verdict(report, "eth_floor").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# value_ceiling — MISSING DATA must not vacuously pass
# --------------------------------------------------------------------------------------


def test_value_ceiling_passes_zero_cost_trivially():
    action = Action(kind=ActionKind.RESOLVE_MISSION, function="resolveFleetMission", mission_id=1, rule="3:mission-resolving", rationale="x")
    report = evaluate(action, make_snapshot(), make_policy())
    assert verdict(report, "value_ceiling").status is GuardStatus.PASS


def test_value_ceiling_blocks_when_planet_missing_for_a_costed_action():
    report = evaluate(make_build_action(planet_id=999, cost=Resources(metal=10)), make_snapshot(), make_policy())
    assert verdict(report, "value_ceiling").status is GuardStatus.BLOCK


def test_value_ceiling_escalates_on_zero_holdings():
    planet = make_planet(resources_as_of_now=Resources(metal=0, crystal=0, deuterium=0))
    action = make_build_action(cost=Resources(metal=10))
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "value_ceiling").status is GuardStatus.ESCALATE


def test_value_ceiling_escalates_above_threshold():
    planet = make_planet(resources_as_of_now=Resources(metal=100, crystal=0, deuterium=0))
    action = make_build_action(cost=Resources(metal=50))  # 50% of holdings > 25% default
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "value_ceiling").status is GuardStatus.ESCALATE


def test_value_ceiling_passes_below_threshold():
    planet = make_planet(resources_as_of_now=Resources(metal=1000, crystal=0, deuterium=0))
    action = make_build_action(cost=Resources(metal=50))  # 5% of holdings
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    assert verdict(report, "value_ceiling").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------------------


def test_idempotency_blocks_duplicate_pending_action():
    action = make_build_action()
    key = guard.idempotency_key(action)
    agent_state = AgentState(pending=PendingTx(key=key, tx_hash="0x" + "aa" * 32))
    report = evaluate(action, make_snapshot(), make_policy(), agent_state)
    assert verdict(report, "idempotency").status is GuardStatus.BLOCK


def test_idempotency_passes_for_a_different_key():
    action = make_build_action()
    agent_state = AgentState(pending=PendingTx(key="different-key", tx_hash="0x" + "aa" * 32))
    report = evaluate(action, make_snapshot(), make_policy(), agent_state)
    assert verdict(report, "idempotency").status is GuardStatus.PASS


def test_idempotency_passes_once_the_pending_entry_is_indexed():
    action = make_build_action()
    key = guard.idempotency_key(action)
    agent_state = AgentState(pending=PendingTx(key=key, tx_hash="0x" + "aa" * 32, indexed_at=NOW))
    report = evaluate(action, make_snapshot(), make_policy(), agent_state)
    assert verdict(report, "idempotency").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# revert_streak
# --------------------------------------------------------------------------------------


def test_revert_streak_escalates_past_threshold():
    action = make_build_action()
    key = guard.idempotency_key(action)
    agent_state = AgentState(revert_counts={key: 2})
    policy = make_policy(escalation=EscalationCfg(on_revert_count=2))
    report = evaluate(action, make_snapshot(), policy, agent_state)
    assert verdict(report, "revert_streak").status is GuardStatus.ESCALATE


def test_revert_streak_passes_below_threshold():
    action = make_build_action()
    key = guard.idempotency_key(action)
    agent_state = AgentState(revert_counts={key: 1})
    policy = make_policy(escalation=EscalationCfg(on_revert_count=2))
    report = evaluate(action, make_snapshot(), policy, agent_state)
    assert verdict(report, "revert_streak").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# Decision aggregation
# --------------------------------------------------------------------------------------


def test_decision_is_block_if_any_gate_blocks_even_with_escalations_present():
    action = make_build_action(planet_id=999)  # affordability/reserve/energy/fields/value_ceiling all BLOCK
    report = evaluate(action, make_snapshot(), make_policy())
    assert report.decision is Decision.BLOCK


def test_decision_is_escalate_when_no_block_but_an_escalate_present():
    action = make_build_action(kind=ActionKind.RESEARCH, function="startResearch", entity_id=ids.Technology.ENERGY, target_level=1, cost=Resources(metal=1))
    policy = make_policy(tier=Tier.ECONOMY)
    report = evaluate(
        action, make_snapshot(), policy, live_addresses={LIVE_ADDR}, unsigned_tx=make_unsigned_tx(), gas_cost_wei=None
    )
    assert report.decision is Decision.ESCALATE


# --------------------------------------------------------------------------------------
# Cross-layer agreement
# --------------------------------------------------------------------------------------


def test_tier_map_agrees_with_the_wallet_engines_allowlist():
    """The two enforcement layers are deliberately duplicated; nothing kept them in sync.

    `guard._MIN_TIER_FOR_FUNCTION` (Python) and `allowlist.ts`'s `ECONOMY_SIGNATURES` /
    `LAUNCH_FLEET_MISSION_SIGNATURES` (TypeScript) encode the same tier policy in two
    languages, on purpose: the wallet engine must re-check independently of whatever the
    agent claims to have checked. But the second judge pass observed they "match today by
    inspection only" — there was no test that would notice a future edit to one and not
    the other.

    A *permissive* divergence fails closed (the wallet-side allowlist is the authoritative
    backstop and refuses), so this is not a security hole. It is a correctness and
    debuggability hole: the agent would confidently propose and build an action that
    `walletctl` then rejects, which is exactly the dead-config failure mode that the
    `startShipProduction` bug produced for weeks before anyone noticed.

    Parsed rather than hardcoded so this test tracks the real file. Skipped when the
    wallet skill isn't alongside — an installed copy of this skill is standalone.
    """
    import re
    from pathlib import Path

    import pytest

    # tests/ -> veydrift-agent/ -> skills/  ... then across to the sibling wallet skill.
    allowlist_ts = (
        Path(__file__).resolve().parents[2] / "veydrift-wallet" / "src" / "allowlist.ts"
    )
    if not allowlist_ts.is_file():
        pytest.skip(f"wallet engine not alongside this skill ({allowlist_ts})")

    source = allowlist_ts.read_text()

    def names_in(const: str) -> set[str]:
        block = re.search(rf"const {const}\s*=\s*\[(.*?)\]\s*as const;", source, re.S)
        assert block, f"could not find {const} in {allowlist_ts}"
        # Signatures are full: "startBuildingUpgrade(uint256,uint8)" -> bare name.
        return {m.split("(", 1)[0] for m in re.findall(r'"([^"]+)"', block.group(1))}

    ts_economy = names_in("ECONOMY_SIGNATURES")
    ts_operator_extra = names_in("LAUNCH_FLEET_MISSION_SIGNATURES")

    py_economy = {fn for fn, tier in guard._MIN_TIER_FOR_FUNCTION.items() if tier is Tier.ECONOMY}
    py_operator = {fn for fn, tier in guard._MIN_TIER_FOR_FUNCTION.items() if tier is Tier.OPERATOR}

    assert py_economy == ts_economy, (
        "economy-tier functions disagree between guard.py and allowlist.ts.\n"
        f"  only in guard.py:    {sorted(py_economy - ts_economy)}\n"
        f"  only in allowlist.ts:{sorted(ts_economy - py_economy)}"
    )
    assert py_operator == ts_operator_extra, (
        "operator-tier functions disagree between guard.py and allowlist.ts.\n"
        f"  only in guard.py:    {sorted(py_operator - ts_operator_extra)}\n"
        f"  only in allowlist.ts:{sorted(ts_operator_extra - py_operator)}"
    )
