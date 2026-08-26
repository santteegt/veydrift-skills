"""Deterministic Veydrift game calculators. No network calls except `verify`.

Every function's docstring cites its source: a contract `file:line` at the deployed
commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(`/Users/santteegt/GitRepositories/clones/veydrift`), `docs/RESEARCH-ADDENDUM.md`, or a
dated live probe. Where a contract function exists, it wins over `docs.md` prose — this
module was written by reading `packages/contracts/src/libraries/VeydriftFormulas.sol`,
`VeydriftAntiRaidPrimitives.sol` and `VeydriftGameplayModule.sol` directly, not by
transcribing the addendum's summary of them.

**Hard constraint — no cost-scaling function lives here.** Live cost at the current
building level is served by the API in the `cost` object; per-building factors
(`buildingCostFactor` in `VeydriftCatalog.sol:34-45`) are unpublished rationals, and
recomputing `base * factor ** level` is exactly how an affordability check goes silently
wrong. Every function below that takes a `metal_cost`/`crystal_cost` argument expects the
caller to have read it live, not derived it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import httpx
import typer
from rich.console import Console
from rich.table import Table

from veydrift_agent import ids
from veydrift_agent.models import EnergyBalance, Resources

app = typer.Typer(
    no_args_is_help=True,
    help="Deterministic game calculators (no network) plus a live-API duration check.",
)

# --------------------------------------------------------------------------------------
# Integer arithmetic primitives matching Solidity's *toward-zero* int division, which
# disagrees with Python's floor `//` whenever the numerator is negative and not an exact
# multiple of the denominator. Only `solar_satellite_energy` needs this (its numerator
# can go negative on very cold planets); every other formula below operates on
# non-negative uint256-equivalents, where trunc and floor agree and plain `//` is exact.
# --------------------------------------------------------------------------------------


def _trunc_div(numerator: int, denominator: int) -> int:
    quotient = numerator // denominator
    if (numerator % denominator != 0) and ((numerator < 0) != (denominator < 0)):
        quotient += 1
    return quotient


# --------------------------------------------------------------------------------------
# Level-scaling primitives. `scaled_level` is the single most-reused formula in the
# contract: OGame's classic `base * level * 1.1^level` growth curve, used for mine
# production, energy required/produced and (with a ceiling instead of a floor) fusion
# deuterium upkeep.
# --------------------------------------------------------------------------------------


def scale_by_factor(value: int, exponent: int, numerator: int, denominator: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:174-181 (`scaleByFactor`).

    ``floor(value * numerator**exponent / denominator**exponent)``.
    """
    return (value * numerator**exponent) // denominator**exponent


