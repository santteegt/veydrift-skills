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

import pytest

from veydrift_agent import ids, techtree
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
    UnlockStep,
    describe,
    graviton_energy_requirement,
    missile_silo_capacity,
    next_step_toward,
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


# --------------------------------------------------------------------------------------
# next_step_toward() — Phase 4 of the general-strategy-engine program (docs/SPEC.md §5.4
# "Phase 4"). Walks unmet()'s output backwards, one depth level at a time, to find the
# shallowest currently-buildable prerequisite toward a locked target.
# --------------------------------------------------------------------------------------

#: A fresh-planet, fresh-player level vector: every building and every technology known
#: (level 0, not absent). Small Cargo -> Shipyard 2 + Combustion Drive 2 -> Shipyard needs
#: Robotics Factory 2 (Robotics Factory itself has no entry in BUILDING_REQUIREMENTS, so
#: it's the base case) is the hand-worked chain the WP4 brief specifies; Combustion Drive's
#: own branch (Research Lab 1, Energy 1 -- Energy itself needs Research Lab 1) is strictly
#: deeper (depth 3), so Robotics Factory at depth 2 must win.
_ALL_ZERO_BUILDING_LEVELS: dict[int, int | None] = {b.value: 0 for b in ids.Building}
_ALL_ZERO_TECHNOLOGY_LEVELS: dict[int, int | None] = {t.value: 0 for t in ids.Technology}


def test_next_step_toward_hand_worked_small_cargo_chain():
    """Small Cargo -> {Shipyard 2, Combustion Drive 2} -> Shipyard's own gate (Robotics
    Factory 2) is shallower than Combustion Drive's own gate (Research Lab 1 AND Energy
    1, itself gated on Research Lab), and Robotics Factory has no requirement of its own
    -- it is the shallowest buildable step, not Shipyard (still locked) and not Small
    Cargo (the final target, several hops further down)."""
    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=_ALL_ZERO_BUILDING_LEVELS,
        technology_levels=_ALL_ZERO_TECHNOLOGY_LEVELS,
    )

    assert step is not None
    assert step.family is EntityFamily.BUILDING
    assert step.entity_id == ids.Building.ROBOTICS_FACTORY
    assert step.depth == 2
    assert [u.requirement.entity_id for u in step.chain] == [ids.Building.SHIPYARD, ids.Building.ROBOTICS_FACTORY]
    assert step.chain[-1].have == 0  # Robotics Factory's own current level, known


def test_next_step_toward_returns_first_step_not_final_target_when_shallower():
    """Once Robotics Factory is already >= 2, Shipyard's own gate is satisfied, so
    Shipyard itself (not Robotics Factory, and not Small Cargo) is the shallowest
    buildable step -- proves the walk returns the *nearest* unmet link, not the deepest
    ancestor and not the original target."""
    building_levels = {**_ALL_ZERO_BUILDING_LEVELS, ids.Building.ROBOTICS_FACTORY: 2}

    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=building_levels,
        technology_levels=_ALL_ZERO_TECHNOLOGY_LEVELS,
    )

    assert step is not None
    assert step.family is EntityFamily.BUILDING
    assert step.entity_id == ids.Building.SHIPYARD
    assert step.depth == 1
    assert step.entity_id != ids.Ship.SMALL_CARGO


def test_next_step_toward_already_unlocked_target_returns_none():
    building_levels = {**_ALL_ZERO_BUILDING_LEVELS, ids.Building.SHIPYARD: 2, ids.Building.ROBOTICS_FACTORY: 2}
    technology_levels = {**_ALL_ZERO_TECHNOLOGY_LEVELS, ids.Technology.COMBUSTION_DRIVE: 2}

    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=building_levels,
        technology_levels=technology_levels,
    )

    assert step is None


def test_next_step_toward_cross_family_walk_can_resolve_to_a_technology():
    """When Small Cargo's Shipyard branch is already satisfied, the only remaining branch
    is Combustion Drive (a technology), which itself needs Research Lab (building,
    already met here) and Energy (technology, not met) -- Energy has no requirement of
    its own beyond Research Lab, so it is the shallowest step, proving the walk correctly
    switches from a BUILDING lookup to a RESEARCH lookup (ReqSource.TECHNOLOGY ->
    EntityFamily.RESEARCH) and back."""
    building_levels = {**_ALL_ZERO_BUILDING_LEVELS, ids.Building.SHIPYARD: 2, ids.Building.RESEARCH_LAB: 1}
    technology_levels = {**_ALL_ZERO_TECHNOLOGY_LEVELS}

    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=building_levels,
        technology_levels=technology_levels,
    )

    assert step is not None
    assert step.family is EntityFamily.RESEARCH
    assert step.entity_id == ids.Technology.ENERGY
    assert [u.requirement.entity_id for u in step.chain] == [ids.Technology.COMBUSTION_DRIVE, ids.Technology.ENERGY]


