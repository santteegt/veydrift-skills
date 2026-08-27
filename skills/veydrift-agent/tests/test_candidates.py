"""Tests for veydrift_agent.candidates — the generate/filter/score/select pipeline that
replaced `plan.py`'s hardcoded rung 5-9 entity selection (Phase 2 of the
general-strategy-engine program, docs/SPEC.md §5.4).

`tests/test_plan.py` is the behaviour-preservation suite (every pre-Phase-2 assertion
still passes unmodified, driven through `plan_next_action`/`_next_building_action`).
This file instead exercises the new seam directly: generator/score/rank as isolated
units, per the WP2 brief's four required cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veydrift_agent import candidates, ids, techtree
from veydrift_agent.models import (
    ActionKind,
    ActionsCfg,
    CrawlerProduction,
    EnergyBalance,
    Entity,
    EntityTarget,
    Limits,
    PlanetSnapshot,
    Policy,
    QueueEntry,
    QueueKind,
    Resources,
    Snapshot,
    StorageCfg,
    StrategyCfg,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_snapshot(name: str) -> Snapshot:
    return Snapshot.model_validate(json.loads((FIXTURES / name).read_text()))


def make_policy(**overrides) -> Policy:
    base = {
        "wallet": "0x224aba5d489675a7bd3ce07786fada466b46fa0f",
        "planets": [],
        "limits": Limits(
            gas_per_tx_wei=3_000_000_000_000_000,
            gas_per_day_wei=20_000_000_000_000_000,
            eth_gas_floor_wei=2_000_000_000_000_000,
        ),
        "actions": ActionsCfg(allow_building=True, allow_research=True, allow_defense=False, allow_ships=False),
        "storage": StorageCfg(hours_to_cap_trigger=2.0),
    }
    base.update(overrides)
    return Policy(**base)


def _blocked_planet() -> PlanetSnapshot:
    """Every mine at level 0, zero energy produced -- upgrading *any* mine by one level
    needs strictly positive energy (`calc.energy_balance`: metal/crystal +1 need 11,
    deuterium +1 needs 22), so every mine is energy-unsafe here. Used to prove the
    energy-first hard filter."""
    return PlanetSnapshot(
        planet_id=1,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=0, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=60, crystal=15)),
            Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=0, cost=Resources(metal=48, crystal=24)),
            Entity(
                id=ids.Building.DEUTERIUM_SYNTHESIZER,
                name="Deuterium Synthesizer",
                level=0,
                cost=Resources(metal=225, crystal=75),
            ),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=0, cost=Resources(metal=75, crystal=30)),
        ],
        ships=[],
        defenses=[],
    )


def _dummy_candidate(name: str, *, family: str = "mine", score: float | None, entity_id: int = 0) -> candidates.Candidate:
    from veydrift_agent.models import Action, ActionKind

    return candidates.Candidate(
        action=Action(kind=ActionKind.BUILD, entity_id=entity_id, entity_name=name),
        family=family,
        score=score,
        score_basis=f"basis for {name}",
    )


# --------------------------------------------------------------------------------------
# 1. A scored candidate beats a worse-scored one.
# --------------------------------------------------------------------------------------


def test_rank_candidates_orders_scored_ascending_by_payback_hours():
    cheap = _dummy_candidate("cheap", score=5.0, entity_id=1)
    expensive = _dummy_candidate("expensive", score=47.0, entity_id=2)

    ranked = candidates.rank_candidates([expensive, cheap])

    assert [c.action.entity_name for c in ranked] == ["cheap", "expensive"]


# --------------------------------------------------------------------------------------
# 2. An unscored candidate never outranks a scored one within the economic band.
# --------------------------------------------------------------------------------------


def test_unscored_candidate_never_outranks_a_scored_one():
    unscored = _dummy_candidate("locked", score=None, entity_id=1)
    scored = _dummy_candidate("buildable", score=999.0, entity_id=2)  # even a bad payback

    ranked = candidates.rank_candidates([unscored, scored])

    assert [c.action.entity_name for c in ranked] == ["buildable", "locked"]


# --------------------------------------------------------------------------------------
# 3. The energy-first filter still prevents a mine candidate from being generated.
# --------------------------------------------------------------------------------------


def test_energy_unsafe_mine_is_never_generated_as_a_mine_candidate():
    planet = _blocked_planet()
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    mine_candidates = candidates.generate_mine_candidates(snapshot, policy, planet)

    # None of the three mines clears the post-upgrade energy check at 0 produced --
    # the whole family comes back empty, never "generated then filtered out downstream".
    assert mine_candidates == []


def test_energy_unsafe_mine_is_replaced_by_an_energy_candidate_at_selection():
    """The other half of the same invariant: `select_building_candidate` still proposes
    *something* -- the cheaper of Solar Plant / Solar Satellite -- even though no mine
    candidate exists to select from."""
    planet = _blocked_planet()
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family == "energy"
    assert winner.action.entity_id == ids.Building.SOLAR_PLANT  # no Shipyard -> satellite locked


def test_energy_safe_mine_is_generated_once_solar_plant_covers_it():
    """Sanity check on the fixture itself: raise Solar Plant so metal mine's post-upgrade
    energy requirement (11) is covered, and confirm the metal mine candidate reappears."""
    planet = _blocked_planet()
    planet = planet.model_copy(
        update={
            "buildings": [
                b if b.id != ids.Building.SOLAR_PLANT else b.model_copy(update={"level": 5})
                for b in planet.buildings
            ],
            "energy": EnergyBalance(produced=200, required=0, scale_bps=10_000, solar_satellite_energy=4),
        }
    )
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    mine_candidates = candidates.generate_mine_candidates(snapshot, policy, planet)

    assert ids.Building.METAL_MINE in {c.action.entity_id for c in mine_candidates}


# --------------------------------------------------------------------------------------
# 3b. _mine_priority_order's tie-break: an exact density tie is broken by ascending
# payback hours when supplied, and falls back to today's dict-declaration-order
# (Metal Mine first) when not -- Metal(level 2)/Crystal(level 1)/Deuterium(level 5) at
# 1x multipliers is an exact tie between Metal and Crystal (both score 1e-5); Deuterium
# is deliberately left off the tie (6e-5) so the fixture isolates a clean two-way tie.
# --------------------------------------------------------------------------------------


def _tied_mine_planet(
    *,
    energy_produced: int = 100_000,
    crystal_storage_cap: int = 100_000,
    metal_cost: Resources = Resources(metal=60, crystal=15),
    crystal_cost: Resources = Resources(metal=20, crystal=10),
) -> PlanetSnapshot:
    """Metal level 2, Crystal level 1, Deuterium level 5, all 1x multipliers -- an exact
    density tie between Metal and Crystal ((2+1)/30 == (1+1)/20 == 1e-5), Deuterium
    clearly behind (6e-5). Solar Plant level 10 keeps `calc.production_per_hour`'s
    internal energy check unthrottled so payback scores are real, nonzero numbers;
    `energy_produced` separately controls the energy-*safety* filter
    (`_mine_energy_safe`/`_produced_now`, which reads `planet.energy.produced` directly,
    not the Solar Plant level) -- the two are independent knobs on purpose, matching how
    the real code separates them."""
    return PlanetSnapshot(
        planet_id=1,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        storage_caps=Resources(metal=100_000, crystal=crystal_storage_cap, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=energy_produced, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=2, cost=metal_cost),
            Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=1, cost=crystal_cost),
            Entity(
                id=ids.Building.DEUTERIUM_SYNTHESIZER,
                name="Deuterium Synthesizer",
                level=5,
                cost=Resources(metal=225, crystal=75),
            ),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=10, cost=Resources(metal=75, crystal=30)),
            Entity(id=ids.Building.CRYSTAL_STORAGE, name="Crystal Storage", level=0, cost=Resources(metal=1_000, crystal=500)),
        ],
        ships=[],
        defenses=[],
    )


def test_mine_priority_order_default_tie_break_is_dict_declaration_order():
    """Pins today's default explicitly for the first time -- no existing test asserted
    this before. `tie_break=None` (the default, and every call site except
    `select_building_candidate`'s) must stay byte-identical: Metal Mine first on the
    tie, exactly as before this parameter existed."""
    planet = _tied_mine_planet()

    order = candidates._mine_priority_order(planet)

    assert order[0] == ids.Building.METAL_MINE
    assert order[1] == ids.Building.CRYSTAL_MINE


def test_mine_priority_order_tie_break_prefers_lower_payback():
    planet = _tied_mine_planet()

    order = candidates._mine_priority_order(
        planet, tie_break={ids.Building.METAL_MINE: 10.0, ids.Building.CRYSTAL_MINE: 5.0}
    )
    assert order[0] == ids.Building.CRYSTAL_MINE

    # Direction check, not just "something changed": swap which one has the lower number.
    order_reversed = candidates._mine_priority_order(
        planet, tie_break={ids.Building.METAL_MINE: 5.0, ids.Building.CRYSTAL_MINE: 10.0}
    )
    assert order_reversed[0] == ids.Building.METAL_MINE


def test_mine_priority_order_tie_break_with_no_scores_falls_back_to_dict_order():
    """An explicitly-supplied (non-`None`) map that simply doesn't cover either tied id
    -- e.g. both mines' `score_payback` returned `None` -- must degrade to the same
    dict-declaration-order fallback as `tie_break=None`, not crash or reorder
    arbitrarily. Distinct from the `tie_break=None` test above: this pins the
    `.get(id, inf)` fallback path specifically."""
    planet = _tied_mine_planet()

    order = candidates._mine_priority_order(planet, tie_break={})

    assert order[0] == ids.Building.METAL_MINE
    assert order[1] == ids.Building.CRYSTAL_MINE


def test_select_building_candidate_breaks_a_real_tie_by_computed_payback():
    """Integration-level, self-verifying: reads the *actual* computed payback scores
    (never hand-predicted) and asserts the winner is whichever tied mine really has the
    lower one -- proving the values flow generate_mine_candidates -> mine_tie_break ->
    _mine_priority_order -> select_building_candidate's winner, not just that
    _mine_priority_order's own sort key is correct in isolation (the tests above)."""
    planet = _tied_mine_planet()
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    mine_candidates = candidates.generate_mine_candidates(snapshot, policy, planet)
    metal_c = next(c for c in mine_candidates if c.action.entity_id == ids.Building.METAL_MINE)
    crystal_c = next(c for c in mine_candidates if c.action.entity_id == ids.Building.CRYSTAL_MINE)
    assert metal_c.score is not None and crystal_c.score is not None
    assert metal_c.score != crystal_c.score  # tie-break must have something real to resolve

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    expected_winner_id = min([metal_c, crystal_c], key=lambda c: c.score).action.entity_id
    assert winner is not None
    assert winner.family == "mine"
    assert winner.action.entity_id == expected_winner_id


def test_mine_tie_with_an_energy_blocked_twin_prefers_the_energy_safe_one_directly():
    """Family-flip case: before this fix, an energy-blocked mine that ties on primary
    density still won the priority walk by dict order, forcing an energy-substitute
    proposal even though the *other* tied mine was energy-safe and could have been
    proposed directly. An energy-unsafe mine is never in `mine_tie_break` (it was never
    generated as a candidate in the first place), so it now always sorts last on a tie
    -- the safe, tied mine wins directly as a mine, and no energy building is proposed."""
    planet = _tied_mine_planet(energy_produced=210)  # 211 required if Metal upgrades, 209 if Crystal does
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    safe, required_metal = candidates._mine_energy_safe(planet, snapshot, ids.Building.METAL_MINE, produced_now=210)
    assert safe is False and required_metal == 211
    safe, required_crystal = candidates._mine_energy_safe(planet, snapshot, ids.Building.CRYSTAL_MINE, produced_now=210)
    assert safe is True and required_crystal == 209

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family == "mine"
    assert winner.action.entity_id == ids.Building.CRYSTAL_MINE


def test_mine_tie_break_winner_still_defers_to_storage_precondition():
    """The tie-break changes *which* mine is the tentative winner, but must not bypass
    the storage-cap precondition (`_resolve_storage_precondition`) that already applies
    to whatever `_mine_priority_order` hands it. Crystal Mine wins the payback tie-break
    (see test_select_building_candidate_breaks_a_real_tie_by_computed_payback), but its
    cost (10 crystal) exceeds a deliberately tiny crystal storage cap here -- the winner
    must become the matching storage substitute, not Crystal Mine directly, and not
    Metal Mine (today's dict-order winner) either."""
    planet = _tied_mine_planet(crystal_storage_cap=5)  # Crystal Mine costs 10 crystal -- exceeds this
    snapshot = Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[1])

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family == "storage"
    assert winner.action.entity_id == ids.Building.CRYSTAL_STORAGE


