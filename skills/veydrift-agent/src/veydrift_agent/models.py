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
    """What the planner decided. Only the first six map to a contract call."""

    BUILD = "build"                      # startBuildingUpgrade(uint256,uint8)
    RESEARCH = "research"                # startResearch(uint256,uint8)
    SHIP = "ship"                        # startShipProduction(uint256,uint8,uint32)
    DEFENSE = "defense"                  # startDefenseProduction(uint256,uint8,uint32)
    RESOLVE_MISSION = "resolve_mission"  # resolveFleetMission(uint256)
    #: launchFleetMission — **overloaded on the deployed ABI** (a 7-arg and a 6-arg form).
    #: Always resolve it by full canonical signature, never by name (AGENTS.md §7 trap 2).
    #: Non-combat mission types only; combat types are refused independently by
    #: `guard.py`'s mission-type gate and `allowlist.ts`'s OPERATOR_ALLOWED_MISSION_TYPES.
    FLEET_MISSION = "fleet_mission"
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
    incoming_fleets: list[IncomingFleet] = Field(default_factory=list)

    def planet(self, planet_id: int) -> PlanetSnapshot | None:
        return next((p for p in self.planets if p.planet_id == planet_id), None)


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
    #: Deliberately ignored by every code path. Combat requires a code change, not config.
    allow_combat: bool = False


class EscalationCfg(Base):
    on_incoming_fleet: bool = True
    on_game_paused: bool = True
    on_abi_hash_change: bool = True
    on_health_unhealthy_minutes: int = 30
    on_revert_count: int = 2


class WalletEngineCfg(Base):
    provider: Literal["keystore", "envkey"] = "keystore"
    require_confirmation: bool = True


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
    planet_id: int | None = None
    entity_id: int | None = None
    entity_name: str | None = None
    target_level: int | None = None
    quantity: int | None = None
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

    # ----------------------------------------------------------------------------------
    # Fleet-mission fields (Phase 5c). All `None`/empty for every other `ActionKind` —
    # only `FLEET_MISSION` populates them.
    # ----------------------------------------------------------------------------------
    #: `ids.FleetMissionType`. Non-combat only in practice: `guard.py`'s mission-type gate
    #: and `allowlist.ts`'s OPERATOR_ALLOWED_MISSION_TYPES each refuse combat types
    #: independently. Deliberately typed as a plain int, not the enum — the enum is
    #: complete and auditable (it *lists* combat types), and narrowing the type here would
    #: imply an enforcement this field does not provide. Both gates default-deny.
    mission_type: int | None = None
    #: The planet the fleet departs from. Distinct from `planet_id`, which for a fleet
    #: mission names the *subject* planet of the action for logging/idempotency purposes.
    origin_planet_id: int | None = None
    #: Destination as "G:S:P", the same shape `PlanetSnapshot.coordinates` carries.
    target_coordinates: str | None = None
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
    #: itself for `Attack` and the two counterplay types (none reachable here); for every
    #: mission type this codebase can produce it is either ignored
    #: (Transport/Deploy/Harvest) or **required to be exactly 0** — Colonize reverts with
    #: `InvalidId` on anything else (`VeydriftColonizationModule._launchColonizeFleetMission`).
    #: So it is encoded as-is, defaulting to 0, and is expected to stay unset. Naming it
    #: after the guessed meaning would invite someone to set a duration here and hit a
    #: silent Colonize revert.
    randomness_request_id: int | None = None

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
