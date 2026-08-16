"""Tests for veydrift_agent.techtree — the on-chain prerequisite table.

Table-driven spot-checks transcribed verbatim from
`packages/contracts/src/libraries/VeydriftDependencies.sol` and
`VeydriftCatalog.sol` at the pinned commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(see `techtree.py`'s own module docstring for exact line citations). The most important
tests are the absent-data ones: `unmet()` must never resolve "the snapshot didn't report
this level" to "the requirement is satisfied," and must never return `()` just because the
inputs were incomplete.
"""

from __future__ import annotations

from veydrift_agent import ids
from veydrift_agent.techtree import (
    BUILDING_REQUIREMENTS,
    DEFENSE_REQUIREMENTS,
    MAX_DEFENSE_PER_PLANET,
    MISSILE_SLOTS,
    RESEARCH_REQUIREMENTS,
    SHIP_REQUIREMENTS,
    EntityFamily,
    ReqSource,
    Requirement,
    describe,
    graviton_energy_requirement,
    missile_silo_capacity,
    unmet,
)

# --------------------------------------------------------------------------------------
# Spot-checks lifted verbatim from the Solidity source.
# --------------------------------------------------------------------------------------


def test_shipyard_requires_robotics_factory_2():
    assert BUILDING_REQUIREMENTS[ids.Building.SHIPYARD] == (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 2),
    )


def test_research_lab_requires_robotics_factory_1():
    assert BUILDING_REQUIREMENTS[ids.Building.RESEARCH_LAB] == (
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 1),
    )


def test_nanite_factory_requires_robotics_10_and_computer_10():
    reqs = set(BUILDING_REQUIREMENTS[ids.Building.NANITE_FACTORY])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMPUTER, 10),
    }


def test_fusion_reactor_requires_deuterium_synthesizer_5_and_energy_3():
    reqs = set(BUILDING_REQUIREMENTS[ids.Building.FUSION_REACTOR])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.DEUTERIUM_SYNTHESIZER, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 3),
    }


def test_terraformer_requires_nanite_1_and_energy_12():
    reqs = set(BUILDING_REQUIREMENTS[ids.Building.TERRAFORMER])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.NANITE_FACTORY, 1),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 12),
    }


def test_missile_silo_requires_shipyard_1():
    assert BUILDING_REQUIREMENTS[ids.Building.MISSILE_SILO] == (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
    )


def test_destroyer_requires_shipyard_9_hsdrive_6_hyperspace_5():
    reqs = set(SHIP_REQUIREMENTS[ids.Ship.DESTROYER])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 9),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 5),
    }


def test_reaper_five_way_conjunction():
    """Shipyard 10 AND HyperspaceDrive 7 AND Hyperspace 6 AND Shielding 6 AND Energy 5 --
    the contract's `(hyperspaceDriveLevel < 7 || hyperspaceLevel < 6)` disjunction-of-
    failure is two conjunctive requirements here, not one, per the module docstring's
    point 2."""
    reqs = set(SHIP_REQUIREMENTS[ids.Ship.REAPER])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.SHIELDING, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 5),
    }


def test_crawler_four_way_conjunction():
    """Shipyard 5 AND CombustionDrive 4 AND Armor 4 AND Laser 4 -- same disjunction-of-
    failure pattern as Reaper (`(combustionDriveLevel < 4 || armorLevel < 4 ||
    laserLevel < 4)`)."""
    reqs = set(SHIP_REQUIREMENTS[ids.Ship.CRAWLER])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 5),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMBUSTION_DRIVE, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ARMOR, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 4),
    }


def test_deathstar_requires_shipyard_12_hsdrive_7_hyperspace_6_graviton_1():
    reqs = set(SHIP_REQUIREMENTS[ids.Ship.DEATHSTAR])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 12),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE_DRIVE, 7),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 6),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.GRAVITON, 1),
    }


def test_ion_cannon_requires_lab_4_energy_4_laser_5():
    reqs = set(RESEARCH_REQUIREMENTS[ids.Technology.ION])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.ENERGY, 4),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.LASER, 5),
    }


