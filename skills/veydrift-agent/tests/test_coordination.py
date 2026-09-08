"""Tests for veydrift_agent.coordination — the ACS defense coordination suggestion layer
derived from radar.py's own `incoming_fleet` findings."""

from __future__ import annotations

from veydrift_agent import coordination
from veydrift_agent.models import RadarFinding, RadarReport

WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"


def _attack_finding(**overrides) -> RadarFinding:
    base = dict(
        kind="incoming_fleet",
        wallet=WALLET,
        planet_id=664,
        detail="Attack incoming from planet 1, arriving 2026-08-12T13:00:00+00:00",
        mission_id=99001,
        mission_type_name="Attack",
    )
    base.update(overrides)
    return RadarFinding(**base)


def test_suggest_coordination_produces_one_suggestion_per_attack_finding():
    report = RadarReport(findings=[_attack_finding()])
    result = coordination.suggest_coordination(report)
    assert len(result.suggestions) == 1
    s = result.suggestions[0]
    assert s.wallet == WALLET
    assert s.target_planet_id == 664
    assert s.hostile_mission_id == 99001


def test_suggest_coordination_names_all_four_functions_in_detail():
    report = RadarReport(findings=[_attack_finding()])
    detail = coordination.suggest_coordination(report).suggestions[0].detail
    for name in ("AcsDefend", "Intercept", "launchDefenseHold", "openDefenseIntent", "allow_acs_defense"):
        assert name in detail


def test_suggest_coordination_states_the_open_defense_intent_detection_gap():
    report = RadarReport(findings=[_attack_finding()])
    detail = coordination.suggest_coordination(report).suggestions[0].detail
    assert "already been opened" in detail


def test_suggest_coordination_ignores_non_incoming_fleet_findings():
    report = RadarReport(
        findings=[
            RadarFinding(kind="resolved_attack", wallet=WALLET, planet_id=664, detail="battleReport 1: AttackerWin"),
            RadarFinding(kind="debris", wallet=WALLET, planet_id=664, detail="debris field"),
        ]
    )
    assert coordination.suggest_coordination(report).suggestions == []


def test_suggest_coordination_ignores_non_attack_mission_types():
    """AcsDefend/Intercept's own launch gate requires exactly Attack (Opus review finding
    2) -- a suggestion for anything else would point at an action that can never pass
    `guard._gate_acs_defend_target`."""
    report = RadarReport(
        findings=[
            _attack_finding(mission_type_name="Harvest"),
            _attack_finding(mission_type_name="Transport"),
            _attack_finding(mission_type_name=None),
        ]
    )
    assert coordination.suggest_coordination(report).suggestions == []


def test_suggest_coordination_still_surfaces_a_finding_with_no_mission_id():
    """A missing mission_id means the AcsDefend/Intercept `--action` path isn't yet
    actionable, but the finding is still worth surfacing -- not silently dropped."""
    report = RadarReport(findings=[_attack_finding(mission_id=None)])
    suggestions = coordination.suggest_coordination(report).suggestions
    assert len(suggestions) == 1
    assert suggestions[0].hostile_mission_id is None
    assert "no live mission id" in suggestions[0].detail
    assert "launchDefenseHold" in suggestions[0].detail
    assert "openDefenseIntent" in suggestions[0].detail


def test_suggest_coordination_handles_multiple_findings_across_planets():
    report = RadarReport(
        findings=[
            _attack_finding(planet_id=664, mission_id=1),
            _attack_finding(planet_id=665, mission_id=2),
        ]
    )
    suggestions = coordination.suggest_coordination(report).suggestions
    assert {s.target_planet_id for s in suggestions} == {664, 665}
    assert {s.hostile_mission_id for s in suggestions} == {1, 2}


def test_suggest_coordination_empty_report_yields_empty_suggestions():
    assert coordination.suggest_coordination(RadarReport()).suggestions == []