# --------------------------------------------------------------------------------------
# 4. score_payback returns None for a storage building (never moves production_per_hour).
# --------------------------------------------------------------------------------------


def test_score_payback_returns_none_when_before_and_after_are_identical():
    cost = Resources(metal=1000, crystal=500, deuterium=0)
    weights = Resources(metal=1, crystal=1, deuterium=1)
    same = Resources(metal=100, crystal=50, deuterium=10)

    score, basis = candidates.score_payback(cost, weights, same, same)

    assert score is None
    assert "no production_per_hour change" in basis


def test_storage_candidates_are_always_unscored():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    near_cap = planet.model_copy(
        update={
            "resources_as_of_now": Resources(metal=9_900, crystal=1_000, deuterium=0),
            "production_per_hour": Resources(metal=500, crystal=0, deuterium=0),
            "storage_caps": Resources(metal=10_000, crystal=10_000, deuterium=10_000),
        }
    )
    policy = make_policy(planets=[664])

    storage_candidates = candidates.generate_storage_candidates(snapshot, policy, near_cap)

    assert storage_candidates  # metal is within the trigger window
    assert all(c.score is None for c in storage_candidates)
    assert all(c.family == "storage" for c in storage_candidates)


# --------------------------------------------------------------------------------------
# score_payback also positively scores a real marginal move (not just the None cases
# above) -- both branches of the "iff it moves production_per_hour" rule.
# --------------------------------------------------------------------------------------


def test_score_payback_returns_a_positive_payback_hours_for_a_real_delta():
    cost = Resources(metal=60, crystal=15, deuterium=0)
    weights = Resources(metal=1, crystal=1, deuterium=1)
    before = Resources(metal=0, crystal=0, deuterium=0)
    after = Resources(metal=30, crystal=0, deuterium=0)  # +30/hr metal

    score, basis = candidates.score_payback(cost, weights, before, after)

    assert score == 75.0 / 30.0  # weighted cost 75 / weighted marginal 30 per hour
    assert "payback" in basis


# --------------------------------------------------------------------------------------
# select_building_candidate on the real, live-derived fixtures agrees with plan.py's own
# behaviour-preservation suite -- cross-checked here at the candidates.py seam directly.
# --------------------------------------------------------------------------------------


def test_select_building_candidate_matches_planet_664s_solar_plant_pick():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664])

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.action.entity_id == ids.Building.SOLAR_PLANT
    assert winner.action.entity_id != ids.Ship.SOLAR_SATELLITE
    # Runner-ups are ranked (scored ascending, unscored last) and capped by the caller
    # (plan.py), not by select_building_candidate itself.
    assert candidates.rank_candidates(alternatives) == alternatives


# --------------------------------------------------------------------------------------
# Phase 3 of the general-strategy-engine program (docs/SPEC.md §5.4): ship_targets /
# defense_targets stock-keeping, crawlers, proactive storage, infrastructure,
# research_priority. `_ready_snapshot` builds a planet with Shipyard 5 and the tech
# levels needed to unlock Light Fighter / Crawler / Small Shield Dome / Anti-Ballistic
# Missile all at once, so individual tests can toggle counts/caps without re-deriving
# the whole unlock set each time. Destroyer (Shipyard >= 9) stays locked on purpose --
# it is this section's "locked target" fixture.
# --------------------------------------------------------------------------------------