def test_graviton_requires_lab_12_only():
    """Graviton's `researchLabRequirement` is 12 and it has no extra per-tech conjunct in
    `requireResearch` -- its *other* real gate (energy) is intentionally not a
    `Requirement`; see `test_graviton_energy_requirement_formula`."""
    assert RESEARCH_REQUIREMENTS[ids.Technology.GRAVITON] == (
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 12),
    )


def test_intergalactic_research_network_requires_lab_10_computer_8_hyperspace_8():
    reqs = set(RESEARCH_REQUIREMENTS[ids.Technology.INTERGALACTIC_RESEARCH_NETWORK])
    assert reqs == {
        Requirement(ReqSource.BUILDING, ids.Building.RESEARCH_LAB, 10),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.COMPUTER, 8),
        Requirement(ReqSource.TECHNOLOGY, ids.Technology.HYPERSPACE, 8),
    }


def test_every_defense_carries_the_implicit_shipyard_1():
    """`requireDefense`'s unconditional `if (shipyardLevel == 0) revert` at the top of the
    function -- every defense id, even Rocket Launcher which has no other requirement."""
    for defense_id, reqs in DEFENSE_REQUIREMENTS.items():
        assert Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1) in reqs, defense_id


def test_rocket_launcher_has_only_the_implicit_shipyard_requirement():
    assert DEFENSE_REQUIREMENTS[ids.Defense.ROCKET_LAUNCHER] == (
        Requirement(ReqSource.BUILDING, ids.Building.SHIPYARD, 1),
    )


def test_both_shield_dome_caps():
    assert MAX_DEFENSE_PER_PLANET[ids.Defense.SMALL_SHIELD_DOME] == 1
    assert MAX_DEFENSE_PER_PLANET[ids.Defense.LARGE_SHIELD_DOME] == 1
    # An uncapped defense (Rocket Launcher) has no entry at all.
    assert ids.Defense.ROCKET_LAUNCHER not in MAX_DEFENSE_PER_PLANET


def test_missile_slot_arithmetic():
    assert MISSILE_SLOTS[ids.Defense.ANTI_BALLISTIC_MISSILE] == 1
    assert MISSILE_SLOTS[ids.Defense.INTERPLANETARY_MISSILE] == 2
    assert missile_silo_capacity(0) == 0
    assert missile_silo_capacity(1) == 10
    assert missile_silo_capacity(4) == 40


def test_graviton_energy_requirement_formula():
    """`300_000 * 3^currentLevel` (`VeydriftCatalog.sol:429-441`) -- not a `Requirement`,
    a separate energy-vs-production check the caller must run itself."""
    assert graviton_energy_requirement(0) == 300_000
    assert graviton_energy_requirement(1) == 900_000
    assert graviton_energy_requirement(3) == 300_000 * 27


# --------------------------------------------------------------------------------------
# unmet() — the fail-closed core.
# --------------------------------------------------------------------------------------


def test_unmet_empty_when_all_requirements_satisfied():
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.SHIPYARD,
        building_levels={ids.Building.ROBOTICS_FACTORY: 2},
        technology_levels={},
    )
    assert result == ()


def test_unmet_reports_have_for_known_insufficient_level():
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.SHIPYARD,
        building_levels={ids.Building.ROBOTICS_FACTORY: 0},
        technology_levels={},
    )
    assert len(result) == 1
    assert result[0].have == 0
    assert result[0].requirement == Requirement(ReqSource.BUILDING, ids.Building.ROBOTICS_FACTORY, 2)


def test_unmet_reports_have_none_for_absent_key():
    """Robotics Factory not present in the mapping at all -- `.get()` returns `None`,
    not `0`."""
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.SHIPYARD,
        building_levels={},
        technology_levels={},
    )
    assert len(result) == 1
    assert result[0].have is None


def test_unmet_reports_have_none_for_key_present_but_none():
    """A snapshot entity that exists but has `level=None` (never reported) must resolve
    the same way as a fully-absent key -- both are "the snapshot didn't say," never `0`."""
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.SHIPYARD,
        building_levels={ids.Building.ROBOTICS_FACTORY: None},
        technology_levels={},
    )
    assert len(result) == 1
    assert result[0].have is None


