"""opportunities.py — every band's own candidate, surfaced independent of `plan.py`'s
ladder outcome.

Why this exists: `plan_next_action` (`plan.py`) is a straight-line early-return chain —
once an earlier band's candidate wins (a mine upgrade, say), every later band's generator
is never even called that tick, not called-and-discarded. This module closes that gap by
calling the same `candidates.py` functions a second time, independent of the ladder, and
reporting every result. Two distinct uses this now serves:

- **Late-band opportunities** (`attack`/`missile`/`colonize`/`foreign_harvest`/
  `transport`): `attack_targets`/`missile_targets`/`foreign_debris_targets`/
  `colonize_targets` are already fetched every tick, tier-independently, gated only by
  their own policy flag exactly like every other read in this codebase — but the data
  *derived* from them (is there a raid target worth knowing about? an open colonize slot?
  foreign debris to harvest?) was invisible on any tick where a higher-priority band won,
  which in practice is most ticks.
- **Early-band ("is the ladder stuck?") diagnosis** (`storage`/`building`/`research`/
  `shipyard`/`unlock_chain`, added alongside `policy.strategy.planet_rotation`): Bands
  1-4 have no cross-tick fairness of their own kind either — Band 2 (building) fires on
  *any* tick where a target planet's building queue is empty and any mine/energy/infra
  candidate exists, which unconditionally precedes Bands 3-4 (research, shipyard/
  defense, unlock-chain) with no policy-configurable weight between them (there is no
  field that reorders the ladder — declaring more `research_priority`/`ship_targets`/
  `defense_targets` names does not help). On an account whose building queue empties
  often relative to its tick cadence, this can make Band 2 win essentially every tick,
  starving research/ships/defense of proposals indefinitely even though nothing is
  misconfigured. This module surfaces what Bands 1-4 would each independently propose
  *right now* so a human or agent who notices the tick loop looks stuck on one band can
  see what's queued up behind it, and, if that's genuinely the right call, force one of
  the alternatives via `vd tick --action` + `policy.strategy.allow_agent_action_override`
  -- see `references/manual-action-override.md`. This is a diagnostic surface, not an
  automatic fix: nothing here changes the ladder's own precedence or fires on its own.

Deliberately excluded (see references/opportunities.md for the full rationale, not
repeated here):

- Deploy — the account's own fleet-logistics move, not an external opportunity to know
  about. Transport WAS excluded on the same reasoning until the ACS defense coordination
  plan's explicit scope decision to make it override-executable like everything else this
  module surfaces -- see `_scan_transport`'s own docstring for why it needed a dedicated
  call rather than fitting `_scan_planet`'s existing dispatch dict.
- No new `policy.*` toggle — visibility is governed entirely by the same policy flags
  each underlying `candidates.py` function already checks internally.
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
    QueueKind,
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
    different best raid target reachable from each of its planets. `_scan_ladder_bands`
    (below) additionally surfaces Bands 1-4's own single winner each, for the same
    unrotated `target_planets` order -- see its own docstring and this module's.

    Every one of the four per-planet generators is pure and already self-gates on its own
    policy flag internally (`allow_combat` for attack/missile, `strategy.colonize` for
    colonize, `allow_fleet_noncombat` for foreign_harvest) -- see this module's
    docstring. Calling them here needs no additional gating: a policy with every flag at
    its default (off) produces an empty `OpportunityReport`, at negligible cost (no
    network call, pure computation over data the caller already fetched for the
    ladder)."""
    target_planets = plan_mod._target_planets(snapshot, policy)

    findings: list[OpportunityFinding] = list(_scan_ladder_bands(snapshot, policy, target_planets))
    for planet in target_planets:
        findings.extend(_scan_planet(snapshot, policy, planet, "attack", attack_targets=attack_targets))
        findings.extend(_scan_planet(snapshot, policy, planet, "missile", missile_targets=missile_targets))
        findings.extend(_scan_planet(snapshot, policy, planet, "colonize", colonize_targets=colonize_targets))
        findings.extend(
            _scan_planet(snapshot, policy, planet, "foreign_harvest", foreign_debris_targets=foreign_debris_targets)
        )
        findings.extend(_scan_transport(snapshot, policy, planet, target_planets))

    return OpportunityReport(findings=findings)