def _ready_snapshot(
    *,
    ship_counts: dict[int, int] | None = None,
    defense_counts: dict[int, int] | None = None,
    building_missile_silo_level: int | None = 4,
    snapshot_missile_silo_level: int | None = 4,
    crawler_production: CrawlerProduction | None = None,
    mine_levels: int = 20,
) -> Snapshot:
    ship_counts = ship_counts or {}
    defense_counts = defense_counts or {}
    buildings = [
        Entity(id=ids.Building.SHIPYARD, name="Shipyard", level=5, cost=Resources(metal=400, crystal=200, deuterium=100)),
        Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=mine_levels, cost=Resources(metal=60, crystal=15)),
        Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=mine_levels, cost=Resources(metal=48, crystal=24)),
        Entity(
            id=ids.Building.DEUTERIUM_SYNTHESIZER,
            name="Deuterium Synthesizer",
            level=mine_levels,
            cost=Resources(metal=225, crystal=75),
        ),
        # Solar Plant 25 -> 5,417 produced, comfortably above the 5,380 required at mine
        # level 20 each (calc.energy_balance) -- calc.production_per_hour's own internal
        # energy gate would otherwise scale everything to 0 regardless of crawler_count,
        # independent of `PlanetSnapshot.energy` (which the scoring path never reads).
        # Mine level 20 (not 5) is also load-bearing: base per-hour output must be large
        # enough that a 2 bps crawler-boost delta survives integer truncation in
        # `calc._scale_by_bps` instead of rounding away to an identical integer.
        Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=25, cost=Resources(metal=50_000, crystal=20_000)),
    ]
    if building_missile_silo_level is not None:
        buildings.append(
            Entity(
                id=ids.Building.MISSILE_SILO,
                name="Missile Silo",
                level=building_missile_silo_level,
                cost=Resources(metal=20_000, crystal=20_000, deuterium=1_000),
            )
        )
    ships = [
        Entity(
            id=ids.Ship.LIGHT_FIGHTER,
            name="Light Fighter",
            count=ship_counts.get(ids.Ship.LIGHT_FIGHTER, 0),
            cost=Resources(metal=3_000, crystal=1_000),
        ),
        Entity(
            id=ids.Ship.CRAWLER,
            name="Crawler",
            count=ship_counts.get(ids.Ship.CRAWLER, 0),
            cost=Resources(metal=8_000, crystal=4_000, deuterium=2_000),
        ),
        Entity(
            id=ids.Ship.DESTROYER,
            name="Destroyer",
            count=ship_counts.get(ids.Ship.DESTROYER, 0),
            cost=Resources(metal=50_000, crystal=25_000, deuterium=15_000),
        ),
    ]
    defenses = [
        Entity(
            id=ids.Defense.SMALL_SHIELD_DOME,
            name="Small Shield Dome",
            count=defense_counts.get(ids.Defense.SMALL_SHIELD_DOME, 0),
            cost=Resources(metal=10_000, crystal=10_000),
        ),
        Entity(
            id=ids.Defense.ANTI_BALLISTIC_MISSILE,
            name="Anti-Ballistic Missile",
            count=defense_counts.get(ids.Defense.ANTI_BALLISTIC_MISSILE, 0),
            cost=Resources(metal=8_000, deuterium=2_000),
        ),
        Entity(
            id=ids.Defense.INTERPLANETARY_MISSILE,
            name="Interplanetary Missile",
            count=defense_counts.get(ids.Defense.INTERPLANETARY_MISSILE, 0),
            cost=Resources(metal=12_500, crystal=2_500, deuterium=10_000),
        ),
    ]
    planet = PlanetSnapshot(
        planet_id=700,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=1_000_000, crystal=1_000_000, deuterium=1_000_000),
        storage_caps=Resources(metal=10_000_000, crystal=10_000_000, deuterium=10_000_000),
        production_per_hour=Resources(metal=100, crystal=100, deuterium=100),
        energy=EnergyBalance(produced=1_000_000, required=1, scale_bps=10_000, solar_satellite_energy=4),
        buildings=buildings,
        ships=ships,
        defenses=defenses,
        missile_silo_level=snapshot_missile_silo_level,
        crawler_production=crawler_production,
        queues={},
    )
    technologies = [
        Entity(id=ids.Technology.COMBUSTION_DRIVE, name="Combustion Drive", level=4, cost=Resources()),
        Entity(id=ids.Technology.ARMOR, name="Armor Technology", level=4, cost=Resources()),
        Entity(id=ids.Technology.LASER, name="Laser Technology", level=4, cost=Resources()),
        Entity(id=ids.Technology.SHIELDING, name="Shielding Technology", level=2, cost=Resources()),
    ]
    return Snapshot(taken_at="2026-08-16T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet], technologies=technologies)


def test_ship_target_below_count_is_proposed():
    snapshot = _ready_snapshot(ship_counts={ids.Ship.LIGHT_FIGHTER: 2})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Light Fighter", count=5)]),
    )

    result = candidates.generate_ship_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].action.entity_id == ids.Ship.LIGHT_FIGHTER
    assert result[0].action.quantity == 1
    assert result[0].family == "ship"
    assert not result[0].score_basis.startswith("locked:")


def test_ship_target_at_count_is_not_proposed():
    snapshot = _ready_snapshot(ship_counts={ids.Ship.LIGHT_FIGHTER: 5})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Light Fighter", count=5)]),
    )

    result = candidates.generate_ship_target_candidates(snapshot, policy, planet)

    assert result == []


def test_locked_ship_target_is_skipped_with_techtree_describe_in_the_reason():
    snapshot = _ready_snapshot(ship_counts={ids.Ship.DESTROYER: 0})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Destroyer", count=1)]),
    )

    result = candidates.generate_ship_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score_basis.startswith("locked:")
    assert "needs Shipyard" in result[0].score_basis  # techtree.describe() text, e.g. "needs Shipyard 9 (have 5)"


def test_ship_target_does_not_touch_solar_satellites_separate_energy_path():
    """Solar Satellite named as a ship_targets entry stock-keeps like any other ship --
    proving the two mechanisms are independent, not merged, per the Phase 3 brief."""
    snapshot = _ready_snapshot()
    planet = snapshot.planet(700)
    assert planet is not None
    satellite = Entity(id=ids.Ship.SOLAR_SATELLITE, name="Solar Satellite", count=0, cost=Resources(metal=2_500))
    planet = planet.model_copy(update={"ships": [*planet.ships, satellite]})
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Solar Satellite", count=3)]),
    )

    result = candidates.generate_ship_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].action.entity_id == ids.Ship.SOLAR_SATELLITE
    assert result[0].score is None  # policy-declared stock-keeping, not the scored energy path
    assert "policy-declared stock target" in result[0].score_basis


def test_unknown_ship_target_name_fails_loudly():
    snapshot = _ready_snapshot()
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Not A Real Ship", count=1)]),
    )

    with pytest.raises(ValueError, match="does not match any known entity name"):
        candidates.generate_ship_target_candidates(snapshot, policy, planet)


def test_unknown_defense_target_name_fails_loudly():
    snapshot = _ready_snapshot()
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Not A Real Defense", count=1)]),
    )

    with pytest.raises(ValueError, match="does not match any known entity name"):
        candidates.generate_defense_target_candidates(snapshot, policy, planet)


def test_unknown_research_priority_name_fails_loudly():
    snapshot = _ready_snapshot()
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_research=True), strategy=StrategyCfg(research_priority=["Not A Real Tech"]))

    with pytest.raises(ValueError, match="does not match any known entity name"):
        candidates._research_priority_order(snapshot, policy, building_levels={}, technology_levels={})


def test_second_small_shield_dome_is_refused():
    snapshot = _ready_snapshot(defense_counts={ids.Defense.SMALL_SHIELD_DOME: 1})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Small Shield Dome", count=2)]),
    )

    result = candidates.generate_defense_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score_basis.startswith("locked:")
    assert "capped at 1" in result[0].score_basis


def test_missiles_over_silo_capacity_are_refused():
    # Missile Silo level 4 -> 40 slots (techtree.missile_silo_capacity); 40 already built.
    snapshot = _ready_snapshot(defense_counts={ids.Defense.ANTI_BALLISTIC_MISSILE: 40})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Anti-Ballistic Missile", count=41)]),
    )

    result = candidates.generate_defense_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score_basis.startswith("locked:")
    assert "silo slot" in result[0].score_basis


def test_defense_target_missile_silo_level_none_fails_closed_not_as_zero():
    """The exact bug class two prior judge passes found: `missile_silo_level is None`
    must never be read as `0`. `building_missile_silo_level` (the `planet.buildings`
    Missile Silo *level*, which satisfies the plain techtree requirement) stays 4 here --
    only the new, independent `PlanetSnapshot.missile_silo_level` field is unset, so this
    isolates `_defense_capacity_reason`'s own fail-closed branch from the unrelated
    techtree-requirement check."""
    snapshot = _ready_snapshot(defense_counts={ids.Defense.ANTI_BALLISTIC_MISSILE: 0}, snapshot_missile_silo_level=None)
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Anti-Ballistic Missile", count=1)]),
    )

    result = candidates.generate_defense_target_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score_basis.startswith("locked:")
    assert "Missile Silo level not reported" in result[0].score_basis