def test_next_step_toward_defense_target_crosses_into_technology():
    """Small Shield Dome needs Shipyard >= 1 (building) AND Shielding >= 2 (technology).
    With Shipyard already built, Shielding -- which itself needs Research Lab 6 and
    Energy 3 -- is the remaining branch; Research Lab (building, unmet) is shallower than
    Shielding itself, so Research Lab wins."""
    building_levels = {
        **_ALL_ZERO_BUILDING_LEVELS,
        ids.Building.SHIPYARD: 1,
        # Research Lab's own gate (Robotics Factory >= 1) is pre-satisfied so it qualifies
        # directly, rather than the walk needing a fourth hop down into Robotics Factory.
        ids.Building.ROBOTICS_FACTORY: 1,
    }
    technology_levels = {**_ALL_ZERO_TECHNOLOGY_LEVELS}

    step = next_step_toward(
        EntityFamily.DEFENSE,
        ids.Defense.SMALL_SHIELD_DOME,
        building_levels=building_levels,
        technology_levels=technology_levels,
    )

    assert step is not None
    assert step.family is EntityFamily.BUILDING
    assert step.entity_id == ids.Building.RESEARCH_LAB


def test_next_step_toward_absent_level_data_yields_no_confidently_chosen_step():
    """Robotics Factory has no requirement of its own (the base case), but if its *own*
    current level was never reported, proposing "upgrade it by one" is a guess -- the
    walk must refuse it and, finding nothing else resolvable either, return `None` rather
    than act on an `UnmetRequirement(have=None)`."""
    building_levels: dict[int, int | None] = {ids.Building.SHIPYARD: 0}  # Robotics Factory absent
    technology_levels: dict[int, int | None] = {}  # everything absent

    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=building_levels,
        technology_levels=technology_levels,
    )

    assert step is None


def test_real_requirement_graph_is_acyclic():
    """The design brief's own instruction: assert this rather than assume it. Builds a
    directed graph from all four requirement tables (edges point from an entity to each
    of its prerequisites) and runs a recursion-stack DFS looking for a back edge."""
    edges: dict[tuple[EntityFamily, int], set[tuple[EntityFamily, int]]] = {}

    def add_edges(source_family: EntityFamily, table: dict[int, tuple[Requirement, ...]]) -> None:
        for entity_id, requirements in table.items():
            node = (source_family, entity_id)
            for requirement in requirements:
                target_family = EntityFamily.BUILDING if requirement.source is ReqSource.BUILDING else EntityFamily.RESEARCH
                edges.setdefault(node, set()).add((target_family, requirement.entity_id))

    add_edges(EntityFamily.BUILDING, BUILDING_REQUIREMENTS)
    add_edges(EntityFamily.SHIP, SHIP_REQUIREMENTS)
    add_edges(EntityFamily.DEFENSE, DEFENSE_REQUIREMENTS)
    add_edges(EntityFamily.RESEARCH, RESEARCH_REQUIREMENTS)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[EntityFamily, int], int] = {}

    def visit(node: tuple[EntityFamily, int]) -> None:
        color[node] = GRAY
        for neighbor in edges.get(node, ()):
            state = color.get(neighbor, WHITE)
            if state == GRAY:
                raise AssertionError(f"cycle detected: {node} -> {neighbor}")
            if state == WHITE:
                visit(neighbor)
        color[node] = BLACK

    for node in list(edges):
        if color.get(node, WHITE) == WHITE:
            visit(node)


def test_next_step_toward_is_depth_bounded_against_a_synthetic_cycle(monkeypatch):
    """Injects a two-node cycle into a *copy* of the building-requirement table (never
    mutating the real `BUILDING_REQUIREMENTS`/`_TABLES` module objects -- `monkeypatch`
    reverts this automatically at teardown) and asserts the walk still terminates rather
    than hanging, returning `None` because no node in the cycle ever satisfies its own
    `unmet()`."""
    CYCLE_A = 9001
    CYCLE_B = 9002
    cyclic_building_table = dict(BUILDING_REQUIREMENTS)
    cyclic_building_table[CYCLE_A] = (Requirement(ReqSource.BUILDING, CYCLE_B, 1),)
    cyclic_building_table[CYCLE_B] = (Requirement(ReqSource.BUILDING, CYCLE_A, 1),)
    patched_tables = dict(techtree._TABLES)
    patched_tables[EntityFamily.BUILDING] = cyclic_building_table
    monkeypatch.setattr(techtree, "_TABLES", patched_tables)

    step = next_step_toward(
        EntityFamily.BUILDING,
        CYCLE_A,
        building_levels={CYCLE_A: 0, CYCLE_B: 0},
        technology_levels={},
    )

    assert step is None  # never hangs; every node in the cycle stays permanently unmet

    # The real table object is untouched -- `cyclic_building_table` was a shallow copy of
    # `BUILDING_REQUIREMENTS`, never that dict itself, so the module-level table used by
    # every other caller never saw the injected cycle even while this test's monkeypatch
    # of `_TABLES` was active.
    assert CYCLE_A not in BUILDING_REQUIREMENTS


def test_next_step_toward_returns_unlockstep_namedtuple_shape():
    step = next_step_toward(
        EntityFamily.SHIP,
        ids.Ship.SMALL_CARGO,
        building_levels=_ALL_ZERO_BUILDING_LEVELS,
        technology_levels=_ALL_ZERO_TECHNOLOGY_LEVELS,
    )
    assert isinstance(step, UnlockStep)
    assert step.depth == len(step.chain)
