"""coordination.py — ACS defense coordination suggestions, derived from `radar.py`'s own
`incoming_fleet` findings.

Why this exists: `radar.py` already detects a live incoming hostile fleet
(`RadarFinding(kind="incoming_fleet", ...)`), but stops at reporting it -- nothing tells
the operator that AcsDefend/Intercept/`launchDefenseHold`/`openDefenseIntent` exist as
real, executable responses (`policy.actions.allow_acs_defense`, manual-override only, see
`ActionsCfg.allow_acs_defense`'s docstring and `references/coordination.md`). This module
closes that gap the same way `opportunities.py` closes the ladder's own blind spot: by
deriving a second, independent report from data already fetched, with zero new network
calls, zero new persisted state, and zero effect on `plan.py`'s ladder or `guard.py`'s
gates.

Filters `radar_report.findings` to `kind == "incoming_fleet"` and `mission_type_name ==
"Attack"` specifically -- the same exactly-Attack requirement `guard._gate_acs_defend_
target`'s own live re-check enforces (AcsDefend/Intercept's counterplay branch only ever
accepts an Attack as the hostile mission being defended against/intercepted; Intercept/
MissileAttack/AcsAttack/DefenseHold rows, if `_incoming_fleet`'s own long-standing
`hostile=True` TODO is ever resolved to include allied-reinforcement mission types
arriving in the *same* array, are correctly excluded here rather than suggested as
something to defend against).

**Known limitation, stated plainly rather than silently assumed away**: this module
cannot detect whether an `openDefenseIntent` has already been opened for a given hostile
mission -- that would need a direct read of the `AllianceDefenseIntentOpened` event or an
equivalent indexed route, which this codebase does not have (`references/coordination.md`
documents the gap). A suggestion is therefore re-surfaced every tick the hostile mission
stays live and reachable, identically, regardless of whether the operator (or an ally)
already acted on it -- exactly the same "current-state fact, correctly re-reported every
tick" posture `opportunities.py`'s own docstring already states for its own findings, not
a defect specific to this module.

Suggestions stay human-readable summary text, same shape decision as `OpportunityFinding`
-- never an auto-generated `--action` JSON. The human/agent still writes that file, now
informed by a real, usable `hostile_mission_id` instead of a `None` (Opus review finding
13, closed by `radar.py`'s own `RadarFinding.mission_id`/`mission_type_name` population).
"""

from __future__ import annotations

from veydrift_agent.models import CoordinationReport, CoordinationSuggestion, RadarReport


def suggest_coordination(radar_report: RadarReport) -> CoordinationReport:
    """One `CoordinationSuggestion` per `incoming_fleet` finding whose `mission_type_name
    == "Attack"` -- every other `kind` (`resolved_attack`/`debris`) and every other
    `mission_type_name` (including `None`, an older/unpopulated finding) is skipped.

    A finding missing `mission_id` (should not happen for a real `incoming_fleet` row
    post-radar-fix, but `RadarFinding.mission_id` stays optional) still produces a
    suggestion -- `hostile_mission_id=None` and `detail` says so plainly -- rather than
    being silently dropped: knowing an attack is incoming with no actionable mission id
    is still worth surfacing, just not yet actionable via `--action`."""
    suggestions: list[CoordinationSuggestion] = []
    for finding in radar_report.findings:
        if finding.kind != "incoming_fleet" or finding.mission_type_name != "Attack":
            continue
        if finding.mission_id is not None:
            detail = (
                f"{finding.detail} -- coordination options: AcsDefend/Intercept "
                f"(launchFleetMission, mission_id={finding.mission_id}) or launchDefenseHold "
                "(station a fleet ahead of time), plus openDefenseIntent to record alliance "
                "coordination for this mission. All four require policy.actions."
                "allow_acs_defense=true and are reachable only via `vd tick --action` "
                "(manual override) -- see references/coordination.md. Cannot detect whether "
                "an openDefenseIntent has already been opened for this mission."
            )
        else:
            detail = (
                f"{finding.detail} -- no live mission id available from this finding, so an "
                "AcsDefend/Intercept `--action` cannot reference it yet; launchDefenseHold "
                "(no hostile-mission reference needed) and openDefenseIntent remain "
                "unaffected by this gap. See references/coordination.md."
            )
        suggestions.append(
            CoordinationSuggestion(
                wallet=finding.wallet,
                target_planet_id=finding.planet_id,
                hostile_mission_id=finding.mission_id,
                detail=detail,
            )
        )
    return CoordinationReport(suggestions=suggestions)