def test_defense_target_below_count_is_proposed_and_at_count_is_not():
    snapshot = _ready_snapshot(defense_counts={ids.Defense.ANTI_BALLISTIC_MISSILE: 3})
    planet = snapshot.planet(700)
    assert planet is not None
    below = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Anti-Ballistic Missile", count=5)]),
    )
    at_count = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Anti-Ballistic Missile", count=3)]),
    )

    below_result = candidates.generate_defense_target_candidates(snapshot, below, planet)
    at_count_result = candidates.generate_defense_target_candidates(snapshot, at_count, planet)

    assert len(below_result) == 1
    assert not below_result[0].score_basis.startswith("locked:")
    assert at_count_result == []


def test_defense_targets_supersede_the_default_rocket_launcher_when_declared():
    snapshot = _ready_snapshot()
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(
        planets=[700],
        actions=ActionsCfg(allow_defense=True),
        strategy=StrategyCfg(defense_targets=[EntityTarget(name="Small Shield Dome", count=1)]),
    )

    result = candidates.generate_defense_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].action.entity_id == ids.Defense.SMALL_SHIELD_DOME  # not the Rocket Launcher default


def test_crawler_candidate_is_scored_when_boost_has_room_to_grow():
    # effective_cap = (20+20+20)*8 = 480 -- 1 crawler is far from saturating it.
    snapshot = _ready_snapshot(ship_counts={ids.Ship.CRAWLER: 1})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_ships=True), strategy=StrategyCfg(enable_crawler=True))

    result = candidates.generate_crawler_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].family == "crawler"
    assert result[0].score is not None
    assert result[0].score > 0


def test_crawler_candidate_respects_the_eight_per_mine_level_cap():
    # effective_cap = (20+20+20)*8 = 480 -- already at the cap, so one more crawler moves
    # calc.production_per_hour's output by exactly zero (score_payback returns None).
    snapshot = _ready_snapshot(ship_counts={ids.Ship.CRAWLER: 480})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_ships=True), strategy=StrategyCfg(enable_crawler=True))

    result = candidates.generate_crawler_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score is None


def test_crawler_candidate_prefers_the_live_capped_flag_over_recomputing():
    live = CrawlerProduction(total=10, effective=10, max_effective=10, boost_bps=20, capped=True)
    snapshot = _ready_snapshot(ship_counts={ids.Ship.CRAWLER: 10}, crawler_production=live)
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_ships=True), strategy=StrategyCfg(enable_crawler=True))

    result = candidates.generate_crawler_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].score is None
    assert "capped" in result[0].score_basis


def test_crawler_locked_without_shipyard_five():
    snapshot = _ready_snapshot(ship_counts={ids.Ship.CRAWLER: 0})
    planet = snapshot.planet(700)
    assert planet is not None
    downgraded = planet.model_copy(
        update={"buildings": [b if b.id != ids.Building.SHIPYARD else b.model_copy(update={"level": 1}) for b in planet.buildings]}
    )
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_ships=True), strategy=StrategyCfg(enable_crawler=True))

    result = candidates.generate_crawler_candidates(snapshot, policy, downgraded)

    assert len(result) == 1
    assert result[0].score_basis.startswith("locked:")


def test_crawler_candidates_empty_when_not_opted_in():
    """Judge finding 4 (2026-08-17): `generate_crawler_candidates` used to be gated only
    on `allow_ships`, so an entirely empty `policy.strategy` could still let a scored,
    unlocked Crawler win `select_shipyard_candidate`'s ranking over Solar Satellite --
    contradicting the "Solar Satellite's priority is unchanged when nothing new is
    configured" AC (docs/SPEC.md §9). `policy.strategy.enable_crawler` defaults `False`;
    this pins that default reproducing pre-Phase-3 behaviour exactly, even when the
    Crawler is fully unlocked and scoreable."""
    snapshot = _ready_snapshot(ship_counts={ids.Ship.CRAWLER: 1})
    planet = snapshot.planet(700)
    assert planet is not None
    policy = make_policy(planets=[700], actions=ActionsCfg(allow_ships=True))

    assert candidates.generate_crawler_candidates(snapshot, policy, planet) == []


def test_proactive_storage_candidate_scored_none_and_present_regardless_of_urgency():
    """Unlike `generate_storage_candidates` (Band 1, deadline-driven), this one is
    present even when no resource is anywhere near its storage cap -- planet 664's
    fixture is zero-state, so the reactive generator returns nothing at all here."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664])

    reactive = candidates.generate_storage_candidates(snapshot, policy, planet)
    proactive = candidates.generate_proactive_storage_candidates(snapshot, policy, planet)

    assert reactive == []
    assert proactive
    assert all(c.score is None and c.family == "storage" for c in proactive)


def test_building_priority_orders_infrastructure_candidates():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664], strategy=StrategyCfg(building_priority=["Shipyard", "Robotics Factory"]))

    result = candidates.generate_infrastructure_candidates(snapshot, policy, planet)

    assert [c.action.entity_id for c in result[:2]] == [ids.Building.SHIPYARD, ids.Building.ROBOTICS_FACTORY]


def test_building_priority_selects_first_unlocked_declared_building():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    # Real zero-state 664 only holds 1,000 metal / 1,000 crystal / 0 deuterium -- not
    # enough to afford Robotics Factory (400/120/200) even though it's unlocked. Bumped
    # here so this test isolates priority-order + lock-status (its actual subject),
    # leaving the currently-unaffordable case to its own test below.
    planet.resources_as_of_now = Resources(metal=100_000, crystal=100_000, deuterium=100_000)
    policy = make_policy(planets=[664], strategy=StrategyCfg(building_priority=["Shipyard", "Robotics Factory"]))

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    # Shipyard needs Robotics Factory >= 2 (locked at planet 664's baseline); Robotics
    # Factory itself has no prerequisite in the source, so it wins.
    assert winner.action.entity_id == ids.Building.ROBOTICS_FACTORY
    assert winner.family == "infrastructure"


def test_building_priority_target_currently_unaffordable_falls_through_to_ordinary_picker():
    """Dated fix: before `_resolve_affordability_precondition` existed, a declared
    `building_priority` target that was unlocked but currently unaffordable still won
    outright -- guard.py's `_gate_affordability` would then BLOCK it every tick forever,
    since nothing here ever re-tried a different pick. Real zero-state planet 664 (1,000
    metal / 1,000 crystal / 0 deuterium) is exactly this case: Robotics Factory is
    unlocked but needs 200 deuterium it doesn't have. The fix falls through past the
    entire (single-entry, now-exhausted) building_priority list to the ordinary
    economic picker below it, which lands on the same energy-first opener
    `test_planet_664_energy_first_opener_never_proposes_satellite` (test_plan.py) pins."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664], strategy=StrategyCfg(building_priority=["Robotics Factory"]))

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family != "infrastructure"
    assert winner.action.entity_id != ids.Building.ROBOTICS_FACTORY
    # Demoted, not dropped -- still visible so a human reviewing the proposal can see the
    # declared target was considered and why it lost.
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.ROBOTICS_FACTORY]
    assert demoted


def test_infrastructure_fallback_order_prefers_unlock_breadth_over_level(monkeypatch):
    """Isolates the sort key itself from the real requirement graph's actual content
    (already covered by test_techtree.py's own unlock_breadth tests) by controlling what
    each id's unlock_breadth reports: Robotics Factory (level 5, one full unlock) must
    still outrank Nanite Factory (level 0, only a partial advance) despite its much lower
    level -- proving unlock_breadth, not level, is the primary key."""
    scores = {
        ids.Building.ROBOTICS_FACTORY: (1, 0),
        ids.Building.NANITE_FACTORY: (0, 1),
    }
    monkeypatch.setattr(candidates, "unlock_breadth", lambda family, entity_id, **kw: scores.get(entity_id, (0, 0)))
    building_levels = {b: 0 for b in candidates._INFRASTRUCTURE_BUILDING_IDS}
    building_levels[ids.Building.ROBOTICS_FACTORY] = 5  # much higher level than Nanite Factory's 0

    order = candidates._infrastructure_fallback_order(building_levels=building_levels, technology_levels={})

    assert order[0] == ids.Building.ROBOTICS_FACTORY
    assert order.index(ids.Building.ROBOTICS_FACTORY) < order.index(ids.Building.NANITE_FACTORY)


