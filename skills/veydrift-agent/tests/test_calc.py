"""Tests for veydrift_agent.calc — pure functions checked against contract source
(packages/contracts/src/libraries/VeydriftFormulas.sol, VeydriftAntiRaidPrimitives.sol,
VeydriftGameplayModule.sol at commit 701bed3578cff4d134657c714c599dbdb55a4b6a) and, where
available, the live API (docs/NOTES.md §12.4, docs/RESEARCH-ADDENDUM.md §5).
"""

from __future__ import annotations

import math

import pytest

from veydrift_agent import calc

# --------------------------------------------------------------------------------------
# scaled_level — the core ×1.1-per-level curve.
# --------------------------------------------------------------------------------------


def test_scaled_level_zero_at_level_zero():
    assert calc.scaled_level(10, 0) == 0
    assert calc.scaled_level(999, 0) == 0


def test_scaled_level_matches_notes_energy_tech_example():
    # docs/NOTES.md §12.4: Energy Tech duration at Lab 0 isolates this exact curve via
    # research_seconds; here we check the underlying value directly.
    assert calc.scaled_level(10, 1) == 11  # floor(10*1*11/10)


def test_scaled_level_matches_hand_computed_values():
    # floor(20*5*11^5/10^5) = floor(1_610_510_00 / 100_000) = 161
    assert calc.scaled_level(20, 5) == 161
    # floor(10*11*11^11/10^11) = 313
    assert calc.scaled_level(10, 11) == 313


# --------------------------------------------------------------------------------------
# Planet traits.
# --------------------------------------------------------------------------------------


def test_deuterium_multiplier_bps_planet_664():
    # docs/RESEARCH-ADDENDUM.md §5 / live probe: temperature -111 -> deuteriumMultiplierBps 15020.
    assert calc.deuterium_multiplier_bps(-111) == 15_020


def test_deuterium_multiplier_bps_clamped_at_zero():
    # 12_800 - 640*20 == 0; anything hotter would go negative and must clamp to 0.
    assert calc.deuterium_multiplier_bps(640) == 0
    assert calc.deuterium_multiplier_bps(1000) == 0


def test_max_temp_from_bps_round_trips_664():
    assert calc.max_temp_from_bps(15_020) == -111


def test_solar_satellite_energy_planet_664_confirmed_live():
    # docs/RESEARCH-ADDENDUM.md §5: energyBalance.sources.solarSatelliteEnergy: "4" for
    # planet 664 (temperature -111), read live 2026-08-12.
    assert calc.solar_satellite_energy(-111) == 4


def test_solar_satellite_energy_clamped_bounds():
    assert calc.solar_satellite_energy(-140) == 1  # raw <= 0 -> clamp to 1
    assert calc.solar_satellite_energy(-139) == 1  # raw == 0 -> clamp to 1
    assert calc.solar_satellite_energy(1000) == 65  # raw huge -> clamp to 65


def test_solar_satellite_energy_uses_truncating_not_floor_division():
    # maxTemperature < -140 makes the numerator negative. Solidity's int256 division
    # truncates toward zero; Python's `//` floors toward -inf and would disagree here
    # if used directly. Both must still clamp to the same result (1), which is the
    # regression this test guards: a naive `//` implementation would silently also
    # produce 1 in this specific case, so we check a value where trunc and floor
    # disagree in principle (-10 // 6) but the *clamp* makes the final answer identical
    # either way -- i.e. this asserts the clamp, not the division mode directly.
    assert calc.solar_satellite_energy(-150) == 1
    assert calc._trunc_div(-10, 6) == -1  # trunc toward zero
    assert (-10) // 6 == -2  # Python floor division disagrees


# --------------------------------------------------------------------------------------
# Energy.
# --------------------------------------------------------------------------------------


def test_energy_balance_all_zero_is_full_scale():
    result = calc.energy_balance(0, 0, 0, 0, 0, 0, 0, 0)
    assert result.produced == 0
    assert result.required == 0
    assert result.scale_bps == 10_000  # required == 0 -> full scale, contract's own rule


def test_energy_balance_shortage_scales_down():
    # metal mine level 1 alone requires 11 energy; solar at 0 produces 0.
    result = calc.energy_balance(1, 0, 0, 0, 0, 0, 0, 0)
    assert result.required == 11
    assert result.produced == 0
    assert result.scale_bps == 0


def test_energy_balance_produced_equal_to_required_is_full_scale():
    # Contract uses `producedEnergy >= requiredEnergy`, not strictly greater.
    # Solar level 1 produces 22; metal+crystal mine level 1 each requires 11+11=22.
    result = calc.energy_balance(1, 1, 0, 1, 0, 0, 0, 0)
    assert result.produced == 22
    assert result.required == 22
    assert result.scale_bps == 10_000


