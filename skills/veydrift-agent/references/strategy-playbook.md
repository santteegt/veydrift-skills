# Strategy playbook — deriving a build order for ANY planet

This is the document a human reads to check the planner's reasoning without reading
`plan.py`/`candidates.py` itself. It generalizes an earlier manual, one-off derivation
method ("how to re-derive a strategy for another planet") into the algorithm the planner
actually runs on every tick, for any planet the wallet holds — planet 664 appears in
examples only because it is the account's real planet, never as a special case in code.

**2026-08-16 (Phase 2 of the general-strategy-engine program): the derivation below is
unchanged, but the code that implements it moved.** Rungs 0-4 (vetoes) are still in
`plan.py`. Rungs 5-9's actual entity selection — everything §3-§8 below describe — now
lives in a new module, `src/veydrift_agent/candidates.py`, as a generate/filter/score/
select pipeline: one pure generator per family (`mine`, `energy`, `storage`, `research`,
`ship`, `defense`), a `score_payback` scorer, and a `select_*` function per rung. This is
the same reasoning, restructured — every number and every branch condition below is
still exactly what the code computes; only the function names changed (noted inline where
it matters). See `plan.py`'s own module docstring for the three-band precedence.

**2026-08-16 (Phase 3, the same program): every planet-local entity becomes reachable.**
`candidates.py` gains `crawler` (scored) and a broadened `infrastructure` family (was
reserved/unused since Phase 2), and `ship`/`defense`/`research` gain declared-target
stock-keeping/ordering (`policy.strategy.ship_targets`/`defense_targets`/
`research_priority`/`building_priority`). §8 below is updated inline; the governing
principle — the engine computes legality/affordability/economics, the policy declares
intent for everything else — is stated once at the end of §8 rather than repeated per
family.

If you are reviewing a proposal `vd plan` made and want to know "is this right," this
document plus `references/formulas.md` §9 (the worked energy-source example) should be
enough to check it by hand.

## Table of contents