def scaled_level(base: int, level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:221-223 (`_scaledLevelValue`,
    which is ``_scaledLevelValueWithFactor(base, level, 11, 10)`` at :231-238).

    ``floor(base * level * 11**level / 10**level)`` — 0 at level 0. This is the classic
    OGame ×1.1-per-level curve and underlies mine production, energy demand/supply, and
    (via :func:`fusion_energy`) Fusion Reactor output.
    """
    if level <= 0:
        return 0
    return scale_by_factor(base * level, level, 11, 10)


def _scaled_level_value_ceil(base: int, level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:225-229
    (`_scaledLevelValueCeil`) — same curve as :func:`scaled_level`, rounded up instead of
    down. Used only for Fusion Reactor's deuterium upkeep."""
    if level <= 0:
        return 0
    denominator = 10**level
    return (base * level * 11**level + denominator - 1) // denominator


def _scale_by_bps(value: int, bps: int, base_bps: int = 10_000) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:213-219 (`_scaleByBps`)."""
    return (value * bps) // base_bps


# --------------------------------------------------------------------------------------
# Planet traits: temperature drives the deuterium multiplier and the Solar Satellite
# energy yield. These two effects pull in opposite directions on the same input, which is
# the entire strategic character of a planet (docs/NOTES.md §12.5, §12.7).
# --------------------------------------------------------------------------------------


def deuterium_multiplier_bps(max_temperature: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:25-35 (`planetMultipliers`).

    ``max(0, 12_800 - max_temperature * 20)``. Metal and crystal multipliers are *always*
    10_000 regardless of temperature (:32-33 of the same function) — only deuterium
    varies with temperature; there is no metal/crystal equivalent of this function.

    **Not called from the live path, unlike its sibling :func:`solar_satellite_energy`
    (which the live path falls back to this function's temperature-only equivalent for).**
    `production_per_hour`'s `deuterium_multiplier_bps_` argument is always populated from
    the API's own live `PlanetSnapshot.deuterium_multiplier_bps` field
    (`candidates.py`'s `_planet_production_context`) — never recomputed from temperature
    via this function, so there is no fallback path that reaches it either. Kept for
    fixtures, cross-checking against the live field, and as the documented source of the
    inverse :func:`max_temp_from_bps` uses for that same cross-check.
    """
    return max(0, 12_800 - max_temperature * 20)


def max_temp_from_bps(deuterium_bps: int) -> int:
    """Inverse of :func:`deuterium_multiplier_bps`, for cross-checking a reported
    multiplier against a reported `temperature` field — the method
    docs/NOTES.md §12.5 used to confirm the API's `temperature` really is the formulas'
    "planet maximum temperature". **Diagnostic only**: the API returns `temperature`
    directly, so prefer that; this is not exact once the live multiplier is clamped to 0
    (hot planets, `max_temperature >= 640`).
    """
    return (12_800 - deuterium_bps) // 20


def solar_satellite_energy(max_temperature: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:143-149
    (`solarSatelliteEnergy`).

    ``clamp(trunc((max_temperature + 140) / 6), 1, 65)``. Confirmed against the live API
    for planet 664: ``energyBalance.sources.solarSatelliteEnergy: "4"`` at
    ``temperature: -111`` (docs/RESEARCH-ADDENDUM.md §5, live probe 2026-08-12) — this
    function reproduces that value exactly.

    **Prefer the live value.** `plan.py` should read
    `PlanetSnapshot.energy.solar_satellite_energy` (sourced from
    `energyBalance.sources.solarSatelliteEnergy`) rather than call this — the contract's
    own value is already served to you (docs/SPEC.md §5.4). This function exists for
    fixtures, the crossover table, and cross-checking, not as the primary source in the
    live path.
    """
    raw = _trunc_div(max_temperature + 140, 6)
    return max(1, min(65, raw))


# --------------------------------------------------------------------------------------
# Energy and production.
# --------------------------------------------------------------------------------------


def fusion_energy(level: int, energy_technology_level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:131-137
    (`fusionReactorEnergyProduction`).

    ``floor(30 * level * (105 + energy_technology_level)**level / 100**level)``.
    """
    if level <= 0:
        return 0
    return scale_by_factor(30 * level, level, 105 + energy_technology_level, 100)


def fusion_deuterium_upkeep(level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:139-141
    (`fusionReactorDeuteriumConsumption`), via `_scaledLevelValueCeil` at :225-229.

    ``ceil(10 * level * 11**level / 10**level)``.
    """
    return _scaled_level_value_ceil(10, level)


def crawler_boost_bps(crawler_count: int, metal_level: int, crystal_level: int, deuterium_level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:93-105
    (`crawlerProductionBoostBps`).

    0.02% per effective crawler (`CRAWLER_BOOST_BPS_PER_UNIT`), capped at 8 crawlers per
    combined mine level (`CRAWLER_MAX_PER_MINE_LEVEL`) and 5_000 bps total
    (`CRAWLER_MAX_BOOST_BPS` = 50%).
    """
    if crawler_count <= 0:
        return 0
    effective_cap = (metal_level + crystal_level + deuterium_level) * 8
    effective = min(crawler_count, effective_cap)
    boost = effective * 2
    return min(boost, 5_000)


def energy_balance(
    metal_level: int,
    crystal_level: int,
    deuterium_level: int,
    solar_level: int,
    fusion_level: int,
    energy_technology_level: int,
    solar_satellite_count: int,
    solar_satellite_energy_per_unit: int,
) -> EnergyBalance:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:107-129 (`energyBalance`).

    Takes ``solar_satellite_energy_per_unit`` as an explicit input rather than a
    temperature, so callers pass the API's live
    ``energyBalance.sources.solarSatelliteEnergy`` (docs/SPEC.md §5.4) instead of
    recomputing it. Use :func:`solar_satellite_energy` to derive it only when no live
    value is available (fixtures, or a hypothetical planet).

    Returns the same three quantities the contract does: energy produced, energy
    required, and ``scale_bps`` — 10_000 when ``produced >= required`` (the contract's own
    ``>=``, not ``>``), else ``floor(produced * 10_000 / required)``. This is the number
    that silently throttles every mine on the planet when energy runs short.
    """
    required = (
        scaled_level(10, metal_level) + scaled_level(10, crystal_level) + scaled_level(20, deuterium_level)
    )
    produced = (
        scaled_level(20, solar_level)
        + fusion_energy(fusion_level, energy_technology_level)
        + solar_satellite_energy_per_unit * solar_satellite_count
    )
    scale_bps = 10_000 if required == 0 or produced >= required else (produced * 10_000) // required
    return EnergyBalance(
        produced=produced,
        required=required,
        scale_bps=scale_bps,
        solar_satellite_energy=solar_satellite_energy_per_unit,
    )


def production_per_hour(
    metal_level: int,
    crystal_level: int,
    deuterium_level: int,
    solar_level: int,
    fusion_level: int,
    solar_satellite_count: int,
    crawler_count: int,
    energy_technology_level: int,
    metal_multiplier_bps: int,
    crystal_multiplier_bps: int,
    deuterium_multiplier_bps_: int,
    solar_satellite_energy_per_unit: int,
) -> Resources:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:37-91 (`productionPerHour`).

    Applies, in the contract's own order: base mine output x planet multiplier, then the
    crawler boost, then Fusion Reactor's deuterium upkeep, then the energy shortage
    factor (``energy_balance(...).scale_bps``) — energy throttles *after* crawlers and
    upkeep, exactly as the contract does it, so this must not be reordered.
    """
    energy = energy_balance(
        metal_level,
        crystal_level,
        deuterium_level,
        solar_level,
        fusion_level,
        energy_technology_level,
        solar_satellite_count,
        solar_satellite_energy_per_unit,
    )
    metal = _scale_by_bps(scaled_level(30, metal_level), metal_multiplier_bps)
    crystal = _scale_by_bps(scaled_level(20, crystal_level), crystal_multiplier_bps)
    deuterium = _scale_by_bps(scaled_level(10, deuterium_level), deuterium_multiplier_bps_)

    boost = crawler_boost_bps(crawler_count, metal_level, crystal_level, deuterium_level)
    if boost:
        metal = _scale_by_bps(metal, 10_000 + boost)
        crystal = _scale_by_bps(crystal, 10_000 + boost)
        deuterium = _scale_by_bps(deuterium, 10_000 + boost)

    upkeep = fusion_deuterium_upkeep(fusion_level)
    deuterium = deuterium - upkeep if deuterium > upkeep else 0

    if energy.required != 0:
        metal = _scale_by_bps(metal, energy.scale_bps)
        crystal = _scale_by_bps(crystal, energy.scale_bps)
        deuterium = _scale_by_bps(deuterium, energy.scale_bps)

    return Resources(metal=metal, crystal=crystal, deuterium=deuterium)


def solar_crossover_table(max_mine_level: int = 10) -> list[tuple[int, int, int]]:
    """The smallest Solar Plant level whose energy alone covers metal+crystal+deuterium
    mines all sitting at the same level `L` (no fusion, no satellites).

    Reproduces docs/NOTES.md §12.5's table exactly (mine 3 -> solar 5, mine 5 -> solar 8,
    mine 7 -> solar 11, mine 10 -> solar 14) from
    packages/contracts/src/libraries/VeydriftFormulas.sol:110-111 (required energy) and
    :113 (`_scaledLevelValue(20, solarLevel)`, produced). The gap between mine level and
    solar level *widens* as level increases — this is why the energy-first invariant in
    `plan.py` recomputes required-vs-produced at every step instead of using a fixed
    offset (docs/SPEC.md §5.4).

    Returns ``(mine_level, required_energy, min_solar_level)`` tuples.
    """
    rows: list[tuple[int, int, int]] = []
    for level in range(1, max_mine_level + 1):
        required = scaled_level(10, level) * 2 + scaled_level(20, level)
        solar = 1
        while scaled_level(20, solar) < required:
            solar += 1
        rows.append((level, required, solar))
    return rows


# --------------------------------------------------------------------------------------
# Durations. All three share a `universe_speed` term; docs/NOTES.md §12.4 verified
# `universe_speed == 1` three independent ways by isolating a different divisor in each.
# `vd calc verify` re-runs that check against the live API.
# --------------------------------------------------------------------------------------


def build_seconds(
    robotics_level: int,
    nanite_level: int,
    metal_cost: int,
    crystal_cost: int,
    universe_speed: int = 1,
    min_queue_seconds: int = 1,
) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:160-172 (`buildingDuration`).

    ``floor((metal_cost + crystal_cost) * 3600 / (2500 * (robotics_level+1) *
    2**nanite_level * universe_speed))``, floored at `min_queue_seconds` (contract default
    1, `MIN_QUEUE_SECONDS` in `VeydriftGameStorage.sol`).
    """
    if universe_speed <= 0:
        raise ValueError("universe_speed must be > 0")
    denominator = 2500 * (robotics_level + 1) * (2**nanite_level) * universe_speed
    raw = ((metal_cost + crystal_cost) * 3600) // denominator
    return max(raw, min_queue_seconds)


def ship_seconds(
    shipyard_level: int,
    nanite_level: int,
    metal_cost: int,
    crystal_cost: int,
    quantity: int = 1,
    universe_speed: int = 1,
    min_queue_seconds: int = 1,
) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:182-197 (`unitDuration`).

    Same shape as :func:`build_seconds` but *ceiling* divided (a partial second still
    costs a full second of queue time) and scaled by `quantity`.
    """
    if universe_speed <= 0:
        raise ValueError("universe_speed must be > 0")
    denominator = 2500 * (shipyard_level + 1) * (2**nanite_level) * universe_speed
    numerator = (metal_cost + crystal_cost) * quantity * 3600
    raw = (numerator + denominator - 1) // denominator
    return max(raw, min_queue_seconds)


def research_seconds(
    lab_level: int,
    metal_cost: int,
    crystal_cost: int,
    universe_speed: int = 1,
    min_queue_seconds: int = 1,
) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:199-211 (`researchDuration`).

    ``floor((metal_cost + crystal_cost) * 3600 / (1000 * (lab_level+1) *
    universe_speed))``.
    """
    if universe_speed <= 0:
        raise ValueError("universe_speed must be > 0")
    denominator = 1000 * (lab_level + 1) * universe_speed
    raw = ((metal_cost + crystal_cost) * 3600) // denominator
    return max(raw, min_queue_seconds)


# --------------------------------------------------------------------------------------
# Storage.
# --------------------------------------------------------------------------------------

#: packages/contracts/src/libraries/VeydriftFormulas.sol:241-295 (`_storageCap`).
#: Index == storage building level (0-50, `MAX_LEVEL` in VeydriftGameStorage.sol).
#: A literal lookup table on the contract side, not a formula — transcribed verbatim.
_STORAGE_CAP_BY_LEVEL: tuple[int, ...] = (
    10_000,
    20_000,
    40_000,
    75_000,
    140_000,
    255_000,
    470_000,
    865_000,
    1_590_000,
    2_920_000,
    5_355_000,
    9_820_000,
    18_005_000,
    33_005_000,
    60_510_000,
    110_925_000,
    203_350_000,
    372_785_000,
    683_385_000,
    1_252_785_000,
    2_296_600_000,
    4_210_115_000,
    7_717_970_000,
    14_148_545_000,
    25_937_050_000,
    47_547_690_000,
    87_164_210_000,
    159_789_040_000,
    292_924_545_000,
    536_987_950_000,
    984_403_885_000,
    1_804_604_750_000,
    3_308_193_270_000,
    6_064_564_940_000,
    11_117_533_015_000,
    20_380_611_235_000,
    37_361_644_330_000,
    68_491_197_375_000,
    125_557_753_210_000,
    230_171_905_210_000,
    421_950_095_435_000,
    773_517_006_225_000,
    1_418_007_876_745_000,
    2_599_485_625_175_000,
    4_765_365_289_085_000,
    8_735_846_091_420_000,
    16_014_513_537_450_000,
    29_357_733_773_850_000,
    53_818_464_752_040_000,
    98_659_766_131_065_000,
    180_862_636_975_685_000,
)


def storage_cap(level: int) -> int:
    """packages/contracts/src/libraries/VeydriftFormulas.sol:241-295 (`_storageCap`).

    A literal per-level table (0-50), identical for Metal/Crystal Storage and the
    Deuterium Tank — the contract calls the same private function for all three
    (`storageCaps` at :150-158). Raises `ValueError` above level 50 (`MAX_LEVEL`),
    mirroring the contract's `LevelTooHigh` revert.
    """
    if level < 0 or level >= len(_STORAGE_CAP_BY_LEVEL):
        raise ValueError(f"level {level} out of range 0..{len(_STORAGE_CAP_BY_LEVEL) - 1}")
    return _STORAGE_CAP_BY_LEVEL[level]


def hours_to_cap(current: int, per_hour: int, cap: int) -> float | None:
    """Hours until `current` reaches `cap` at a constant `per_hour` production rate.

    Plain arithmetic, not a contract formula — feeds `vd read`'s ``--summary`` digest
    (docs/SPEC.md §5.2: "hours-to-cap per resource") and `plan.py`'s storage-overflow
    rung. Returns `None` if the cap will never be reached at this rate (`per_hour <= 0`).
    """
    if per_hour <= 0:
        return None
    remaining = cap - current
    if remaining <= 0:
        return 0.0
    return remaining / per_hour


def hours_to_afford(current: int, per_hour: int, cost: int, cap: int) -> float | None:
    """Hours until `current` (growing at `per_hour`) reaches `cost` -- the
    affordability-ETA counterpart to `hours_to_cap` above (same arithmetic; `cap` there
    is `cost` here, just a different kind of target: a spend requirement, not a storage
    ceiling). Kept as a distinctly-named wrapper rather than called directly as
    `hours_to_cap(current, per_hour, cost)` because that reads misleadingly at call
    sites -- there is no "cap" involved in "when can I afford this".

    Returns `None` when it will never happen via production alone:
    - `per_hour <= 0` (delegates to `hours_to_cap`'s own convention), or
    - `cost > cap`: storage overflow discards anything above the cap, so passive
      accumulation can never reach a cost that exceeds it. `hours_to_cap` itself can't
      hit this case (the cap IS its target there), so it has no such guard -- this
      wrapper adds it explicitly before delegating.
    """
    if cost > cap:
        return None
    return hours_to_cap(current, per_hour, cost)


# --------------------------------------------------------------------------------------
# Distance, travel, fuel, cargo.
# --------------------------------------------------------------------------------------

Coordinates = tuple[int, int, int] | str


def _parse_coordinates(coordinates: Coordinates) -> tuple[int, int, int]:
    if isinstance(coordinates, str):
        galaxy, system, position = coordinates.split(":")
        return int(galaxy), int(system), int(position)
    galaxy, system, position = coordinates
    return int(galaxy), int(system), int(position)


def distance(a: Coordinates, b: Coordinates) -> int:
    """packages/contracts/src/VeydriftGameplayModule.sol:814-829 (`_planetDistance` /
    `_absoluteDifference`).

    Accepts either a ``(galaxy, system, position)`` tuple or a ``"G:S:P"`` string (the
    `PlanetSnapshot.coordinates` format, e.g. ``"7:181:14"``). Confirmed identical to the
    published `docs.md` formula (live fetch 2026-08-12): different galaxy =
    ``20_000 * |galaxy diff|``; same galaxy, different system =
    ``2_700 + 95 * |system diff|``; same system, different position =
    ``1_000 + 5 * |position diff|``; same planet = ``0``. Local Harvest missions use a
    fixed distance of 5 instead (`LOCAL_HARVEST_DISTANCE`,
    `VeydriftGameStorage.sol:52`) — not reproduced here since it is not a function of two
    coordinates.
    """
    a_galaxy, a_system, a_position = _parse_coordinates(a)
    b_galaxy, b_system, b_position = _parse_coordinates(b)

    galaxy_diff = abs(a_galaxy - b_galaxy)
    if galaxy_diff:
        return galaxy_diff * 20_000
    system_diff = abs(a_system - b_system)
    if system_diff:
        return 2_700 + system_diff * 95
    position_diff = abs(a_position - b_position)
    if position_diff:
        return 1_000 + position_diff * 5
    return 0


def travel_seconds(
    distance_: int,
    slowest_ship_speed: int,
    speed_percent: int = 100,
    universe_speed: int = 1,
) -> int:
    """packages/contracts/src/libraries/VeydriftAntiRaidPrimitives.sol:55-68
    (`travelSeconds`, the 4-argument overload).

    ``10 + floor(isqrt(distance*10*122_500 / slowest_ship_speed) * 100 / (speed_percent *
    universe_speed))``. Computed as one integer square root of the full product, not as
    ``350 * isqrt(distance*10/slowest_ship_speed)`` — `sqrt(122_500) == 350`, but
    factoring it out of the square root can round differently for some inputs, so this
    mirrors the contract's own order of operations. Uses `math.isqrt`, Python's exact
    floor integer square root, matching the contract's own bit-shift Babylonian-method
    `_sqrt` (:240-278 of the same file) for every non-negative input.
    """
    if slowest_ship_speed == 0:
        return 0
    if not (10 <= speed_percent <= 100 and speed_percent % 10 == 0):
        raise ValueError("speed_percent must be a multiple of 10 in [10, 100]")
    speed_factor = universe_speed if universe_speed else 1
    variable_seconds = math.isqrt((distance_ * 10 * 122_500) // slowest_ship_speed)
    return 10 + (variable_seconds * 100) // (speed_percent * speed_factor)


_FUEL_SPEED_SCALE = 10**9
_FULL_MISSION_SPEED_PERCENT = 100


def _ogame_fuel_numerator(
    ship_fuel_consumption: int,
    quantity: int,
    distance_: int,
    ship_speed: int,
    slowest_ship_speed: int,
    speed_percent: int,
) -> int:
    """packages/contracts/src/libraries/VeydriftAntiRaidPrimitives.sol:93-116
    (`ogameFuelNumerator`)."""
    if 0 in (ship_fuel_consumption, quantity, distance_, ship_speed, slowest_ship_speed):
        return 0
    speed_ratio_scaled = math.isqrt(
        (slowest_ship_speed * _FUEL_SPEED_SCALE * _FUEL_SPEED_SCALE) // ship_speed
    )
    effective_speed_scaled = speed_percent * speed_ratio_scaled
    speed_multiplier_scaled = _FULL_MISSION_SPEED_PERCENT * _FUEL_SPEED_SCALE + effective_speed_scaled
    return ship_fuel_consumption * quantity * distance_ * speed_multiplier_scaled * speed_multiplier_scaled


def mission_fuel(
    ships: Iterable[tuple[int, int, int]],
    distance_: int,
    slowest_ship_speed: int,
    speed_percent: int = 100,
) -> int:
    """packages/contracts/src/libraries/VeydriftAntiRaidPrimitives.sol:93-131
    (`ogameFuelNumerator` / `ogameFuelDenominator` / `ogameFuelCostFromNumerator`),
    aggregated across ship types exactly as
    packages/contracts/src/libraries/VeydriftFleetFuel.sol:9-34 (`ogameMissionFuelCost`)
    does: sum each flyable ship type's numerator, then convert once.

    ``ships`` is an iterable of ``(fuel_consumption, quantity, speed)`` per flyable ship
    type in the mission, with `fuel_consumption` and `speed` already resolved for the
    player's current drive-tech levels (`VeydriftCatalog.shipMovementStats`) — this
    function has no notion of tech levels itself, only the resulting per-ship numbers.
    Ships with `quantity <= 0` are ignored. Returns 0 if no ship in the mission consumes
    fuel (e.g. an all-zero fleet).
    """
    numerator = 0
    has_fuel = False
    for fuel_consumption, quantity, speed in ships:
        if quantity <= 0 or fuel_consumption <= 0:
            continue
        has_fuel = True
        numerator += _ogame_fuel_numerator(
            fuel_consumption, quantity, distance_, speed, slowest_ship_speed, speed_percent
        )
    if not has_fuel:
        return 0
    denominator = (
        35_000
        * _FULL_MISSION_SPEED_PERCENT
        * _FULL_MISSION_SPEED_PERCENT
        * _FUEL_SPEED_SCALE
        * _FUEL_SPEED_SCALE
    )
    return 1 + (numerator + denominator // 2) // denominator


def available_cargo(total_cargo_capacity: int, fuel_cost: int) -> int:
    """docs/RESEARCH-ADDENDUM.md §5: "available cargo = total ship cargo - mission fuel".
    Fuel is deuterium and ships to the same cargo hold, so it is subtracted directly, not
    converted. Clamped at 0 — a mission that cannot afford its own fuel has no cargo room,
    not negative room.
    """
    return max(0, total_cargo_capacity - fuel_cost)


def max_planets(astrophysics_level: int) -> int:
    """packages/contracts/src/VeydriftGame.sol:596-597 (`maxPlanets`).

    ``1 + astrophysics_level``. docs/NOTES.md §13.5 only said Astrophysics "raises colony
    capacity"; this is the exact formula, read from the facade contract. Confirmed live
    (docs/COVERAGE.md's `max_planets` row): `VeydriftColonizationModule.sol:289-301`'s
    `PlanetLimitReached` reverted at exactly `limit = 10` for an account with Astrophysics
    9 and 10 owned planets, matching this formula exactly.

    Used by `guard.py`'s `_gate_mission_type` (the `mission_type` gate's Colonize branch)
    to BLOCK a Colonize `launchFleetMission` before send when
    `Snapshot.owned_planet_count` is already at or above this cap — a pre-flight check for
    a revert `tick.py`'s wallet-engine boundary would otherwise only discover after
    spending gas. There is still no candidate generator that *proposes* a Colonize action
    (docs/COVERAGE.md's own note); this is guard-layer defense for whatever proposes one,
    manually or otherwise, not the "where to colonise" planner itself.
    """
    return 1 + astrophysics_level


# --------------------------------------------------------------------------------------
# Ship movement stats (Phase 5c, docs/SPEC.md §5.4) — fixed lookup tables straight from
# `packages/contracts/src/libraries/VeydriftCatalog.sol` (pinned commit 701bed35). This is
# NOT the banned "cost-scaling function" category above: that ban is specifically about
# per-building/tech/ship/defense *cost* factors, which really are unpublished rationals
# (`buildingCostFactor`, `VeydriftCatalog.sol:34-45`) that must be read live, never
# recomputed. Cargo capacity, fuel consumption and speed are a different kind of number —
# a small, fully-published, `pure` lookup table with no live/per-account state at all
# (`shipCargoCapacity`/`_shipFuelConsumption`/`_shipSpeed`/`_driveSpeed`,
# `VeydriftCatalog.sol:146-227,497-503`) — reading it once from source is exactly what
# this module already does for every other formula it carries (see module docstring).
# No live API route ever reports these (`/shipyard` gives only `cost`/`durationSeconds`/
# `count`, references/api-routes.md §3.9), so there is no "prefer the live value" option
# the way `Entity.cost` has.
# --------------------------------------------------------------------------------------

#: Ship id -> cargo capacity, level/tech-independent (`shipCargoCapacity`,
#: `VeydriftCatalog.sol:146-163`). Non-flyable ships (SolarSatellite, Crawler) are `0`,
#: matching the contract, but they must still never appear in a fleet tuple
#: (`ids.NON_FLYABLE_SHIPS`, AGENTS.md §7 trap 1) — a `0` capacity here is not permission
#: to fly them.
SHIP_CARGO_CAPACITY: dict[int, int] = {
    ids.Ship.SMALL_CARGO: 5_000,
    ids.Ship.LIGHT_FIGHTER: 50,
    ids.Ship.RECYCLER: 20_000,
    ids.Ship.COLONY_SHIP: 7_500,
    ids.Ship.LARGE_CARGO: 25_000,
    ids.Ship.HEAVY_FIGHTER: 100,
    ids.Ship.CRUISER: 800,
    ids.Ship.BATTLESHIP: 1_500,
    ids.Ship.BOMBER: 500,
    ids.Ship.SOLAR_SATELLITE: 0,
    ids.Ship.DESTROYER: 2_000,
    ids.Ship.DEATHSTAR: 1_000_000,
    ids.Ship.BATTLECRUISER: 750,
    ids.Ship.REAPER: 7_000,
    ids.Ship.PATHFINDER: 12_000,
    ids.Ship.CRAWLER: 0,
}

_SHIP_FUEL_CONSUMPTION_FLAT: dict[int, int] = {
    ids.Ship.LIGHT_FIGHTER: 20,
    ids.Ship.RECYCLER: 300,
    ids.Ship.COLONY_SHIP: 1_000,
    ids.Ship.LARGE_CARGO: 50,
    ids.Ship.HEAVY_FIGHTER: 75,
    ids.Ship.CRUISER: 300,
    ids.Ship.BATTLESHIP: 500,
    ids.Ship.BOMBER: 1_000,
    ids.Ship.DESTROYER: 1_000,
    ids.Ship.DEATHSTAR: 1,
    ids.Ship.BATTLECRUISER: 250,
    ids.Ship.REAPER: 1_000,
    ids.Ship.PATHFINDER: 300,
}


def ship_fuel_consumption(ship_id: int, impulse_drive_level: int) -> int:
    """`VeydriftCatalog.sol:176-193` (`_shipFuelConsumption`). Only Small Cargo's
    consumption depends on drive tech (Impulse Drive >= 5 switches it from 10 to 20 — the
    contract's own note is that this reflects the faster Impulse-Drive route, not a
    scaling formula). Raises `ValueError` for a non-flyable ship id (SolarSatellite,
    Crawler) or an unknown id, matching the contract's `revert InvalidId()` — never
    silently returns 0, which would look like "free fuel" rather than "cannot fly"."""
    if ship_id == ids.Ship.SMALL_CARGO:
        return 20 if impulse_drive_level >= 5 else 10
    if ship_id in _SHIP_FUEL_CONSUMPTION_FLAT:
        return _SHIP_FUEL_CONSUMPTION_FLAT[ship_id]
    raise ValueError(f"ship id {ship_id} cannot fly (no fuel consumption defined) or is unknown")


def _drive_speed(base_speed: int, drive_level: int, percent_per_level: int) -> int:
    """`VeydriftCatalog.sol:497-503` (`_driveSpeed`): ``base * (100 + level *
    percent_per_level) / 100``, integer division matching Solidity's toward-zero `/` for
    these always-non-negative inputs."""
    return (base_speed * (100 + drive_level * percent_per_level)) // 100


def ship_speed(
    ship_id: int,
    combustion_drive_level: int,
    impulse_drive_level: int,
    hyperspace_drive_level: int,
) -> int:
    """`VeydriftCatalog.sol:194-227` (`_shipSpeed`). Each flyable ship's base speed scales
    with exactly one drive technology (Small Cargo and Bomber each have a tech-level
    threshold that switches which drive applies — reproduced exactly, not approximated).
    Raises `ValueError` for a non-flyable or unknown ship id, matching the contract's
    `revert InvalidId()`."""
    if ship_id == ids.Ship.SMALL_CARGO:
        if impulse_drive_level >= 5:
            return _drive_speed(10_000, impulse_drive_level, 20)
        return _drive_speed(5_000, combustion_drive_level, 10)
    if ship_id == ids.Ship.LIGHT_FIGHTER:
        return _drive_speed(12_500, combustion_drive_level, 10)
    if ship_id == ids.Ship.RECYCLER:
        return _drive_speed(2_000, combustion_drive_level, 10)
    if ship_id == ids.Ship.COLONY_SHIP:
        return _drive_speed(2_500, impulse_drive_level, 20)
    if ship_id == ids.Ship.LARGE_CARGO:
        return _drive_speed(7_500, combustion_drive_level, 10)
    if ship_id == ids.Ship.HEAVY_FIGHTER:
        return _drive_speed(10_000, impulse_drive_level, 20)
    if ship_id == ids.Ship.CRUISER:
        return _drive_speed(15_000, impulse_drive_level, 20)
    if ship_id == ids.Ship.BATTLESHIP:
        return _drive_speed(10_000, hyperspace_drive_level, 30)
    if ship_id == ids.Ship.BOMBER:
        if hyperspace_drive_level >= 8:
            return _drive_speed(5_000, hyperspace_drive_level, 30)
        return _drive_speed(4_000, impulse_drive_level, 20)
    if ship_id == ids.Ship.DESTROYER:
        return _drive_speed(5_000, hyperspace_drive_level, 30)
    if ship_id == ids.Ship.DEATHSTAR:
        return _drive_speed(100, hyperspace_drive_level, 30)
    if ship_id == ids.Ship.BATTLECRUISER:
        return _drive_speed(10_000, hyperspace_drive_level, 30)
    if ship_id == ids.Ship.REAPER:
        return _drive_speed(7_000, hyperspace_drive_level, 30)
    if ship_id == ids.Ship.PATHFINDER:
        return _drive_speed(12_000, hyperspace_drive_level, 30)
    raise ValueError(f"ship id {ship_id} cannot fly (no speed formula defined) or is unknown")


def ship_movement_stats(
    ship_id: int,
    combustion_drive_level: int,
    impulse_drive_level: int,
    hyperspace_drive_level: int,
) -> tuple[int, int, int]:
    """``(cargo_capacity, fuel_consumption, speed)`` — `VeydriftCatalog.sol:166-172`
    (`shipMovementStats`), the single entry point `candidates.py`'s logistics generators
    use rather than calling the three lookups above separately."""
    return (
        SHIP_CARGO_CAPACITY[ship_id],
        ship_fuel_consumption(ship_id, impulse_drive_level),
        ship_speed(ship_id, combustion_drive_level, impulse_drive_level, hyperspace_drive_level),
    )


# --------------------------------------------------------------------------------------
# `vd calc verify` — the one command in this module that touches the network. Re-runs
# docs/NOTES.md §12.4's three duration checks (research / ship / building divisors) live,
# using each entity's *live* cost and level from the API — never a recomputed cost.
# --------------------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.veydrift.com"
#: docs/RESEARCH-ADDENDUM.md's example account, also `assets/policy.example.json`'s
#: default wallet (docs/SPEC.md §5.6). Overridable via --wallet / --planet-id.
DEFAULT_WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
DEFAULT_PLANET_ID = 664


def _find_by_id(items: list[dict], id_: int) -> dict:
    return next(item for item in items if item["id"] == id_)


def _live_cost(entity: dict) -> tuple[int, int]:
    return int(entity["cost"]["metal"]), int(entity["cost"]["crystal"])


@app.command()
def verify(
    wallet: str = typer.Option(DEFAULT_WALLET, help="Wallet address to probe."),
    planet_id: int = typer.Option(DEFAULT_PLANET_ID, help="Planet to probe."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, help="API base URL."),
) -> None:
    """Re-run the three duration checks from docs/NOTES.md §12.4 against the live API.

    Each check recomputes a duration formula (:func:`research_seconds`,
    :func:`ship_seconds`, :func:`build_seconds`) from the API's *live* level and cost for
    one entity — Energy Technology, Small Cargo, Metal Mine — and compares it to the
    API's own reported `durationSeconds`. All three formulas share a `universe_speed`
    term but isolate a different divisor (research lab / shipyard / robotics), which is
    why agreement across all three is much stronger evidence than any one alone
    (docs/NOTES.md §12.4). If they still agree with `universe_speed=1`, universe speed has
    not drifted; if not, exits non-zero rather than guessing which changed.
    """
    console = Console()
    try:
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            research = client.get(
                f"/wallet/{wallet}/research", params={"planetId": planet_id}
            ).raise_for_status().json()
            shipyard = client.get(
                f"/wallet/{wallet}/shipyard", params={"planetId": planet_id}
            ).raise_for_status().json()
            infra = client.get(
                f"/wallet/{wallet}/infrastructure", params={"planetId": planet_id}
            ).raise_for_status().json()
    except httpx.HTTPError as exc:
        console.print(f"[red]network error contacting {base_url}: {exc}[/red]")
        raise typer.Exit(code=3) from exc

    checks: list[tuple[str, int, int]] = []

    lab_level = research["researchLabLevel"]
    energy_tech = _find_by_id(research["technologies"], ids.Technology.ENERGY)
    metal, crystal = _live_cost(energy_tech)
    checks.append((
        "Energy Technology duration",
        research_seconds(lab_level, metal, crystal),
        energy_tech["durationSeconds"],
    ))

    shipyard_level = shipyard["shipyardLevel"]
    shipyard_nanite_level = shipyard.get("naniteLevel", 0)
    small_cargo = _find_by_id(shipyard["ships"], ids.Ship.SMALL_CARGO)
    metal, crystal = _live_cost(small_cargo)
    checks.append((
        "Small Cargo duration",
        ship_seconds(shipyard_level, shipyard_nanite_level, metal, crystal, quantity=1),
        small_cargo["durationSeconds"],
    ))

    robotics_level = _find_by_id(infra["buildings"], ids.Building.ROBOTICS_FACTORY)["level"]
    infra_nanite_level = _find_by_id(infra["buildings"], ids.Building.NANITE_FACTORY)["level"]
    metal_mine = _find_by_id(infra["buildings"], ids.Building.METAL_MINE)
    metal, crystal = _live_cost(metal_mine)
    checks.append((
        "Metal Mine duration",
        build_seconds(robotics_level, infra_nanite_level, metal, crystal),
        metal_mine["durationSeconds"],
    ))

    table = Table(title=f"vd calc verify -- {base_url} wallet={wallet} planet={planet_id}")
    table.add_column("check")
    table.add_column("computed", justify="right")
    table.add_column("live", justify="right")
    table.add_column("status")

    drift = False
    for name, computed, live in checks:
        ok = computed == live
        drift = drift or not ok
        table.add_row(name, str(computed), str(live), "[green]match[/green]" if ok else "[red]DRIFT[/red]")
    console.print(table)

    if drift:
        console.print(
            "[red]Duration drift detected -- universe speed may no longer be 1, "
            "or a duration formula changed.[/red]"
        )
        raise typer.Exit(code=1)
    console.print("[green]universe speed confirmed == 1 (all three duration formulas agree)[/green]")


@app.command()
def crossover(max_mine_level: int = typer.Option(10, help="Highest mine level to include.")) -> None:
    """Print the Solar Plant energy-crossover table (docs/NOTES.md §12.5), generated live
    from :func:`solar_crossover_table` — this is what populates the table in
    `references/formulas.md`, not a hand-typed copy.
    """
    console = Console()
    table = Table(title="Solar Plant energy crossover (metal = crystal = deuterium mine level L)")
    table.add_column("mine level L", justify="right")
    table.add_column("required energy", justify="right")
    table.add_column("min solar level", justify="right")
    for level, required, solar in solar_crossover_table(max_mine_level):
        table.add_row(str(level), str(required), str(solar))
    console.print(table)


if __name__ == "__main__":
    app()
