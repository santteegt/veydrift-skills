"""Tests for veydrift_agent.brief — the observed-vs-inferred proposal briefing.

Every test builds its own minimal `Snapshot`/`Policy`/`Action` rather than relying on
`tests/fixtures/*.json` — the point of this module is deterministic risk/fact rules, and
a fixture file would obscure exactly which input triggers which output.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from veydrift_agent import brief, ids, plan, tick
from veydrift_agent import guard as guard_mod
from veydrift_agent.models import (
    Action,
    ActionKind,
    EnergyBalance,
    Entity,
    GameMaintenance,
    IncomingFleet,
    Limits,
    PlanetSnapshot,
    Policy,
    Resources,
    Snapshot,
    StorageCfg,
)

WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"


def _planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=664,
        coordinates="7:181:14",
        resources_as_of_now=Resources(metal=10_000, crystal=10_000, deuterium=5_000),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=100, crystal=100, deuterium=50),
        energy=EnergyBalance(produced=500, required=200, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=2, cost=Resources(metal=150, crystal=60))],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def _snapshot(*, planets: list[PlanetSnapshot] | None = None, **overrides) -> Snapshot:
    base = dict(
        taken_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        wallet=WALLET,
        health_ok=True,
        game_maintenance=GameMaintenance(paused=False),
        latest_indexed_block=100,
        planets=planets if planets is not None else [_planet()],
    )
    base.update(overrides)
    return Snapshot(**base)


def _policy(**overrides) -> Policy:
    base = dict(
        wallet=WALLET,
        planets=[664],
        limits=Limits(gas_per_tx_wei=1, gas_per_day_wei=1, eth_gas_floor_wei=1, escalate_above_pct_of_resources=25),
        storage=StorageCfg(hours_to_cap_trigger=2.0),
    )
    base.update(overrides)
    return Policy(**base)


def _build_action(**overrides) -> Action:
    base = dict(
        kind=ActionKind.BUILD,
        function="startBuildingUpgrade",
        planet_id=664,
        entity_id=ids.Building.SOLAR_PLANT,
        entity_name="Solar Plant",
        target_level=3,
        cost=Resources(metal=150, crystal=60),
        rule="6:building-queue-empty",
        rationale="test action",
    )
    base.update(overrides)
    return Action(**base)


# --------------------------------------------------------------------------------------
# attach() basics: off-chain kinds never get a brief; on-chain kinds always do.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [ActionKind.NOOP, ActionKind.ESCALATE, ActionKind.HALT])
def test_off_chain_actions_never_get_a_brief(kind):
    action = Action(kind=kind, rule="9:no-match", rationale="nothing to do")
    result = brief.attach(action, _snapshot(), _policy())
    assert result.brief is None
    assert result is action  # unchanged, not even a needless copy


def test_on_chain_action_always_gets_a_brief():
    result = brief.attach(_build_action(), _snapshot(), _policy())
    assert result.brief is not None
    assert result.brief.goal  # every on-chain rule this test suite uses has a goal


def test_manual_override_with_unrecognized_rule_gets_the_override_goal():
    action = _build_action(rule="", source="manual_override")
    result = brief.attach(action, _snapshot(), _policy())
    assert result.brief.goal == brief._MANUAL_OVERRIDE_GOAL


def test_unrecognized_rule_falls_back_to_a_generic_goal_rather_than_crashing():
    action = _build_action(rule="99:not-a-real-rule")
    result = brief.attach(action, _snapshot(), _policy())
    assert "99:not-a-real-rule" in result.brief.goal


# --------------------------------------------------------------------------------------
# Every rule plan.py can actually emit for an on-chain Action has a goal. Grepped from
# plan.py's own source rather than hand-maintained, so a new rung can't ship without one.
# --------------------------------------------------------------------------------------

_OFF_CHAIN_RULES = {
    "0:killswitch",
    "1:health-not-ok",
    "1b:game-paused",
    "2:pending-tx-unreconciled",
    "4:incoming-hostile-fleet",
    "9:no-match",
}


def test_every_on_chain_planner_rule_has_a_goal():
    source = Path(plan.__file__).read_text()
    found = set(re.findall(r'"(\d[\w.:-]*)"', source))
    on_chain_rules = found - _OFF_CHAIN_RULES
    assert on_chain_rules, "regex found no rule literals -- plan.py's rule strings changed shape"
    missing = sorted(r for r in on_chain_rules if r not in brief._GOAL_BY_RULE)
    assert not missing, f"plan.py can emit these on-chain rules with no brief goal: {missing}"


# --------------------------------------------------------------------------------------
# Observed vs inferred: every observed fact's source names a real Snapshot path;
# missing data renders "unknown", never a substituted 0.
# --------------------------------------------------------------------------------------

_OBSERVED_SOURCE_RE = re.compile(r"^(planets\[\d+\]\.[\w.]+|research_queue)( \(.+\))?$")


def test_observed_facts_all_name_a_real_snapshot_field():
    result = brief.attach(_build_action(), _snapshot(), _policy())
    assert result.brief.observed
    for fact in result.brief.observed:
        assert _OBSERVED_SOURCE_RE.match(fact.source), f"observed fact {fact.label!r} has a non-snapshot source: {fact.source!r}"


def test_missing_planet_energy_reports_unknown_not_zero():
    planet = _planet(energy=None)
    result = brief.attach(_build_action(), _snapshot(planets=[planet]), _policy())
    energy_fact = next(f for f in result.brief.observed if f.label == "Energy on planet 664")
    assert energy_fact.value == "unknown"


def test_inferred_facts_carry_the_winning_candidates_score_basis():
    result = brief.attach(_build_action(), _snapshot(), _policy(), score_basis="cost 210 / +14/hr = 15.00h payback")
    fact = next(f for f in result.brief.inferred if f.label == "Why this candidate won")
    assert fact.value == "cost 210 / +14/hr = 15.00h payback"
    assert "score_basis" in fact.source


def test_inferred_duration_used_when_entity_duration_seconds_absent():
    # planet's Solar Plant Entity has no duration_seconds -- calc.build_seconds fills in,
    # and must be marked inferred, not observed.
    result = brief.attach(_build_action(), _snapshot(), _policy())
    fact = next(f for f in result.brief.inferred if f.label.startswith("Duration"))
    assert fact.source.startswith("calc.build_seconds")


def test_observed_duration_used_when_the_api_reported_one():
    entity = Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=2, cost=Resources(metal=150, crystal=60), duration_seconds=999)
    planet = _planet(buildings=[entity])
    result = brief.attach(_build_action(), _snapshot(planets=[planet]), _policy())
    fact = next(f for f in result.brief.observed if f.label.startswith("Duration"))
    assert fact.value == "999s"
    assert fact.source.startswith("Entity.duration_seconds")


# --------------------------------------------------------------------------------------
# Prerequisites: techtree.unmet(), never re-derived.
# --------------------------------------------------------------------------------------


def test_prerequisites_reports_all_met_when_unlocked():
    result = brief.attach(_build_action(), _snapshot(), _policy())
    assert result.brief.prerequisites == ["all prerequisites met"]


def test_prerequisites_reports_unmet_requirements():
    action = _build_action(entity_id=ids.Building.NANITE_FACTORY, entity_name="Nanite Factory")
    result = brief.attach(action, _snapshot(), _policy())
    assert result.brief.prerequisites  # Nanite Factory needs Robotics Factory 10 -- unmet on a fresh planet
    assert result.brief.prerequisites != ["all prerequisites met"]


# --------------------------------------------------------------------------------------
# Risks: each rule fires/doesn't in isolation, and the list is sorted high -> low.
# --------------------------------------------------------------------------------------


def _risk_codes(action: Action, snapshot: Snapshot, policy: Policy) -> set[str]:
    return {r.code for r in brief.attach(action, snapshot, policy).brief.risks}


def test_hostile_fleet_incoming_is_a_high_risk_independent_of_the_action():
    snapshot = _snapshot(incoming_fleets=[IncomingFleet(hostile=True)])
    assert "hostile_fleet_incoming" in _risk_codes(_build_action(), snapshot, _policy())


def test_non_hostile_incoming_fleet_does_not_flag_the_risk():
    snapshot = _snapshot(incoming_fleets=[IncomingFleet(hostile=False)])
    assert "hostile_fleet_incoming" not in _risk_codes(_build_action(), snapshot, _policy())


def test_spend_share_fires_above_the_policy_threshold():
    planet = _planet(resources_as_of_now=Resources(metal=100, crystal=100, deuterium=0))
    action = _build_action(cost=Resources(metal=100, crystal=0, deuterium=0))  # 50% of holdings
    assert "spend_share" in _risk_codes(action, _snapshot(planets=[planet]), _policy())


def test_spend_share_does_not_fire_below_the_threshold():
    planet = _planet(resources_as_of_now=Resources(metal=10_000, crystal=10_000, deuterium=0))
    action = _build_action(cost=Resources(metal=10, crystal=0, deuterium=0))
    assert "spend_share" not in _risk_codes(action, _snapshot(planets=[planet]), _policy())


def test_below_reserve_fires_when_spend_would_breach_the_floor():
    planet = _planet(resources_as_of_now=Resources(metal=100, crystal=0, deuterium=0))
    action = _build_action(cost=Resources(metal=50, crystal=0, deuterium=0))
    policy = _policy(reserves=Resources(metal=80, crystal=0, deuterium=0))
    assert "below_reserve" in _risk_codes(action, _snapshot(planets=[planet]), policy)


def test_raidable_after_spend_fires_when_planet_has_raidable_resources():
    planet = _planet(raidable_resources=Resources(metal=500, crystal=0, deuterium=0))
    assert "raidable_after_spend" in _risk_codes(_build_action(), _snapshot(planets=[planet]), _policy())


def test_storage_overflow_while_busy_fires_when_a_cap_is_close():
    planet = _planet(
        resources_as_of_now=Resources(metal=99_000, crystal=0, deuterium=0),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=10_000, crystal=0, deuterium=0),  # ~0.1h to cap
    )
    policy = _policy(storage=StorageCfg(hours_to_cap_trigger=2.0))
    assert "storage_overflow_while_busy" in _risk_codes(_build_action(), _snapshot(planets=[planet]), policy)


def test_data_unavailable_fires_when_the_action_planet_is_missing_from_the_snapshot():
    action = _build_action(planet_id=999999)
    result = brief.attach(action, _snapshot(), _policy())
    assert any(r.code == "data_unavailable" for r in result.brief.risks)
    # every planet-scoped observed fact is genuinely unavailable, not silently omitted
    assert result.brief.observed == []


def test_snapshot_stale_fires_on_unsafe_indexed_state():
    snapshot = _snapshot(safe_to_serve_indexed_state=False)
    assert "snapshot_stale" in _risk_codes(_build_action(), snapshot, _policy())


def test_combat_loss_fires_for_missile_attack():
    action = Action(
        kind=ActionKind.MISSILE_ATTACK,
        function="launchInterplanetaryMissileAttack",
        planet_id=664,
        origin_planet_id=664,
        target_planet_id=42,
        primary_target=ids.Defense.ROCKET_LAUNCHER,
        quantity=1,
        cost=Resources(),
        rule="8f:missile",
        rationale="test",
    )
    assert "combat_loss" in _risk_codes(action, _snapshot(), _policy())


def test_combat_loss_fires_for_attack_mission_type():
    action = Action(
        kind=ActionKind.FLEET_MISSION,
        function="launchFleetMission",
        planet_id=664,
        origin_planet_id=664,
        mission_type=int(ids.FleetMissionType.ATTACK),
        cost=Resources(),
        rule="8e:attack",
        rationale="test",
    )
    assert "combat_loss" in _risk_codes(action, _snapshot(), _policy())


def test_non_combat_mission_type_does_not_flag_combat_loss():
    action = Action(
        kind=ActionKind.FLEET_MISSION,
        function="launchFleetMission",
        planet_id=664,
        origin_planet_id=664,
        mission_type=int(ids.FleetMissionType.TRANSPORT),
        cost=Resources(),
        rule="8c:logistics-transport",
        rationale="test",
    )
    assert "combat_loss" not in _risk_codes(action, _snapshot(), _policy())


def test_risks_are_sorted_high_before_medium_before_low():
    planet = _planet(
        resources_as_of_now=Resources(metal=100, crystal=100, deuterium=0),
        raidable_resources=Resources(metal=500, crystal=0, deuterium=0),
    )
    action = _build_action(cost=Resources(metal=100, crystal=0, deuterium=0))
    snapshot = _snapshot(planets=[planet], incoming_fleets=[IncomingFleet(hostile=True)])
    risks = brief.attach(action, snapshot, _policy()).brief.risks
    severities = [r.severity for r in risks]
    assert severities == sorted(severities, key=lambda s: brief._SEVERITY_RANK[s])
    assert severities[0] == "high"


# --------------------------------------------------------------------------------------
# guard.py must never read .brief -- it is informational only, exactly like
# Action.alternatives.
# --------------------------------------------------------------------------------------


def test_guard_never_reads_brief():
    source = Path(guard_mod.__file__).read_text()
    assert ".brief" not in source


# --------------------------------------------------------------------------------------
# The fingerprint used for proposals.jsonl dedup must ignore brief -- its observed facts
# (live resource amounts, queue seconds-remaining) legitimately change every tick even
# when the proposed action is a genuine content-identical repeat.
# --------------------------------------------------------------------------------------


def test_brief_is_excluded_from_the_proposal_fingerprint():
    record_a = {"ts": "t1", "tick": 1, "rule": "6:x", "brief": {"observed": [{"label": "a", "value": "1000", "source": "x"}]}}
    record_b = {"ts": "t2", "tick": 2, "rule": "6:x", "brief": {"observed": [{"label": "a", "value": "1005", "source": "x"}]}}
    assert tick._fingerprint_proposal(record_a) == tick._fingerprint_proposal(record_b)


def test_brief_excluded_key_actually_differs_would_otherwise_defeat_dedup():
    """Sanity check the fixture above is meaningful -- without the exclusion, these two
    records WOULD hash differently."""
    record_a = {"ts": "t1", "tick": 1, "rule": "6:x", "brief": {"observed": [{"label": "a", "value": "1000", "source": "x"}]}}
    record_b = {"ts": "t2", "tick": 2, "rule": "6:x", "brief": {"observed": [{"label": "a", "value": "1005", "source": "x"}]}}
    naive_a = json.dumps({k: v for k, v in record_a.items() if k not in ("ts", "tick")}, sort_keys=True)
    naive_b = json.dumps({k: v for k, v in record_b.items() if k not in ("ts", "tick")}, sort_keys=True)
    assert hashlib.sha256(naive_a.encode()).hexdigest() != hashlib.sha256(naive_b.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# The generated schema stays in sync with the models (regenerate via
# scripts/generate_schemas.py, never hand-edit).
# --------------------------------------------------------------------------------------


def test_action_schema_matches_the_generated_model_schema():
    from veydrift_agent.models import Action as ActionModel

    schema_path = Path(__file__).parent.parent / "schemas" / "action.schema.json"
    on_disk = json.loads(schema_path.read_text())
    generated = json.loads(json.dumps(ActionModel.model_json_schema(), sort_keys=True))
    on_disk_sorted = json.loads(json.dumps(on_disk, sort_keys=True))
    assert on_disk_sorted == generated, "schemas/action.schema.json is stale -- run scripts/generate_schemas.py"


# --------------------------------------------------------------------------------------
# render_lines: compact vs full, and the top-risk "+N more" summary.
# --------------------------------------------------------------------------------------


def test_render_lines_compact_is_short_and_full_has_observed_and_inferred_sections():
    result = brief.attach(_build_action(), _snapshot(), _policy(), score_basis="15.00h payback")
    compact = brief.render_lines(result.brief, full=False)
    full = brief.render_lines(result.brief, full=True)
    assert len(compact) <= 4
    assert any(line.startswith("observed (API,") for line in full)
    assert any(line.startswith("inferred (planner):") for line in full)
    assert len(full) > len(compact)


def test_render_lines_compact_risk_line_shows_plus_n_more():
    planet = _planet(
        resources_as_of_now=Resources(metal=100, crystal=100, deuterium=0),
        raidable_resources=Resources(metal=500, crystal=0, deuterium=0),
    )
    action = _build_action(cost=Resources(metal=100, crystal=0, deuterium=0))
    snapshot = _snapshot(planets=[planet], incoming_fleets=[IncomingFleet(hostile=True)])
    result = brief.attach(action, snapshot, _policy())
    assert len(result.brief.risks) >= 2
    compact = brief.render_lines(result.brief, full=False)
    risk_line = next(line for line in compact if line.startswith("risk:"))
    assert "+1 more" in risk_line or "more" in risk_line
