# Strategy playbook — deriving a build order for ANY planet

This is the document a human reads to check the planner's reasoning without reading
`plan.py` itself. It generalizes an earlier manual, one-off derivation method ("how to
re-derive a strategy for another planet") into the algorithm `plan.py` actually runs on
every tick, for any planet the wallet holds — planet 664 appears in examples only because
it is the account's real planet, never as a special case in code.

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
| 9. Read `energyBalance.sources.solarSatelliteEnergy` — "decides the whole energy strategy" | `plan._satellite_energy_per_unit`: prefers `PlanetSnapshot.energy.solar_satellite_energy` (live), falls back to `calc.solar_satellite_energy(temperature)` only if absent |
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

## 5. Deriving the energy source: cost per energy point

When the energy-first check *does* fire, `plan.py` chooses between Solar Plant and Solar
Satellite by comparing **cost per unit of energy gained**, using only live-served costs:

- Solar Plant's marginal cost-per-energy for its next level: live `cost` (metal+crystal)
  for that level, divided by `scaled_level(20, L+1) - scaled_level(20, L)`.
- Solar Satellite's cost-per-energy: its live per-unit `cost`, divided by the live
  `solar_satellite_energy_per_unit`.

Whichever is cheaper wins. The full numeric derivation, including a generated table of
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
same code path produces opposite answers.

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
crossover, so the counterfactual is not trivial:

1. Buildings: Solar Plant level 15, Metal/Crystal/Deuterium mines all level 11.
2. Currently: `produced = scaled_level(20, 15) = 1,253`; `required` at mines 11/11/11 =
   `scaled_level(10,11)*2 + scaled_level(20,11) = 1,253` — exactly balanced, energy-safe
   right now.
3. Mine priority (§4): with metal/crystal multipliers always 10,000 and this planet's
   deuterium multiplier at 12,000 (`12_800 - 40*20`), Metal still ranks first — the same
   ranking logic as 664, different numeric inputs.
4. Energy-first check: Metal Mine 11->12 would push required to 1,316 > 1,253 produced.
   **Blocked, same as 664's very first check** — the mechanism is identical.
5. Energy-source choice: Solar Plant 15->16 costs 45,978 for 217 energy (211.88/energy).
   Solar Satellite costs 2,500 for 30 energy (83.33/energy). **Satellite is now
   2.5x cheaper per energy point** — the inversion has happened.
6. **Result:** `startShipProduction(900001, SolarSatellite, 1)`.

`tests/test_plan.py::test_matched_building_levels_isolate_temperature_as_the_only_variable`
goes one step further than fixture comparison: it takes the *real* 664 fixture and
overwrites only its building levels to match the hot planet's (Solar Plant 15, mines
11/11/11), leaving temperature, multipliers and satellite energy untouched at 664's real
cold values. The planner still proposes Solar Plant, not a satellite — proving the
inversion in §7 is driven by temperature alone, not by how far along the mines happen to
be.

## 8. The full ladder, rung by rung

This codebase's decision ladder, implemented exactly, first match wins
(`plan.plan_next_action`):

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
   `plan._storage_overflow_action` for the reasoning in full. An earlier version of this
   rung attempted the storage-building fallback even when the queue was busy, which would
   have produced a proposal that reverts on submission; caught while writing this
   document and fixed before shipping (`tests/test_plan.py::test_storage_overflow_with_busy_queue_proposes_nothing_unsafe`).
6. **Building queue empty -> next build.** §3-§5's derivation, run per target planet in
   policy order.
7. **Research queue empty -> next research.** Deliberately the least-derived rung in this
   module: picks the technology with the lowest current level account-wide, ties broken
   by ascending contract id. This is *not* as rich as the energy invariant on purpose —
   the SPEC's rung 7 only asks for "next research," not a tech-tree strategy, and
   `VeydriftCatalog.researchLabRequirement` (prerequisite tiers) is not modeled. Treat
   this rung's output as a reasonable default, not a derived recommendation the way
   rungs 6's mine/energy choice is.