- [1. The method, in one page](#1-the-method-in-one-page)
- [2. Reading a planet's traits](#2-reading-a-planets-traits)
- [3. The energy-first invariant, in plain language](#3-the-energy-first-invariant-in-plain-language)
- [4. Deriving mine priority: resource value density](#4-deriving-mine-priority-resource-value-density)
- [5. Deriving the energy source: cost per energy point](#5-deriving-the-energy-source-cost-per-energy-point)
- [6. Worked walkthrough: planet 664](#6-worked-walkthrough-planet-664)
- [7. Worked walkthrough: a hot planet, and why it inverts](#7-worked-walkthrough-a-hot-planet-and-why-it-inverts)
- [8. The full ladder, rung by rung](#8-the-full-ladder-rung-by-rung)
- [9. There is no exit — why compounding is the only strategy here](#9-there-is-no-exit--why-compounding-is-the-only-strategy-here)
- [10. What is unobserved, and which planner paths that leaves untested](#10-what-is-unobserved-and-which-planner-paths-that-leaves-untested)
- [11. Checklist: sanity-checking a proposal by hand](#11-checklist-sanity-checking-a-proposal-by-hand)
- [12. Working out the build-up: the unlock-chain rung](#12-working-out-the-build-up-the-unlock-chain-rung)

---

## 1. The method, in one page

This codebase's earlier research gave twelve manual steps for re-deriving a strategy for a
new planet or account. `plan.py` is that method turned into code that runs unattended. The
correspondence:

| Manual derivation step | `plan.py` / `calc.py` equivalent |
| --- | --- |
| 1-2. `/runtime-config`, `/health` | Not this module's job — `read.py` (WP1) gates on these before a `Snapshot` ever reaches `plan_next_action` |
| 3. `/wallet/{addr}/settlement` (coords, fields, temperature, multipliers) | `PlanetSnapshot.temperature`, `.metal_multiplier_bps`, `.crystal_multiplier_bps`, `.deuterium_multiplier_bps` |
| 4. `/wallet/{addr}/infrastructure` (levels, live costs, energyBalance) | `PlanetSnapshot.buildings`, `.energy` |
| 5-6. `/research`, `/shipyard` | `Snapshot.technologies`, `PlanetSnapshot.ships` |
| 8. Invert `maxTemp` from `deutMultBps`, cross-check | `calc.max_temp_from_bps` — a diagnostic only; `plan.py` reads `PlanetSnapshot.temperature` directly and never needs the inversion |
| 9. Read `energyBalance.sources.solarSatelliteEnergy` — "decides the whole energy strategy" | `candidates._satellite_energy_per_unit`: prefers `PlanetSnapshot.energy.solar_satellite_energy` (live), falls back to `calc.solar_satellite_energy(temperature)` only if absent |
| 10. Generate the energy-crossover table | `calc.solar_crossover_table` — see `references/formulas.md` §8 |
| 12. Re-run the three duration checks, confirm universe speed | `vd calc verify` |

Steps 8-9 are, in the original method's own words, "the ones that produce
planet-*specific* advice rather than generic advice." `plan.py`'s two core derivations
(§4 and §5 below) are exactly steps 8-9, generalized: read a planet's temperature and its
live multipliers/energy yield, and let *those numbers* — not a planet id — decide the
opener.

## 2. Reading a planet's traits

Every derivation in this document starts from four numbers, all on `PlanetSnapshot`:

- `temperature` (°C, planet maximum) — drives everything else.
- `deuterium_multiplier_bps` — `12_800 - temperature*20`, clamped at 0
  (`calc.deuterium_multiplier_bps`; `references/formulas.md` §2). Metal and crystal
  multipliers are **always** 10_000; there is no per-planet metal or crystal bonus.
- `energy.solar_satellite_energy` — the live per-satellite energy yield
  (`clamp(trunc((temp+140)/6), 1, 65)`, but read live rather than recomputed — §3 of this
  document explains why that matters).
- current building/ship levels — `buildings`, `ships` (for the current Solar Plant level
  and Solar Satellite count).

Two planets with the same temperature will get the same opener; two planets with
different temperatures can get opposite openers even at identical building levels
(§7 proves this with a paired fixture).

## 3. The energy-first invariant, in plain language

**The rule:** before proposing to upgrade any mine, compute what energy that upgrade
would *require*, and compare it to what the planet currently *produces*. If the upgrade
would push requirement past production, don't propose the mine — propose an energy
building instead.

**Why "at the post-upgrade level," not the current level:** a mine that is *currently*
energy-safe can become energy-unsafe the moment it goes up one level, because required
energy grows faster than produced energy holds steady. Checking the current state and
approving the next upgrade on that basis is checking the wrong thing.

**Why not a fixed offset ("keep Solar Plant 2 levels above your highest mine")?** Because
the true gap is not constant. `references/formulas.md` §8's crossover table, generated by
running the actual formula, shows the gap widening from +2 levels at mine level 3 to +5
at mine level 14. A rule tuned on early-game numbers looks correct for a while and then
fails exactly when the stakes are highest — a scaled-down mine at level 14 wastes far
more resource than one at level 3. This was a mistake the original manual analysis itself
made and had to correct once the full table was generated; `plan.py` avoids repeating it
by never hand-tuning an offset at all.

**The consequence that surprises people:** because `scaled_level(10, 1) == 11` while
`scaled_level(20, 0) == 0`, a planet with *zero* buildings already fails the check on its
very first proposed mine upgrade (Metal Mine level 0->1 needs 11 energy; Solar Plant at
level 0 produces 0). This is not a bug — it is why the acceptance test calls this "an
energy-first *opener*": on a fresh planet, the very first proposal `plan.py` makes is a
Solar Plant, not a mine. `tests/test_plan.py::test_planet_664_energy_first_opener_never_proposes_satellite`
runs this exact scenario against planet 664's real, current (all-zero) state and asserts
the first proposal is `startBuildingUpgrade(664, SolarPlant)` to level 1.

## 4. Deriving mine priority: resource value density

Once energy is not the binding constraint, which mine goes next? `plan.py` ranks Metal,
Crystal and Deuterium mines by **value density**: the contract's own base production rate
for that mine (30 / 20 / 10 per scaled level — [VeydriftFormulas.sol:70-72](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/libraries/VeydriftFormulas.sol#L70-L72)) multiplied
by this planet's *live* multiplier for that resource.

```
density(Metal)      = 30 * metal_multiplier_bps       (always 30 * 10_000 = 300_000)
density(Crystal)     = 20 * crystal_multiplier_bps     (always 20 * 10_000 = 200_000)
density(Deuterium)  = 10 * deuterium_multiplier_bps   (varies with temperature)
```

The mine chosen next is the one with the lowest `(current_level + 1) / density` — i.e.
the resource this planet is comparatively best at, weighted against how much is already
invested in it, so the ranking doesn't just pick the same mine forever. This is what lets
a deuterium-rich cold planet's opener lean toward Deuterium Synthesizer earlier than a
1×-multiplier planet's opener would — an earlier, purely qualitative finding ("push
Deuterium Synthesizer earlier than a generic opener would") generalized here into an
actual ranking rule, not just restated.

On planet 664 specifically: density(Metal)=300,000, density(Crystal)=200,000,
density(Deuterium)=150,200 (at the live multiplier 15,020 bps). Metal still ranks first
at level 0 — the deuterium lean does not overtake crystal outright at this account's
current (zero) levels, but it sits much closer to crystal's density than it would on a
1×-multiplier planet (where deuterium density would be 100,000, half of crystal's — a
much wider gap). The lean is real and derived from the live multiplier; it is not an
overstated "deuterium always wins here" claim.

**Breaking an exact tie.** `(current_level + 1) / density` can land on the identical
float for two mines — e.g. Metal level 14 and Crystal level 9 at 1x multipliers both
score `5.00e-4` (`(15)/30 == (10)/20`), and this recurs any time the two mines' levels
happen to sit at the corresponding ratio, not just that one pair. Rather than let that
default to Python's dict-declaration order (which always favored Metal Mine, an
incidental artifact never a deliberate rule), the walk breaks an exact tie by each
tied mine's own already-computed payback score (`score_payback`, weighted by
`policy.strategy.resource_weights` — the same number `Action.alternatives` already
displays) — ascending, so the cheaper-to-recoup mine wins. This is a genuinely narrow
exception, not a backdoor into letting payback drive the whole ranking: it only ever
fires on an exact tie, using a number already computed for the same family, never an
invented cross-family judgement. One side effect worth knowing: if the *other* tied
mine is currently energy-blocked, this now lets the safe one win directly as a mine,
instead of the blocked one winning by dict-order luck and forcing an energy-substitute
proposal (Solar Plant/Satellite) that a live tie-break would have avoided. One accepted
gap: the winning mine's rationale text doesn't currently say a tie was broken this way —
if you're explaining a proposal to a user and it names a mine that doesn't obviously
have the lowest raw density, check whether it tied with another mine before assuming
something's wrong.

**Where `resource_weights` actually decides something, in full.** This mine tie-break is
one of exactly three places `policy.strategy.resource_weights` ever changes *which*
candidate wins, rather than just a displayed number in `alternatives`:

1. **This exact mine tie**, above.
2. **Multiple simultaneously-locked declared targets** — when more than one
   `ship_targets`/`defense_targets`/`research_priority` entry is locked at once, the
   cheapest weighted unlock step across all of them wins the unlock-chain rung; §12 has
   the mechanism.
3. **Crawler vs. Solar Satellite, only once `policy.strategy.enable_crawler` is on** — §8's
   rung 8 covers this.

Everywhere else — research selection, `building_priority`'s infrastructure walk, defense
selection, Fusion Reactor's displayed payback score in the ordinary economic band, and a
mine that *isn't* tied with another — `resource_weights` still gets computed and shown in
`alternatives` for context, but never changes the winner. Setting `deuterium: 3` expecting
the planner to broadly favor deuterium-producing picks won't do that; it only ever bites
in the three places above.

## 5. Deriving the energy source: cost per energy point

When the energy-first check *does* fire, `plan.py` chooses among Solar Plant, Solar
Satellite, and Fusion Reactor (three-way as of this skill's `CHANGELOG.md`'s `1.6.2`
entry) by comparing **cost per unit of energy gained**, using only live-served costs:

- Solar Plant's marginal cost-per-energy for its next level: live `cost` (metal+crystal)
  for that level, divided by `scaled_level(20, L+1) - scaled_level(20, L)`.
- Solar Satellite's cost-per-energy: its live per-unit `cost`, divided by the live
  `solar_satellite_energy_per_unit`.
- Fusion Reactor's cost-per-energy: live `cost` (metal+crystal+deuterium) for its next
  level, plus 24 hours of its own ongoing deuterium upkeep delta (see below), divided by
  `calc.fusion_energy(L+1, energy_tech) - calc.fusion_energy(L, energy_tech)`.

Whichever is cheapest wins. The full numeric derivation, including a generated table of
Solar Plant's marginal cost climbing from 4.77 at level 0->1 to 211.88 at level 15->16,
is in `references/formulas.md` §9 — it is not repeated here because that document is
where the numbers should live; this document is where the *reasoning* should live.

**Why this inverts on a hot planet:** a Solar Satellite's cost is flat regardless of
level (ships don't scale with count the way buildings scale with level). Solar Plant's
marginal cost grows without bound as level increases. The two curves must cross
somewhere, and *where* they cross depends entirely on `solar_satellite_energy_per_unit` —
high on a hot planet (satellites deliver more energy per unit, so a lower Solar Plant
level is enough to make them the cheaper option), low on a cold one. Planet 664's
satellite energy is 4; the hot fixture's is 30. That one number is the entire reason the
same code path produces different answers for Solar Plant vs. Solar Satellite between
those two fixtures.

**Fusion Reactor is not temperature-driven the way that inversion is.** Its cost and
energy output depend only on its own level and `energy_technology_level` — never on a
planet's multipliers or `solar_satellite_energy_per_unit` — so whether it wins is purely
a question of build progress (is it unlocked: Deuterium Synthesizer >= 5, Energy
Technology >= 3) and its own level, not planet traits. Unlike Solar Plant and Solar
Satellite, it also carries an ongoing operating cost — deuterium upkeep,
`calc.fusion_deuterium_upkeep`, recurring every hour it exists — so a raw one-time-cost
comparison would favor it unfairly against two options with no such cost. It's amortized
over a fixed, documented constant, `_ENERGY_UPKEEP_AMORTIZATION_HOURS = 24`, before
comparison: `(one_time_cost + upkeep_delta_per_hour * 24) / energy_gained`. This window
is a deliberate choice, not an arbitrary one that happens not to matter — on the hot
fixture (§7 below) a 7-day window would have flipped the winner back to Solar Satellite.

## 6. Worked walkthrough: planet 664

Real, current, zero-state data (`tests/fixtures/planet_664.json`, captured live
2026-08-12). It is zero-state because this account has taken no actions since settlement.

1. Health ok, no killswitch, no pending tx, no resolvable mission, no incoming fleet —
   rungs 0-4 all pass through.
2. No resource is near its storage cap (production is 0/hr everywhere at zero state) —
   rung 5 does not fire.
3. Building queue is idle — rung 6 fires.
4. Mine priority (§4): Metal ranks first (density 300,000 vs. crystal 200,000, deuterium
   150,200; all at level 0, so the ranking is density order directly).
5. Energy-first check (§3): Metal Mine 0->1 would require `scaled_level(10,1) = 11`
   energy; the planet currently produces 0. **11 > 0 — the mine upgrade is blocked.**
6. Energy-source choice (§5): Solar Plant 0->1 costs 105 (metal+crystal) for 22 energy
   gained (4.77/energy). Solar Satellite costs 2,500 for 4 energy (625/energy). Solar
   Plant is ~130x cheaper per energy point.
7. **Result:** `startBuildingUpgrade(664, SolarPlant)`, target level 1. Never a Solar
   Satellite proposal, at this level or (per §5's crossover analysis) at any level this
   account is likely to reach.

## 7. Worked walkthrough: a hot planet, and why it inverts

`tests/fixtures/planet_hot.json` is a synthetic fixture (planet id 900001, temperature
40 °C, `archetype: "scorching-molten"` per the archetype values this project's research
has observed live) built specifically to sit *past* the Solar-Plant-vs-satellite
crossover, so the counterfactual is not trivial. It also happens to have Fusion Reactor
unlocked (Deuterium Synthesizer 11 >= 5, Energy Technology 5 >= 3), which is why this is
the fixture §5's three-way energy-source comparison is pinned against:

1. Buildings: Solar Plant level 15, Metal/Crystal/Deuterium mines all level 11, Fusion
   Reactor level 0 (unlocked but not yet built).
2. Currently: `produced = scaled_level(20, 15) = 1,253`; `required` at mines 11/11/11 =
   `scaled_level(10,11)*2 + scaled_level(20,11) = 1,253` — exactly balanced, energy-safe
   right now.
3. Mine priority (§4): with metal/crystal multipliers always 10,000 and this planet's
   deuterium multiplier at 12,000 (`12_800 - 40*20`), Metal still ranks first — the same
   ranking logic as 664, different numeric inputs.
4. Energy-first check: Metal Mine 11->12 would push required to 1,316 > 1,253 produced.
   **Blocked, same as 664's very first check** — the mechanism is identical.
5. Energy-source choice (three-way, §5): Solar Plant 15->16 costs 45,978 for 217 energy
   (211.88/energy). Solar Satellite costs 2,500 for 30 energy (83.33/energy) — cheaper
   than Solar Plant, the inversion §5 describes. Fusion Reactor 0->1 costs 1,440 for 33
   energy one-time (43.64/energy); amortizing 24h of its upkeep delta (11 deuterium/hour)
   makes it 1,440 + 11*24 = 1,704, for 51.64/energy. **Fusion Reactor is cheapest of the
   three, by a wide margin** — 1.6x cheaper than Satellite, 4.1x cheaper than Solar Plant.
6. **Result:** `startBuildingUpgrade(900001, FusionReactor)`, target level 1.

Two counterfactuals worth naming explicitly, since they don't come up on this fixture's
own numbers: with the amortization window widened to 7 days (168h) instead of 24h,
Fusion Reactor's cost becomes `(1,440 + 11*168) / 33 = 99.6/energy`, which *would* lose
to Satellite's 83.33 — confirming the 24h choice in §5 is genuinely load-bearing, not
cosmetic. And with Fusion Reactor locked (Energy Technology < 3, the isolated variable
`tests/test_plan.py::_fusion_locked_hot_planet` uses), the comparison degrades back to
the original two-way Solar-Plant-vs-Satellite result this section used to describe.

`tests/test_plan.py::test_matched_building_levels_isolate_temperature_as_the_only_variable`
goes one step further than fixture comparison: it takes the *real* 664 fixture and
overwrites only its building levels to match the hot planet's (Solar Plant 15, mines
11/11/11), leaving temperature, multipliers and satellite energy untouched at 664's real
cold values. The planner still proposes Solar Plant, not a satellite — proving the
inversion in §7 is driven by temperature alone, not by how far along the mines happen to
be.

## 8. The full ladder, rung by rung

This codebase's decision ladder, implemented exactly, first match wins
(`plan.plan_next_action`). Rungs 0-4 are vetoes, unchanged since before Phase 2. Rungs
5-9 are described here exactly as before (same numbers, same branch conditions) — as of
Phase 2 they are driven by `candidates.py`'s `select_storage_candidate` /
`select_building_candidate` / `select_research_candidate` / `select_shipyard_candidate`
rather than by four standalone functions in `plan.py`; each function name below is noted
where it moved.

0. **KILLSWITCH present -> HALT.** Not read from `$VEYDRIFT_HOME` by this module —
   `killswitch_active` is a parameter `tick.py` (WP3) is expected to pass in, since
   filesystem/state concerns belong to `state.py`, not `plan.py`.
1. **`/health` not ok -> NO-OP.** Reads `Snapshot.health_ok` directly.
2. **Pending tx unreconciled -> NO-OP.** Same pattern as rung 0: `pending_tx_unreconciled`
   is caller-supplied, since reconciliation state lives in `agent-state.json`
   (WP3's `state.py`), not on `Snapshot`.
3. **Mission Resolving > 60s -> `resolveFleetMission`.** See §10 — this rung is
   implemented but cannot fire today; the frozen `Snapshot` model has no field for the
   player's own in-flight missions.
4. **Incoming hostile fleet -> ESCALATE.** Reads `Snapshot.incoming_fleets`, filtered to
   `hostile=True`, gated on `policy.escalation.on_incoming_fleet`.
5. **Resource within N hours of cap -> spend it, or build storage.** Computes
   `calc.hours_to_cap` for every resource on every target planet; the most urgent one
   (if within `policy.storage.hours_to_cap_trigger`) determines the action.
   - If the building queue is **busy**, this rung proposes nothing at all and falls
     through. The contract allows only one active `BuildingConstruction` per planet
     (`buildingConstructions[planetId].active` -> `ConstructionActive` revert,
     [VeydriftGame.sol:117-138](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol#L117-L138)) — a second `startBuildingUpgrade` while one is already
     in flight would be a guaranteed-revert proposal, whether it's "spend it" or the
     matching storage building. Rung 6 will independently see the same busy queue and
     also not fire; the ladder either finds something else to do further down (research,
     shipyard) or reaches an honest rung-9 NOOP that at least does not propose a doomed
     transaction.
   - If the queue is **idle**, "spend it" reuses the exact §4/§5 derivation (the same
     mine-or-energy pick rung 6 would make) — starting *any* building upgrade deducts
     cost from current holdings, which generically helps drain whichever resource is at
     risk regardless of which specific mine gets upgraded. Only if that derivation finds
     nothing to propose does the rung fall back to upgrading the matching storage
     building for the specific resource that is at risk.

   This is a documented interpretation of the SPEC's terse rung 5 text ("spend it, or
   build the matching storage"), not a literal one — see the docstring on
   `candidates.select_storage_candidate` (moved from `plan._storage_overflow_action` in
   Phase 2) for the reasoning in full. An earlier version of this rung attempted the
   storage-building fallback even when the queue was busy, which would have produced a
   proposal that reverts on submission; caught while writing this document and fixed
   before shipping (`tests/test_plan.py::test_storage_overflow_with_busy_queue_proposes_nothing_unsafe`).
   Note that this band is deadline-driven, not economically scored — every candidate
   `candidates.generate_storage_candidates` produces carries `score=None`.
6. **Building queue empty -> next build.** §3-§5's derivation, run per target planet in
   policy order. As of Phase 2, `candidates.select_building_candidate` is the entity
   picker: `candidates.generate_mine_candidates` never generates a mine candidate that
   fails the energy-first check (§3) at all — the energy substitute
   (`candidates.generate_energy_candidates`) is what fills that gap — and each generated
   mine/energy candidate is scored (`candidates.score_payback`, payback hours) whenever
   the level change actually moves `calc.production_per_hour`'s output. Runner-ups
   populate the proposal's `alternatives` — informational only, never read by `guard.py`
   or any `Decision` logic. **Phase 3** adds three things to this rung, none changing the
   winner when unconfigured:
   - `generate_energy_candidates` also scores **Fusion Reactor** (locked/scored the same
     way as Solar Plant). Originally the energy-first *substitution* comparison
     (`_cheapest_energy_choice`) compared only Solar Plant vs. Solar Satellite, so Fusion
     Reactor could only ever win as an ordinary scored candidate — since this skill's
     `CHANGELOG.md`'s `1.6.2` entry (2026-08-27), that substitution comparison is
     three-way and Fusion Reactor can win the substitution directly too, its own cost
     amortized over a fixed 24h window of its ongoing deuterium upkeep first (§5 has the
     full derivation).
   - `generate_proactive_storage_candidates` adds storage as an always-`score=None`
     candidate, visible before the reactive overflow trigger (rung 5) would ever fire.
     **Fix, 2026-08-21:** it's no longer purely informational. If the mine/energy (or
     `building_priority`) pick that would otherwise win this rung costs more than the
     planet's *current* storage cap for a resource it needs — a cost that can never be
     saved up to, not merely one current resources don't cover yet — the matching
     proactive-storage candidate takes its place instead (or, if none is available, the
     capped pick is skipped in favour of the next one, same as an energy-unsafe mine).
     Before this fix `guard.py`'s `_gate_affordability` would BLOCK such a pick forever
     ("never affordable: cost exceeds storage cap") while the ladder kept re-proposing it
     every tick.
   - **Fix, dated (see CHANGELOG):** a distinct, narrower gap in the same family —
     a pick that fits comfortably under the storage cap can still be one current
     holdings simply don't cover *yet* (a plain resource shortfall, not a permanent
     ceiling). `_resolve_affordability_precondition`, composed with the storage-cap
     check above by `_resolve_building_preconditions`, applies the same "demote and try
     the next candidate" treatment to this case — there's no single substitute building
     to offer here (unlike the storage-cap case), so falling through to the next-ranked
     mine/energy/`building_priority` pick is the fix itself. For a mine walk ordered by
     value density, that naturally tends to land on whichever mine produces the resource
     actually in short supply (e.g. a crystal shortage blocking a metal-dense top pick
     resolves to the crystal mine next in line), without this module ever needing to
     identify a "bottleneck resource" as its own concept. `guard.py`'s
     `_gate_affordability` is unchanged and remains the authoritative, independent final
     check — this is a planning-layer improvement, not a relaxation of that gate.
   - If `policy.strategy.building_priority` is set, `select_building_candidate` checks
     `generate_infrastructure_candidates` (Robotics Factory, Nanite Factory, Shipyard,
     Research Lab, Terraformer, Missile Silo, `score=None`, in declared order) **first**,
     ahead of the mine walk — an explicit `building_priority` is a declared human intent
     and wins outright, per this phase's governing principle (below). Left unset, this
     never fires. **Footgun, asymmetric with every other declared-name field in this
     policy:** a genuinely unrecognized name still hard-errors the next tick, same as
     `ship_targets`/`defense_targets`/`research_priority` — but name resolution here
     isn't restricted to the six infrastructure buildings, so a *correctly spelled*
     building name outside that set (e.g. `"Metal Mine"`) resolves fine and then gets
     silently filtered out of `_infrastructure_priority_order` — no error, no candidate,
     nothing logged. Only the six infrastructure names above do anything in this field.
7. **Research queue empty -> next research.** Deliberately the least-derived rung in this
   module: filtered through `techtree.unmet()` (a locked candidate is skipped in favour
   of the next unlocked one). This is *not* as rich as the energy invariant on purpose —
   the SPEC's rung 7 only asks for "next research," not a tech-tree strategy.
   `candidates.select_research_candidate` (Phase 2) always scores a research candidate
   `None` — nothing in `calc.py` models a technology moving `production_per_hour`.
   **Phase 3**: if `policy.strategy.research_priority` names technologies
   (case-insensitive), those are tried first, in declared order; everything not named
   becomes the *fallback*, and its `score_basis` is explicitly prefixed `"default: ..."`
   so a reader can tell "this is the fallback" from "this is what the operator asked for"
   at a glance. **Dated correction (see this skill's `CHANGELOG.md`'s `1.4.0` entry)**: the fallback used to be pure
   lowest-current-level-account-wide, ties broken by ascending id (Phase 2's original
   ordering) — it's now ranked by `techtree.unlock_breadth` descending instead (fully-
   unlocked-count first, partial-advance count as tiebreak, level then id only as the
   final tiebreak), so a level-up that actually opens something up (e.g. Energy
   Technology reaching the level Laser Technology needs) is preferred over one that
   doesn't, computed purely from `techtree.py`'s already-verified requirement tables —
   never an invented value judgement. `generate_infrastructure_candidates`'s own
   undeclared-tail ordering (point above, "in declared order") got the identical
   treatment at the same time.

   **Neither `research_priority` nor `building_priority` round-robins through a
   multi-name list.** Both always propose the first declared name that's currently
   reachable, and keep re-proposing further levels of that *same* entry indefinitely —
   there's no "build it once, then move to the next name" logic, because neither field
   carries a target level or count to complete against (unlike `ship_targets`/
   `defense_targets`'s `count`, which genuinely does advance). A level-up never
   un-satisfies its own prerequisites, so the planner has no structural reason to cede
   the slot to the next declared name — it only moves on if the first entry itself
   becomes locked. Declaring `research_priority: ["Energy Technology", "Espionage
   Technology"]` locks research onto Energy Technology permanently, never touching
   Espionage Technology, until the list is edited by hand. Treat both fields as "my #1
   priority, with the rest as fallback names for if #1 ever becomes locked," not as a
   build order the planner works through.
8. **Shipyard idle AND economy on track -> ships/defense per policy.** Fires only if
   `policy.actions.allow_ships` or `allow_defense` is true (both default `false` in
   `assets/policy.example.json`, so this rung rarely fires in practice) and something
   else (building or research) is already actively progressing
   (`candidates.economy_on_track`). If ships are allowed and a satellite is currently the
   cheaper energy source on this planet (§5, `candidates.generate_ship_candidates`),
   proposes one; if defense is allowed, proposes the cheapest defense entry (Rocket
   Launcher, `candidates.generate_defense_candidates`, always `score=None`) as a
   policy-driven default in the absence of any threat model. **Phase 3** adds two ship
   candidates and a defense-target mechanism, all reachable only via explicit policy:
   - **Crawler** (`candidates.generate_crawler_candidates`) — **gated on
     `policy.strategy.enable_crawler` (default `false`); ungated, this generator returns
     `[]` entirely**, not just an unscored candidate — reachability, not preference. This
     is a separate switch from naming `"Crawler"` in `ship_targets` below: that path
     always stock-keeps a declared count regardless of this flag. Once enabled, this is
     the one *scored* addition here, via `calc.crawler_boost_bps`'s marginal effect on
     `calc.production_per_hour`; its weighted payback then competes directly against
     Solar Satellite's for the shipyard slot (`resource_weights` genuinely picking a
     winner here, not just tie-breaking — see §4's consolidated note). The formula's own
     caps (8 per combined mine level, 5,000 bps total) mean a saturated crawler count
     scores `None` automatically; the live `PlanetSnapshot.crawler_production.capped`
     flag short-circuits the same conclusion without recomputing, when the API reports
     it.
   - **`policy.strategy.ship_targets`** (`candidates.generate_ship_target_candidates`) —
     stock-keeping toward a declared standing count for *any* of the 16 ships, filtered
     through `techtree.unmet()`. This never touches Solar Satellite's separate
     energy-driven path above; naming Solar Satellite in `ship_targets` stock-keeps it
     as an ordinary policy-declared ship, independent of the energy mechanism. Among
     everything `generate_ship_candidates` yields for a planet, the best-*scored*
     selectable candidate wins (falls back to generation order — satellite first — when
     nothing is scored, preserving pre-Phase-3 priority when nothing new is configured).
     An unrecognized `name` in `ship_targets` is a hard error the next tick (`ValueError`
     from name resolution) — the same "a typo must never mean silence" posture the rest
     of `policy.json` takes. **Footgun:** a target is only ever compared as `entity.count
     >= target.count` — a negative `count` makes that trivially true, so the entry is
     silently treated as already met and never proposed toward, forever, with nothing
     logged to say so.
   - **`policy.strategy.defense_targets`** (`candidates.generate_defense_target_candidates`)
     — the same shape, same name-resolution/negative-count rules, for *any* of the 10
     defenses, plus `techtree`'s hard caps: shield domes at 1 built+queued per planet,
     missiles against `missile_silo_level * 10` slots (ABM 1 slot, Interplanetary Missile
     2). **Declaring `defense_targets` entirely replaces the old hardcoded Rocket
     Launcher default** — a human who states explicit defense intent has superseded the
     "reasonable policy-driven default in the absence of a threat model" the pre-Phase-3
     comment describes. Left unset, the old default fires exactly as before — an
     asymmetry from `ship_targets`, which is simply off when empty, worth knowing before
     assuming "empty list" means the same thing on both fields.
8b. **(Phase 4, 2026-08-16) Nothing above fired -> propose the unlock chain.** New rung,
   `candidates.select_unlock_chain_candidate`, checked only after rungs 5-8 above have all
   found nothing at all for every target planet. For every *locked* declared
   `ship_targets`/`defense_targets`/`research_priority` entry, walks
   `techtree.next_step_toward` and proposes the shallowest currently-buildable
   prerequisite instead of the (still-locked) target itself — see §12 for the full
   mechanism and a worked example. Always `score=None`. Deliberately placed *last*, not
   folded into rung 6's `building_priority` precedence: an unlock step must never outrank
   the storage-overflow deadline (rung 5) and must never displace a scored economic
   candidate (rung 6) or a policy-declared research/ship/defense pick (rungs 7-8) — giving
   it the final rung makes that a property of the ladder's control flow, not a flag this
   function has to remember to check.
8c. **(Phase 5c, 2026-08-17) Nothing above fired -> propose non-combat fleet logistics.**
   New rung, `candidates.select_logistics_candidate`, checked only after rungs 5-8b above
   have all found nothing for every target planet — same "never outrank a scored
   economic pick or the storage-overflow deadline" precedence rule rung 8b already uses,
   extended by one more rung. Two generators, first selectable one per planet wins:
   - **Transport** (`generate_transport_candidates`): moves whichever resource is
     furthest above `policy.reserves` on the origin planet to whichever other own planet
     currently holds the least of it, using already-built cargo-capable ships only.
     Bounded by `calc.available_cargo` (capacity minus `calc.mission_fuel`'s fuel cost),
     never by surplus alone.
   - **Harvest** (`generate_harvest_candidates`): the contract's own local special case
     (`originPlanetId == targetPlanetId`) — a planet's own debris field, never a foreign
     one. Requires an already-built Recycler. Not live-reachable today: no caller wires a
     live debris source into it yet (the frozen `Snapshot` carries none, and the one live
     route that might is unconfirmed in shape — see the generator's own docstring).
   - Both gated, independently, on `policy.actions.allow_fleet_noncombat` (default
     `false`) — with the default policy this rung never fires, same safety property
     every earlier phase's new rung shipped with.
   - Always `score=None`, same reasoning as rung 8b: this is an opportunity to use idle
     capacity, not something comparable to `calc.production_per_hour`'s payback-hours
     scoring.
9. **Otherwise -> NO-OP with an explicit reason.** Always reachable; `Action.rationale`
   is never empty.

**Phase 3's governing principle, stated once here:** *the engine computes what is legal
(`techtree.unmet()`), affordable (`guard.py`, unchanged) and economically comparable
(`score_payback`, where a level change genuinely moves `calc.production_per_hour`'s
output); the policy declares intent for everything else.*
This module is deliberately not a fleet doctrine or a threat model — `ship_targets`/
`defense_targets` never choose *how many* of something to want, only whether the
declared count is legal and, for ships, how it ranks against other scored options.

## 9. There is no exit — why compounding is the only strategy here

Planets are not transferable — no `transferPlanet`, no NFT, ownership is a plain struct
field (`_planets[planetId].owner`). What sharpens this for planet 664 specifically:
`abandonPlanet` reverts with `CannotAbandonHomePlanet` when the target is the caller's
home planet —

```solidity
// packages/contracts/src/VeydriftPlanetManagementModule.sol:150
if (homePlanetOf[msg.sender] == planetId) revert CannotAbandonHomePlanet();
```

This wallet's only planet **is** its home planet (single planet, no colonies). So neither
transfer nor abandonment is available — not because it's unwise, but because the contract
makes both unreachable for this specific account (abandoning any *other, non-home* planet
would work fine; there just isn't one).

The practical consequence for strategy: growing this planet's economy is not merely the
best available option among several — it is the only one the contract permits. There is
no "cut losses and start over" path short of handing over the private key entirely, which
is custody transfer, not strategy. This reinforces every recommendation in this document:
the account has an unbounded time horizon on a single planet, which is exactly the
condition under which compounding growth (energy-safe mines now, position for research and
Astrophysics-driven colonization later) dominates any short-term optimization.

## 10. What is unobserved, and which planner paths that leaves untested

**Correction — this section originally described the account as zero-state; that is no
longer true and hasn't been for a while.** It used to open with "the account has taken
zero on-chain actions... every level 0... unchanged since settlement," matching this
project's very first observation of it. On-chain levels read directly from the deployed
contract since then: Metal Mine 10, Crystal Mine 9, Deuterium Synthesizer 5, Solar Plant
11, Robotics Factory 2, Shipyard 1, Research Lab 1, Energy Technology 2, Computer 0 — the
account has been played by hand through the game UI. Separately, and more directly
relevant to this document's own claims: this codebase has since submitted real
transactions to mainnet itself, through its own `build → simulate → send` path, at tier 2
(`economy`) and tier 3 (`operator`) — not fixtures, not a fork. What that changes,
narrower than the original three items below claimed:

1. **Cost scaling above level 0 — still genuinely unverified.** `vd calc verify`
   cross-checks three duration formulas (Energy Technology research, Small Cargo ship
   production, Metal Mine building) against live API data at the account's current,
   non-zero level, and passes — but that verifies *duration*, not *cost*. No
   per-building cost-scaling *factor* has been independently observed or reconstructed
   by this codebase at any level; live cost is always read as an opaque number from the
   API, by design (the hard constraint in `references/formulas.md`). This remains
   intentional, not a gap: the point of never recomputing cost is that it is impossible
   for this assumption to go stale.
2. **Queue behavior under load — observed at least once, not generally.** A local Anvil
   fork run of this codebase's own send path populated and later lazily settled a real
   queue above level 0 (`startBuildingUpgrade`, Metal Mine 10 → 11), with
   `calc.build_seconds` matching the chain's own resolved duration exactly (1556s) — the
   first time this system, not a human through the UI, watched a queue actually behave
   this way. That is one selector, observed once, on a fork seeded from real chain
   state — not confirmation that every queue kind (research/ship/defense) and every
   settlement path behaves identically at every level.
3. **Lazy settlement — same caveat as above, not "never observed."** Confirmed real for
   `startBuildingUpgrade` specifically, via that same fork run: no separate
   `finishBuildingUpgrade` call was needed. Whether `resources_as_of_now` vs. `resources`
   (both present on `PlanetSnapshot`) ever meaningfully diverges in practice hasn't been
   independently re-confirmed since; `plan.py` uses `resources_as_of_now` throughout per
   the model's own guidance ("prefer this for affordability checks") regardless.

**Concretely, which `plan.py` code paths this leaves untested against live state:** the
table below predates the correction above and reflects what was independently confirmed
at the time each row was last verified — real tier 2/3 mainnet sends mean `plan_next_action`
has since run against real, progressed live state on at least some ticks, but nothing
here catalogs which specific rungs fired on which one, so treat every "untested against"
cell as "not independently reconfirmed by this document since," not as "definitely never
happened at all."

| Code path | Tested against | Untested against (by this document, specifically) |
| --- | --- | --- |
| Energy-first invariant (rungs 5-6, §3-§5) | Real planet 664 (level-0 case, §6) + fixtures (progressed levels, §7) | This account's current progressed levels — the level-0 case was the only live data point this document itself worked from |
| Mine priority ranking (§4) | Fixtures only (multiple temperatures) | Whether a real account's multi-resource holdings ever make "spend it" (rung 5) diverge from "build the matching storage" in a way only observable at higher levels |
| Storage overflow (rung 5) | Synthetic fixtures with hand-set `resources_as_of_now`/`production_per_hour` | A real planet ever approaching a storage cap — this account's production was 0/hr for the entire period this document was written against; whether that's still true at its current progressed levels is unconfirmed here |
| Research selection (rung 7) | Fixture with all-zero tech levels (tie-break only) | Any scenario with mixed tech levels — this account's own tech levels are no longer all-zero (see above), but rung 7's behavior against them hasn't been independently reconfirmed by this document |
| Shipyard rung (rung 8) | Fixture only, `allow_ships`/`allow_defense` forced `true` for the test | Real economy-on-track detection, still unconfirmed by this document |
| Mission-resolving rung (rung 3) | Not tested against real data at all — see below | Everything; the rung cannot fire without data `Snapshot` does not carry |

**Rung 3 (`resolveFleetMission`) is a documented gap, not a silent omission.** The frozen
`Snapshot` model carries `incoming_fleets` (for rung 4's hostile detection) but no list of
the player's *own* fleet missions and their status, so there is nothing to check
"Resolving > 60s" against. `plan_next_action` accepts `resolvable_mission_ids` as an
explicit caller-supplied parameter (default empty) specifically so the rung is
implemented and ready — `tests/test_plan.py::test_resolvable_mission_takes_priority_over_building`
confirms it fires correctly *given* that data — but until `read.py`/`models.py` grows a
field for it (a future, additive change per `models.py`'s own stated policy of adding
fields freely), rung 3 will never fire from a real tick.

## 11. Checklist: sanity-checking a proposal by hand

Given a `vd plan run` proposal, in order:

1. **Rule matches the reasoning.** Does `Action.rule` (e.g. `"6:building-queue-empty"`)
   match what §8 says should fire given the snapshot's queues/health/incoming fleets?
2. **If it's a mine upgrade:** is it the mine §4 would pick — the one with the lowest
   `(level+1)/density` among Metal/Crystal/Deuterium? Recompute density by hand from the
   planet's live multipliers if in doubt.
3. **If it's an energy building or a satellite:** recompute both cost-per-energy numbers
   from the live `cost` fields per §5/`formulas.md` §9, and confirm the cheaper one is
   what got proposed.
4. **Energy math:** does `required` (recomputed via `calc.energy_balance` at the
   post-upgrade level) actually exceed `produced` (the live `energy.produced`)? If the
   proposal is a mine upgrade, this should be false; if it's an energy building or
   satellite, this should be true.
5. **Rationale is legible.** `Action.rationale` should state the specific numbers that
   drove the decision (this is by design — every candidate generator in `candidates.py`
   writes required/produced/satellite-energy into the rationale string precisely so a
   human doesn't have to re-run the code to check it).
6. **Alternatives (Phase 2) make sense, if present.** `Action.alternatives` lists the
   runner-ups from the same generate/filter/score/select pass, each with a `why_not` —
   either a payback-hours comparison against the winner, or a `techtree.describe()` lock
   reason. This is informational only: it should never look like an ROI verdict
   overriding the actual proposal, and a locked alternative's reason should match what
   `techtree.unmet()` would report for that entity's current levels. The list itself is
   capped by `policy.strategy.max_alternatives` (default 5) — a short list here can mean
   "few real alternatives existed" or just "the cap trimmed the rest," not necessarily
   the former.
7. **If the rule is `"8b:unlock-chain"`:** see §12 — the proposed entity should be
   *unlocked* right now (`techtree.unmet()` on it directly returns `()`), and it should be
   the shallowest such entity on the path toward whichever declared target the rationale
   names, not the target itself.

## 12. Working out the build-up: the unlock-chain rung

**The problem this rung exists for.** A policy can declare `ship_targets: [{"name":
"Small Cargo", "count": 1}]` on a fresh planet. Small Cargo needs Shipyard 2 and
Combustion Drive 2 — neither of which exist yet. Before Phase 4 (2026-08-16), every
generator correctly refused to propose Small Cargo itself (§8's rung 8 filters it through
`techtree.unmet()`, same as everything else), which is right — proposing it would be a
guaranteed on-chain revert — but nothing ever proposed *what would unlock it*. The target
was declared, legal to want, and permanently unreachable. A human would have to work out
"first raise Shipyard, which needs Robotics Factory" by hand and separately populate
`building_priority` to make any progress at all.

**The mechanism.** `techtree.next_step_toward(family, entity_id, *, building_levels,
technology_levels) -> UnlockStep | None` walks `unmet()`'s output *backwards*: breadth-
first, one requirement-table lookup ("depth") at a time, starting from the target's own
immediate unmet requirements. A node at the current depth *qualifies* as the answer when
both are true: its own `unmet()` is empty (nothing further blocks it), and its own current
level is known (an `UnmetRequirement(have=None)` — the snapshot never reported this
entity's level — can never become a confidently-chosen step; the walk treats that branch
as a dead end rather than guessing). The first depth at which anything qualifies wins —
"shallowest," never "deepest" and never "first-declared." `candidates.
generate_unlock_chain_candidates` then turns that `UnlockStep` into an ordinary
`Candidate` (`score=None` — see below), gated on the matching `policy.actions.allow_*`
flag and queue idleness like any other family that can emit that action kind.

**When more than one declared target is locked at once.** This isn't a one-target-only
mechanism — `generate_unlock_chain_candidates` runs the walk above for *every* currently
locked `ship_targets`/`defense_targets`/`research_priority` entry, not just the first
declared one, deduplicating by the resulting step's own `(family, entity_id)` when two
targets happen to share an unmet prerequisite (e.g. two ships both gated on the same
Shipyard level — proposed once, not twice). The resulting candidates are then sorted by
weighted cost ascending (`policy.strategy.resource_weights`, the same weights
`score_payback` uses), and `select_unlock_chain_candidate` picks the cheapest across all
of them, across every target planet, as its winner. This is the second of the three
places `resource_weights` genuinely picks a winner rather than just tie-breaking or
annotating `alternatives` — §4 has the consolidated list. Same framing as
`generate_unlock_chain_candidates`'s own docstring: not an ROI comparison (every
candidate here is still `score=None`), just a tie-break among otherwise-incomparable
proposals, using the one number this codebase is willing to compare them by.

**Worked example, planet 664 at its zero-state baseline** (every building and technology
level 0 and known — see `tests/fixtures/planet_664.json`, and
`tests/test_techtree.py::test_next_step_toward_hand_worked_small_cargo_chain` for the pinned
assertion):

```
Small Cargo
├── needs Shipyard 2 (have 0)             -- depth 1, itself locked (needs Robotics Factory 2)
│   └── needs Robotics Factory 2 (have 0) -- depth 2, UNLOCKED (no entry in BUILDING_REQUIREMENTS
│                                             at all) -- this is the answer
└── needs Combustion Drive 2 (have 0)     -- depth 1, itself locked (needs Research Lab 1, Energy 1)
    ├── needs Research Lab 1 (have 0)     -- depth 2, still locked (needs Robotics Factory 1)
    └── needs Energy Technology 1 (have 0)-- depth 2, still locked (needs Research Lab 1)
```

Robotics Factory qualifies at depth 2 (its own `unmet()` returns `()` — it has no entry in
`BUILDING_REQUIREMENTS` at all — and its current level, 0, is known); nothing on the
Combustion Drive branch qualifies until *after* Research Lab is raised, which is itself
gated on Robotics Factory. The proposal: `startBuildingUpgrade(Robotics Factory, 0 -> 1)`,
with `Action.rationale` naming the whole chain ("Robotics Factory is the next unmet
prerequisite for your Small Cargo target...") and `Action.expected_effect` naming what is
still needed *after* this step (Shipyard 2, Combustion Drive 2's own branch) — informational
only, re-derived from live state on the next tick, never a committed multi-tick plan.

**Why `score=None`, always.** An unlock step's value is entirely in what it eventually
enables — Small Cargo, several ticks away, contingent on levels the account doesn't have
yet. `calc.production_per_hour` cannot price that (Robotics Factory doesn't move it
directly), and inventing a number for "how much is a step toward an unbuilt ship worth"
would be exactly the kind of unbounded-future-plan assumption this codebase has already
refused three times over: no cost-scaling function (`calc.py`), no ROI verdict on
`alternatives` (Phase 2), no activity-classification score (`tick.py`'s log-reading code).
An unlock-chain candidate is always ranked in `alternatives` the same way every other
unscored candidate is — after every scored one, in generation order among themselves.

**Why this is the *last* rung, not folded into rung 6.** `policy.strategy.building_priority`
(§8, rung 6) is a declared human intent that wins outright over a scored mine/energy
candidate — that is deliberate for `building_priority` specifically. An unlock-chain step
must **not** get the same treatment: it must never outrank the deadline-driven storage
overflow (rung 5) and must never displace a scored economic candidate (rung 6) or a
policy-declared research/ship/defense pick (rungs 7-8). Placing it as the final rung, only
ever reached once every earlier one has produced nothing at all, makes that ordering a
structural property of the ladder rather than something `generate_unlock_chain_candidates`
would have to know about `building_priority` to respect.

**Cross-family walks are normal, not an edge case.** A ship's chain can pass through a
building (Shipyard) and a technology (Combustion Drive) in the same walk; a defense's
chain can too (Small Shield Dome needs both Shipyard and Shielding Technology). The walk
switches lookup tables (`ReqSource.BUILDING -> BUILDING_REQUIREMENTS`,
`ReqSource.TECHNOLOGY -> RESEARCH_REQUIREMENTS`) transparently — there is nothing
building-specific or research-specific about `next_step_toward` itself.

**What this rung is not.** Not a fleet doctrine, not a build-order planner beyond the very
next step, not a commitment: every tick re-derives from scratch, so if the account's
levels change between ticks (including from a human's own manual play), the next proposed
step reflects that, never a queue laid down in advance. `building_priority` is unaffected
by any of this — it keeps its own, separate, higher-precedence reachability path.