def test_infrastructure_fallback_order_reachable_without_any_declaration():
    """generate_infrastructure_candidates itself still requires building_priority to be
    non-empty (its own reachability switch, unchanged by this fix) -- but the ordering
    function underneath it is now always computable and never returns an empty list, so
    a future caller that wants infra reachable by default has a real order to use."""
    order = candidates._infrastructure_fallback_order(building_levels={}, technology_levels={})
    assert sorted(order) == sorted(candidates._INFRASTRUCTURE_BUILDING_IDS)


# --------------------------------------------------------------------------------------
# Storage-cap precondition on the winning pick. Before this fix, a scored mine/energy (or
# declared building_priority) winner whose cost exceeded the planet's *current* storage
# cap was still crowned winner -- `generate_proactive_storage_candidates` only ever
# appeared as an informational alternative, never able to outrank it, so the ladder kept
# re-proposing a pick guard.py's `_gate_affordability` would BLOCK forever ("never
# affordable: cost exceeds storage cap"). `_resolve_storage_precondition` makes this a
# hard precondition instead: substitute the matching storage candidate, or (if none is
# available) fall through to the next candidate, exactly like the energy-first filter.
# --------------------------------------------------------------------------------------


def _capped_planet(*, with_metal_storage: bool = True) -> PlanetSnapshot:
    """Metal Mine's next upgrade is energy-safe and would ordinarily win Band 2's
    scoring, but its cost (200 metal) exceeds this planet's tiny metal storage cap (50)
    -- that cost can never be saved up to without raising Metal Storage first. Crystal
    Mine is energy-safe and fits comfortably under its own (large) crystal cap, so it's
    the fallback winner when no Metal Storage candidate is available to substitute.
    `resources_as_of_now` is set well above every building's cost here deliberately --
    this fixture isolates the storage-*cap* precondition specifically, not the separate
    currently-affordable precondition (see `_underfunded_planet` below for that one), so
    current holdings must never be the thing that decides the winner in these tests."""
    buildings = [
        Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=200, crystal=50)),
        Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=0, cost=Resources(metal=48, crystal=24)),
        Entity(
            id=ids.Building.DEUTERIUM_SYNTHESIZER,
            name="Deuterium Synthesizer",
            level=0,
            cost=Resources(metal=225, crystal=75),
        ),
        Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=0, cost=Resources(metal=75, crystal=30)),
    ]
    if with_metal_storage:
        buildings.append(
            Entity(id=ids.Building.METAL_STORAGE, name="Metal Storage", level=0, cost=Resources(metal=40, crystal=0))
        )
    return PlanetSnapshot(
        planet_id=2,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        storage_caps=Resources(metal=50, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=1_000, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=buildings,
        ships=[],
        defenses=[],
    )


def test_mine_winner_capped_by_storage_is_replaced_by_matching_storage_candidate():
    planet = _capped_planet(with_metal_storage=True)
    snapshot = Snapshot(taken_at="2026-08-21T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[2])

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family == "storage"
    assert winner.action.entity_id == ids.Building.METAL_STORAGE
    assert "more than the planet's current metal storage cap" in winner.action.rationale
    # The capped mine pick is demoted to an alternative, not dropped.
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.METAL_MINE and c.family == "mine"]
    assert demoted


def test_mine_winner_capped_by_storage_falls_through_when_no_storage_substitute_available():
    planet = _capped_planet(with_metal_storage=False)
    snapshot = Snapshot(taken_at="2026-08-21T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[2])

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    # No Metal Storage entity on this planet to substitute -- falls through to the next
    # mine in priority order instead of getting stuck on the capped Metal Mine pick.
    assert winner.action.entity_id == ids.Building.CRYSTAL_MINE
    assert winner.family == "mine"
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.METAL_MINE and c.family == "mine"]
    assert demoted


def test_building_priority_winner_capped_by_storage_is_replaced_by_matching_storage_candidate():
    planet = _capped_planet(with_metal_storage=True)
    buildings = planet.buildings + [
        Entity(id=ids.Building.ROBOTICS_FACTORY, name="Robotics Factory", level=0, cost=Resources(metal=200, crystal=50))
    ]
    planet = planet.model_copy(update={"buildings": buildings})
    snapshot = Snapshot(taken_at="2026-08-21T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[2], strategy=StrategyCfg(building_priority=["Robotics Factory"]))

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    # Robotics Factory has no prerequisite (so it's selectable), but its cost also
    # exceeds the tiny metal storage cap -- the same precondition applies to a declared
    # building_priority winner, not only the scored mine/energy path.
    assert winner.family == "storage"
    assert winner.action.entity_id == ids.Building.METAL_STORAGE
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.ROBOTICS_FACTORY]
    assert demoted


# --------------------------------------------------------------------------------------
# Currently-affordable precondition on the winning pick. Distinct from the storage-cap
# section above: here the cost fits comfortably under every storage cap it needs, but
# current holdings (`resources_as_of_now`) don't cover it *yet* -- e.g. the top-ranked
# mine by value density needs a resource the planet is presently short on, while a
# lower-ranked mine is fully affordable right now. Before this fix, nothing in this
# module checked current holdings at all: the ladder would keep re-proposing the same
# pick every tick, and guard.py's `_gate_affordability` would BLOCK it every time, with
# no path back here to try the next candidate -- the "one resource shortage blocks the
# whole ladder" failure mode. `_resolve_affordability_precondition` (composed with the
# storage-cap check by `_resolve_building_preconditions`) makes falling through to the
# next-ranked candidate the default, exactly like the storage-cap and energy-first cases.
# --------------------------------------------------------------------------------------


def _underfunded_planet() -> PlanetSnapshot:
    """Metal Mine ranks first by value density (300,000 vs Crystal's 200,000) and is
    energy-safe and comfortably under its storage cap -- but the planet is presently
    short on crystal, one of the two resources Metal Mine's upgrade needs (60 metal / 40
    crystal, only 15 crystal held). Crystal Mine, ranked second, costs 20 metal / 10
    crystal -- affordable right now, since 15 covers its 10 -- and, not incidentally, is
    exactly the resource the planet is short on: the scenario a human operator would call
    "a crystal-shortage bottleneck blocking the ladder", reproduced structurally rather
    than by a bespoke bottleneck-detection heuristic (see
    `_resolve_affordability_precondition`'s docstring)."""
    return PlanetSnapshot(
        planet_id=3,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=10_000, crystal=15, deuterium=10_000),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=1_000, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=60, crystal=40)),
            Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=0, cost=Resources(metal=20, crystal=10)),
            Entity(
                id=ids.Building.DEUTERIUM_SYNTHESIZER,
                name="Deuterium Synthesizer",
                level=0,
                cost=Resources(metal=225, crystal=75),
            ),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=0, cost=Resources(metal=75, crystal=30)),
        ],
        ships=[],
        defenses=[],
    )


def test_mine_winner_currently_unaffordable_falls_through_to_next_affordable_mine():
    planet = _underfunded_planet()
    snapshot = Snapshot(taken_at="2026-08-27T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[3])

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    # Metal Mine ranks first by density but needs 40 crystal against 5 held -- falls
    # through. Crystal Mine (20 metal / 10 crystal, both covered) wins instead.
    assert winner.family == "mine"
    assert winner.action.entity_id == ids.Building.CRYSTAL_MINE
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.METAL_MINE and c.family == "mine"]
    assert demoted