8. **Shipyard idle AND economy on track -> ships/defense per policy.** Fires only if
   `policy.actions.allow_ships` or `allow_defense` is true (both default `false` in
   `assets/policy.example.json`, so this rung rarely fires in practice) and something
   else (building or research) is already actively progressing. If ships are allowed and
   a satellite is currently the cheaper energy source on this planet (§5), proposes one;
   if defense is allowed, proposes the cheapest defense entry (Rocket Launcher) as a
   policy-driven default in the absence of any threat model.
9. **Otherwise -> NO-OP with an explicit reason.** Always reachable; `Action.rationale`
   is never empty.

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

**The account has taken zero on-chain actions.** All queues are `null`, all building/tech
levels are 0, resources are the untouched starting grant (1,000 metal / 1,000 crystal /
0 deuterium) — unchanged since settlement at block 49,666,196. Concretely, that leaves
three things this codebase has never observed:

1. **Cost scaling above level 0.** The cost-fingerprinting method this project's research
   first used only works at level 0, where live cost equals base cost — the moment any building goes
   to level 1, that method stops working, and nothing in this codebase has since watched
   a live cost respond to a level-up. `calc.py`'s duration formulas (§5, verified live at
   level 0 by `vd calc verify`) are the only contract-derived formulas checked against
   live data; the cost values themselves, at any level, have only ever been read as
   opaque numbers from the API, never independently reconstructed and compared — by
   design, per the hard constraint in `references/formulas.md`. This is intentional, not
   a gap: the point of never recomputing cost is that it is impossible for this
   assumption to go stale.
2. **Queue behavior under load.** No building, research, ship or defense queue has ever
   been observed non-`null`. `plan.py`'s queue-empty checks
   (`planet.queues.get(QueueKind.BUILDING) is None`, etc.) have only ever been exercised
   against synthetic `QueueEntry` objects in tests, never a real one returned by the API.
   If the live `queue` field's shape differs even slightly from what `models.QueueEntry`
   expects, `read.py`'s parsing (not this module) would be where that surfaces —
   untested here because it cannot be tested here.
3. **Lazy settlement.** `startBuildingUpgrade` is confirmed to settle
   resources first (no separate `finishBuildingUpgrade` call is needed), but this account
   has never triggered that path — `lastSettledAt` has not moved since the settlement
   block. `resources_as_of_now` vs. `resources` (both present on `PlanetSnapshot`) has
   never been observed to differ in practice; `plan.py` uses `resources_as_of_now`
   throughout per the model's own guidance ("Prefer this for affordability checks"), but
   that preference has only been exercised where the two fields happen to be equal.

**Concretely, which `plan.py` code paths this leaves untested against live state:**

| Code path | Tested against | Untested against |
| --- | --- | --- |
| Energy-first invariant (rungs 5-6, §3-§5) | Real planet 664 (level-0 case, §6) + fixtures (progressed levels, §7) | A real account with progressed levels — the level-0 case is the *only* live-observed data point |
| Mine priority ranking (§4) | Fixtures only (multiple temperatures) | Whether a real account's multi-resource holdings ever make "spend it" (rung 5) diverge from "build the matching storage" in a way only observable at higher levels |
| Storage overflow (rung 5) | Synthetic fixtures with hand-set `resources_as_of_now`/`production_per_hour` | A real planet ever approaching a storage cap — this account's production has been 0/hr for its entire observed history |
| Research selection (rung 7) | Fixture with all-zero tech levels (tie-break only) | Any scenario with mixed tech levels, since none has ever existed on this account |
| Shipyard rung (rung 8) | Fixture only, `allow_ships`/`allow_defense` forced `true` for the test | Real economy-on-track detection, since building/research queues have never been non-idle live |
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
   drove the decision (this is by design — every branch in `_next_building_action`
   writes required/produced/satellite-energy into the rationale string precisely so a
   human doesn't have to re-run the code to check it).