def test_energy_balance_solar_satellite_energy_passthrough():
    result = calc.energy_balance(0, 0, 0, 0, 0, 0, 3, 4)
    assert result.produced == 12  # 3 satellites * 4 energy/unit
    assert result.solar_satellite_energy == 4


def test_fusion_energy_zero_at_level_zero():
    assert calc.fusion_energy(0, 0) == 0


def test_fusion_energy_matches_formula():
    # floor(30*1*(105+0)^1/100^1) = floor(30*105/100) = 31
    assert calc.fusion_energy(1, 0) == 31


def test_fusion_deuterium_upkeep_uses_ceiling():
    # ceil(10*1*11/10) = ceil(11) = 11 (exact, no rounding difference at level 1)
    assert calc.fusion_deuterium_upkeep(1) == 11
    # level 3: 10*3*11^3/10^3 = 30*1331/1000 = 39.93 -> ceil 40
    assert calc.fusion_deuterium_upkeep(3) == 40
    assert calc.scaled_level(10, 3) == 39  # the floor sibling rounds down instead


def test_crawler_boost_bps_capped_at_50_percent():
    # effective cap = (metal+crystal+deut levels) * 8 = 600*8 = 4_800; boost = 4_800*2 =
    # 9_600 bps, which the contract clamps to the 5_000 bps ceiling (CRAWLER_MAX_BOOST_BPS).
    assert calc.crawler_boost_bps(100_000, 200, 200, 200) == 5_000


def test_crawler_boost_bps_scales_linearly_below_cap():
    # effective cap = (1+1+1)*8 = 24; 10 crawlers is below the per-mine-level cap.
    assert calc.crawler_boost_bps(10, 1, 1, 1) == 20  # 10 * 2 bps/crawler


def test_crawler_boost_bps_zero_crawlers():
    assert calc.crawler_boost_bps(0, 10, 10, 10) == 0


def test_production_per_hour_zero_state_is_zero():
    result = calc.production_per_hour(0, 0, 0, 0, 0, 0, 0, 0, 10_000, 10_000, 10_000, 0)
    assert result.metal == 0
    assert result.crystal == 0
    assert result.deuterium == 0


def test_production_per_hour_deuterium_multiplier_applies():
    # deuterium synth level 1 at 664's live multiplier (15_020 bps), energy-unconstrained.
    result = calc.production_per_hour(0, 0, 1, 100, 0, 0, 0, 0, 10_000, 10_000, 15_020, 0)
    base = calc.scaled_level(10, 1)  # 11
    assert result.deuterium == (base * 15_020) // 10_000


# --------------------------------------------------------------------------------------
# Solar crossover table — reproduces docs/NOTES.md §12.5 exactly, generated by code.
# --------------------------------------------------------------------------------------


def test_solar_crossover_table_matches_notes_12_5():
    rows = {level: solar for level, _required, solar in calc.solar_crossover_table(10)}
    assert rows[3] == 5
    assert rows[5] == 8
    assert rows[7] == 11
    assert rows[10] == 14


def test_solar_crossover_gap_widens_not_fixed_offset():
    rows = {level: solar for level, _required, solar in calc.solar_crossover_table(10)}
    gap_at_3 = rows[3] - 3
    gap_at_10 = rows[10] - 10
    assert gap_at_3 == 2
    assert gap_at_10 == 4
    assert gap_at_10 > gap_at_3  # the whole reason a fixed offset rule is wrong


# --------------------------------------------------------------------------------------
# Durations — the three docs/NOTES.md §12.4 checks, at level 0, universe_speed=1.
# --------------------------------------------------------------------------------------


def test_research_seconds_energy_tech_at_lab_zero():
    assert calc.research_seconds(0, 0, 800) == 2880


def test_ship_seconds_small_cargo_at_shipyard_zero():
    assert calc.ship_seconds(0, 0, 2000, 2000, quantity=1) == 5760


def test_build_seconds_metal_mine_at_robotics_zero():
    assert calc.build_seconds(0, 0, 60, 15) == 108


def test_ship_seconds_ceiling_rounds_up():
    # unitDuration ceiling-divides (VeydriftFormulas.sol:182-197), unlike
    # buildingDuration/researchDuration which floor. metal=1, crystal=1, shipyard level 1:
    # numerator = 2*3600 = 7200, denominator = 2500*2 = 5000, 7200/5000 = 1.44 -> ceil 2.
    assert calc.ship_seconds(1, 0, 1, 1, quantity=1) == 2
    # The floor sibling of the same division would give 1, confirming this is a real
    # rounding-mode difference, not a coincidence of the chosen numbers.
    assert (2 * 3600) // 5000 == 1


