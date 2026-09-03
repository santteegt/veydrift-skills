"""opportunities.py — attack/missile/colonize/foreign-harvest candidates, surfaced
independent of `plan.py`'s ladder outcome.

Why this exists: `plan_next_action` (`plan.py`) is a straight-line early-return chain —
once an earlier band's candidate wins (a mine upgrade, say), every later band's generator
(logistics, colonize, attack, missile) is never even called that tick, not
called-and-discarded. `attack_targets`/`missile_targets`/`foreign_debris_targets`/
`colonize_targets` are already fetched every tick, tier-independently, gated only by
their own policy flag exactly like every other read in this codebase — but the data
*derived* from them (is there a raid target worth knowing about? an open colonize slot?
foreign debris to harvest?) was invisible on any tick where a higher-priority band won,
which in practice is most ticks. This module closes that gap by calling the same
generators a second time, independent of the ladder, and reporting every result.

Deliberately excluded (see references/opportunities.md for the full rationale, not
repeated here):

- Transport/Deploy — the account's own fleet-logistics moves, not an external
  opportunity to know about.
- No new `policy.*` toggle — visibility is governed entirely by the same
  `allow_combat`/`allow_fleet_noncombat`/`strategy.colonize` flags each generator already
  checks internally.
- No persisted de-duplication state — an opportunity is a current-state fact, correctly
  re-reported every tick it's still true, unlike a radar finding (a one-time event).
- No standalone CLI — `vd tick` integration only.

Never touches guard.py. The `Action`s embedded in each `Candidate` here are never built
into calldata, never simulated, never sent -- purely descriptive.
"""

from __future__ import annotations

from veydrift_agent import candidates
from veydrift_agent import plan as plan_mod
from veydrift_agent.models import (
    OpportunityFinding,
    OpportunityReport,
    PlanetSnapshot,
    Policy,
    Resources,
    Snapshot,
)


def scan_opportunities(
    snapshot: Snapshot,
    policy: Policy,
    *,
    attack_targets: dict[int, tuple[str, Resources, bool | None]],
    missile_targets: dict[int, tuple[str, dict[int, int], bool | None]],
    foreign_debris_targets: dict[int, tuple[str, Resources]],
    colonize_targets: list[tuple[str, int]],
) -> OpportunityReport:
    """Calls `candidates.generate_attack_candidates`/`generate_missile_candidates`/
    `generate_colonize_candidates`/`generate_foreign_harvest_candidates` once per owned
    planet (`plan._target_planets(snapshot, policy)` -- the same helper the ladder
    itself uses, reused directly rather than reimplemented), for every planet a
    generator finds a viable candidate. Unlike the ladder (which picks one global
    winner), this surfaces one finding per planet that has a reachable target, since
    reachability/fuel cost is planet-dependent -- a multi-planet account can have a
    different best raid target reachable from each of its planets.

    Every one of the four generators is pure and already self-gates on its own policy
    flag internally (`allow_combat` for attack/missile, `strategy.colonize` for
    colonize, `allow_fleet_noncombat` for foreign_harvest) -- see this module's
    docstring. Calling them here needs no additional gating: a policy with every flag at
    its default (off) produces an empty `OpportunityReport`, at negligible cost (no
    network call, pure computation over data the caller already fetched for the
    ladder)."""
    target_planets = plan_mod._target_planets(snapshot, policy)

    findings: list[OpportunityFinding] = []
    for planet in target_planets:
        findings.extend(_scan_planet(snapshot, policy, planet, "attack", attack_targets=attack_targets))
        findings.extend(_scan_planet(snapshot, policy, planet, "missile", missile_targets=missile_targets))
        findings.extend(_scan_planet(snapshot, policy, planet, "colonize", colonize_targets=colonize_targets))
        findings.extend(
            _scan_planet(snapshot, policy, planet, "foreign_harvest", foreign_debris_targets=foreign_debris_targets)
        )

    return OpportunityReport(findings=findings)


def _scan_planet(
    snapshot: Snapshot,
    policy: Policy,
    planet: PlanetSnapshot,
    family: str,
    **target_kwarg: object,
) -> list[OpportunityFinding]:
    generator = {
        "attack": candidates.generate_attack_candidates,
        "missile": candidates.generate_missile_candidates,
        "colonize": candidates.generate_colonize_candidates,
        "foreign_harvest": candidates.generate_foreign_harvest_candidates,
    }[family]
    results = generator(snapshot, policy, planet, **target_kwarg)  # type: ignore[operator]
    return [_to_finding(family, planet, candidate) for candidate in results]


def _to_finding(family: str, planet: PlanetSnapshot, candidate: candidates.Candidate) -> OpportunityFinding:
    # `origin_planet_id` comes from the launch `planet` passed to the generator, not
    # `candidate.action.planet_id` -- the latter is optional on the frozen `Action`
    # model and this module already has the real, non-optional origin in hand.
    action = candidate.action
    return OpportunityFinding(
        family=family,  # type: ignore[arg-type]
        origin_planet_id=planet.planet_id,
        target_planet_id=action.target_planet_id,
        target_coordinates=action.target_coordinates,
        detail=action.rationale or candidate.score_basis,
    )
