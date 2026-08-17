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

from veydrift_agent import candidates, ids
from veydrift_agent.models import (
    ActionsCfg,
    EnergyBalance,
    Entity,
    Limits,
    PlanetSnapshot,
    Policy,
    Resources,
    Snapshot,
    StorageCfg,
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