def test_mine_winner_currently_unaffordable_with_no_affordable_mine_falls_through_to_noop():
    """Every mine (not just the top-ranked one) is currently unaffordable -- the walk
    must exhaust the whole priority order and return no winner at all, never silently
    accept an unaffordable pick just because it ran out of alternatives to try."""
    planet = _underfunded_planet()
    planet.resources_as_of_now = Resources(metal=1, crystal=1, deuterium=1)

    snapshot = Snapshot(taken_at="2026-08-27T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[3])

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is None


def test_building_priority_target_falls_through_when_currently_unaffordable_no_storage_issue():
    """Same fixture family as the storage-cap `building_priority` test above, but the
    blocker here is current holdings, not the storage cap -- proving the affordability
    precondition applies to a declared `building_priority` winner too, not only the
    ordinary mine walk."""
    planet = _underfunded_planet()
    buildings = planet.buildings + [
        Entity(id=ids.Building.ROBOTICS_FACTORY, name="Robotics Factory", level=0, cost=Resources(metal=5_000, crystal=1))
    ]
    planet = planet.model_copy(update={"buildings": buildings})
    # 25 metal covers Crystal Mine's 20 (the fallback winner) but not Robotics Factory's
    # 5,000 -- enough to prove the fallthrough lands on a real winner, not just that the
    # declared target gets rejected.
    planet.resources_as_of_now = Resources(metal=25, crystal=10_000, deuterium=10_000)
    snapshot = Snapshot(taken_at="2026-08-27T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[3], strategy=StrategyCfg(building_priority=["Robotics Factory"]))

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family != "infrastructure"
    assert winner.action.entity_id != ids.Building.ROBOTICS_FACTORY
    demoted = [c for c in alternatives if c.action.entity_id == ids.Building.ROBOTICS_FACTORY]
    assert demoted


def _storage_substitute_also_underfunded_planet() -> PlanetSnapshot:
    """Metal Mine is storage-capped (200 metal cost vs a 50 metal cap), so it would
    normally substitute to Metal Storage -- but Metal Storage is deliberately priced at
    500 metal here, well above the 100 metal actually held, so the substitute itself
    fails the currently-affordable check too. Crystal Mine (20 metal / 10 crystal) is
    cheap enough to be affordable at 100 metal and wins instead."""
    return PlanetSnapshot(
        planet_id=4,
        metal_multiplier_bps=10_000,
        crystal_multiplier_bps=10_000,
        deuterium_multiplier_bps=10_000,
        resources_as_of_now=Resources(metal=100, crystal=10_000, deuterium=10_000),
        storage_caps=Resources(metal=50, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(metal=0, crystal=0, deuterium=0),
        energy=EnergyBalance(produced=1_000, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[
            Entity(id=ids.Building.METAL_MINE, name="Metal Mine", level=0, cost=Resources(metal=200, crystal=10)),
            Entity(id=ids.Building.CRYSTAL_MINE, name="Crystal Mine", level=0, cost=Resources(metal=20, crystal=10)),
            Entity(
                id=ids.Building.DEUTERIUM_SYNTHESIZER,
                name="Deuterium Synthesizer",
                level=0,
                cost=Resources(metal=225, crystal=75),
            ),
            Entity(id=ids.Building.SOLAR_PLANT, name="Solar Plant", level=0, cost=Resources(metal=75, crystal=30)),
            Entity(id=ids.Building.METAL_STORAGE, name="Metal Storage", level=0, cost=Resources(metal=500, crystal=0)),
        ],
        ships=[],
        defenses=[],
    )


def test_storage_cap_substitute_that_is_itself_currently_unaffordable_falls_through_further():
    """Composition case for `_resolve_building_preconditions`: Metal Mine is storage-
    capped, so it substitutes to Metal Storage -- but Metal Storage's own cost (500
    metal) is *also* more than current holdings cover here (100 metal). The combined
    precondition must fall through past the substitute too, landing on Crystal Mine, not
    silently accept the currently-unaffordable storage substitute."""
    planet = _storage_substitute_also_underfunded_planet()
    snapshot = Snapshot(taken_at="2026-08-27T00:00:00Z", wallet="0xabc", health_ok=True, planets=[planet])
    policy = make_policy(planets=[4])

    winner, alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.family == "mine"
    assert winner.action.entity_id == ids.Building.CRYSTAL_MINE
    demoted_metal_mine = [c for c in alternatives if c.action.entity_id == ids.Building.METAL_MINE and c.family == "mine"]
    assert demoted_metal_mine


def test_research_priority_overrides_lowest_level_first():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 1  # unlocks Computer Technology (needs only Research Lab >= 1)
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=False, allow_research=True),
        strategy=StrategyCfg(research_priority=["Computer Technology"]),
    )

    winner, _alternatives = candidates.select_research_candidate(snapshot, policy, [planet])

    assert winner is not None
    # Without research_priority the winner would be Energy Technology (lowest id at
    # level 0) -- see test_research_fallback_is_explicitly_labelled_default below.
    assert winner.action.entity_id == ids.Technology.COMPUTER
    assert "research_priority" in winner.score_basis


def test_research_fallback_is_explicitly_labelled_default():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 1
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    winner, _alternatives = candidates.select_research_candidate(snapshot, policy, [planet])

    assert winner is not None
    # Every technology is level 0 with zero unlock_breadth on this fixture, so the new
    # ranking degenerates to its own tiebreak (level, then id) -- Energy (id 0) wins for
    # the same reason it always did, not because unlock_breadth was exercised here. See
    # test_research_fallback_order_prefers_unlock_breadth_over_level below for a case
    # where it actually decides the outcome.
    assert winner.action.entity_id == ids.Technology.ENERGY
    assert winner.score_basis.startswith("default:")


def test_research_fallback_order_prefers_unlock_breadth_over_level():
    """Energy Technology at level 1 fully unlocks Laser Technology (needs Energy >= 2);
    Combustion Drive at level 0 only partially advances something else (Reaper's
    five-way conjunction, still locked on its other legs). Energy's higher unlock_breadth
    outranks its higher level -- confirms the fallback tail is genuinely ranked by
    unlock_breadth first, not merely falling back to the old lowest-level-first order."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    lab = next(b for b in planet.buildings if b.id == ids.Building.RESEARCH_LAB)
    lab.level = 2
    energy_tech = next(t for t in snapshot.technologies if t.id == ids.Technology.ENERGY)
    energy_tech.level = 1
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=False, allow_research=True))

    building_levels = candidates._level_vector(planet.buildings)
    technology_levels = candidates._level_vector(snapshot.technologies)
    order, _declared = candidates._research_priority_order(
        snapshot, policy, building_levels=building_levels, technology_levels=technology_levels
    )

    assert order[0] == ids.Technology.ENERGY
    assert order.index(ids.Technology.ENERGY) < order.index(ids.Technology.COMBUSTION_DRIVE)


# --------------------------------------------------------------------------------------
# Acceptance criterion (docs/SPEC.md §9, Phase 3): empty strategy targets reproduce
# Phase 2 behaviour exactly. Every pre-existing test in this file and test_plan.py
# passing unmodified is the main proof; these two are the explicit, direct pin the
# brief asks for.
# --------------------------------------------------------------------------------------


def test_empty_strategy_targets_generate_nothing_new():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=True, allow_research=True, allow_defense=True, allow_ships=True))

    assert candidates.generate_ship_target_candidates(snapshot, policy, planet) == []
    assert candidates.generate_defense_target_candidates(snapshot, policy, planet) == []
    assert candidates.generate_infrastructure_candidates(snapshot, policy, planet) == []
    building_levels = candidates._level_vector(planet.buildings)
    technology_levels = candidates._level_vector(snapshot.technologies)
    order, declared = candidates._research_priority_order(
        snapshot, policy, building_levels=building_levels, technology_levels=technology_levels
    )
    assert declared == set()
    # Dated correction (docs/SPEC.md): the empty-research_priority fallback is no longer
    # pure lowest-level-then-id -- it's ranked by techtree.unlock_breadth descending,
    # with level-then-id only as the tiebreak. See test_unlock_breadth_fallback_order_*
    # below for the ranking itself; this test only pins that every known technology id
    # still appears exactly once (nothing dropped, nothing invented).
    assert sorted(order) == sorted(t.id for t in snapshot.technologies)


def test_empty_strategy_targets_select_building_candidate_matches_phase_2_exactly():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664])

    winner, _alternatives = candidates.select_building_candidate(snapshot, policy, planet)

    assert winner is not None
    assert winner.action.entity_id == ids.Building.SOLAR_PLANT
    assert winner.family == "energy"


# --------------------------------------------------------------------------------------
# generate_unlock_chain_candidates / select_unlock_chain_candidate (Phase 4 of the
# general-strategy-engine program, docs/SPEC.md §5.4 "Phase 4"). planet_664.json is
# zero-state on every axis, which is exactly the fixture the WP4 brief's hand-worked
# chain uses (Small Cargo -> Shipyard 2 + Combustion Drive 2 -> Shipyard needs Robotics
# Factory 2 -> Robotics Factory needs nothing).
# --------------------------------------------------------------------------------------


def test_generate_unlock_chain_candidates_empty_with_no_declared_targets():
    """The Phase 2/3 safety property, restated for Phase 4: no declared targets means
    nothing for this generator to unlock toward."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_building=True, allow_research=True, allow_defense=True, allow_ships=True))

    assert candidates.generate_unlock_chain_candidates(snapshot, policy, planet) == []
    winner, alternatives = candidates.select_unlock_chain_candidate(snapshot, policy, [planet])
    assert winner is None
    assert alternatives == []


def test_generate_unlock_chain_candidates_proposes_shallowest_prerequisite_for_locked_ship_target():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True, allow_ships=False),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    result = candidates.generate_unlock_chain_candidates(snapshot, policy, planet)

    assert len(result) == 1
    winner = result[0]
    assert winner.family == "unlock"
    assert winner.score is None
    assert winner.action.kind == ActionKind.BUILD
    assert winner.action.function == "startBuildingUpgrade"
    assert winner.action.entity_id == ids.Building.ROBOTICS_FACTORY
    assert winner.action.target_level == 1
    assert "Small Cargo" in winner.action.rationale
    assert "Shipyard" in winner.action.rationale


def test_generate_unlock_chain_candidates_already_unlocked_target_proposes_nothing():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    unlocked = planet.model_copy(
        update={
            "buildings": [
                b.model_copy(update={"level": 2}) if b.id in (ids.Building.SHIPYARD, ids.Building.ROBOTICS_FACTORY) else b
                for b in planet.buildings
            ]
        }
    )
    technologies = [t.model_copy(update={"level": 2}) if t.id == ids.Technology.COMBUSTION_DRIVE else t for t in snapshot.technologies]
    unlocked_snapshot = snapshot.model_copy(update={"technologies": technologies, "planets": [unlocked]})
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True, allow_ships=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    result = candidates.generate_unlock_chain_candidates(unlocked_snapshot, policy, unlocked)

    assert result == []


def test_generate_unlock_chain_candidates_dedups_two_targets_sharing_one_prerequisite():
    """Two locked declared targets (a ship and a defense) that both bottom out on the
    same Robotics Factory prerequisite must be proposed once, not twice."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True),
        strategy=StrategyCfg(
            ship_targets=[EntityTarget(name="Small Cargo", count=1)],
            defense_targets=[EntityTarget(name="Small Shield Dome", count=1)],
        ),
    )

    result = candidates.generate_unlock_chain_candidates(snapshot, policy, planet)

    assert len(result) == 1
    assert result[0].action.entity_id == ids.Building.ROBOTICS_FACTORY


def test_generate_unlock_chain_candidates_ties_broken_by_weighted_cost_ascending():
    """With Robotics Factory already at 2, Small Cargo's Shipyard branch and Weapons
    Technology's Research Lab branch resolve to two *different* depth-1 steps (Shipyard,
    cost 700 unweighted; Research Lab, cost 800) -- ordered cheapest first."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    elevated = planet.model_copy(
        update={"buildings": [b.model_copy(update={"level": 2}) if b.id == ids.Building.ROBOTICS_FACTORY else b for b in planet.buildings]}
    )
    elevated_snapshot = snapshot.model_copy(update={"planets": [elevated]})
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True, allow_research=True),
        strategy=StrategyCfg(
            ship_targets=[EntityTarget(name="Small Cargo", count=1)],
            research_priority=["Weapons Technology"],
        ),
    )

    result = candidates.generate_unlock_chain_candidates(elevated_snapshot, policy, elevated)

    assert [c.action.entity_id for c in result] == [ids.Building.SHIPYARD, ids.Building.RESEARCH_LAB]
    shipyard_cost = result[0].action.cost
    research_lab_cost = result[1].action.cost
    assert shipyard_cost.metal + shipyard_cost.crystal + shipyard_cost.deuterium < (
        research_lab_cost.metal + research_lab_cost.crystal + research_lab_cost.deuterium
    )

    winner, alternatives = candidates.select_unlock_chain_candidate(elevated_snapshot, policy, [elevated])
    assert winner is not None
    assert winner.action.entity_id == ids.Building.SHIPYARD
    assert [c.action.entity_id for c in alternatives] == [ids.Building.RESEARCH_LAB]


