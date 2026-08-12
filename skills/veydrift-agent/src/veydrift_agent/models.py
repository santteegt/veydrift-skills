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
    """What the planner decided. Only the first five map to a contract call."""

    BUILD = "build"                      # startBuildingUpgrade(uint256,uint8)
    RESEARCH = "research"                # startResearch(uint256,uint8)
    SHIP = "ship"                        # startShipProduction(uint256,uint8,uint32)
    DEFENSE = "defense"                  # startDefenseProduction(uint256,uint8,uint32)
    RESOLVE_MISSION = "resolve_mission"  # resolveFleetMission(uint256)
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


class Snapshot(Base):
    """Everything one tick needs. Produced by `vd read snapshot --json`."""

    taken_at: datetime
    wallet: str
    #: True only when /health reports ok AND readiness.ready. Replica nulls are not outages.
    health_ok: bool
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
    on_abi_hash_change: bool = True
    on_health_unhealthy_minutes: int = 30
    on_revert_count: int = 2


class WalletEngineCfg(Base):
    provider: Literal["keystore", "envkey"] = "keystore"
    require_confirmation: bool = True


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


# --------------------------------------------------------------------------------------
# Planner output
# --------------------------------------------------------------------------------------


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