def test_unmet_never_returns_empty_on_absent_data_for_an_entity_with_requirements():
    """The invariant this whole module exists to enforce: incomplete inputs must never
    look identical to "requirements satisfied" for an entity that genuinely has
    requirements."""
    result = unmet(
        EntityFamily.RESEARCH,
        ids.Technology.ION,  # requires Lab 4, Energy 4, Laser 5
        building_levels={},
        technology_levels={},
    )
    assert result != ()
    assert all(u.have is None for u in result)
    assert len(result) == 3


def test_unmet_empty_for_entity_with_no_requirements_even_on_empty_maps():
    """Metal Mine has no entry in BUILDING_REQUIREMENTS -- `()` here is the genuine
    "nothing to check" case, not an absent-data false-pass."""
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.METAL_MINE,
        building_levels={},
        technology_levels={},
    )
    assert result == ()


def test_unmet_multi_source_conjunction_partial_satisfaction():
    """Fusion Reactor: Deuterium Synthesizer 5 (satisfied) AND Energy tech 3 (not) ->
    exactly one unmet requirement, from the technology side."""
    result = unmet(
        EntityFamily.BUILDING,
        ids.Building.FUSION_REACTOR,
        building_levels={ids.Building.DEUTERIUM_SYNTHESIZER: 5},
        technology_levels={ids.Technology.ENERGY: 1},
    )
    assert len(result) == 1
    assert result[0].requirement.source is ReqSource.TECHNOLOGY
    assert result[0].requirement.entity_id == ids.Technology.ENERGY
    assert result[0].have == 1


# --------------------------------------------------------------------------------------
# describe()
# --------------------------------------------------------------------------------------


def test_describe_known_insufficient_level():
    u = unmet(
        EntityFamily.BUILDING,
        ids.Building.SHIPYARD,
        building_levels={ids.Building.ROBOTICS_FACTORY: 0},
        technology_levels={},
    )[0]
    assert describe(u) == "needs Robotics Factory 2 (have 0)"


def test_only_the_strictest_unmet_requirement_per_target_is_reported():
    """Every Defense carries the blanket `Shipyard >= 1` (`if (shipyardLevel == 0) revert`)
    *plus* its own `Shipyard >= N`, so a Plasma Turret at Shipyard 0 matches both. The
    tables keep both rows -- they mirror the contract verbatim -- but `unmet()` reports only
    `Shipyard >= 8`, so the detail text reads as one problem rather than two."""
    unmet_reqs = unmet(
        EntityFamily.DEFENSE,
        ids.Defense.PLASMA_TURRET,
        building_levels={ids.Building.SHIPYARD: 0},
        technology_levels={ids.Technology.PLASMA: 0},
    )

    shipyard_reqs = [
        u for u in unmet_reqs if u.requirement.entity_id == ids.Building.SHIPYARD
    ]
    assert len(shipyard_reqs) == 1, f"expected one Shipyard clause, got {shipyard_reqs}"
    assert shipyard_reqs[0].requirement.min_level == 8

    detail = "; ".join(describe(u) for u in unmet_reqs)
    assert detail.count("Shipyard") == 1, detail
    assert "needs Shipyard 8 (have 0)" in detail
    assert "needs Plasma Technology 7 (have 0)" in detail


def test_dedup_never_empties_a_genuinely_unmet_result():
    """Collapsing duplicates must never turn "locked" into "no verdicts" -- that would be
    the vacuous-pass failure the fail-closed guarantee exists to prevent."""
    unmet_reqs = unmet(
        EntityFamily.DEFENSE,
        ids.Defense.ROCKET_LAUNCHER,
        building_levels={},  # Shipyard level not reported at all
        technology_levels={},
    )
    assert unmet_reqs, "an unreported Shipyard level must never yield zero unmet requirements"
    assert unmet_reqs[0].have is None


def test_describe_absent_level():
    """Light Laser needs `Shipyard >= 2` of its own on top of the blanket `Shipyard >= 1`
    every Defense carries; `unmet()` reports the stricter of the two, so this renders as
    `>= 2`, not `>= 1`."""
    u = unmet(
        EntityFamily.DEFENSE,
        ids.Defense.LIGHT_LASER,
        building_levels={},
        technology_levels={ids.Technology.ENERGY: 1, ids.Technology.LASER: 3},
    )[0]
    assert describe(u) == "needs Shipyard >= 2 (level not reported)"
