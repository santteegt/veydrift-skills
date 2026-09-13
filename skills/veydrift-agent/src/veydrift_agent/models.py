"""Frozen interface contract shared by the read, plan, guard, tick and log layers.

This module is the integration point between work packages. Treat every model here as a
published API: add fields freely, but do not rename or retype an existing one without
updating every consumer.

Conventions
-----------
* Resource and wei quantities are ``int``. The Veydrift API serialises them as decimal
  strings; pydantic coerces those automatically, so ``"1000"`` and ``1000`` both parse.
* Times are timezone-aware UTC ``datetime``, or ``int`` unix seconds where the source
  gives a raw contract clock value (``lastSettledAt``).
* ``None`` means "the API did not report this", never "zero". An idle queue is ``None``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------------------
# Enums — canonical, from packages/contracts/src/libraries/VeydriftTypes.sol and
# VeydriftGameStorage.sol. See docs/RESEARCH-ADDENDUM.md §3. Do not reorder.
# --------------------------------------------------------------------------------------


class Tier(str, Enum):
    ADVISOR = "advisor"
    ECONOMY = "economy"
    OPERATOR = "operator"


class QueueKind(str, Enum):
    BUILDING = "building"
    RESEARCH = "research"
    SHIP = "ship"
    DEFENSE = "defense"


class ActionKind(str, Enum):
    """What the planner decided. Every kind before NOOP maps to a contract call."""

    BUILD = "build"                      # startBuildingUpgrade(uint256,uint8)
    RESEARCH = "research"                # startResearch(uint256,uint8)
    SHIP = "ship"                        # startShipProduction(uint256,uint8,uint32)
    DEFENSE = "defense"                  # startDefenseProduction(uint256,uint8,uint32)
    RESOLVE_MISSION = "resolve_mission"  # resolveFleetMission(uint256)
    #: launchFleetMission — **overloaded on the deployed ABI** (a 7-arg and a 6-arg form).
    #: Always resolve it by full canonical signature, never by name (AGENTS.md §7 trap 2).
    #: Non-combat mission types, plus AcsDefend(5)/Intercept(6) since the ACS-coordination
    #: feature (gated on `policy.actions.allow_acs_defense`, `operator` tier, manual-
    #: override only -- no `candidates.py` generator, no `plan.py` ladder rung, same
    #: reachability posture as `ALLIANCE` below). Every other combat type is still refused
    #: independently by `guard.py`'s mission-type gate and `allowlist.ts`'s
    #: OPERATOR_ALLOWED_MISSION_TYPES/ACS_MISSION_TYPES.
    FLEET_MISSION = "fleet_mission"
    #: launchDefenseHold(uint256,uint256,MissionShips,Resources,uint16,uint256) -- ACS
    #: coordination feature. Shares NOTHING with FLEET_MISSION: not an overload of
    #: `launchFleetMission`, no `mission_type` argument at all (`FleetMissionType.
    #: DefenseHold` (9) is dead enum space for that function's own dispatch -- this is a
    #: wholly separate contract entrypoint, confirmed by reading
    #: `VeydriftDefenseHoldModule.sol`/`VeydriftGame.sol` directly), same "wholly separate
    #: entrypoint, own ActionKind" precedent `MISSILE_ATTACK` already set. Still consumes a
    #: fleet slot and real ships -- `_gate_fleet_slots`/`_gate_fleet_ship_availability` are
    #: widened to trigger on this kind too, not just FLEET_MISSION. Gated on
    #: `policy.actions.allow_acs_defense`, `operator` tier, manual-override only.
    DEFENSE_HOLD = "defense_hold"
    #: launchInterplanetaryMissileAttack(uint256,uint256,uint8,uint32) -- added commit 7
    #: of the launch-actions plan. Shares NOTHING with FLEET_MISSION: no fleet tuple, no
    #: `mission_type` argument (`FleetMissionType.MissileAttack` (7) is unreachable dead
    #: enum space for this codebase's own `launchFleetMission` path -- this is a wholly
    #: separate contract entrypoint, not a mission type on that one), no fleet slot, no
    #: travel time -- fully synchronous, confirmed by reading
    #: `VeydriftPlanetManagementModule.sol`'s `launchInterplanetaryMissileAttack`
    #: directly. Gated on `policy.actions.allow_combat` (the same master combat flag
    #: Attack uses) at both enforcement layers, `guard.py`'s `_MIN_TIER_FOR_FUNCTION`
    #: (`operator` tier) and `veydrift-wallet`'s `COMBAT_SIGNATURES`.
    MISSILE_ATTACK = "missile_attack"
    #: One of 15 membership functions on `VeydriftAllianceSystem`, plus (ACS coordination
    #: feature) a 16th, `openDefenseIntent` -- a wholly separate deployed contract, its own
    #: pinned ABI, its own address (`allianceContractAddress`). `action.function`
    #: disambiguates which one (createAlliance, inviteMember, acceptInvite, leaveAlliance,
    #: openDefenseIntent, etc.) the same "one kind, many functions" shape `FLEET_MISSION`
    #: already uses toward `launchFleetMission`'s mission types. **Never planner-produced**
    #: -- no `candidates.py` generator, no `plan.py` ladder rung emits this kind. Reachable
    #: only via `vd tick --action` + `policy.strategy.allow_agent_action_override`. The 15
    #: membership functions are additionally gated on `policy.actions.allow_alliance` at
    #: `economy` tier (membership carries no fund/combat risk). `openDefenseIntent` is
    #: gated on the *different* `policy.actions.allow_acs_defense` flag instead, still at
    #: `economy` tier (it opens a coordination record, moves no fleet/resource) -- checked
    #: as its own special case in `guard._gate_alliance_action`, deliberately not folded
    #: into `_ALLIANCE_FUNCTIONS`/`ALLIANCE_SIGNATURES` (see `_ACS_ALLIANCE_FUNCTIONS`'s own
    #: docstring for why the cross-layer test needs them kept separate). See
    #: `references/manual-action-override.md` for worked examples of both kinds of gating.
    ALLIANCE = "alliance"
    NOOP = "noop"
    ESCALATE = "escalate"
    HALT = "halt"


class GuardStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    ESCALATE = "escalate"


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"


# --------------------------------------------------------------------------------------
# Shared value objects
# --------------------------------------------------------------------------------------


class Base(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=False)


class Resources(Base):
    """A metal/crystal/deuterium triple. Used for holdings, costs, caps and reserves."""

    metal: int = 0
    crystal: int = 0
    deuterium: int = 0

    def covers(self, cost: "Resources") -> bool:
        return (
            self.metal >= cost.metal
            and self.crystal >= cost.crystal
            and self.deuterium >= cost.deuterium
        )


class CrawlerProduction(Base):
    """`/infrastructure`'s `crawlerProduction` block (Phase 3 of the general-strategy-
    engine program, docs/SPEC.md §5.4). Prefer these live numbers over recomputing via
    `calc.crawler_boost_bps` wherever they're present -- same posture
    `EnergyBalance.solar_satellite_energy` already takes. Every field defaults to `None`;
    a `None` here means the API did not report this sub-block, never that the value is
    zero -- treat it as unverifiable, not as "no boost/not capped" (AGENTS.md §5)."""

    total: int | None = None
    effective: int | None = None
    max_effective: int | None = None
    boost_bps: int | None = None
    capped: bool | None = None


class EnergyBalance(Base):
    produced: int
    required: int
    #: 10000 == unscaled. Anything lower means every mine is throttled by this factor.
    scale_bps: int
    #: Read from energyBalance.sources.solarSatelliteEnergy — never recompute it.
    solar_satellite_energy: int | None = None


class Entity(Base):
    """A building, technology, ship or defense as the API reports it.

    ``cost`` is the *live* cost at the current level. Never recompute cost scaling:
    per-building factors are unpublished rationals (docs/RESEARCH-ADDENDUM.md §5).
    """

    id: int
    name: str
    level: int | None = None      # buildings and technologies
    count: int | None = None      # ships and defenses
    cost: Resources
    duration_seconds: int | None = None


class QueueEntry(Base):
    kind: QueueKind
    entity_id: int
    entity_name: str
    target_level: int | None = None
    quantity: int | None = None
    ready_at: datetime | None = None
    seconds_remaining: int | None = None


class GameMaintenance(Base):
    """From /health's gameMaintenance block -- confirmed live (2026-08-21) to always be
    present, `{"paused": false, ...}` when not paused. `None` on Snapshot is defensive
    fail-closed handling for a malformed/future-changed response, not the normal
    not-paused shape -- treat `None` as "unknown", never as "confirmed not paused"."""

    paused: bool
    paused_since: datetime | None = None
    #: Seconds paused so far, from the API directly -- prefer this over computing
    #: duration from paused_since client-side (avoids clock-skew/timezone math).
    pause_age_seconds: int = 0


class RandomnessReadiness(Base):
    """From /health's randomnessReadiness block -- combat-only (gates new attacks via a
    randomness safety check).

    `Snapshot.combat_only_degradation()`'s health-gate exception (`plan.py` rung 1,
    `guard.py`'s `health` gate) still lets a tick proceed through a `/health` failure
    caused *solely* by this signal -- but that no longer means "irrelevant to every
    proposal this codebase could make." The launch-actions plan's commit 5 made
    `allow_combat` a real, checked gate for the Attack mission type at both enforcement
    layers (see `ActionsCfg.allow_combat`'s own docstring), and **commit 6 corrected
    `combat_only_degradation()`'s consumer accordingly**: `guard.py`'s `health` gate now
    takes the action itself as a parameter and withdraws the exception specifically for a
    combat (Attack) action -- a randomness-degraded state must BLOCK an Attack, which
    requests VRF at launch and cannot resolve while degraded, even while every non-combat
    action still passes through the same exception unchanged. `generate_attack_candidates`
    (`candidates.py`) also independently requires `randomness_readiness.ready` before ever
    proposing an Attack in the first place -- belt and suspenders, not redundant: the
    generator-time check keeps a degraded-randomness tick from proposing Attack at all,
    the guard-time check is what actually enforces it if that generator check were ever
    bypassed (a manual `vd tick --action` override, for instance). `None` on Snapshot
    means unconfirmed -- same fail-closed convention as `GameMaintenance`, never read as
    "combat readiness is fine."""

    ready: bool
    reasons: list[str] = Field(default_factory=list)


class IncomingFleet(Base):
    """From /wallet/{addr}/fleet-visibility.incoming — the hostile-fleet escalation trigger."""

    mission_id: str | None = None
    mission_type: int | None = None
    mission_type_name: str | None = None
    origin: str | None = None
    target_planet_id: int | None = None
    arrives_at: datetime | None = None
    hostile: bool = True


class PlanetSnapshot(Base):
    planet_id: int
    coordinates: str | None = None            # "7:181:14"
    name: str | None = None
    archetype: str | None = None
    temperature: int | None = None            # planet MAXIMUM temperature, °C
    fields_used: int | None = None
    fields_total: int | None = None
    metal_multiplier_bps: int = 10_000
    crystal_multiplier_bps: int = 10_000
    deuterium_multiplier_bps: int = 10_000

    resources: Resources = Field(default_factory=Resources)
    #: Lazily-settled "as of now" projection. Prefer this for affordability checks.
    resources_as_of_now: Resources = Field(default_factory=Resources)
    storage_caps: Resources = Field(default_factory=Resources)
    production_per_hour: Resources = Field(default_factory=Resources)
    raidable_resources: Resources | None = None
    #: Semantics UNCONFIRMED (docs/NOTES.md §6). Do not build a loot model on this.
    protected_resources: Resources | None = None

    energy: EnergyBalance | None = None
    buildings: list[Entity] = Field(default_factory=list)
    ships: list[Entity] = Field(default_factory=list)
    defenses: list[Entity] = Field(default_factory=list)
    #: Queues are per-planet except research, which is per-player. None == idle.
    queues: dict[QueueKind, QueueEntry | None] = Field(default_factory=dict)

    #: Phase 3 of the general-strategy-engine program (docs/SPEC.md §5.4). Sourced from
    #: `/wallet/{addr}/defenses`'s `missileSiloLevel`, alongside `shipyardLevel`/
    #: `naniteLevel` on that same route -- `read.py` already fetches this response for
    #: `defenses[]` and previously discarded the field. `None` means the route did not
    #: report it (or the route wasn't reachable), never level 0 -- a missile-silo
    #: capacity check must fail closed on `None`, not divide-by-zero-as-if-empty.
    missile_silo_level: int | None = None
    #: Sourced from `/infrastructure`'s `crawlerProduction` block (also previously
    #: fetched-and-discarded by `read.py`). `None` means the API did not report this
    #: sub-block at all.
    crawler_production: CrawlerProduction | None = None


class Snapshot(Base):
    """Everything one tick needs. Produced by `vd read snapshot --json`."""

    taken_at: datetime
    wallet: str
    #: True only when /health reports ok AND readiness.ready. Replica nulls are not outages.
    health_ok: bool
    #: True only when gameMaintenance.paused is confirmed true. Cheap flat flag for
    #: plan.py/guard.py; game_maintenance carries the detail for rationale/BLOCK strings.
    #: NOT itself the fail-closed signal -- `game_maintenance is None` is (see that
    #: model's docstring). A gate must never read `game_paused` alone as "confirmed not
    #: paused"; it means that *or* "unknown."
    game_paused: bool = False
    game_maintenance: GameMaintenance | None = None
    #: From readiness.degradationReasons -- generic, not pause-specific (see docstring
    #: note on gameMaintenance/degradationReasons in read.py's `_game_maintenance`).
    degradation_reasons: list[str] = Field(default_factory=list)
    #: The raw readiness.ready flag, recovered separately from the combined `health_ok`
    #: bool (which folds `ok` AND `readiness.ready` together and so can't distinguish
    #: "ok=false but readiness.ready=true" from "readiness.ready=false" on its own).
    #: Needed by `combat_only_degradation` below. Defaults `False` (fail-closed).
    readiness_ready: bool = False
    #: `None` means unconfirmed -- see `RandomnessReadiness`'s own docstring.
    randomness_readiness: RandomnessReadiness | None = None
    indexed_state: str | None = None          # "healthy" is the value that matters
    safe_to_serve_indexed_state: bool | None = None
    latest_indexed_block: int | None = None
    deployment_abi_hash: str | None = None
    eth_balance_wei: int | None = None

    #: Per-player, not per-planet.
    technologies: list[Entity] = Field(default_factory=list)
    research_lab_level: int = 0
    research_queue: QueueEntry | None = None
    fleet_slots_active: int | None = None
    fleet_slots_limit: int | None = None

    planets: list[PlanetSnapshot] = Field(default_factory=list)
    #: Total planets this wallet owns, from `/wallet/{addr}/planets`'s full response --
    #: **not** `len(planets)`. `tick.py`'s `_fetch_snapshot` narrows `read.snapshot`'s own
    #: per-planet detail fetch to a single planet when `policy.planets` names exactly one
    #: id (its own cost-saving fast path), so `planets` above can legitimately hold just
    #: one `PlanetSnapshot` for an account that owns several. `None` means unconfirmed --
    #: same fail-closed convention as every other optional field here (AGENTS.md §5): a
    #: colony-cap check must BLOCK on `None`, never assume "not yet at the cap."
    owned_planet_count: int | None = None
    incoming_fleets: list[IncomingFleet] = Field(default_factory=list)

    def planet(self, planet_id: int) -> PlanetSnapshot | None:
        return next((p for p in self.planets if p.planet_id == planet_id), None)

    def combat_only_degradation(self) -> bool:
        """True only when every subsystem this codebase can ever act on is positively
        confirmed healthy, even though `health_ok` is False -- i.e. `ok` is false SOLELY
        because randomnessReadiness (combat-only) is degraded. Fail-closed: requires
        `readiness_ready` True, no other `degradation_reasons`, `game_maintenance`
        positively confirmed not paused, and `randomness_readiness` positively confirmed
        not-ready (never `None`/unconfirmed) -- any other combination returns `False`,
        unchanged from the plain `health_ok` check. Structural (positively confirms
        everything else is fine), not an allowlist of known-safe reason strings -- this
        never inspects `randomness_readiness.reasons`' text, so it's robust against that
        wording changing."""
        return (
            self.readiness_ready
            and not self.degradation_reasons
            and self.game_maintenance is not None
            and not self.game_maintenance.paused
            and self.randomness_readiness is not None
            and not self.randomness_readiness.ready
        )


# --------------------------------------------------------------------------------------
# Alliance state -- fetched fresh each tick from GET /wallet/{addr}/alliance, passed as an
# explicit `evaluate_guardrails(..., alliance_state=...)` parameter. Deliberately NOT a
# field on `Snapshot`/`PlanetSnapshot` above: `Snapshot` is frozen (AGENTS.md §4) and this
# data spans membership, rosters, and pending-request lookups across the 15 alliance
# functions -- flattening it into a dozen more `evaluate_guardrails` kwargs would be worse
# than one grouped model, but it's still guard-time-only live data, the same posture
# `attack_protection_allowed`/`outgoing_colonize_count` already take (tick.py fetches it,
# nobody else does). `extra="ignore"` (via `Base`) tolerates the live indexer response's
# shape drifting without a schema change here.
# --------------------------------------------------------------------------------------


class AllianceMembership(Base):
    """The caller's own membership row, `allianceOf(address)` on the deployed contract --
    or the live indexer's `membership` field, which is what `tick.py` actually reads.
    `None` at the `AllianceState` level means "not in an alliance," not "unknown" -- the
    live route always reports this key, `null` or populated, for any wallet with a home
    planet."""

    alliance_id: int
    role: int  # alliance_ids.AllianceRole value, plain int -- same convention as Action.role
    joined_at: datetime | None = None


class AllianceMember(Base):
    """One row of the caller's own alliance roster (`alliance_state.members`) -- used by
    `guard._gate_alliance_action` to verify a `kickMember`/`setMemberRole`/
    `transferAllianceOwnership` target is a real member with the role the action assumes,
    rather than trusting the generation-time read."""

    address: str
    role: int
    joined_at: datetime | None = None
    total_score: int | None = None


class AlliancePendingInvite(Base):
    """One alliance id the caller holds an active, unaccepted invite for --
    `acceptInvite`'s precondition."""

    alliance_id: int


class AlliancePendingJoinRequest(Base):
    """One alliance id the caller has an active, un-dismissed join request against --
    `cancelJoinRequest`'s precondition."""

    alliance_id: int


class AllianceJoinRequestForOwner(Base):
    """One incoming join request against the CALLER'S OWN alliance (only populated when
    the caller is Officer/Owner of that alliance) -- `approveJoinRequest`/
    `dismissJoinRequest`'s precondition that a matching `(alliance_id, requester)` row
    actually exists."""

    alliance_id: int
    requester: str


class AllianceDirectoryEntry(Base):
    """One alliance from the live indexer's `directory` list -- used by
    `requestJoinAlliance`'s precondition that the target alliance exists and is active.
    Keyed by `alliance_id` in `AllianceState.directory` below, not carried as its own
    field, to make the "does this id exist at all" check a plain dict lookup."""

    active: bool | None = None
    member_count: int | None = None


class AllianceState(Base):
    """Everything `guard._gate_alliance_action` needs to independently re-derive every one
    of the 15 alliance functions' preconditions, sourced from a single
    `GET /wallet/{addr}/alliance` fetch (`tick._alliance_state`). `None` at the top level
    (not this class -- the caller's own `alliance_state: AllianceState | None` parameter)
    means the fetch failed; the gate BLOCKs on that, never assumes "no alliance
    involvement" (AGENTS.md §5)."""

    membership: AllianceMembership | None = None
    #: The roster of the CALLER'S OWN alliance only (empty/irrelevant if not a member) --
    #: not a global member directory.
    members: list[AllianceMember] = Field(default_factory=list)
    pending_invites: list[AlliancePendingInvite] = Field(default_factory=list)
    pending_join_requests: list[AlliancePendingJoinRequest] = Field(default_factory=list)
    alliance_join_requests: list[AllianceJoinRequestForOwner] = Field(default_factory=list)
    #: Keyed by alliance id. Sourced from the live indexer's `directory` list.
    directory: dict[int, AllianceDirectoryEntry] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Radar — one wallet+planet a `radar.check_targets` run watches, and what it found.
# Sourced/produced by `radar.py`; never touches guard.py (radar never constructs an
# on-chain Action). See references/radar.md.
# --------------------------------------------------------------------------------------


class WatchTarget(Base):
    """One planet, on one wallet, radar.py should check. `galaxy`/`system`/`position`
    are the coordinate components needed for the debris signal
    (`fetch_universe_system(galaxy, system)`, then matching the returned slot by
    `position` -- the universe route's slots carry no planet id of their own, only
    `position`, per `references/api-routes.md` §3.16). `None` when unresolved (e.g. a
    planet with no parseable coordinates string), in which case `check_targets` simply
    skips the debris check for that one target rather than failing the whole run."""

    wallet: str
    planet_id: int
    galaxy: int | None = None
    system: int | None = None
    position: int | None = None


class RadarFinding(Base):
    """One thing radar.py's `check_targets` found worth reporting. `kind` distinguishes
    the three independent signal sources (see references/radar.md for why all three are
    needed -- notably, `incoming_fleet` alone cannot see a resolved attack)."""

    kind: Literal["incoming_fleet", "resolved_attack", "debris"]
    wallet: str
    planet_id: int
    #: Human-readable detail, e.g. "Attack from 8:34:5 arriving in 2h14m" or
    #: "battleReport 12345: AttackerWin, 5000 looted". Deliberately free text rather
    #: than a dozen structured sub-fields -- the three `kind`s have genuinely different
    #: shapes and this is reporting-only, never a Decision input.
    detail: str
    occurred_at: datetime | None = None
    #: Populated only for kind="incoming_fleet" -- the raw `missionId` from
    #: /wallet/{addr}/fleet-visibility's `incoming[]` row (previously discarded entirely
    #: by `_incoming_fleet_findings`). `None` for the other two kinds. Lets a downstream
    #: consumer (`coordination.py`'s `suggest_coordination`) reference the real, live
    #: mission id an ACS Defend/Intercept action would need, without parsing `detail`.
    mission_id: int | None = None
    #: Populated only for kind="incoming_fleet" -- the raw `missionType` string (e.g.
    #: "Attack", "Harvest"). `None` for the other two kinds. Lets `coordination.py` filter
    #: to Attack rows specifically without parsing the free-text `detail` string.
    mission_type_name: str | None = None


class RadarReport(Base):
    """Output of one `radar.check_targets` run. `errors` is populated, not raised,
    for a per-wallet/per-system fetch failure -- same best-effort posture as
    `tick._own_planet_debris`/`_resolvable_mission_ids` -- so a report with findings AND
    errors is possible and both must be surfaced, never one silently dropped for the
    other."""

    findings: list[RadarFinding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Opportunities — attack/missile/colonize/foreign-harvest candidates surfaced
# independent of `plan.py`'s ladder outcome (see references/opportunities.md). Sourced
# from `candidates.py`'s existing generators, unchanged; never touches guard.py, never
# sent, never persisted (an opportunity is a current-state fact, re-reported every tick
# it's still true, not a one-time event needing de-duplication like a radar finding).
# --------------------------------------------------------------------------------------


class OpportunityFinding(Base):
    """One candidate `opportunities.scan_opportunities` found for a single launch
    planet, regardless of whether `plan.py`'s ladder ever reached that band this tick.
    `target_planet_id`/`target_coordinates` mirror `Action`'s own fields of the same
    name (`candidate.action.target_planet_id`/`.target_coordinates`) -- `None` when the
    underlying `Action` doesn't resolve a numeric target id (mirrors `Action`'s own
    optionality here, not a new ambiguity)."""

    family: Literal["attack", "missile", "colonize", "foreign_harvest", "transport"]
    origin_planet_id: int
    target_planet_id: int | None = None
    target_coordinates: str | None = None
    #: `candidate.action.rationale`, falling back to `candidate.score_basis` on the rare
    #: chance rationale is empty -- both are already-written human explanations from
    #: `candidates.py`, never re-derived here.
    detail: str


class OpportunityReport(Base):
    findings: list[OpportunityFinding] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Coordination — ACS defense coordination suggestions, derived from an already-built
# RadarReport (see references/coordination.md). Never touches guard.py, never constructs
# an Action -- purely advisory text, the human/agent still writes the `--action` JSON.
# --------------------------------------------------------------------------------------


class CoordinationSuggestion(Base):
    """One suggestion `coordination.suggest_coordination` derived from a single
    `incoming_fleet` RadarFinding whose `mission_type_name == "Attack"`. Human-readable
    summary, same shape decision as `OpportunityFinding` -- never an auto-generated
    action file."""

    wallet: str  # whose planet is under attack -- self or an alliance member
    target_planet_id: int
    #: The live hostile mission id an AcsDefend/Intercept `--action` would reference
    #: (`Action.mission_id`). `None` only if the source RadarFinding's own `mission_id`
    #: was unset (should not happen for a real incoming_fleet row post-Phase-2, but this
    #: stays optional rather than assumed).
    hostile_mission_id: int | None = None
    detail: str  # what's incoming + the coordination options available (see module docstring)


class CoordinationReport(Base):
    suggestions: list[CoordinationSuggestion] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Policy — parsed from $VEYDRIFT_HOME/policy.json. Invalid policy is a hard stop.
# --------------------------------------------------------------------------------------


class Cadence(Base):
    economy_minutes: int = 10
    research_minutes: int = 10
    fleet_minutes: int = 10
    universe_hours: int = 24


class Limits(Base):
    gas_per_tx_wei: int
    gas_per_day_wei: int
    eth_gas_floor_wei: int
    escalate_above_pct_of_resources: int = 25
    max_index_wait_s: int = 300
    field_warn_pct: int = 80


class StorageCfg(Base):
    hours_to_cap_trigger: float = 2.0


class ActionsCfg(Base):
    allow_building: bool = True
    allow_research: bool = True
    allow_defense: bool = False
    allow_ships: bool = False
    allow_fleet_noncombat: bool = False
    #: Live since the launch-actions plan's commit 5 (2026-08-28) -- was previously
    #: deliberately ignored by every code path ("combat requires a code change, not
    #: config"); that source-level friction still applies to *which* mission type this
    #: gates (only Attack, `guard._ALLOWED_MISSION_TYPES`'s `_COMBAT_MISSION_TYPES`
    #: half) -- widening it to another combat type is still a code change, never a
    #: config edit. But for Attack specifically, this flag is now the actual gate, at
    #: both enforcement layers independently (`guard.py`'s `mission_type` gate and
    #: `veydrift-wallet`'s `checkAllowlist`, each resolving it from `policy.json`
    #: separately -- never one trusting the other's read of it). Still requires tier
    #: `operator` on top of this. **Commit 6** adds the first generator that can actually
    #: produce an Attack `Action` (`candidates.generate_attack_candidates`, gated on this
    #: same flag) -- this flag alone was never a planner gate; it is now the same
    #: "empty/off == old behaviour" opt-in every other `policy.actions`/`policy.strategy`
    #: flag in this codebase already is.
    allow_combat: bool = False
    #: Alliance-membership actions (`ActionKind.ALLIANCE` -- createAlliance, inviteMember,
    #: acceptInvite, leaveAlliance, and 11 more, all on the separate `VeydriftAllianceSystem`
    #: contract). Default `False` reproduces the pre-existing behaviour exactly: no path
    #: (planner or override) can submit an alliance action while this is off, same
    #: "empty/off == old behaviour" convention every flag in this class already uses.
    #: Unlike `allow_combat`, this unlocks at tier `economy`, not `operator` -- membership
    #: actions carry no fund/combat risk, so gating them behind the same bar real
    #: fund-moving/combat actions need would be stricter than the actual risk warrants.
    #: Checked independently at both enforcement layers (`guard.py`'s
    #: `_gate_alliance_action` and `veydrift-wallet`'s `checkAllowlist`, each resolving it
    #: from `policy.json` separately), the same "never one layer trusting the other's
    #: read" discipline `allow_combat` already established. There is no CLI flag or
    #: environment variable for this at the wallet layer, ever -- see
    #: `veydrift-wallet/src/policy.ts`'s `resolveAllowAlliance`.
    allow_alliance: bool = False
    #: ACS defense coordination -- AcsDefend/Intercept (`launchFleetMission` mission types
    #: 5/6), `launchDefenseHold` (a wholly separate entrypoint), and `openDefenseIntent`
    #: (on `VeydriftAllianceSystem`) as one feature, since all three combine into a single
    #: shared defense at battle resolution on-chain. Default `False` reproduces the
    #: pre-existing behaviour exactly: no path (planner or override) can submit any of
    #: these four while this is off -- same "empty/off == old behaviour" convention every
    #: flag in this class already uses. **Never planner-produced** -- no `candidates.py`
    #: generator, no `plan.py` ladder rung; reachable only via `vd tick --action` +
    #: `policy.strategy.allow_agent_action_override`, the same manual-override-only
    #: posture the 15 alliance membership functions already established.
    #:
    #: Tier floor is split within this one flag, a new pattern in this codebase: AcsDefend/
    #: Intercept/`launchDefenseHold` require `operator` (real fleet movement, real loss
    #: risk -- same floor Attack/Missile/Transport already require). `openDefenseIntent`
    #: requires only `economy` (it opens a coordination record; moves no fleet, spends no
    #: resource -- same floor the other 15 alliance functions already use). Both still
    #: require this flag; only the tier requirement differs by function. Checked
    #: independently at both enforcement layers (`guard.py`'s `_gate_mission_type`/
    #: `_MIN_TIER_FOR_FUNCTION`/`_gate_alliance_action` and `veydrift-wallet`'s
    #: `checkAllowlist`), never one trusting the other's read of it. No CLI flag or
    #: environment variable for this at the wallet layer, ever -- see `veydrift-wallet/
    #: src/policy.ts`'s `resolveAllowAcsDefense`. See `references/coordination.md` for the
    #: full contract mechanics and the honest verification-status caveats.
    allow_acs_defense: bool = False


class EscalationCfg(Base):
    on_incoming_fleet: bool = True
    on_game_paused: bool = True
    on_abi_hash_change: bool = True
    on_health_unhealthy_minutes: int = 30
    on_revert_count: int = 2


class WalletEngineCfg(Base):
    provider: Literal["keystore", "envkey"] = "keystore"
    require_confirmation: bool = True


class RadarCfg(Base):
    """Gates the `vd tick` integration of radar.py's incoming-fleet / resolved-attack /
    debris check, scoped to `policy.planets` (empty == every owned planet, same
    convention `Policy.planets` already uses for the snapshot itself).

    Default `True` -- a deliberate departure from this codebase's usual "empty/off ==
    old behaviour" convention every other `ActionsCfg`/`StrategyCfg` flag follows. Every
    one of those flags gates something that can spend gas, move resources, or reach
    combat; radar is read-only and finds things a tick would otherwise miss silently --
    see references/radar.md's incident writeup: `snapshot.incoming_fleets` alone missed
    a real attack because it only ever lists future arrivals, not resolved ones. Do not
    "fix" this default to `False` by pattern-matching the other flags in this file; that
    would reproduce the exact gap this field exists to close."""

    enabled: bool = True


class EntityTarget(Base):
    """One declared standing-count target (Phase 3 of the general-strategy-engine
    program, docs/SPEC.md §5.4/§5.6): "I want N of this ship/defense." Resolved against
    `ids.py`'s `*_NAMES` maps case-insensitively via `name`, or directly via `id` —
    exactly one of the two should be set. `candidates.py` resolves `name` at generation
    time and raises loudly (`ValueError`) on an unknown name rather than treating it as
    "no target" — `Policy` is `extra="forbid"`, so a typo in a key is already a hard
    stop; a typo in a *value* here must be too, for the same reason."""

    name: str | None = None
    id: int | None = None
    count: int = 0


class StrategyCfg(Base):
    """Phase 2 (docs/SPEC.md §5.4/§5.6) added `resource_weights`/`max_alternatives`;
    Phase 3 adds the four fields below. All are additive — an older `policy.json` with
    no `strategy` key, or one that sets `strategy` but omits these newer keys, still
    loads (defaults below reproduce pre-Phase-3 behaviour exactly), but a policy file
    that sets one of these new keys is rejected by any agent build predating this field
    (`Policy.model_config` is `extra="forbid"`)."""

    #: Relative worth of one unit of each resource when collapsing a cost triple to a
    #: scalar for payback scoring. Default 1:1:1 preserves the assumption plan.py's
    #: `_energy_candidate` already made implicitly (it summed metal+crystal+deuterium
    #: unweighted) before this field existed.
    resource_weights: Resources = Field(default_factory=lambda: Resources(metal=1, crystal=1, deuterium=1))
    #: Caps `Action.alternatives` so `proposals.jsonl` stays bounded.
    max_alternatives: int = 5

    #: Desired standing ship/defense counts (Phase 3). Production is proposed toward the
    #: first target below its count when the relevant queue is idle, filtered through
    #: `techtree.unmet()` and the entity's own caps. Empty (default) == today's
    #: behaviour: Solar Satellite's separate energy-driven path is untouched by this
    #: field, and defenses fall back to the pre-Phase-3 hardcoded Rocket Launcher pick.
    ship_targets: list[EntityTarget] = Field(default_factory=list)
    defense_targets: list[EntityTarget] = Field(default_factory=list)
    #: Ordered technology preference, by name (resolved via `ids.technology_id`,
    #: case-insensitive). Empty == lowest-level-first, as today. Names not present here
    #: are still considered, ordered after the declared ones by the same lowest-level-
    #: first fallback rule.
    research_priority: list[str] = Field(default_factory=list)
    #: Ordered building preference for the new "infrastructure" family (Robotics
    #: Factory, Nanite Factory, Shipyard, Research Lab, Terraformer, Missile Silo).
    #: Empty == infrastructure buildings are never proposed as a distinct family (they
    #: remain reachable only if some other rung happens to touch them, which none does
    #: pre-Phase-3) — setting this is what makes them reachable at all.
    building_priority: list[str] = Field(default_factory=list)
    #: Explicit opt-in for Crawler production (`candidates.generate_crawler_candidates`).
    #: Default `False` reproduces pre-Phase-3 behaviour exactly: Crawler generation
    #: returns `[]` unconditionally, so `select_shipyard_candidate`'s scored ranking can
    #: never pick it over Solar Satellite, matching `ship_targets`/`building_priority`'s
    #: own "empty/off == old behaviour" convention. Judge finding (2026-08-17): with an
    #: entirely empty `policy.strategy`, an unlocked, scoreable Crawler could silently
    #: outrank Solar Satellite on `select_shipyard_candidate`'s ranked winner, which is
    #: exactly the AC docs/SPEC.md §9 / this field's own docstring say must not happen.
    #: This flag is what makes that outcome opt-in rather than automatic.
    enable_crawler: bool = False
    #: Gates `vd tick --action <file>` (tick.py): lets an operator/agent supply their own
    #: `Action` instead of `plan.py`'s own choice, for the case where their reasoning about
    #: the best next move genuinely diverges from the planner's and that divergence is
    #: blocking real strategy progress -- not a general substitute for planner judgement.
    #: Default `False` means the flag is refused outright, never silently ignored. Every
    #: other rung of `_run_tick` (guard evaluation, tier gates, `require_confirmation`,
    #: the lockfile, audit logging) still runs exactly as it does for a planner-chosen
    #: action; this only substitutes which `Action` is evaluated. Lives under `strategy`
    #: rather than at the top level because it's a strategic-override lever, the same
    #: family as `ship_targets`/`research_priority`/etc, not a wallet-engine or top-level
    #: account setting. See `references/manual-action-override.md`.
    allow_agent_action_override: bool = False
    #: Explicit opt-in for Colonize target-selection (commit 4 of the launch-actions
    #: plan). Default `False` reproduces the pre-existing behaviour exactly: no rung ever
    #: proposes a Colonize action -- `generate_colonize_candidates` returns `[]`
    #: unconditionally when this is `false`, the same "empty/off == old behaviour"
    #: convention every prior `strategy` flag uses.
    colonize: bool = False
    #: The planet flyable ships should be permanently repositioned to via Deploy (commit
    #: 4). `None` (default) reproduces pre-existing behaviour exactly:
    #: `generate_deploy_candidates` returns `[]` unconditionally. Also requires
    #: `policy.actions.allow_fleet_noncombat`, the same gate every other non-combat fleet
    #: generator already requires -- this field alone does not enable fleet logistics.
    fleet_home_planet_id: int | None = None
    #: Explicit opt-in for cross-tick planet-fairness rotation. Three ladder rungs walk
    #: `policy.planets` in list order and return on the first planet with anything
    #: selectable, never scoring across planets the way storage overflow and the
    #: research rung already do: "6:building-queue-empty" (`plan._next_building_action`),
    #: "8b:unlock-chain" (`candidates.select_unlock_chain_candidate`), and
    #: "8:shipyard-idle" (`candidates.select_shipyard_candidate`). As long as an
    #: earlier-listed planet has any pending work on one of those three rungs, a
    #: later-listed planet (a freshly settled colony, most concretely) never reaches
    #: them at all -- confirmed live managing a two-planet account. Default `False`
    #: reproduces pre-existing behaviour exactly: `tick.py` only ever threads a real
    #: `AgentState.last_attended_planet_id` into `plan_next_action` when this is `true`;
    #: `plan_next_action` itself doesn't know this flag exists, it just rotates whenever
    #: given a non-`None` pointer, same "empty/off == old behaviour" convention every
    #: other `strategy` flag uses. When on, `tick.py` advances the pointer only when the
    #: winning action for one of the three rungs above actually sent or was handed to a
    #: human for confirmation (`executed` or `confirm_hint` -- see `_finish_tick`) --
    #: never on a bare proposal, so a tier-1/dry-run account's repeated identical
    #: recommendation still dedups exactly as it always has (see
    #: `last_proposal_fingerprint`'s docstring, `state.py`). Storage overflow and the
    #: research rung are deliberately never rotated -- storage already picks the single
    #: most urgent overflow across every target planet regardless of order, and
    #: `generate_research_candidates` reads `target_planets[0]` specifically as the
    #: planet `startResearch` is actually submitted through, which rotating would
    #: silently change. Bands 5-8f (fleet logistics, Colonize, Attack, Missile) are also
    #: left un-rotated: each is opt-in, single-fire and high-stakes, where planet
    #: fairness is a much smaller concern than for routine building/ship/defense work.
    planet_rotation: bool = False


class Policy(Base):
    model_config = ConfigDict(extra="forbid")  # unknown keys are a hard error, never ignored

    version: Literal[1] = 1
    tier: Tier = Tier.ADVISOR
    wallet: str
    #: Empty list means: discover via /wallet/{addr}/planets.
    planets: list[int] = Field(default_factory=list)
    chain_id: int = 8453
    cadence: Cadence = Field(default_factory=Cadence)
    limits: Limits
    reserves: Resources = Field(default_factory=Resources)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    actions: ActionsCfg = Field(default_factory=ActionsCfg)
    escalation: EscalationCfg = Field(default_factory=EscalationCfg)
    wallet_engine: WalletEngineCfg = Field(default_factory=WalletEngineCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    radar: RadarCfg = Field(default_factory=RadarCfg)


# --------------------------------------------------------------------------------------
# Planner output
# --------------------------------------------------------------------------------------


class AlternativeNote(Base):
    """One candidate the strategy pipeline (`candidates.py`, docs/SPEC.md §5.4 Phase 2)
    considered but did not select, alongside the `Action` it lost to. Purely informational
    — never a `Decision` input, never re-derived by `guard.py`. `why_not` is free text:
    an ROI comparison ("payback 47h vs 31h") for two scored candidates, or a
    `techtree.describe()` string ("locked: needs Shipyard 2 (have 0)") for a locked one."""

    family: str
    entity_name: str | None = None
    score: float | None = None
    why_not: str = ""


class Action(Base):
    """Zero or one of these per tick. `function` is None for noop/escalate/halt."""

    kind: ActionKind
    #: Deployed contract function name, e.g. "startBuildingUpgrade". See §4 of the addendum.
    function: str | None = None
    #: For `launchFleetMission`/`launchDefenseHold`, the origin planet (the fleet departs
    #: from here). For `openDefenseIntent`, the defended planet (`defenderPlanetId`) --
    #: the ACS-coordination feature's one deliberate exception to "origin" being this
    #: field's usual meaning, since `openDefenseIntent` has no fleet/origin at all.
    planet_id: int | None = None
    entity_id: int | None = None
    entity_name: str | None = None
    target_level: int | None = None
    quantity: int | None = None
    #: Three distinct meanings by `function`, documented here rather than split across
    #: three near-duplicate fields (same "reuse across kinds, document per call site"
    #: convention `quantity`/`target_player` already use): `resolveFleetMission`/
    #: `recallFleetMission`'s own single `missionId` argument; for `launchFleetMission`
    #: with `mission_type` AcsDefend(5)/Intercept(6), the *hostile* mission being
    #: defended against/intercepted (see `mission_type`'s docstring for the encoding-time
    #: repurposing this implies); and for `openDefenseIntent`, its own `hostileMissionId`
    #: argument (a real, non-repurposed named parameter there — no encoding trick needed).
    mission_id: int | None = None
    cost: Resources = Field(default_factory=Resources)
    #: Which rung of the decision ladder fired, e.g. "5:storage-overflow". Makes the
    #: log auditable without re-running the planner.
    rule: str = ""
    rationale: str = ""
    expected_effect: str = ""
    #: Runner-up candidates from the same generate/filter/score/select pass that produced
    #: this Action, ranked, capped at `policy.strategy.max_alternatives`. Informational
    #: only (docs/SPEC.md §5.4 Phase 2) — never an ROI verdict, never consulted by
    #: `guard.py` or any `Decision` logic.
    alternatives: list[AlternativeNote] = Field(default_factory=list)
    #: Where this `Action` came from -- `"planner"` (the default, `plan.py`'s own choice)
    #: or `"manual_override"` (`vd tick --action <file>`, tick.py). `tick.py`'s CLI path
    #: forcibly overwrites this after validating a supplied file, so a stray `"source"`
    #: key inside a hand-written override JSON can never spoof it as planner-chosen.
    #: Purely a provenance tag for `proposals.jsonl`/`actions.jsonl` auditability -- never
    #: consulted by `guard.py` or any `Decision` logic.
    source: Literal["planner", "manual_override"] = "planner"

    # ----------------------------------------------------------------------------------
    # Fleet-mission fields (Phase 5c). All `None`/empty for every other `ActionKind` —
    # only `FLEET_MISSION` populates them.
    # ----------------------------------------------------------------------------------
    #: `ids.FleetMissionType`. Non-combat by default: `guard.py`'s mission-type gate and
    #: `allowlist.ts`'s OPERATOR_ALLOWED_MISSION_TYPES each refuse every combat type
    #: independently EXCEPT Attack (3), permitted only when `policy.actions.allow_combat`
    #: is true (launch-actions plan commit 5) — see `ActionsCfg.allow_combat`'s docstring
    #: — and EXCEPT AcsDefend (5)/Intercept (6), permitted only when `policy.actions.
    #: allow_acs_defense` is true, `operator` tier, manual-override only (ACS coordination
    #: feature; see `ActionsCfg.allow_acs_defense`'s docstring and `references/
    #: coordination.md`). For these two, `mission_id` (below) carries the referenced
    #: *hostile* mission's id — `target_planet_id`/`target_coordinates` stay `None`,
    #: unused. `tick.py`'s encoder places `mission_id`'s value into the deployed
    #: contract's `targetPlanetId` calldata slot for these two mission types specifically
    #: (the contract re-derives the real target planet internally) — a deliberate
    #: encoding-time-only repurposing, kept out of this clean model on purpose; see
    #: `mission_id`'s own docstring and `AGENTS.md` §7's third silent-corruption trap.
    #: MissileAttack (7)/AcsAttack (8)/
    #: DefenseHold (9, its own dedicated entrypoint and `ActionKind` — see `DEFENSE_HOLD`)
    #: stay refused/inapplicable here regardless of policy. Deliberately typed as a plain
    #: int, not the enum — the enum is complete and auditable, and narrowing the type here
    #: would imply an enforcement this field does not provide. Both gates default-deny.
    mission_type: int | None = None
    #: The planet the fleet departs from. Distinct from `planet_id`, which for a fleet
    #: mission names the *subject* planet of the action for logging/idempotency purposes.
    origin_planet_id: int | None = None
    #: Destination as "G:S:P", the same shape `PlanetSnapshot.coordinates` carries. For a
    #: mission against one of the wallet's own planets, `tick.py`'s encoder resolves this
    #: to the real on-chain `targetPlanetId` by matching it against `Snapshot.planets` —
    #: the only planets the frozen `Snapshot` model carries. Still set (for
    #: `guard._derive_fleet_mission_spend`'s distance re-derivation and for display) on a
    #: foreign target too; see `target_planet_id` below for what resolves the numeric id
    #: in that case.
    target_coordinates: str | None = None
    #: The real on-chain planet id for a foreign target — a planet outside
    #: `Snapshot.planets` that `target_coordinates` alone cannot resolve (added commit 3
    #: of the launch-actions plan, for foreign Harvest; commit 6 reuses it identically for
    #: Attack, whose target is by definition always a foreign planet). `None` for every
    #: mission against an owned planet, where `tick.py`'s coordinate lookup already works.
    #: When set, `tick._resolve_target_planet_id` uses it directly and skips the snapshot
    #: lookup — the generator that set it (`candidates.generate_foreign_harvest_
    #: candidates` / `generate_attack_candidates`) already knows the real id from its own
    #: data source and has no reason to make `tick.py` re-derive it from coordinates it
    #: would have to search for outside the snapshot anyway. Also what
    #: `tick._attack_protection_allowed` reads directly (falling back to
    #: `_resolve_target_planet_id` only if unset) to know which target to re-check
    #: `/wallet/{addr}/attack-protection` against. Also `launchDefenseHold`'s real, named
    #: `targetPlanetId` argument (own or a fellow alliance member's planet) — a genuine,
    #: non-repurposed use, unlike AcsDefend/Intercept's `mission_id` (see that field's
    #: docstring) which leaves this field `None`.
    target_planet_id: int | None = None
    #: Ship id -> count. **Not a fleet tuple.** The deployed contract takes a 14-slot
    #: tuple that omits the two non-flyable ships (SolarSatellite id 9, Crawler id 15), so
    #: tuple indices 9–13 map to Ship ids 10–14 — Destroyer sits at index 9, not 10
    #: (AGENTS.md §7 trap 1). Conversion happens at the encoder boundary and must agree
    #: with `veydrift-wallet`'s `shipCountsToFleetTuple`; never index a tuple with a raw
    #: Ship id.
    ships: dict[int, int] = Field(default_factory=dict)
    #: Resources loaded onto the fleet. Bounded by `calc.available_cargo` (capacity minus
    #: fuel), never by capacity alone.
    cargo: Resources = Field(default_factory=Resources)
    #: Percentage of full speed, contract-side `uint16`. `None` means "not specified by
    #: the planner" — never silently substitute a default at the encoder.
    speed_pct: int | None = None
    #: The trailing `uint256` both `launchFleetMission` overloads share. It is
    #: `randomnessRequestId` in the deployed source, **not** a holding duration — an
    #: earlier draft of this field guessed the latter and was wrong. The contract sets it
    #: itself for `Attack` (`_requestAttackBattleRandomness`, `guard.py`'s `attack_
    #: protection`/`mission_type` gates are what actually govern whether an Attack may be
    #: submitted at all, not this field) and AcsDefend/Intercept (the contract overwrites
    #: it with the referenced hostile mission's id -- `randomnessRequestId =
    #: hostileMissionId` -- for its own resolution bookkeeping; this codebase's own
    #: `Action.mission_id` already carries that same id cleanly, so nothing here needs to
    #: set this field for those two either); for every mission type this codebase can
    #: produce it is either ignored by the contract (Transport/Deploy/Harvest/Attack/
    #: AcsDefend/Intercept) or **required to be exactly 0** — Colonize reverts with
    #: `InvalidId` on anything else (`VeydriftColonizationModule._launchColonizeFleetMission`).
    #: So it is encoded as-is, defaulting to 0, and is expected to stay unset for every
    #: mission type this codebase generates or manually constructs — `generate_attack_
    #: candidates` never sets it, the same posture `generate_colonize_candidates` already
    #: takes. Naming it after the guessed meaning would invite someone to set a duration
    #: here and hit a silent Colonize revert.
    randomness_request_id: int | None = None

    # ----------------------------------------------------------------------------------
    # Missile field (commit 7 of the launch-actions plan). `None`/unused for every other
    # `ActionKind` — only `MISSILE_ATTACK` populates it. `quantity` (already declared
    # above, shared with SHIP/DEFENSE) doubles as the missile count for this kind --
    # `launchInterplanetaryMissileAttack`'s own `quantity` argument; `origin_planet_id`/
    # `target_planet_id`/`target_coordinates` (already declared above, shared with
    # FLEET_MISSION) are reused identically for a missile's origin/target.
    # ----------------------------------------------------------------------------------
    #: `ids.Defense` id of the defense type this missile batch targets --
    #: `launchInterplanetaryMissileAttack`'s `primaryTarget` argument. Must be
    #: `<= ids.Defense.LARGE_SHIELD_DOME` (7) -- the contract reverts `InvalidMissileTarget`
    #: on `AntiBallisticMissile` (8) or `InterplanetaryMissile` (9) itself, since neither
    #: is a valid missile target. `guard._gate_missile_target` independently re-checks
    #: this bound rather than trusting the generator that set it.
    primary_target: int | None = None

    # ----------------------------------------------------------------------------------
    # DefenseHold field (ACS coordination feature). `None`/unused for every other
    # `ActionKind` — only `DEFENSE_HOLD` populates it. `planet_id`/`target_planet_id`/
    # `target_coordinates`/`ships`/`cargo`/`speed_pct` (already declared above, shared
    # with FLEET_MISSION) are reused identically for `launchDefenseHold`'s
    # origin/target/ships/cargo/speedPercent arguments.
    # ----------------------------------------------------------------------------------
    #: `launchDefenseHold`'s trailing `holdSeconds` argument -- no existing field fits;
    #: `randomness_request_id` is a different trailing `uint256` on a different function
    #: and its own docstring already forecloses reuse as a duration (guessed that way once
    #: before, wrongly). Contract bounds: `MIN_DEFENSE_HOLD_SECONDS` (1 hour) to
    #: `MAX_DEFENSE_HOLD_SECONDS` (32 hours) inclusive -- `guard._gate_defense_hold_target`
    #: independently re-checks this bound rather than trusting the caller.
    hold_seconds: int | None = None

    # ----------------------------------------------------------------------------------
    # Alliance fields (membership-only, VeydriftAllianceSystem). `None`/empty for every
    # other `ActionKind` — only `ALLIANCE` populates them.
    # ----------------------------------------------------------------------------------
    #: Shared by every one of the 15 functions except `leaveAlliance` (no on-chain args at
    #: all -- `alliance_id` is informational only there, resolved by `tick.py` from live
    #: `AllianceState` for logging/idempotency) and `createAlliance` (returns, does not
    #: take, an allianceId -- also informational, unknowable before send).
    alliance_id: int | None = None
    #: The one address argument beyond `alliance_id` most of these functions take --
    #: `inviteMember`/`cancelInvite`/`dismissJoinRequest`/`approveJoinRequest`/
    #: `kickMember`/`setMemberRole`'s target, and `transferAllianceOwnership`'s `newOwner`.
    #: Reused across those meanings rather than one field per function, the same "reuse
    #: across kinds, document per call site" convention `quantity` already takes for
    #: missile count vs. ship/defense count.
    target_player: str | None = None
    #: `kickMembers`/`setMembersRole` only -- the batch address array those two functions
    #: take in place of `target_player`.
    target_players: list[str] = Field(default_factory=list)
    #: `alliance_ids.AllianceRole` value for `setMemberRole`/`setMembersRole`. Deliberately
    #: a plain int, not the enum -- the same "narrowing here would imply an enforcement
    #: this field does not provide" convention `mission_type` above already documents.
    role: int | None = None
    #: `createAlliance`/`updateAllianceProfile` only.
    alliance_tag: str | None = None
    alliance_name: str | None = None
    alliance_description: str | None = None

    def is_onchain(self) -> bool:
        return self.function is not None


# --------------------------------------------------------------------------------------
# Guard output
# --------------------------------------------------------------------------------------


class GuardVerdict(Base):
    gate: str
    status: GuardStatus
    detail: str = ""


class GuardReport(Base):
    """Every gate is evaluated and reported — never short-circuited. The full verdict
    list is the audit artifact, so a passing tick is as informative as a blocked one."""

    decision: Decision
    verdicts: list[GuardVerdict] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for v in self.verdicts if v.status is GuardStatus.PASS)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    def blocking(self) -> list[GuardVerdict]:
        return [
            v
            for v in self.verdicts
            if v.status in (GuardStatus.BLOCK, GuardStatus.ESCALATE)
        ]


# --------------------------------------------------------------------------------------
# Unsigned transaction — handed to walletctl, which independently re-validates it.
# --------------------------------------------------------------------------------------


class UnsignedTx(Base):
    to: str
    data: str
    value: int = 0
    chain_id: int = 8453
    gas: int | None = None