def test_unlock_weighted_cost_orders_unknown_cost_after_every_known_cost():
    weights = Resources(metal=1, crystal=1, deuterium=1)
    cheap = candidates._unlock_weighted_cost(Resources(metal=10), weights)
    expensive = candidates._unlock_weighted_cost(Resources(metal=1_000_000), weights)
    unknown = candidates._unlock_weighted_cost(None, weights)

    ordered = sorted([unknown, expensive, cheap])
    assert ordered == [cheap, expensive, unknown]


def test_generate_unlock_chain_candidates_respects_allow_building_false():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=False),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    assert candidates.generate_unlock_chain_candidates(snapshot, policy, planet) == []


def test_generate_unlock_chain_candidates_respects_building_queue_busy():
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None

    busy = planet.model_copy(
        update={"queues": {QueueKind.BUILDING: QueueEntry(kind=QueueKind.BUILDING, entity_id=ids.Building.SOLAR_PLANT, entity_name="Solar Plant")}}
    )
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True),
        strategy=StrategyCfg(ship_targets=[EntityTarget(name="Small Cargo", count=1)]),
    )

    assert candidates.generate_unlock_chain_candidates(snapshot, policy, busy) == []


def test_generate_unlock_chain_candidates_respects_allow_research_and_research_queue():
    """Weapons Technology's own unmet is only Research Lab -- a BUILDING step -- so to
    exercise the research-kind gating we need a target whose resolved step is itself a
    technology: with Research Lab already at 1, Laser Technology's own Energy Technology
    branch resolves directly (Energy only needs Research Lab, already satisfied)."""
    snapshot = load_snapshot("planet_664.json")
    planet = snapshot.planet(664)
    assert planet is not None
    elevated = planet.model_copy(
        update={"buildings": [b.model_copy(update={"level": 1}) if b.id == ids.Building.RESEARCH_LAB else b for b in planet.buildings]}
    )
    elevated_snapshot = snapshot.model_copy(update={"planets": [elevated]})
    policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True, allow_research=False),
        strategy=StrategyCfg(research_priority=["Laser Technology"]),
    )

    assert candidates.generate_unlock_chain_candidates(elevated_snapshot, policy, elevated) == []

    allowed_policy = make_policy(
        planets=[664],
        actions=ActionsCfg(allow_building=True, allow_research=True),
        strategy=StrategyCfg(research_priority=["Laser Technology"]),
    )
    result = candidates.generate_unlock_chain_candidates(elevated_snapshot, allowed_policy, elevated)
    assert len(result) == 1
    assert result[0].action.kind == ActionKind.RESEARCH
    assert result[0].action.entity_id == ids.Technology.ENERGY

    elevated_snapshot.research_queue = QueueEntry(kind=QueueKind.RESEARCH, entity_id=ids.Technology.ENERGY, entity_name="Energy Technology")
    assert candidates.generate_unlock_chain_candidates(elevated_snapshot, allowed_policy, elevated) == []


# --------------------------------------------------------------------------------------
# Logistics family (Phase 5c, docs/SPEC.md §5.4): Transport between the player's own
# planets, local Harvest of a planet's own debris. Both gated on
# policy.actions.allow_fleet_noncombat (defaults False).
# --------------------------------------------------------------------------------------


def _origin_planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=664,
        coordinates="7:181:14",
        resources_as_of_now=Resources(metal=5000, crystal=0, deuterium=0),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(),
        buildings=[],
        ships=[Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources(metal=2000, crystal=2000))],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def _destination_planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=665,
        coordinates="7:181:15",
        resources_as_of_now=Resources(metal=0, crystal=0, deuterium=0),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(),
        buildings=[],
        ships=[],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def _two_planet_snapshot(*, origin=None, destination=None) -> Snapshot:
    return Snapshot(
        taken_at="2026-08-17T12:00:00Z",
        wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f",
        health_ok=True,
        planets=[origin or _origin_planet(), destination or _destination_planet()],
    )


def test_generate_transport_candidates_empty_by_default_policy():
    snapshot = _two_planet_snapshot()
    policy = make_policy(planets=[664, 665])  # allow_fleet_noncombat defaults False
    origin = snapshot.planet(664)
    result = candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets)
    assert result == []