def test_durations_reject_zero_universe_speed():
    with pytest.raises(ValueError):
        calc.build_seconds(0, 0, 60, 15, universe_speed=0)
    with pytest.raises(ValueError):
        calc.research_seconds(0, 0, 800, universe_speed=0)
    with pytest.raises(ValueError):
        calc.ship_seconds(0, 0, 2000, 2000, universe_speed=0)


def test_durations_never_below_min_queue_seconds():
    # An enormous divisor would floor the raw duration to 0; the contract clamps it up.
    assert calc.build_seconds(1_000_000, 0, 1, 1) == 1


# --------------------------------------------------------------------------------------
# Storage.
# --------------------------------------------------------------------------------------


def test_storage_cap_level_zero():
    assert calc.storage_cap(0) == 10_000


def test_storage_cap_level_fifty():
    assert calc.storage_cap(50) == 180_862_636_975_685_000


def test_storage_cap_out_of_range_raises():
    with pytest.raises(ValueError):
        calc.storage_cap(51)
    with pytest.raises(ValueError):
        calc.storage_cap(-1)


def test_hours_to_cap_basic():
    assert calc.hours_to_cap(9_000, 500, 10_000) == 2.0


def test_hours_to_cap_already_at_cap():
    assert calc.hours_to_cap(10_000, 500, 10_000) == 0.0


def test_hours_to_cap_never_reached():
    assert calc.hours_to_cap(0, 0, 10_000) is None


def test_hours_to_afford_basic():
    assert calc.hours_to_afford(current=200, per_hour=100, cost=500, cap=10_000) == 3.0


def test_hours_to_afford_zero_rate_never():
    assert calc.hours_to_afford(current=0, per_hour=0, cost=500, cap=10_000) is None


def test_hours_to_afford_cost_exceeds_cap_never():
    assert calc.hours_to_afford(current=0, per_hour=100, cost=20_000, cap=10_000) is None


# --------------------------------------------------------------------------------------
# Distance, travel, fuel, cargo.
# --------------------------------------------------------------------------------------


def test_distance_same_planet_is_zero():
    assert calc.distance("7:181:14", "7:181:14") == 0


def test_distance_same_system():
    assert calc.distance("7:181:14", "7:181:8") == 1_000 + 5 * 6


def test_distance_same_galaxy_different_system():
    assert calc.distance((7, 181, 14), (7, 180, 14)) == 2_700 + 95 * 1


def test_distance_different_galaxy():
    assert calc.distance((7, 181, 14), (1, 181, 14)) == 20_000 * 6


def test_distance_accepts_string_and_tuple_interchangeably():
    assert calc.distance("7:181:14", (7, 180, 14)) == calc.distance((7, 181, 14), (7, 180, 14))


def test_travel_seconds_zero_speed_is_zero():
    assert calc.travel_seconds(1000, 0) == 0


def test_travel_seconds_matches_isqrt_formula():
    distance_ = 1000
    speed = 5000
    expected = 10 + (math.isqrt((distance_ * 10 * 122_500) // speed) * 100) // (100 * 1)
    assert calc.travel_seconds(distance_, speed) == expected


def test_travel_seconds_rejects_invalid_speed_percent():
    with pytest.raises(ValueError):
        calc.travel_seconds(1000, 5000, speed_percent=55)
    with pytest.raises(ValueError):
        calc.travel_seconds(1000, 5000, speed_percent=5)


def test_mission_fuel_all_zero_quantity_is_zero():
    assert calc.mission_fuel([(10, 0, 5000)], distance_=1000, slowest_ship_speed=5000) == 0


def test_mission_fuel_positive_for_a_real_fleet():
    # Small Cargo: fuel=10, speed=5000 (combustion 0), one ship, short hop.
    fuel = calc.mission_fuel([(10, 1, 5000)], distance_=1000, slowest_ship_speed=5000)
    assert fuel >= 1  # contract's `1 +` floor ensures at least 1 if any ship consumes fuel


def test_mission_fuel_scales_with_quantity():
    one = calc.mission_fuel([(10, 1, 5000)], distance_=5000, slowest_ship_speed=5000)
    ten = calc.mission_fuel([(10, 10, 5000)], distance_=5000, slowest_ship_speed=5000)
    assert ten > one


def test_available_cargo_clamped_at_zero():
    assert calc.available_cargo(100, 500) == 0
    assert calc.available_cargo(500, 100) == 400


def test_max_planets_formula():
    assert calc.max_planets(0) == 1
    assert calc.max_planets(5) == 6