def _scan_ladder_bands(
    snapshot: Snapshot, policy: Policy, target_planets: list[PlanetSnapshot]
) -> list[OpportunityFinding]:
    """Surfaces the single winner Bands 1-4 (storage overflow, building, research,
    shipyard/defense, unlock-chain) would each independently pick right now -- see this
    module's docstring for why. Unlike `_scan_planet`'s per-planet families (which report
    every viable `Candidate`), these five call the same `select_*` functions `plan.py`
    itself calls and report only the winner: their generation logic bakes in real
    preconditions (storage caps, current affordability, `economy_on_track`, locked-vs-
    selectable filtering) that would be wrong to bypass by listing raw candidates the
    ladder itself would never actually pick.

    `target_planets` here is always the plain, unrotated `plan._target_planets(...)`
    result, the same list `scan_opportunities` already computed and passes in --
    `policy.strategy.planet_rotation`'s own rotated view is irrelevant to a snapshot of
    "what's on offer right now" and is deliberately not threaded through here.

    Two of the five `select_*` functions (storage, shipyard) already self-gate on their
    own real-time precondition internally (queue-empty, `economy_on_track`) -- calling
    them directly is enough. The other three do not; `plan.py` applies the gate itself
    before calling them, so this function replicates the exact same external check
    (verbatim from `plan_next_action`) rather than surfacing a "winner" that would
    actually revert or simply not be submittable right now:

    - Building: only for planets whose own `QueueKind.BUILDING` queue is currently empty
      -- a planet with a build already queued cannot start another regardless of what
      `select_building_candidate` would otherwise pick for it.
    - Research: only when `snapshot.research_queue is None` -- the account-wide research
      queue, not per-planet.
    - Unlock-chain has no queue precondition of its own to replicate (its own generator
      never assumes the building queue is idle -- it's a Band reached only once bands 1-3
      have already found nothing at all, so this survey reports it unconditionally, same
      as the real ladder would evaluate it in that situation)."""
    findings: list[OpportunityFinding] = []

    def _add(family: str, winner: candidates.Candidate | None) -> None:
        if winner is None:
            return
        finding = _to_finding_from_winner(family, winner)
        if finding is not None:
            findings.append(finding)

    storage_winner, _ = candidates.select_storage_candidate(snapshot, policy, target_planets)
    _add("storage", storage_winner)

    for planet in target_planets:
        if planet.queues.get(QueueKind.BUILDING) is not None:
            continue
        building_winner, _ = candidates.select_building_candidate(snapshot, policy, planet)
        _add("building", building_winner)

    if snapshot.research_queue is None:
        research_winner, _ = candidates.select_research_candidate(snapshot, policy, target_planets)
        _add("research", research_winner)

    shipyard_winner, _ = candidates.select_shipyard_candidate(snapshot, policy, target_planets)
    _add("shipyard", shipyard_winner)

    unlock_winner, _ = candidates.select_unlock_chain_candidate(snapshot, policy, target_planets)
    _add("unlock_chain", unlock_winner)

    return findings


def _to_finding_from_winner(family: str, winner: candidates.Candidate) -> OpportunityFinding | None:
    """`origin_planet_id` comes from `winner.action.planet_id` directly, unlike
    `_to_finding` below -- all five `_scan_ladder_bands` families always set it (they're
    ordinary planner-shaped actions, not one of the combat/coordination kinds that
    document `Action.planet_id` as optional). Returns `None` rather than raising on the
    shouldn't-happen case where it isn't, matching this module's own "never touches
    guard.py, purely descriptive" posture -- a missing id here means silently drop the
    finding, not crash the tick."""
    action = winner.action
    if action.planet_id is None:
        return None
    return OpportunityFinding(
        family=family,  # type: ignore[arg-type]
        origin_planet_id=action.planet_id,
        target_planet_id=action.target_planet_id,
        target_coordinates=action.target_coordinates,
        detail=action.rationale or winner.score_basis,
    )


def _scan_transport(
    snapshot: Snapshot, policy: Policy, planet: PlanetSnapshot, target_planets: list[PlanetSnapshot]
) -> list[OpportunityFinding]:
    """`candidates.generate_transport_candidates` takes `target_planets` as a required
    POSITIONAL 4th argument, unlike the other four generators' `**target_kwarg`-only
    shape `_scan_planet` dispatches through -- needs its own dedicated call rather than
    forcing that dict to special-case one entry. Reuses the same `target_planets` list
    `scan_opportunities` already computed for the ladder's own ordering, no extra work."""
    results = candidates.generate_transport_candidates(snapshot, policy, planet, target_planets)
    return [_to_finding("transport", planet, candidate) for candidate in results]


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