def test_generate_transport_candidates_moves_surplus_to_the_planet_that_needs_it_most():
    snapshot = _two_planet_snapshot()
    policy = make_policy(
        planets=[664, 665],
        actions=ActionsCfg(allow_fleet_noncombat=True),
        reserves=Resources(metal=100),
    )
    origin = snapshot.planet(664)
    result = candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets)
    assert len(result) == 1
    winner = result[0]
    assert winner.family == "logistics-transport"
    action = winner.action
    assert action.kind == ActionKind.FLEET_MISSION
    assert action.function == "launchFleetMission"
    assert action.mission_type == ids.FleetMissionType.TRANSPORT
    assert action.origin_planet_id == 664
    assert action.target_coordinates == "7:181:15"  # planet 665 -- the only other own planet
    # judge finding 3: send only what the cargo requires. 1 Small Cargo (5000 capacity,
    # fuel 2 at this distance/speed -- verified against calc.py directly) already covers
    # the 4900 surplus with room to spare (4998 available), so only 1 of the 2 owned
    # Small Cargo is committed, not both.
    assert action.ships == {ids.Ship.SMALL_CARGO: 1}
    assert action.cargo.metal == 4900
    assert action.cargo.crystal == 0
    assert action.cargo.deuterium == 0
    # judge finding 1: Action.cost must be the true launch spend (cargo + fuel, fuel as
    # deuterium), not the frozen-model default of zero.
    assert action.cost.metal == 4900
    assert action.cost.crystal == 0
    assert action.cost.deuterium == 2


def test_generate_transport_candidates_empty_without_cargo_ships():
    snapshot = _two_planet_snapshot(origin=_origin_planet(ships=[]))
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True))
    origin = snapshot.planet(664)
    assert candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets) == []


def test_generate_transport_candidates_never_commits_combat_ships_or_the_recycler():
    """Judge finding 3 (2026-08-17): the pre-fix filter was "nonzero cargo capacity",
    true for every flyable ship including the Deathstar -- so Transport committed the
    planet's ENTIRE fleet (defenceless for the round trip) at combat-ship fuel rates.
    A planet with only a Battleship (has cargo capacity 1,500 in calc.py's table, but is
    a combat ship) and a Recycler (reserved for Harvest) and no genuine hauler must
    produce nothing, even though both those ships technically have nonzero capacity."""
    origin = _origin_planet(
        ships=[
            Entity(id=ids.Ship.BATTLESHIP, name="Battleship", count=5, cost=Resources()),
            Entity(id=ids.Ship.RECYCLER, name="Recycler", count=3, cost=Resources()),
        ]
    )
    snapshot = _two_planet_snapshot(origin=origin)
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True), reserves=Resources(metal=100))
    result = candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets)
    assert result == []


def test_generate_transport_candidates_uses_only_haulers_when_combat_ships_are_present():
    """Same shape as above, but with a genuine hauler (Small Cargo) also present: the
    Transport must use only the Small Cargo, never the Battleship or the Recycler."""
    origin = _origin_planet(
        ships=[
            Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources()),
            Entity(id=ids.Ship.BATTLESHIP, name="Battleship", count=5, cost=Resources()),
            Entity(id=ids.Ship.RECYCLER, name="Recycler", count=3, cost=Resources()),
        ]
    )
    snapshot = _two_planet_snapshot(origin=origin)
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True), reserves=Resources(metal=100))
    result = candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets)
    assert len(result) == 1
    ships = result[0].action.ships
    assert set(ships) == {ids.Ship.SMALL_CARGO}
    assert ids.Ship.BATTLESHIP not in ships
    assert ids.Ship.RECYCLER not in ships


def test_generate_transport_candidates_empty_without_surplus():
    snapshot = _two_planet_snapshot(origin=_origin_planet(resources_as_of_now=Resources(metal=50)))
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True), reserves=Resources(metal=100))
    origin = snapshot.planet(664)
    assert candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets) == []


def test_generate_transport_candidates_empty_without_another_own_planet():
    snapshot = Snapshot(
        taken_at="2026-08-17T12:00:00Z",
        wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f",
        health_ok=True,
        planets=[_origin_planet()],
    )
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_fleet_noncombat=True))
    origin = snapshot.planet(664)
    assert candidates.generate_transport_candidates(snapshot, policy, origin, snapshot.planets) == []


def _debris_planet(**overrides) -> PlanetSnapshot:
    base = dict(
        planet_id=664,
        coordinates="7:181:14",
        resources_as_of_now=Resources(),
        storage_caps=Resources(metal=100_000, crystal=100_000, deuterium=100_000),
        production_per_hour=Resources(),
        buildings=[],
        ships=[Entity(id=ids.Ship.RECYCLER, name="Recycler", count=1, cost=Resources(metal=10_000, crystal=6_000, deuterium=2_000))],
        defenses=[],
    )
    base.update(overrides)
    return PlanetSnapshot(**base)


def test_generate_harvest_candidates_empty_by_default_policy():
    planet = _debris_planet()
    snapshot = Snapshot(taken_at="2026-08-17T12:00:00Z", wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664])  # allow_fleet_noncombat defaults False
    result = candidates.generate_harvest_candidates(
        snapshot, policy, planet, own_planet_debris={664: Resources(metal=25_000, crystal=5_000)}
    )
    assert result == []


def test_generate_harvest_candidates_empty_without_a_recycler():
    planet = _debris_planet(ships=[])
    snapshot = Snapshot(taken_at="2026-08-17T12:00:00Z", wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_fleet_noncombat=True))
    result = candidates.generate_harvest_candidates(
        snapshot, policy, planet, own_planet_debris={664: Resources(metal=25_000, crystal=5_000)}
    )
    assert result == []


def test_generate_harvest_candidates_empty_without_known_debris():
    """`own_planet_debris` unset (the default, honest-today state -- see the generator's
    own docstring on why no caller wires a live source yet) means no debris is known, not
    that there is none: this must never fabricate a harvest out of absent data."""
    planet = _debris_planet()
    snapshot = Snapshot(taken_at="2026-08-17T12:00:00Z", wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_fleet_noncombat=True))
    assert candidates.generate_harvest_candidates(snapshot, policy, planet) == []
    assert candidates.generate_harvest_candidates(snapshot, policy, planet, own_planet_debris={}) == []


def test_generate_harvest_candidates_produces_a_local_harvest_action():
    planet = _debris_planet()
    snapshot = Snapshot(taken_at="2026-08-17T12:00:00Z", wallet="0x224aba5d489675a7bd3ce07786fada466b46fa0f", health_ok=True, planets=[planet])
    policy = make_policy(planets=[664], actions=ActionsCfg(allow_fleet_noncombat=True))
    result = candidates.generate_harvest_candidates(
        snapshot, policy, planet, own_planet_debris={664: Resources(metal=25_000, crystal=5_000)}
    )
    assert len(result) == 1
    winner = result[0]
    assert winner.family == "logistics-harvest"
    action = winner.action
    assert action.kind == ActionKind.FLEET_MISSION
    assert action.mission_type == ids.FleetMissionType.HARVEST
    assert action.origin_planet_id == 664
    assert action.target_coordinates is None  # local harvest: target IS origin
    assert action.ships == {ids.Ship.RECYCLER: 1}
    # 1 Recycler at drive-tech 0: cargo 20000, fuel_consumption 300, speed 2000 (calc.py) --
    # local harvest fuel at distance 5 is 1, so available cargo is 19999; debris (25000
    # metal) exceeds it, so the harvest is capacity-bound, not debris-bound.
    assert action.cargo.metal == 19_999
    assert action.cargo.crystal == 0
    # judge finding 1: Action.cost must be the true launch spend (cargo + fuel).
    assert action.cost.metal == 19_999
    assert action.cost.crystal == 0
    assert action.cost.deuterium == 1


def test_select_logistics_candidate_returns_none_with_default_policy():
    snapshot = _two_planet_snapshot()
    policy = make_policy(planets=[664, 665])
    winner, alternatives = candidates.select_logistics_candidate(snapshot, policy, snapshot.planets)
    assert winner is None
    assert alternatives == []


def test_select_logistics_candidate_prefers_transport_over_harvest_on_the_same_planet():
    """Both Transport and local Harvest could fire on planet 664 here (cargo ships +
    surplus for Transport, a Recycler + known debris for Harvest); `select_logistics_
    candidate` checks Transport first per planet, so it wins."""
    origin = _origin_planet(
        ships=[
            Entity(id=ids.Ship.SMALL_CARGO, name="Small Cargo", count=2, cost=Resources()),
            Entity(id=ids.Ship.RECYCLER, name="Recycler", count=1, cost=Resources()),
        ]
    )
    snapshot = _two_planet_snapshot(origin=origin)
    policy = make_policy(planets=[664, 665], actions=ActionsCfg(allow_fleet_noncombat=True), reserves=Resources(metal=100))
    winner, alternatives = candidates.select_logistics_candidate(
        snapshot, policy, snapshot.planets, own_planet_debris={664: Resources(metal=1000)}
    )
    assert winner is not None
    assert winner.family == "logistics-transport"
    assert any(alt.family == "logistics-harvest" for alt in alternatives)
