"""Tests for veydrift_agent.guard — the 21-gate guardrail evaluator.

The most important tests here are the "missing data must not vacuously pass" ones (one
per gate where that risk is real: `address`, `abi_hash`, `affordability`, `energy`,
`fields`, `reserve`, `gas`, `eth_floor`, `value_ceiling`, `prerequisites`). Each
constructs a snapshot/policy/action where the relevant field is `None` (or otherwise
absent) and asserts the gate resolves to `BLOCK` or `ESCALATE`, never `PASS` — the exact
defect the work package brief calls out as the most likely real bug in this package.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veydrift_agent import guard, ids
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    Decision,
    EnergyBalance,
    Entity,
    EscalationCfg,
    GameMaintenance,
    GuardStatus,
    Limits,
    PlanetSnapshot,
    Policy,
    RandomnessReadiness,
    Resources,
    Snapshot,
    Tier,
)
from veydrift_agent.state import AgentState, PendingTx

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"

# Sentinel for make_snapshot's game_maintenance default. A plain `None` default can't
# serve here because `None` is itself a meaningful, distinct value -- it means
# "gameMaintenance absent from /health" -- and callers (e.g.
# test_game_paused_gate_blocks_when_game_maintenance_is_none) rely on being able to pass
# it explicitly. _UNSET marks "caller didn't specify," so the default `GameMaintenance`
# instance is built fresh inside the function body on every call, instead of being a
# single mutable object shared across every make_snapshot() call at import time.
_UNSET = object()


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
            # Both at level 1 so the suite's many pre-existing Energy Technology research
            # actions and Solar Satellite ship actions stay legal under the `prerequisites`
            # gate (Energy needs Research Lab >= 1; Solar Satellite needs Shipyard >= 1) --
            # tests that specifically exercise `prerequisites` override these explicitly.
            Entity(id=ids.Building.RESEARCH_LAB, name="Research Lab", level=1, cost=Resources(metal=200, crystal=400, deuterium=200)),
            Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100)),
        ],
        ships=[],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def make_snapshot(
    *, planets=None, health_ok=True, abi_hash=guard.PINNED_ABI_HASH,
    game_maintenance=_UNSET, **overrides,
) -> Snapshot:
    base = dict(
        taken_at=NOW,
        wallet=WALLET,
        health_ok=health_ok,
        deployment_abi_hash=abi_hash,
        game_maintenance=GameMaintenance(paused=False) if game_maintenance is _UNSET else game_maintenance,
        planets=planets if planets is not None else [make_planet()],
        # Deliberately 0, not the "real" count matching `planets` above (1) or `None`
        # (the model's own fail-closed default): a fixture default chosen so tests that
        # don't care about the colony cap never trip it, same reasoning as this
        # function's own `abi_hash=guard.PINNED_ABI_HASH` default. Tests that exercise
        # `_colony_cap_violation` itself override this explicitly.
        owned_planet_count=0,
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
# All 22 gates are always present, never short-circuited.
#
# Was 17, then 18, then 19, then 20, then 21 (this test's own name is now five gates
# stale, kept for git-blame continuity). Most recently, commit 7 of the launch-actions
# plan added `missile_target` (`_gate_missile_target`) -- an independent re-derivation of
# `launchInterplanetaryMissileAttack`'s range/primary-target/owned-missile-count
# preconditions. Before it, commit 6 added `attack_protection`
# (`_gate_attack_protection`) -- a live, target-specific re-check of `/wallet/{addr}/
# attack-protection`, independent of whatever `candidates.generate_attack_candidates`
# already read at generation time. Before it, commit 2 added `fleet_slots`
# (`_gate_fleet_slots`) -- a re-derivation of the contract's `FleetSlotLimitReached`
# check, independent of whatever the planner already verified. Before it, this change
# added the `game_paused` gate (`_gate_game_paused`) as the second, independent line of
# defense behind `plan.py`'s rung `1b` for a chain-side maintenance pause -- see that
# function's docstring. Before that, Phase 5c (docs/SPEC.md §5.5) added `mission_type`
# for `launchFleetMission` -- see guard.py's `_gate_mission_type` and
# `_ALLOWED_MISSION_TYPES`. Each of these is the same situation this test's own comment
# already described: a new *mandatory* gate necessarily changes the fixed-length
# enumeration this test pins, and there is no way to add a gate without that. All five
# are additive and PASS trivially for the routine build action used here (see below), so
# this is the only place their addition is visible in a pre-existing test.
# --------------------------------------------------------------------------------------


def test_all_nineteen_gates_always_present_even_when_blocked():
    action = make_build_action()
    report = evaluate(action, make_snapshot(health_ok=False), make_policy())
    assert report.total == 22
    gates = {v.gate for v in report.verdicts}
    assert gates == {
        "killswitch", "tier", "mission_type", "prerequisites", "fleet_slots", "missile_target", "attack_protection",
        "address",
        "abi_hash", "health",
        "game_paused", "index_lag", "affordability", "energy", "storage_overflow", "fields", "reserve",
        "gas", "eth_floor", "value_ceiling", "idempotency", "revert_streak",
    }
    assert report.decision is Decision.BLOCK
    # health failing does not stop e.g. affordability from also being evaluated
    assert verdict(report, "affordability").status is GuardStatus.PASS
    # mission_type PASSes trivially for a non-launchFleetMission action -- it never adds
    # noise to a routine build/research/ship/defense proposal.
    assert verdict(report, "mission_type").status is GuardStatus.PASS
    # fleet_slots PASSes trivially for the same reason -- scoped to FLEET_MISSION only.
    assert verdict(report, "fleet_slots").status is GuardStatus.PASS
    # game_paused PASSes given make_snapshot's default not-paused game_maintenance.
    assert verdict(report, "game_paused").status is GuardStatus.PASS


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


# --------------------------------------------------------------------------------------
# mission_type — Phase 5c's default-deny gate for launchFleetMission, independent of and
# in addition to `tier`. MISSING DATA (`mission_type is None`) must not vacuously pass,
# same rule as every other gate in this module.
# --------------------------------------------------------------------------------------


def make_fleet_action(**overrides) -> Action:
    from veydrift_agent.models import ActionKind as _ActionKind

    base = dict(
        kind=_ActionKind.FLEET_MISSION,
        function="launchFleetMission",
        planet_id=664,
        mission_type=ids.FleetMissionType.TRANSPORT,
        origin_planet_id=664,
        target_coordinates="7:181:15",
        ships={ids.Ship.SMALL_CARGO: 1},
        rule="10:logistics-transport",
        rationale="test",
    )
    base.update(overrides)
    return Action(**base)


def test_mission_type_passes_trivially_for_a_non_fleet_action():
    report = evaluate(make_build_action(), make_snapshot(), make_policy())
    assert verdict(report, "mission_type").status is GuardStatus.PASS


def test_mission_type_blocks_when_mission_type_is_none_never_passes_vacuously():
    action = make_fleet_action(mission_type=None)
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "no mission_type set" in v.detail


def test_mission_type_allows_transport_deploy_colonize_harvest():
    for mt in (
        ids.FleetMissionType.TRANSPORT,
        ids.FleetMissionType.DEPLOY,
        ids.FleetMissionType.COLONIZE,
        ids.FleetMissionType.HARVEST,
    ):
        action = make_fleet_action(mission_type=mt)
        report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0)
        assert verdict(report, "mission_type").status is GuardStatus.PASS, mt


def test_mission_type_blocks_every_combat_type():
    """With the default policy (`allow_combat=False`), every combat type BLOCKs --
    including Attack, which becomes conditionally allowed once `allow_combat=True` (see
    the dedicated block below); the other five never do, regardless of policy."""
    for mt in (
        ids.FleetMissionType.ATTACK,
        ids.FleetMissionType.ACS_DEFEND,
        ids.FleetMissionType.INTERCEPT,
        ids.FleetMissionType.MISSILE_ATTACK,
        ids.FleetMissionType.ACS_ATTACK,
        ids.FleetMissionType.DEFENSE_HOLD,
    ):
        action = make_fleet_action(mission_type=mt)
        report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR))
        v = verdict(report, "mission_type")
        assert v.status is GuardStatus.BLOCK, mt
        # Combat stays refused even though this gate's BLOCK is the ONLY thing standing in
        # the way here (tier is already OPERATOR) -- confirms it is a real, independent
        # enforcement point, not one that only ever fires alongside the tier gate.
        assert verdict(report, "tier").status is GuardStatus.PASS


def test_mission_type_blocks_independently_of_tier_at_every_tier():
    """A combat mission_type BLOCKs at every tier, including operator, with the default
    policy (`allow_combat=False`) -- this gate never relies on the tier gate to do its
    job."""
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    for tier in (Tier.ADVISOR, Tier.ECONOMY, Tier.OPERATOR):
        report = evaluate(action, make_snapshot(), make_policy(tier=tier))
        assert verdict(report, "mission_type").status is GuardStatus.BLOCK


# --------------------------------------------------------------------------------------
# mission_type — Attack conditionally allowed via policy.actions.allow_combat (launch-
# actions plan, commit 5, 2026-08-28). Only Attack (3); the other five combat types
# (AcsDefend/Intercept/MissileAttack/AcsAttack/DefenseHold) stay refused unconditionally
# regardless of allow_combat -- confirmed below, not just asserted in a docstring.
# --------------------------------------------------------------------------------------


def test_mission_type_allows_attack_when_allow_combat_is_true_at_operator_tier():
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy)
    assert verdict(report, "mission_type").status is GuardStatus.PASS


def test_tier_still_blocks_attack_below_operator_even_with_allow_combat():
    """allow_combat widens `mission_type`'s allowed set, never the separate `tier` gate's
    requirement -- Attack still needs operator tier on top of the flag, exactly like every
    other launchFleetMission mission type, so the overall decision stays BLOCK below
    operator even though `mission_type` itself now PASSes."""
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    for tier in (Tier.ADVISOR, Tier.ECONOMY):
        policy = make_policy(tier=tier, actions=ActionsCfg(allow_combat=True))
        report = evaluate(action, make_snapshot(), policy)
        assert verdict(report, "mission_type").status is GuardStatus.PASS, tier
        assert verdict(report, "tier").status is GuardStatus.BLOCK, tier
        assert report.decision is Decision.BLOCK, tier


def test_mission_type_still_blocks_non_attack_combat_types_even_with_allow_combat():
    """allow_combat widens exactly Attack (3) -- AcsDefend/Intercept/MissileAttack/
    AcsAttack/DefenseHold stay refused unconditionally regardless of the flag."""
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    for mt in (
        ids.FleetMissionType.ACS_DEFEND,
        ids.FleetMissionType.INTERCEPT,
        ids.FleetMissionType.MISSILE_ATTACK,
        ids.FleetMissionType.ACS_ATTACK,
        ids.FleetMissionType.DEFENSE_HOLD,
    ):
        action = make_fleet_action(mission_type=mt)
        report = evaluate(action, make_snapshot(), policy)
        assert verdict(report, "mission_type").status is GuardStatus.BLOCK, mt


# --------------------------------------------------------------------------------------
# attack_protection — new gate, commit 6 of the launch-actions plan. Live,
# target-specific re-check of /wallet/{addr}/attack-protection, independent of whatever
# candidates.generate_attack_candidates already read at generation time. Only relevant to
# an Attack launchFleetMission action; every other action PASSes trivially.
# --------------------------------------------------------------------------------------


def test_attack_protection_passes_trivially_for_a_non_attack_action():
    for action in (make_build_action(), make_fleet_action(mission_type=ids.FleetMissionType.TRANSPORT)):
        report = evaluate(action, make_snapshot(), make_policy(), attack_protection_allowed=None)
        assert verdict(report, "attack_protection").status is GuardStatus.PASS


def test_attack_protection_blocks_when_unknown_never_passes_vacuously():
    """`attack_protection_allowed=None` -- a fetch failure, an unresolvable target, or an
    unparseable response -- must BLOCK, never be treated as allowed (AGENTS.md §5)."""
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy, attack_protection_allowed=None)
    v = verdict(report, "attack_protection")
    assert v.status is GuardStatus.BLOCK
    assert "unverifiable" in v.detail.lower() or "could not" in v.detail.lower()


def test_attack_protection_blocks_when_the_live_check_says_not_allowed():
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy, attack_protection_allowed=False)
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


def test_attack_protection_passes_when_the_live_check_says_allowed():
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy, attack_protection_allowed=True)
    assert verdict(report, "attack_protection").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# attack_protection — missile branch (commit 7 of the launch-actions plan). A missile
# ignores the bashing-limit dimension entirely (countsBashing=false server-side), so a
# target blocked ONLY by bashing is legal for a missile even though it's illegal for a
# fleet Attack; score_protection/not_allied still block a missile exactly like Attack.
# --------------------------------------------------------------------------------------


def make_missile_action(**overrides) -> Action:
    base = dict(
        kind=ActionKind.MISSILE_ATTACK,
        function="launchInterplanetaryMissileAttack",
        planet_id=664,
        origin_planet_id=664,
        target_coordinates="7:181:20",
        target_planet_id=23,
        primary_target=ids.Defense.ROCKET_LAUNCHER,
        quantity=5,
        rule="8f:missile",
        rationale="test",
    )
    base.update(overrides)
    return Action(**base)


def test_attack_protection_passes_trivially_for_a_non_missile_non_attack_action():
    report = evaluate(make_build_action(), make_snapshot(), make_policy(), attack_protection_allowed=None)
    assert verdict(report, "attack_protection").status is GuardStatus.PASS


def test_attack_protection_missile_blocks_on_unknown():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy, attack_protection_allowed=None)
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


def test_attack_protection_missile_passes_when_allowed_is_true():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_snapshot(), policy, attack_protection_allowed=True)
    assert verdict(report, "attack_protection").status is GuardStatus.PASS


def test_attack_protection_missile_exempts_a_bashing_only_block():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(
        action, make_snapshot(), policy, attack_protection_allowed=False, attack_protection_blocked_reason="bashing"
    )
    v = verdict(report, "attack_protection")
    assert v.status is GuardStatus.PASS
    assert "bashing" in v.detail.lower()


def test_attack_protection_missile_still_blocks_on_score_protection():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(
        action,
        make_snapshot(),
        policy,
        attack_protection_allowed=False,
        attack_protection_blocked_reason="score_protection",
    )
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


def test_attack_protection_missile_still_blocks_on_not_allied():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(
        action, make_snapshot(), policy, attack_protection_allowed=False, attack_protection_blocked_reason="not_allied"
    )
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


def test_attack_protection_missile_blocks_on_false_with_no_reason_given():
    """A `False` result with a missing/unparsed `blocked_reason` must still BLOCK for a
    missile -- only a POSITIVELY confirmed bashing-only block is exempted, never absence
    of information."""
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(
        action, make_snapshot(), policy, attack_protection_allowed=False, attack_protection_blocked_reason=None
    )
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


def test_attack_protection_fleet_attack_still_blocks_on_bashing_unlike_missile():
    """The exemption is missile-specific -- a fleet Attack blocked by bashing must still
    BLOCK, confirming the branch is keyed on action type, not merely on the reason."""
    action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(
        action, make_snapshot(), policy, attack_protection_allowed=False, attack_protection_blocked_reason="bashing"
    )
    assert verdict(report, "attack_protection").status is GuardStatus.BLOCK


# --------------------------------------------------------------------------------------
# missile_target — new gate, commit 7 of the launch-actions plan. Independently
# re-derives launchInterplanetaryMissileAttack's range/primary-target/owned-missile-count
# preconditions from Snapshot + Action alone (no live data needed, unlike
# attack_protection above).
# --------------------------------------------------------------------------------------


def make_missile_planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=664,
        coordinates="7:181:14",
        resources_as_of_now=Resources(),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(),
        buildings=[],
        ships=[],
        defenses=[
            Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=10, cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000)),
        ],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def make_missile_snapshot(*, planet=None, impulse_drive_level=5) -> Snapshot:
    technologies = (
        [Entity(id=ids.Technology.IMPULSE_DRIVE, name="Impulse Drive", level=impulse_drive_level, cost=Resources())]
        if impulse_drive_level is not None
        else []
    )
    return Snapshot(
        taken_at=NOW,
        wallet=WALLET,
        health_ok=True,
        deployment_abi_hash=guard.PINNED_ABI_HASH,
        technologies=technologies,
        planets=[planet or make_missile_planet()],
    )


def test_missile_target_passes_trivially_for_a_non_missile_action():
    report = evaluate(make_build_action(), make_missile_snapshot(), make_policy())
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_when_allow_combat_is_false():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR)  # actions.allow_combat defaults False
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "allow_combat" in v.detail


def test_missile_target_blocks_when_primary_target_is_none():
    action = make_missile_action(primary_target=None)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "no primary_target" in v.detail


def test_missile_target_blocks_when_primary_target_is_anti_ballistic_missile():
    action = make_missile_action(primary_target=ids.Defense.ANTI_BALLISTIC_MISSILE)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "InvalidMissileTarget" in v.detail


def test_missile_target_blocks_when_primary_target_is_interplanetary_missile_itself():
    action = make_missile_action(primary_target=ids.Defense.INTERPLANETARY_MISSILE)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    assert verdict(report, "missile_target").status is GuardStatus.BLOCK


def test_missile_target_allows_large_shield_dome_the_top_of_the_valid_range():
    action = make_missile_action(primary_target=ids.Defense.LARGE_SHIELD_DOME, target_coordinates="7:181:20")
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_different_galaxy():
    action = make_missile_action(target_coordinates="9:181:20")
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "galaxy" in v.detail.lower()


def test_missile_target_blocks_out_of_range_same_galaxy():
    # Impulse Drive 5 -> range 24; system 181 -> 181+25 = 206 is 25 away, out of range.
    action = make_missile_action(target_coordinates="7:206:20")
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(impulse_drive_level=5), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "range" in v.detail.lower()


def test_missile_target_allows_exactly_at_the_range_boundary():
    # Impulse Drive 5 -> range 24; system 181 -> 181+24 = 205 is exactly at the boundary.
    action = make_missile_action(target_coordinates="7:205:20")
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(impulse_drive_level=5), policy)
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_any_real_distance_when_impulse_drive_is_zero():
    action = make_missile_action(target_coordinates="7:182:20")  # 1 system away
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(impulse_drive_level=0), policy)
    assert verdict(report, "missile_target").status is GuardStatus.BLOCK


def test_missile_target_allows_same_system_when_impulse_drive_is_zero():
    """Impulse Drive 0 means range exactly 0, not "no range at all" -- a same-system
    target is still in range."""
    action = make_missile_action(target_coordinates="7:181:20")  # same system as origin
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(impulse_drive_level=0), policy)
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_when_impulse_drive_is_unreported():
    """`None`/never-researched both mean level 0 -- a real, narrow range, not
    "unverifiable" -- but this test pins that a same-system target still passes even
    then, confirming the fail-closed posture is about missing SNAPSHOT DATA (defense
    counts, coordinates), not about an unresearched technology defaulting sensibly to 0."""
    action = make_missile_action(target_coordinates="7:181:20")
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(impulse_drive_level=None), policy)
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_zero_or_negative_quantity():
    action = make_missile_action(quantity=0)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "quantity" in v.detail.lower()


def test_missile_target_blocks_when_ipm_count_is_unreported():
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    snapshot = make_missile_snapshot(planet=make_missile_planet(defenses=[]))
    report = evaluate(action, snapshot, policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "not reported" in v.detail.lower()


def test_missile_target_blocks_when_not_enough_ipms_owned():
    action = make_missile_action(quantity=20)  # planet only has 10
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    v = verdict(report, "missile_target")
    assert v.status is GuardStatus.BLOCK
    assert "InvalidQuantity" in v.detail


def test_missile_target_passes_when_every_precondition_is_met():
    action = make_missile_action(quantity=10)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    assert verdict(report, "missile_target").status is GuardStatus.PASS


def test_missile_target_blocks_when_origin_planet_not_in_snapshot():
    action = make_missile_action(origin_planet_id=999)
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, make_missile_snapshot(), policy)
    assert verdict(report, "missile_target").status is GuardStatus.BLOCK


def test_missile_target_idempotency_key_distinguishes_target_and_primary_target():
    """Commit 7's own fix to `idempotency_key` -- two missile actions from the same
    planet against different targets, or the same target with a different
    primary_target, must not collapse onto the same key."""
    a = make_missile_action(target_planet_id=23, primary_target=ids.Defense.ROCKET_LAUNCHER)
    b = make_missile_action(target_planet_id=24, primary_target=ids.Defense.ROCKET_LAUNCHER)
    c = make_missile_action(target_planet_id=23, primary_target=ids.Defense.LARGE_SHIELD_DOME)
    keys = {guard.idempotency_key(a), guard.idempotency_key(b), guard.idempotency_key(c)}
    assert len(keys) == 3


# --------------------------------------------------------------------------------------
# mission_type — Colonize target-coordinate range check (judge finding 2, 2026-08-17).
# An out-of-range galaxy/system/position doesn't fail on-chain; it silently corrupts the
# packed target. This is guard.py's independent re-check of tick.py's own bounds check.
# --------------------------------------------------------------------------------------


def test_mission_type_blocks_an_out_of_range_colonize_position():
    action = make_fleet_action(
        mission_type=ids.FleetMissionType.COLONIZE, target_coordinates="1:2:300", ships={ids.Ship.COLONY_SHIP: 1}
    )
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "position" in v.detail


def test_mission_type_blocks_an_out_of_range_colonize_system():
    action = make_fleet_action(
        mission_type=ids.FleetMissionType.COLONIZE, target_coordinates="1:70000:5", ships={ids.Ship.COLONY_SHIP: 1}
    )
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "system" in v.detail


def test_mission_type_blocks_a_malformed_colonize_coordinate_string():
    action = make_fleet_action(mission_type=ids.FleetMissionType.COLONIZE, target_coordinates=None, ships={ids.Ship.COLONY_SHIP: 1})
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "target_coordinates" in v.detail


def test_mission_type_allows_an_in_range_colonize_target():
    action = make_fleet_action(
        mission_type=ids.FleetMissionType.COLONIZE, target_coordinates="1:2:5", ships={ids.Ship.COLONY_SHIP: 1}
    )
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0)
    assert verdict(report, "mission_type").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# mission_type — Colonize colony-cap check (calc.max_planets, `1 + astrophysicsLevel`).
# An account already at its cap gets `PlanetLimitReached` on-chain
# (`VeydriftColonizationModule.sol:289-301`) rather than a graceful no-op; this is
# guard.py's pre-flight re-derivation of that same cap, independent of whatever proposed
# the Colonize action.
# --------------------------------------------------------------------------------------


def make_colonize_action(**overrides) -> Action:
    base = dict(
        mission_type=ids.FleetMissionType.COLONIZE,
        target_coordinates="1:2:5",
        ships={ids.Ship.COLONY_SHIP: 1},
    )
    base.update(overrides)
    return make_fleet_action(**base)


def test_mission_type_blocks_colonize_when_owned_planet_count_is_unknown_never_passes_vacuously():
    action = make_colonize_action()
    report = evaluate(
        action, make_snapshot(owned_planet_count=None), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0
    )
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "owned planet count is unknown" in v.detail


def test_mission_type_blocks_colonize_at_the_colony_cap():
    # Astrophysics level 0 (no technologies reported) -> cap = 1 + 0 = 1; already owning 1.
    action = make_colonize_action()
    report = evaluate(
        action, make_snapshot(owned_planet_count=1), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0
    )
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "colony cap" in v.detail
    assert "1 owned" in v.detail


def test_mission_type_blocks_colonize_above_the_colony_cap():
    """Defense in depth: BLOCKs even if `owned_planet_count` somehow already exceeds the
    cap (e.g. Astrophysics was since downgraded, or the count is stale), not just when
    exactly at it."""
    action = make_colonize_action()
    report = evaluate(
        action, make_snapshot(owned_planet_count=5), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0
    )
    assert verdict(report, "mission_type").status is GuardStatus.BLOCK


def test_mission_type_allows_colonize_under_the_colony_cap_with_higher_astrophysics():
    # Astrophysics level 2 -> cap = 3; owning 2 is still under it.
    action = make_colonize_action()
    snapshot = make_snapshot(
        owned_planet_count=2,
        technologies=[Entity(id=ids.Technology.ASTROPHYSICS, name="Astrophysics", level=2, cost=Resources())],
    )
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=0)
    assert verdict(report, "mission_type").status is GuardStatus.PASS


def test_mission_type_colony_cap_check_does_not_apply_to_non_colonize_missions():
    """A Transport action at `owned_planet_count=1` (which would BLOCK a Colonize) must
    not be affected -- the cap only ever gates Colonize. Also confirms `outgoing_
    colonize_count` being unset (`None`) never affects a non-Colonize mission."""
    action = make_fleet_action(mission_type=ids.FleetMissionType.TRANSPORT)
    report = evaluate(action, make_snapshot(owned_planet_count=1), make_policy(tier=Tier.OPERATOR))
    assert verdict(report, "mission_type").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# _colony_cap_violation's outgoing_colonize_count dimension (commit 4 of the
# launch-actions plan) -- closes the in-flight-Colonize blind spot: owned_planet_count
# alone only reflects planets that have already resolved.
# --------------------------------------------------------------------------------------


def test_mission_type_blocks_colonize_when_outgoing_colonize_count_is_unknown_never_passes_vacuously():
    """Under the cap by owned_planet_count alone, but the in-flight count is unfetchable
    -- must still BLOCK, never silently assume zero in flight."""
    action = make_colonize_action()
    report = evaluate(
        action, make_snapshot(owned_planet_count=0), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=None
    )
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "in-flight Colonize mission count is unknown" in v.detail


def test_mission_type_blocks_colonize_when_in_flight_missions_would_reach_the_cap():
    """owned_planet_count=0 alone is under the Astro-0 cap of 1 -- but one already-
    in-flight Colonize mission projects to exactly the cap, so this must BLOCK, not PASS
    on the stale owned-count alone."""
    action = make_colonize_action()
    report = evaluate(
        action, make_snapshot(owned_planet_count=0), make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=1
    )
    v = verdict(report, "mission_type")
    assert v.status is GuardStatus.BLOCK
    assert "1 in-flight Colonize" in v.detail


def test_mission_type_allows_colonize_when_in_flight_missions_stay_under_the_cap():
    # Astrophysics level 2 -> cap = 3; 1 owned + 1 in-flight = 2, still under it.
    action = make_colonize_action()
    snapshot = make_snapshot(
        owned_planet_count=1,
        technologies=[Entity(id=ids.Technology.ASTROPHYSICS, name="Astrophysics", level=2, cost=Resources())],
    )
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR), outgoing_colonize_count=1)
    assert verdict(report, "mission_type").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# fleet_slots (commit 2 of the launch-actions plan, 2026-08-28) -- an independent
# re-derivation of the contract's FleetSlotLimitReached(1 + ComputerTechnology) check,
# scoped to FLEET_MISSION actions only.
# --------------------------------------------------------------------------------------


def test_fleet_slots_passes_trivially_for_a_non_fleet_action():
    report = evaluate(make_build_action(), make_snapshot(), make_policy())
    assert verdict(report, "fleet_slots").status is GuardStatus.PASS


def test_fleet_slots_blocks_when_active_equals_limit():
    action = make_fleet_action()
    snapshot = make_snapshot(fleet_slots_active=1, fleet_slots_limit=1)
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "fleet_slots")
    assert v.status is GuardStatus.BLOCK
    assert "no free fleet slot" in v.detail
    assert "1/1" in v.detail


def test_fleet_slots_blocks_when_active_exceeds_limit():
    """Defense in depth: BLOCKs even if `fleet_slots_active` somehow already exceeds the
    limit (e.g. Computer Technology was since downgraded, or the count is stale), not
    just when exactly at it -- same posture as the colony-cap check above it."""
    action = make_fleet_action()
    snapshot = make_snapshot(fleet_slots_active=3, fleet_slots_limit=1)
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR))
    assert verdict(report, "fleet_slots").status is GuardStatus.BLOCK


def test_fleet_slots_passes_when_a_slot_is_free():
    action = make_fleet_action()
    snapshot = make_snapshot(fleet_slots_active=0, fleet_slots_limit=1)
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "fleet_slots")
    assert v.status is GuardStatus.PASS
    assert "1 fleet slot(s) free" in v.detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"fleet_slots_active": None, "fleet_slots_limit": 1},
        {"fleet_slots_active": 0, "fleet_slots_limit": None},
        {"fleet_slots_active": None, "fleet_slots_limit": None},
    ],
)
def test_fleet_slots_blocks_on_missing_data_never_passes_vacuously(overrides):
    action = make_fleet_action()
    snapshot = make_snapshot(**overrides)
    report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR))
    v = verdict(report, "fleet_slots")
    assert v.status is GuardStatus.BLOCK
    assert "unknown" in v.detail


# --------------------------------------------------------------------------------------
# Fleet-mission spend derivation (judge finding 1, 2026-08-17). `generate_transport_
# candidates`/`generate_harvest_candidates` built a FLEET_MISSION Action without setting
# `Action.cost` -- exactly what `affordability`/`reserve`/`value_ceiling` read -- so a
# real launch spend (cargo + fuel) evaluated as zero and every one of those gates passed
# vacuously. Fixed on two independent layers: candidates.py now populates `cost`, AND
# guard.py independently re-derives the true spend from `ships`/`cargo`/route rather than
# trusting `action.cost` at all, so a planner that forgets `cost` again is still caught.
# --------------------------------------------------------------------------------------


def _hauler_planet(**overrides) -> PlanetSnapshot:
    from veydrift_agent.models import Entity as _Entity

    base = dict(ships=[_Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=1, cost=Resources())])
    base.update(overrides)
    return make_planet(**base)


def test_reserve_gate_blocks_a_fleet_mission_breach_even_when_action_cost_is_left_zero():
    """Reproduces the brief's own scenario: a planet holding 50,000 deuterium with a
    40,000 reserve floor proposes a Transport of the 10,000 surplus. `Action.cost` is
    left at the frozen model's zero default (as if the planner forgot to set it, or as
    a hostile/buggy caller) -- the gate must derive the true spend independently and
    still BLOCK the breach, never trust the (absent) `Action.cost`."""
    planet = _hauler_planet(resources_as_of_now=Resources(metal=0, crystal=0, deuterium=50_000))
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(cargo=Resources(deuterium=10_000), cost=Resources())
    policy = make_policy(tier=Tier.OPERATOR, reserves=Resources(deuterium=40_000))
    v = verdict(evaluate(action, snapshot, policy), "reserve")
    assert v.status is GuardStatus.BLOCK
    assert "deuterium" in v.detail


def test_affordability_gate_sees_the_true_fleet_mission_spend_not_a_missing_cost():
    """1 Small Cargo's fuel at this fixture's distance (~1005) is 2 deuterium (verified
    against calc.py directly) -- with only 1 deuterium held and `Action.cost` left at
    zero, the gate must still see the real 2-deuterium fuel spend and BLOCK."""
    planet = _hauler_planet(resources_as_of_now=Resources(metal=0, crystal=0, deuterium=1))
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(cost=Resources())
    policy = make_policy(tier=Tier.OPERATOR)
    v = verdict(evaluate(action, snapshot, policy), "affordability")
    assert v.status is GuardStatus.BLOCK


def test_value_ceiling_gate_sees_the_true_fleet_mission_spend():
    """cargo (50) + fuel (2) = 52 against 100 held = 52% of holdings, above the default
    25% escalate_above_pct_of_resources -- must ESCALATE even though `Action.cost` (left
    at zero) would say the spend is nothing."""
    planet = _hauler_planet(resources_as_of_now=Resources(metal=0, crystal=0, deuterium=100))
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(cargo=Resources(deuterium=50), cost=Resources())
    policy = make_policy(tier=Tier.OPERATOR)
    v = verdict(evaluate(action, snapshot, policy), "value_ceiling")
    assert v.status is GuardStatus.ESCALATE


def test_fleet_mission_spend_unverifiable_blocks_never_passes_as_zero():
    """No ships in the action at all -- the spend genuinely cannot be derived. This must
    resolve to BLOCK on every gate that depends on it, never PASS-as-zero (AGENTS.md §5)."""
    planet = _hauler_planet(resources_as_of_now=Resources(deuterium=50_000))
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(ships={})
    policy = make_policy(tier=Tier.OPERATOR)
    report = evaluate(action, snapshot, policy)
    for gate in ("affordability", "reserve", "value_ceiling"):
        assert verdict(report, gate).status is GuardStatus.BLOCK, gate


def test_fleet_mission_spend_derivation_is_a_passthrough_for_non_fleet_actions():
    """Confirms the derivation helper changes nothing for the other five ActionKinds --
    `action.cost` is used as-is, same as before this fix."""
    action = make_build_action(cost=Resources(metal=60, crystal=15))
    report = evaluate(action, make_snapshot(), make_policy(tier=Tier.ECONOMY))
    assert verdict(report, "affordability").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# prerequisites — FLEET_MISSION ship-availability check (also-worth-fixing #2, judge
# review 2026-08-17). Before this fix, FLEET_MISSION had no entry in
# `_FAMILY_FOR_ACTION_KIND`, so `prerequisites` PASSed trivially regardless of whether the
# origin planet actually owned the ships committed.
# --------------------------------------------------------------------------------------


def test_prerequisites_blocks_a_fleet_mission_without_enough_ships_at_origin():
    from veydrift_agent.models import Entity as _Entity

    planet = make_planet(ships=[_Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources())])
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(ships={ids.Ship.SMALL_CARGO: 5})
    v = verdict(evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR)), "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "Small Cargo" in v.detail


def test_prerequisites_blocks_a_fleet_mission_ship_the_snapshot_never_reported():
    planet = make_planet(ships=[])
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(ships={ids.Ship.SMALL_CARGO: 1})
    v = verdict(evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR)), "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "not reported" in v.detail


def test_prerequisites_passes_a_fleet_mission_with_enough_ships_at_origin():
    from veydrift_agent.models import Entity as _Entity

    planet = make_planet(ships=[_Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources())])
    snapshot = make_snapshot(planets=[planet])
    action = make_fleet_action(ships={ids.Ship.SMALL_CARGO: 2})
    v = verdict(evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR)), "prerequisites")
    assert v.status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# prerequisites — independently re-derives the level vectors from the snapshot; MISSING
# DATA must not vacuously pass. Slotted immediately after `tier` and before `address`
# (docs/SPEC.md §5.5).
# --------------------------------------------------------------------------------------


def test_prerequisites_passes_for_an_unlocked_entity():
    # make_planet()'s default Research Lab is level 1, satisfying Energy Technology's
    # only requirement.
    action = make_build_action(kind=ActionKind.RESEARCH, function="startResearch", entity_id=ids.Technology.ENERGY, target_level=1)
    report = evaluate(action, make_snapshot(), make_policy())
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


def test_prerequisites_blocks_an_entity_whose_requirement_is_unmet():
    """Shipyard requires Robotics Factory >= 2; this planet reports Robotics Factory at a
    known, insufficient level (0) -- a genuine "unmet," distinct from the absent-data case
    below."""
    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.ROBOTICS_FACTORY, name="Robotics Factory", level=0, cost=Resources(metal=400, crystal=120, deuterium=200)),
        ]
    )
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(entity_id=ids.Building.SHIPYARD, entity_name="Shipyard", target_level=1)
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "Robotics Factory 2 (have 0)" in v.detail


def test_prerequisites_blocks_on_an_absent_level_never_passes_vacuously():
    """The gate this whole feature is built around: a snapshot that simply never
    reported Robotics Factory's level must BLOCK exactly like a reported level of 0 --
    absent data is never treated as "must be high enough"."""
    planet = make_planet(buildings=[])  # nothing reported at all
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(entity_id=ids.Building.SHIPYARD, entity_name="Shipyard", target_level=1)
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "not reported" in v.detail


def test_prerequisites_passes_trivially_for_an_action_with_no_entity():
    report = evaluate(Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="x"), make_snapshot(), make_policy())
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


def test_prerequisites_blocks_when_planet_not_found():
    action = make_build_action(planet_id=999999, entity_id=ids.Building.SHIPYARD)
    report = evaluate(action, make_snapshot(), make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "999999" in v.detail


def test_prerequisites_blocks_a_second_small_shield_dome():
    """Small Shield Dome is capped at 1 built+queued per planet
    (`techtree.MAX_DEFENSE_PER_PLANET`). Shielding Technology 2 is supplied so the
    *requirement* check passes cleanly and the cap check is what actually fires."""
    planet = make_planet(
        defenses=[Entity(id=ids.Defense.SMALL_SHIELD_DOME, name="Small Shield Dome", count=1, cost=Resources(metal=10_000, crystal=10_000))]
    )
    snapshot = make_snapshot(
        planets=[planet],
        technologies=[Entity(id=ids.Technology.SHIELDING, name="Shielding Technology", level=2, cost=Resources())],
    )
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.SMALL_SHIELD_DOME,
        entity_name="Small Shield Dome",
        quantity=1,
    )
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "capped at 1" in v.detail


def test_prerequisites_allows_the_first_small_shield_dome():
    planet = make_planet(
        defenses=[Entity(id=ids.Defense.SMALL_SHIELD_DOME, name="Small Shield Dome", count=0, cost=Resources(metal=10_000, crystal=10_000))]
    )
    snapshot = make_snapshot(
        planets=[planet],
        technologies=[Entity(id=ids.Technology.SHIELDING, name="Shielding Technology", level=2, cost=Resources())],
    )
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.SMALL_SHIELD_DOME,
        entity_name="Small Shield Dome",
        quantity=1,
    )
    report = evaluate(action, snapshot, make_policy())
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


def test_prerequisites_blocks_missiles_over_silo_capacity():
    """Missile Silo level 4 -> 40 slots (`techtree.missile_silo_capacity`), satisfying
    Anti-Ballistic Missile's own MissileSilo>=2 requirement. 40 Anti-Ballistic Missiles
    already occupy all 40 slots; requesting one more overruns capacity."""
    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100)),
            Entity(id=ids.Building.MISSILE_SILO, name="Missile Silo", level=4, cost=Resources(metal=20_000, crystal=20_000, deuterium=1_000)),
        ],
        defenses=[
            Entity(id=ids.Defense.ANTI_BALLISTIC_MISSILE, name="Anti-Ballistic Missile", count=40, cost=Resources(metal=8_000, deuterium=2_000)),
            Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=0, cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000)),
        ],
    )
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.ANTI_BALLISTIC_MISSILE,
        entity_name="Anti-Ballistic Missile",
        quantity=1,
    )
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "silo slot" in v.detail


def test_prerequisites_allows_missiles_within_silo_capacity():
    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100)),
            Entity(id=ids.Building.MISSILE_SILO, name="Missile Silo", level=4, cost=Resources(metal=20_000, crystal=20_000, deuterium=1_000)),
        ],
        defenses=[
            Entity(id=ids.Defense.ANTI_BALLISTIC_MISSILE, name="Anti-Ballistic Missile", count=39, cost=Resources(metal=8_000, deuterium=2_000)),
            Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=0, cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000)),
        ],
    )
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.ANTI_BALLISTIC_MISSILE,
        entity_name="Anti-Ballistic Missile",
        quantity=1,
    )
    report = evaluate(action, snapshot, make_policy())
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


def test_prerequisites_blocks_a_multi_unit_shield_dome_request_even_at_zero_built():
    """Phase 3 (docs/SPEC.md §5.4) makes ships/defenses stock-keepable toward a declared
    count, so `Action.quantity` can now be > 1 for a defense the pre-Phase-3 ladder never
    produced with anything but `quantity=1` (the hardcoded Rocket Launcher default). This
    pins that `_gate_prerequisites`/`_defense_cap_violation` already generalizes
    correctly to a multi-unit request without any code change: requesting 2 Small Shield
    Domes in one action, with 0 already built, still exceeds the 1-per-planet cap."""
    planet = make_planet(
        defenses=[Entity(id=ids.Defense.SMALL_SHIELD_DOME, name="Small Shield Dome", count=0, cost=Resources(metal=10_000, crystal=10_000))]
    )
    snapshot = make_snapshot(
        planets=[planet],
        technologies=[Entity(id=ids.Technology.SHIELDING, name="Shielding Technology", level=2, cost=Resources())],
    )
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.SMALL_SHIELD_DOME,
        entity_name="Small Shield Dome",
        quantity=2,
    )
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "capped at 1" in v.detail


def test_prerequisites_blocks_a_multi_unit_missile_request_over_remaining_silo_capacity():
    """Same generalization check, missile-silo side: Missile Silo level 4 -> 40 slots: 38
    Anti-Ballistic Missiles already built leaves 2 slots free. Requesting 3 more in one
    action must BLOCK (38 + 3 = 41 > 40), even though a `quantity=1` request from the same
    starting count would have passed -- proving the cap check reads `action.quantity`,
    not an implicit 1."""
    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100)),
            Entity(id=ids.Building.MISSILE_SILO, name="Missile Silo", level=4, cost=Resources(metal=20_000, crystal=20_000, deuterium=1_000)),
        ],
        defenses=[
            Entity(id=ids.Defense.ANTI_BALLISTIC_MISSILE, name="Anti-Ballistic Missile", count=38, cost=Resources(metal=8_000, deuterium=2_000)),
            Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=0, cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000)),
        ],
    )
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.ANTI_BALLISTIC_MISSILE,
        entity_name="Anti-Ballistic Missile",
        quantity=3,
    )
    report = evaluate(action, snapshot, make_policy())
    v = verdict(report, "prerequisites")
    assert v.status is GuardStatus.BLOCK
    assert "silo slot" in v.detail


def test_prerequisites_allows_a_multi_unit_missile_request_within_remaining_silo_capacity():
    """The pass-side mirror of the previous test, same starting count (38/40 used):
    requesting exactly the 2 remaining slots (quantity=2) must PASS."""
    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100)),
            Entity(id=ids.Building.MISSILE_SILO, name="Missile Silo", level=4, cost=Resources(metal=20_000, crystal=20_000, deuterium=1_000)),
        ],
        defenses=[
            Entity(id=ids.Defense.ANTI_BALLISTIC_MISSILE, name="Anti-Ballistic Missile", count=38, cost=Resources(metal=8_000, deuterium=2_000)),
            Entity(id=ids.Defense.INTERPLANETARY_MISSILE, name="Interplanetary Missile", count=0, cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000)),
        ],
    )
    snapshot = make_snapshot(planets=[planet])
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.ANTI_BALLISTIC_MISSILE,
        entity_name="Anti-Ballistic Missile",
        quantity=2,
    )
    report = evaluate(action, snapshot, make_policy())
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


# --------------------------------------------------------------------------------------
# prerequisites x candidates.generate_unlock_chain_candidates (Phase 4 of the
# general-strategy-engine program, docs/SPEC.md §5.4 "Phase 4"). The new candidate family
# is constructed to only ever propose an already-unlocked step -- this confirms that
# holds through the *independent* re-derivation `_gate_prerequisites` performs, rather
# than merely assuming `candidates.py`'s own filtering is trustworthy.
# --------------------------------------------------------------------------------------


def test_prerequisites_gate_passes_an_unlock_chain_step_from_candidates_py():
    """Research Lab already at 1 satisfies both Laser Technology's own building gate
    (dropped from its `unmet()`) and Energy Technology's only requirement -- so Laser's
    remaining branch (Energy >= 2) resolves directly to Energy Technology as the
    shallowest buildable step, a `startResearch` action `_gate_prerequisites` must
    independently re-derive as unlocked."""
    from veydrift_agent import candidates
    from veydrift_agent.models import StrategyCfg

    planet = make_planet(
        buildings=[
            Entity(id=ids.Building.RESEARCH_LAB, name="Research Lab", level=1, cost=Resources(metal=200, crystal=400, deuterium=200)),
        ]
    )
    snapshot = make_snapshot(
        planets=[planet],
        technologies=[Entity(id=ids.Technology.ENERGY, name="Energy Technology", level=0, cost=Resources(crystal=200))],
    )
    policy = make_policy(
        actions=ActionsCfg(allow_research=True),
        strategy=StrategyCfg(research_priority=["Laser Technology"]),
    )

    result = candidates.generate_unlock_chain_candidates(snapshot, policy, planet)
    assert len(result) == 1
    unlock_action = result[0].action
    assert unlock_action.kind == ActionKind.RESEARCH
    assert unlock_action.entity_id == ids.Technology.ENERGY

    report = evaluate(unlock_action, snapshot, policy)
    assert verdict(report, "prerequisites").status is GuardStatus.PASS


def test_defense_cap_violation_blocks_on_a_missile_silo_level_the_snapshot_never_reported():
    """Whitebox test of `guard._defense_cap_violation`'s own fail-closed branch directly:
    through the full `prerequisites` gate this path is currently unreachable (every
    missile-slot defense already has a Missile Silo `Requirement` in
    `techtree.DEFENSE_REQUIREMENTS`, so an absent Missile Silo level always BLOCKs at the
    requirement check first) -- but the cap-check code is real and independently
    defends against the same absent-data failure mode, so it gets its own direct test
    rather than relying on that always being true of every future table entry."""
    planet = make_planet(
        buildings=[Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=1, cost=Resources(metal=400, crystal=200, deuterium=100))],
        defenses=[
            Entity(id=ids.Defense.ANTI_BALLISTIC_MISSILE, name="Anti-Ballistic Missile", count=0, cost=Resources(metal=8_000, deuterium=2_000)),
        ],
    )
    action = make_build_action(
        kind=ActionKind.DEFENSE,
        function="startDefenseProduction",
        entity_id=ids.Defense.ANTI_BALLISTIC_MISSILE,
        entity_name="Anti-Ballistic Missile",
        quantity=1,
    )
    result = guard._defense_cap_violation(action, planet)
    assert result is not None
    assert "Missile Silo" in result


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


def test_health_passes_on_confirmed_combat_only_degradation():
    """Second, independent layer of the same fix plan.py's rung 1 has -- confirms
    guard.py re-derives the same positive confirmation rather than trusting a proposal
    that already made it past rung 1."""
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=True,
        degradation_reasons=[],
        randomness_readiness=RandomnessReadiness(ready=False, reasons=["randomness safety check unavailable"]),
    )
    report = evaluate(make_build_action(), snapshot, make_policy())
    assert verdict(report, "health").status is GuardStatus.PASS


def test_health_withdraws_the_combat_only_exception_specifically_for_an_attack_action():
    """Commit 6 of the launch-actions plan: the exact same confirmed combat-only
    degradation `test_health_passes_on_confirmed_combat_only_degradation` shows PASSing
    for a non-combat action must BLOCK for an Attack action -- randomness readiness is
    the one subsystem an Attack actually depends on (VRF at launch), so the exception
    that's correctly irrelevant to a build action is exactly the wrong thing to apply
    here."""
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=True,
        degradation_reasons=[],
        randomness_readiness=RandomnessReadiness(ready=False, reasons=["randomness safety check unavailable"]),
    )
    attack_action = make_fleet_action(mission_type=ids.FleetMissionType.ATTACK)
    report = evaluate(attack_action, snapshot, make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True)))
    v = verdict(report, "health")
    assert v.status is GuardStatus.BLOCK
    assert "combat" in v.detail.lower()


def test_health_still_passes_the_exception_for_every_non_combat_fleet_mission():
    """The commit-6 correction is scoped to Attack specifically -- a non-combat
    launchFleetMission action (Transport/Deploy/Colonize/Harvest) still gets the
    exception, unchanged."""
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=True,
        degradation_reasons=[],
        randomness_readiness=RandomnessReadiness(ready=False, reasons=["randomness safety check unavailable"]),
    )
    for mt in (
        ids.FleetMissionType.TRANSPORT,
        ids.FleetMissionType.DEPLOY,
        ids.FleetMissionType.COLONIZE,
        ids.FleetMissionType.HARVEST,
    ):
        action = make_fleet_action(mission_type=mt)
        report = evaluate(action, snapshot, make_policy(tier=Tier.OPERATOR))
        assert verdict(report, "health").status is GuardStatus.PASS, mt


def test_health_still_passes_the_exception_for_a_missile_action():
    """Commit 7: unlike Attack, `launchInterplanetaryMissileAttack` never requests
    randomness (interception is deterministic arithmetic, confirmed by reading
    `VeydriftPlanetManagementModule.sol` directly) -- so a randomness-only degradation
    genuinely is irrelevant to it, and the exception correctly still applies. This is a
    considered exclusion from `_gate_health`'s `is_combat_action`, not a gap the gate
    forgot to extend when Missile was added."""
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=True,
        degradation_reasons=[],
        randomness_readiness=RandomnessReadiness(ready=False, reasons=["randomness safety check unavailable"]),
    )
    action = make_missile_action()
    policy = make_policy(tier=Tier.OPERATOR, actions=ActionsCfg(allow_combat=True))
    report = evaluate(action, snapshot, policy)
    assert verdict(report, "health").status is GuardStatus.PASS


def test_health_still_blocks_when_readiness_itself_is_not_ready():
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=False,
        randomness_readiness=RandomnessReadiness(ready=False, reasons=["randomness safety check unavailable"]),
    )
    report = evaluate(make_build_action(), snapshot, make_policy())
    assert verdict(report, "health").status is GuardStatus.BLOCK


def test_health_still_blocks_on_a_genuinely_different_degradation():
    snapshot = make_snapshot(
        health_ok=False,
        readiness_ready=True,
        degradation_reasons=["Upstream RPC unfinished requests are growing or stale."],
        randomness_readiness=RandomnessReadiness(ready=True),
    )
    report = evaluate(make_build_action(), snapshot, make_policy())
    assert verdict(report, "health").status is GuardStatus.BLOCK


# --------------------------------------------------------------------------------------
# game_paused -- second, independent line of defense behind plan.py's rung 1b.
# --------------------------------------------------------------------------------------


def test_game_paused_gate_passes_when_not_paused():
    report = evaluate(
        make_build_action(), make_snapshot(game_maintenance=GameMaintenance(paused=False)), make_policy()
    )
    assert verdict(report, "game_paused").status is GuardStatus.PASS


def test_game_paused_gate_blocks_when_paused():
    report = evaluate(
        make_build_action(),
        make_snapshot(
            game_maintenance=GameMaintenance(paused=True, pause_age_seconds=90),
            degradation_reasons=["game_paused"],
        ),
        make_policy(),
    )
    verdict_ = verdict(report, "game_paused")
    assert verdict_.status is GuardStatus.BLOCK
    assert "game_paused" in verdict_.detail
    assert report.decision is Decision.BLOCK


def test_game_paused_gate_blocks_when_game_maintenance_is_none():
    """Fail-closed: absent gameMaintenance is "cannot confirm", not "confirmed clear" --
    the explicit case models.py's docstring warns against ever reading `game_paused=False`
    alone as confirmation."""
    report = evaluate(make_build_action(), make_snapshot(game_maintenance=None), make_policy())
    verdict_ = verdict(report, "game_paused")
    assert verdict_.status is GuardStatus.BLOCK
    assert "missing" in verdict_.detail.lower()


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


def test_affordability_block_detail_includes_eta_for_short_resource():
    planet = make_planet(
        resources_as_of_now=Resources(metal=200, crystal=1000, deuterium=0),
        production_per_hour=Resources(metal=100, crystal=0, deuterium=0),
    )
    action = make_build_action(cost=Resources(metal=500, crystal=15, deuterium=0))
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    detail = verdict(report, "affordability").detail
    assert "300 more Metal (affordable in ~3h 0m)" in detail


def test_affordability_block_detail_says_never_when_cost_exceeds_storage_cap():
    planet = make_planet(
        resources_as_of_now=Resources(metal=200, crystal=1000, deuterium=0),
        storage_caps=Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        production_per_hour=Resources(metal=100, crystal=0, deuterium=0),
    )
    action = make_build_action(cost=Resources(metal=20_000, crystal=15, deuterium=0))
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    detail = verdict(report, "affordability").detail
    assert "never affordable: cost exceeds storage cap" in detail
    assert "affordable in ~" not in detail  # the only short resource here is the impossible one


def test_affordability_block_detail_says_never_when_production_is_zero():
    planet = make_planet(
        resources_as_of_now=Resources(metal=200, crystal=1000, deuterium=0),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
    )
    action = make_build_action(cost=Resources(metal=500, crystal=15, deuterium=0))
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    detail = verdict(report, "affordability").detail
    assert "never affordable: no production" in detail


def test_affordability_block_detail_shows_both_resources_when_two_are_short():
    planet = make_planet(
        resources_as_of_now=Resources(metal=200, crystal=100, deuterium=0),
        production_per_hour=Resources(metal=100, crystal=50, deuterium=0),
    )
    action = make_build_action(cost=Resources(metal=500, crystal=250, deuterium=0))
    report = evaluate(action, make_snapshot(planets=[planet]), make_policy())
    detail = verdict(report, "affordability").detail
    assert "300 more Metal (affordable in ~3h 0m)" in detail
    assert "150 more Crystal (affordable in ~3h 0m)" in detail


def test_format_eta_hm():
    assert guard._format_eta_hm(1.6333) == "1h 38m"
    assert guard._format_eta_hm(2.0) == "2h 0m"


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
# idempotency_key -- FLEET_MISSION/RESOLVE_MISSION no longer collide (commit 2 of the
# launch-actions plan): entity_id is always None for both kinds, so the base
# f"{planet_id}:{function}:{entity_id}" triple alone collapsed every fleet mission from
# one planet, and every resolve action across all planets, onto one key.
# --------------------------------------------------------------------------------------


def test_idempotency_key_distinguishes_fleet_missions_by_mission_type():
    transport = make_fleet_action(mission_type=ids.FleetMissionType.TRANSPORT)
    harvest = make_fleet_action(mission_type=ids.FleetMissionType.HARVEST)
    assert guard.idempotency_key(transport) != guard.idempotency_key(harvest)


def test_idempotency_key_distinguishes_fleet_missions_by_target():
    to_a = make_fleet_action(target_coordinates="7:181:15")
    to_b = make_fleet_action(target_coordinates="7:181:16")
    assert guard.idempotency_key(to_a) != guard.idempotency_key(to_b)


def test_idempotency_key_distinguishes_resolve_actions_by_mission_id():
    from veydrift_agent.models import ActionKind as _ActionKind

    resolve_a = Action(kind=_ActionKind.RESOLVE_MISSION, function="resolveFleetMission", mission_id=1, rule="3:mission-resolving", rationale="x")
    resolve_b = Action(kind=_ActionKind.RESOLVE_MISSION, function="resolveFleetMission", mission_id=2, rule="3:mission-resolving", rationale="x")
    assert guard.idempotency_key(resolve_a) != guard.idempotency_key(resolve_b)


def test_idempotency_key_unaffected_for_non_fleet_non_resolve_actions():
    """The base f"{planet_id}:{function}:{entity_id}" formula is unchanged for every
    other action kind -- this fix is scoped to FLEET_MISSION/RESOLVE_MISSION only."""
    action = make_build_action()
    assert guard.idempotency_key(action) == f"{action.planet_id}:{action.function}:{action.entity_id}"


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
        # Strip `//` line comments first (Phase 5, 2026-08-17: a removal-note comment
        # inside this exact array, quoting the signature it removed for context, was
        # briefly miscounted as a live entry -- this regex has no comment awareness at
        # all otherwise). A comment can't itself contain a "..." TS string literal in this
        # file's style, so a blunt `//.*$` strip per line is safe here.
        uncommented = re.sub(r"//.*$", "", block.group(1), flags=re.M)
        # Signatures are full: "startBuildingUpgrade(uint256,uint8)" -> bare name.
        return {m.split("(", 1)[0] for m in re.findall(r'"([^"]+)"', uncommented)}

    ts_economy = names_in("ECONOMY_SIGNATURES")
    ts_operator_extra = names_in("LAUNCH_FLEET_MISSION_SIGNATURES")

    py_economy = {fn for fn, tier in guard._MIN_TIER_FOR_FUNCTION.items() if tier is Tier.ECONOMY}
    py_operator = {fn for fn, tier in guard._MIN_TIER_FOR_FUNCTION.items() if tier is Tier.OPERATOR}
    # Commit 7 of the launch-actions plan: launchInterplanetaryMissileAttack is
    # unconditionally `operator` in guard.py's tier map (that's still its real tier
    # requirement), but allowlist.ts pulls its selector out of the unconditional
    # tierSelectors('operator') set entirely -- it lives in the separate, always-
    # conditional COMBAT_SIGNATURES instead (see guard._COMBAT_ONLY_FUNCTIONS's own
    # docstring for why this is a genuine, not accidental, shape difference between the
    # two layers). Excluded here, diffed against COMBAT_SIGNATURES below instead.
    py_operator_unconditional = py_operator - guard._COMBAT_ONLY_FUNCTIONS

    assert py_economy == ts_economy, (
        "economy-tier functions disagree between guard.py and allowlist.ts.\n"
        f"  only in guard.py:    {sorted(py_economy - ts_economy)}\n"
        f"  only in allowlist.ts:{sorted(ts_economy - py_economy)}"
    )
    assert py_operator_unconditional == ts_operator_extra, (
        "operator-tier functions disagree between guard.py and allowlist.ts.\n"
        f"  only in guard.py:    {sorted(py_operator_unconditional - ts_operator_extra)}\n"
        f"  only in allowlist.ts:{sorted(ts_operator_extra - py_operator_unconditional)}"
    )

    ts_combat_signature_names = names_in("COMBAT_SIGNATURES")
    assert guard._COMBAT_ONLY_FUNCTIONS == ts_combat_signature_names, (
        "combat-only (allow_combat-gated) functions disagree between guard.py's "
        "_COMBAT_ONLY_FUNCTIONS and allowlist.ts's COMBAT_SIGNATURES.\n"
        f"  only in guard.py:    {sorted(guard._COMBAT_ONLY_FUNCTIONS - ts_combat_signature_names)}\n"
        f"  only in allowlist.ts:{sorted(ts_combat_signature_names - guard._COMBAT_ONLY_FUNCTIONS)}"
    )
    # Every combat-only function must still be in _MIN_TIER_FOR_FUNCTION at operator --
    # allow_combat widens WHICH functions are permitted, never the tier requirement
    # itself, the exact same principle _gate_mission_type's docstring states for Attack.
    assert guard._COMBAT_ONLY_FUNCTIONS <= py_operator, (
        f"guard._COMBAT_ONLY_FUNCTIONS contains a function not mapped to Tier.OPERATOR in "
        f"_MIN_TIER_FOR_FUNCTION: {sorted(guard._COMBAT_ONLY_FUNCTIONS - py_operator)}"
    )

    # Phase 5c (docs/SPEC.md §5.5): the two layers must also agree on WHICH mission types
    # launchFleetMission may submit, not just that the function itself is allowed --
    # guard.py's `mission_type` gate and allowlist.ts's calldata-level check are two
    # independent implementations of the same restriction (AGENTS.md §5), and this is the
    # one test that would notice them drifting.
    def numbers_in_readonly_set(const: str) -> set[int]:
        block = re.search(rf"const {const}\s*:\s*ReadonlySet<number>\s*=\s*new Set\(\[(.*?)\]\);", source, re.S)
        assert block, f"could not find {const} in {allowlist_ts}"
        uncommented = re.sub(r"//.*$", "", block.group(1), flags=re.M)
        return {int(n) for n in re.findall(r"-?\d+", uncommented)}

    ts_mission_types = numbers_in_readonly_set("OPERATOR_ALLOWED_MISSION_TYPES")
    py_mission_types = set(guard._ALLOWED_MISSION_TYPES)

    assert py_mission_types == ts_mission_types, (
        "launchFleetMission mission types disagree between guard.py's _ALLOWED_MISSION_TYPES "
        "and allowlist.ts's OPERATOR_ALLOWED_MISSION_TYPES.\n"
        f"  only in guard.py:    {sorted(py_mission_types - ts_mission_types)}\n"
        f"  only in allowlist.ts:{sorted(ts_mission_types - py_mission_types)}"
    )

    # Launch-actions plan, commit 5 (2026-08-28): a second, independent pair of static
    # sets for the mission type gated on policy.actions.allow_combat -- Attack only. Kept
    # separate from the unconditional pair above (both sides), on purpose, so a
    # runtime-conditional set never has to be diffed by this test's static TS parse; two
    # static sets per side, compared independently, is the shape that stays parseable.
    ts_combat_mission_types = numbers_in_readonly_set("COMBAT_ALLOWED_MISSION_TYPES")
    py_combat_mission_types = set(guard._COMBAT_MISSION_TYPES)

    assert py_combat_mission_types == ts_combat_mission_types, (
        "combat-gated launchFleetMission mission types disagree between guard.py's "
        "_COMBAT_MISSION_TYPES and allowlist.ts's COMBAT_ALLOWED_MISSION_TYPES.\n"
        f"  only in guard.py:    {sorted(py_combat_mission_types - ts_combat_mission_types)}\n"
        f"  only in allowlist.ts:{sorted(ts_combat_mission_types - py_combat_mission_types)}"
    )
    assert py_combat_mission_types == {ids.FleetMissionType.ATTACK}, (
        f"guard.py's _COMBAT_MISSION_TYPES is not exactly {{Attack}}: {sorted(py_combat_mission_types)} -- "
        "allow_combat is meant to widen exactly one mission type, not combat as an undifferentiated whole"
    )

    # And neither side's UNCONDITIONAL set has smuggled a combat type in -- Attack is
    # correctly absent from these two (it lives only in the combat-gated pair above), and
    # the remaining five combat types must be absent from all four sets, always, at every
    # tier and regardless of allow_combat.
    assert ids.FleetMissionType.ATTACK not in py_mission_types, "guard.py's unconditional set allows Attack outright"
    assert ids.FleetMissionType.ATTACK not in ts_mission_types, "allowlist.ts's unconditional set allows Attack outright"
    never_allowed_combat_types = {
        ids.FleetMissionType.ACS_DEFEND,
        ids.FleetMissionType.INTERCEPT,
        ids.FleetMissionType.MISSILE_ATTACK,
        ids.FleetMissionType.ACS_ATTACK,
        ids.FleetMissionType.DEFENSE_HOLD,
    }
    for label, s in (
        ("guard.py's unconditional set", py_mission_types),
        ("allowlist.ts's unconditional set", ts_mission_types),
        ("guard.py's combat-gated set", py_combat_mission_types),
        ("allowlist.ts's combat-gated set", ts_combat_mission_types),
    ):
        assert not (s & never_allowed_combat_types), f"{label} allows an always-excluded combat type: {s & never_allowed_combat_types}"
