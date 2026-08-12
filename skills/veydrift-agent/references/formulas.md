# Formulas — every calculator in `calc.py`, with provenance

**Owned by:** WP2 (`calc.py`). Every function below was written by reading
`packages/contracts/src/libraries/VeydriftFormulas.sol`,
`VeydriftAntiRaidPrimitives.sol`, `VeydriftFleetFuel.sol`, `VeydriftCatalog.sol` and
`VeydriftGameplayModule.sol` directly at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(`/Users/santteegt/GitRepositories/clones/veydrift`) — not transcribed from `docs.md`'s
prose, and not from `docs/RESEARCH-ADDENDUM.md`'s summary of it, though every formula
here agrees with both. Where the three disagree, the contract source is what `calc.py`
implements; this document says so at the point of disagreement (there is exactly one,
§7).

**The hard constraint that shapes every function here:** `calc.py` contains **no
cost-scaling function**. `buildingCostFactor` (`VeydriftCatalog.sol:34-45`) returns an
unpublished per-building rational (numerator, denominator); the API serves the *result*
of applying it — live cost at the current level — in every entity's `cost` field. Every
function below that needs a cost takes it as a parameter, read live. If you find yourself
about to write `base * factor ** level`, stop — that function does not belong in this
module.

## Table of contents

- [1. The level-scaling curve (`scaled_level`)](#1-the-level-scaling-curve-scaled_level)
- [2. Planet traits: temperature](#2-planet-traits-temperature)
- [3. Energy](#3-energy)
- [4. Production](#4-production)
- [5. Durations](#5-durations) — the three live-verified checks
- [6. Storage](#6-storage)
- [7. Distance, travel, fuel, cargo](#7-distance-travel-fuel-cargo)
- [8. The Solar Plant energy-crossover table (generated)](#8-the-solar-plant-energy-crossover-table-generated)
- [9. Worked example: the energy-source choice, planet 664 vs. a hot planet](#9-worked-example-the-energy-source-choice-planet-664-vs-a-hot-planet)
- [10. Colony capacity](#10-colony-capacity)
- [11. `vd calc verify` — what it actually checks](#11-vd-calc-verify--what-it-actually-checks)

---

## 1. The level-scaling curve (`scaled_level`)

```
scaled_level(base, level) = floor(base * level * 11^level / 10^level)     [level > 0]
                           = 0                                            [level == 0]
```

Source: `VeydriftFormulas.sol:221-223` (`_scaledLevelValue`), which is
`_scaledLevelValueWithFactor(base, level, 11, 10)` at `:231-238`. This is OGame's classic
×1.1-per-level curve and is the single most-reused formula in the contract: mine
production, energy required/produced, and (via a different numerator/denominator pair)
Fusion Reactor output all reduce to it. `calc.scale_by_factor(value, exponent, numerator,
denominator)` (`VeydriftFormulas.sol:174-181`, `scaleByFactor`) is the general form; every
other level-based function in this module is `scale_by_factor` with specific arguments.

**Worked example:** `scaled_level(10, 1) = floor(10*1*11/10) = 11`. This single value is
why the energy-first invariant fires on the very first mine upgrade of a fresh planet —
see §9.

There is also a ceiling variant, used exactly once (Fusion Reactor's deuterium upkeep):

```
scaled_level_ceil(base, level) = ceil(base * level * 11^level / 10^level)
```

Source: `VeydriftFormulas.sol:225-229` (`_scaledLevelValueCeil`). `fusion_energy` (§3)
uses the floor form via `scale_by_factor` with a different factor; only
`fusion_deuterium_upkeep` uses the ceiling.

## 2. Planet traits: temperature

Temperature drives two effects that pull in **opposite directions**, and that tension is
the entire strategic character of a planet (see §9).

```
deuterium_multiplier_bps(max_temperature) = max(0, 12_800 - max_temperature * 20)
```

Source: `VeydriftFormulas.sol:25-35` (`planetMultipliers`). **Metal and crystal
multipliers are always 10_000** regardless of temperature (`:32-33` of the same
function) — there is no metal/crystal equivalent of this formula. Only deuterium varies.

```
solar_satellite_energy(max_temperature) = clamp(trunc((max_temperature + 140) / 6), 1, 65)
```

Source: `VeydriftFormulas.sol:143-149` (`solarSatelliteEnergy`). **Uses truncating
(toward-zero) division**, matching Solidity's `int256` semantics — `calc._trunc_div`
exists specifically because Python's `//` floors toward `-inf` and would silently
disagree for a negative numerator (`max_temperature < -140`). The disagreement is masked
by the `max(1, ...)` clamp for every temperature actually seen in this universe, but the
function is written correctly regardless rather than relying on the clamp to hide a bug.

**Confirmed against the live API, not just against the formula:** planet 664's
`/wallet/{addr}/infrastructure` response reports
`energyBalance.sources.solarSatelliteEnergy: "4"` at `temperature: -111`
(`docs/RESEARCH-ADDENDUM.md` §5, live probe 2026-08-12) —

```
solar_satellite_energy(-111) = clamp(trunc(29/6), 1, 65) = clamp(4, 1, 65) = 4   ✓
```

`max_temp_from_bps` is the *inverse* of `deuterium_multiplier_bps`, used only as a
diagnostic cross-check (`docs/NOTES.md` §12.5's method: invert the multiplier, then check
it against the API's own `temperature` field). It is not a source of truth — the API
returns `temperature` directly — and it is not exact once the multiplier clamps to 0
(`max_temperature >= 640`).

## 3. Energy

```
required = scaled_level(10, metal_level) + scaled_level(10, crystal_level)
         + scaled_level(20, deuterium_level)

produced = scaled_level(20, solar_level)
         + fusion_energy(fusion_level, energy_technology_level)
         + solar_satellite_energy_per_unit * solar_satellite_count

scale_bps = 10_000                              if required == 0 or produced >= required
          = floor(produced * 10_000 / required)  otherwise
```

Source: `VeydriftFormulas.sol:107-129` (`energyBalance`). Note the contract's own
comparison is `produced >= required`, **not** strictly greater — a planet with produced
exactly equal to required is not throttled. `calc.energy_balance` takes
`solar_satellite_energy_per_unit` as an explicit parameter rather than a temperature, so
callers pass the API's live `energyBalance.sources.solarSatelliteEnergy` instead of
recomputing it (`docs/SPEC.md` §5.4 says this explicitly: "the contract's own value is
served to you"). `solar_satellite_energy` from §2 is available for the cases where no
live value exists — a fixture, or a hypothetical planet.

```
fusion_energy(level, energy_technology_level)
    = floor(30 * level * (105 + energy_technology_level)^level / 100^level)
```

Source: `VeydriftFormulas.sol:131-137` (`fusionReactorEnergyProduction`) —
`scale_by_factor(30*level, level, 105+energy_technology_level, 100)`.

```
fusion_deuterium_upkeep(level) = ceil(10 * level * 11^level / 10^level)
```

Source: `VeydriftFormulas.sol:139-141` (`fusionReactorDeuteriumConsumption`), via the
ceiling variant from §1.

```
effective_cap        = (metal_level + crystal_level + deuterium_level) * 8
effective_crawlers   = min(crawler_count, effective_cap)
crawler_boost_bps    = min(effective_crawlers * 2, 5_000)
```

Source: `VeydriftFormulas.sol:93-105` (`crawlerProductionBoostBps`). 0.02% per effective
crawler, capped at 8 crawlers per combined mine level and 50% total boost.

## 4. Production

```
metal     = scale_bps(scaled_level(30, metal_level),     metal_multiplier_bps)
crystal   = scale_bps(scaled_level(20, crystal_level),   crystal_multiplier_bps)
deuterium = scale_bps(scaled_level(10, deuterium_level), deuterium_multiplier_bps)

# then, in this exact order:
if crawler_boost_bps: multiply all three by (10_000 + crawler_boost_bps)/10_000
deuterium -= fusion_deuterium_upkeep(fusion_level)          # floored at 0
if energy.required != 0: multiply all three by energy.scale_bps/10_000
```

Source: `VeydriftFormulas.sol:37-91` (`productionPerHour`). **The order matters and must
not be reordered**: multiplier first, then the crawler boost, then Fusion Reactor's
deuterium upkeep, then the energy shortage factor last. Energy throttles the
already-reduced (post-upkeep) deuterium figure, not the raw mine output.

## 5. Durations

All three share a `universe_speed` term (contract constant `QUEUE_UNIVERSE_SPEED = 1`,
`VeydriftGameStorage.sol`) and floor at `min_queue_seconds` (contract default 1,
`MIN_QUEUE_SECONDS`).

```
build_seconds     = max(min_queue_seconds, floor((metal_cost+crystal_cost)*3600
                        / (2500 * (robotics_level+1) * 2^nanite_level * universe_speed)))

research_seconds  = max(min_queue_seconds, floor((metal_cost+crystal_cost)*3600
                        / (1000 * (lab_level+1) * universe_speed)))

ship_seconds      = max(min_queue_seconds, ceil((metal_cost+crystal_cost)*quantity*3600
                        / (2500 * (shipyard_level+1) * 2^nanite_level * universe_speed)))
```

Sources: `VeydriftFormulas.sol:160-172` (`buildingDuration`), `:199-211`
(`researchDuration`), `:182-197` (`unitDuration`). **`ship_seconds` ceiling-divides; the
other two floor.** A partial second of ship production still costs a full second of queue
time — this is a real rounding-mode difference between the three, not a typo, and
`test_calc.py::test_ship_seconds_ceiling_rounds_up` guards it with a case where the two
modes disagree (`ship_seconds(1, 0, 1, 1, quantity=1) == 2`, while the floor sibling of
the same division gives `1`).

`docs/NOTES.md` §12.4 established `universe_speed == 1` three independent ways by
isolating a different divisor in each check — this is exactly what `vd calc verify`
re-runs against the live API (§11).

| Entity | Formula | Computed (level 0, speed 1) | Live |
| --- | --- | --: | --: |
| Energy Technology | `research_seconds(0, 0, 800)` | 2880 | 2880 |
| Small Cargo | `ship_seconds(0, 0, 2000, 2000, quantity=1)` | 5760 | 5760 |
| Metal Mine 1 | `build_seconds(0, 0, 60, 15)` | 108 | 108 |

## 6. Storage

```
storage_cap(level)   # a literal per-level table, levels 0-50 -- not a formula
```

Source: `VeydriftFormulas.sol:241-295` (`_storageCap`), used identically for Metal
Storage, Crystal Storage and the Deuterium Tank (`storageCaps` at `:150-158` calls the
same private function for all three). `storage_cap(0) == 10_000`,
`storage_cap(50) == 180_862_636_975_685_000`. Raises `ValueError` above level 50
(`MAX_LEVEL`), mirroring the contract's `LevelTooHigh` revert.

```
hours_to_cap(current, per_hour, cap) = (cap - current) / per_hour     [per_hour > 0]
                                      = None                          [per_hour <= 0]
```

Plain arithmetic, not a contract formula. Feeds `vd read`'s `--summary` digest
(`docs/SPEC.md` §5.2: "hours-to-cap per resource") and `plan.py`'s storage-overflow rung.

## 7. Distance, travel, fuel, cargo

```
distance(a, b):
    if galaxy_diff != 0:  20_000 * galaxy_diff
    elif system_diff != 0: 2_700 + 95 * system_diff
    elif position_diff != 0: 1_000 + 5 * position_diff
    else: 0
```

Source: `VeydriftGameplayModule.sol:814-829` (`_planetDistance` / `_absoluteDifference`).
**This is the one place `docs.md`'s published prose and the contract source were checked
against each other and agree exactly** (`docs.md` fetched live 2026-08-12: "same system
distance = 1000 + 5 * position difference", "same galaxy distance = 2700 + 95 * system
difference", "different galaxy distance = 20000 * galaxy difference"). Local Harvest
missions use a fixed distance of 5 instead (`LOCAL_HARVEST_DISTANCE`,
`VeydriftGameStorage.sol:52`) — not a function of two coordinates, so not reproduced in
`distance()`.

```
travel_seconds = 10 + floor(isqrt(distance * 10 * 122_500 / slowest_ship_speed) * 100
                             / (speed_percent * universe_speed))
```

Source: `VeydriftAntiRaidPrimitives.sol:55-68` (`travelSeconds`, 4-arg overload).
**Computed as one integer square root of the full product**
(`distance*10*122_500/slowest_ship_speed`), not as `350 * isqrt(distance*10/speed)` even
though `sqrt(122_500) == 350` exactly — factoring the constant out of the square root can
round differently for some inputs, so `calc.travel_seconds` mirrors the contract's own
order of operations rather than the algebraically-equivalent-looking shortcut. Uses
`math.isqrt`, Python's exact floor integer square root, which matches the contract's own
bit-shift Babylonian-method `_sqrt` (`VeydriftAntiRaidPrimitives.sol:240-278`) for every
non-negative input.

```
mission_fuel = 1 + floor((sum over ships of ogame_fuel_numerator) + denominator/2)
                    / denominator)
denominator  = 35_000 * 100^2 * (10^9)^2
```

Sources: `VeydriftAntiRaidPrimitives.sol:93-131` (`ogameFuelNumerator` /
`ogameFuelDenominator` / `ogameFuelCostFromNumerator`), aggregated across ship types
exactly as `VeydriftFleetFuel.sol:9-34` (`ogameMissionFuelCost`) does: sum each flyable
ship type's numerator, then convert once. `calc.mission_fuel` takes
`(fuel_consumption, quantity, speed)` triples per ship type — with fuel and speed already
resolved for the player's drive-tech levels — rather than reimplementing
`VeydriftCatalog.shipMovementStats`'s tech-level lookup itself. This differs from
`docs/RESEARCH-ADDENDUM.md` §5's simplified prose formula (`1 + floor(sum(qty * shipFuel
* dist * (1 + eff/100)^2) / 35000 + 0.5)`) in one respect: the addendum's version assumes
every ship in the mission travels at the same effective speed, while the exact contract
formula (`ogameFuelNumerator`) computes a per-ship `speed_ratio_scaled` against the
mission's *slowest* ship and only converts once at the end — `calc.py` implements the
exact contract version, not the addendum's simplification, because the contract source
was available and is the higher-priority source per the standing rules.

```
available_cargo(total_cargo_capacity, fuel_cost) = max(0, total_cargo_capacity - fuel_cost)
```

`docs/RESEARCH-ADDENDUM.md` §5: "available cargo = total ship cargo - mission fuel". Fuel
is deuterium and ships in the same cargo hold, so it is subtracted directly rather than
converted; clamped at 0 rather than allowed to go negative.

## 8. The Solar Plant energy-crossover table (generated)

Reproduces `docs/NOTES.md` §12.5's table, generated by running `calc.solar_crossover_table`
(`uv run --directory skills/veydrift-agent vd calc crossover`), not typed by hand. For
each mine level `L` with metal, crystal and deuterium mines all at `L` (no fusion, no
satellites), the smallest Solar Plant level whose energy alone covers the demand:

| mine level L | required energy | min Solar Plant level |
| --: | --: | --: |
| 1 | 44 | 2 |
| 2 | 96 | 4 |
| 3 | 157 | 5 |
| 4 | 233 | 7 |
| 5 | 321 | 8 |
| 6 | 424 | 9 |
| 7 | 544 | 11 |
| 8 | 684 | 12 |
| 9 | 848 | 13 |
| 10 | 1036 | 14 |
| 11 | 1253 | 15 |
| 12 | 1505 | 17 |
| 13 | 1793 | 18 |
| 14 | 2125 | 19 |

**The gap between mine level and required Solar Plant level widens, it is not constant**:
+2 levels at mine 3, +4 at mine 10, +5 at mine 14. This table is the direct evidence for
why `plan.py`'s energy-first invariant recomputes `required` vs. `produced` explicitly at
the post-upgrade level on every proposal, instead of using a fixed "keep Solar Plant N
levels above your highest mine" offset — `docs/NOTES.md` §12.8 records that exact offset
rule as a mistake the original analysis made and then had to correct once this table was
generated.

## 9. Worked example: the energy-source choice, planet 664 vs. a hot planet

This is the derivation `plan.py`'s `_energy_candidate` implements, walked through by
hand. Full narrative in `references/strategy-playbook.md` §3; this section is the numeric
proof that the two fixtures (`tests/fixtures/planet_664.json`,
`tests/fixtures/planet_hot.json`) actually sit on opposite sides of the same crossover.

A Solar Satellite's cost is flat — `VeydriftCatalog.shipCost(SolarSatellite) = (0, 2000,
500)`, no scaling with count — so its cost-per-energy-point is constant:
`2500 / solar_satellite_energy_per_unit`. A Solar Plant's *marginal* cost-per-energy
**grows** with level, because its cost scales by its own (live, unpublished) factor
(~×1.5/level) while `scaled_level(20, L)`'s growth is milder. The two curves must cross
somewhere; where they cross depends entirely on `solar_satellite_energy_per_unit`, i.e.
on temperature:

| Solar level L -> L+1 | cost (metal+crystal) | energy gained | cost per energy point |
| --- | --: | --: | --: |
| 0 -> 1 | 105 | 22 | 4.77 |
| 5 -> 6 | 796 | 51 | 15.61 |
| 10 -> 11 | 6,053 | 109 | 55.53 |
| 11 -> 12 | 9,081 | 126 | 72.07 |
| 12 -> 13 | 13,622 | 144 | 94.60 |
| 15 -> 16 | 45,978 | 217 | **211.88** |
| 16 -> 17 | 68,968 | 248 | 278.10 |

(Costs computed from `VeydriftCatalog.sol`'s published base cost (75, 30, 0) and factor
(15, 10) for illustration only — this table lives in documentation, not in `calc.py`,
which never recomputes a cost.)

- **Planet 664** (temperature -111 °C): `solar_satellite_energy_per_unit = 4` (confirmed
  live). Satellite cost-per-energy = `2500 / 4 = 625.00`. Even at Solar Plant level
  15->16 (211.88), the satellite is still nearly 3x more expensive per energy point. The
  crossover for 664 does not arrive until roughly Solar Plant level 19 (`test_calc.py`'s
  crossover-table test does not probe this far, but the direction is unambiguous well
  past any level a hobby account will reach) — in practice, **never build satellites on
  664**.
- **The hot fixture** (temperature 40 °C, `tests/fixtures/planet_hot.json`):
  `solar_satellite_energy_per_unit = 30`. Satellite cost-per-energy =
  `2500 / 30 = 83.33`. That is *cheaper* than Solar Plant's marginal cost at level 15->16
  (211.88) — the crossover already happened around level 12-13. At Solar Plant level 15,
  mines 11/11/11, produced = required = 1,253; bumping any mine to 12 pushes required to
  1,316 > 1,253, triggering the energy-first branch, and the satellite wins the
  comparison.

`tests/test_plan.py::test_matched_building_levels_isolate_temperature_as_the_only_variable`
constructs both scenarios at the **identical** building levels (Solar Plant 15, mines
11/11/11) and confirms the *only* thing that flips the answer is temperature — proving
this is planet-trait-derived, not a hardcoded planet id.

## 10. Colony capacity

```
max_planets(astrophysics_level) = 1 + astrophysics_level
```

Source: `VeydriftGame.sol:596-597` (`maxPlanets`). `docs/NOTES.md` §13.5 only said
Astrophysics "raises colony capacity" — this is the exact formula, read from the facade
contract that exposes it as a public view.

## 11. `vd calc verify` — what it actually checks

`uv run --directory skills/veydrift-agent vd calc verify` re-runs the three duration
checks from §5 against the **live** API (Energy Technology, Small Cargo, Metal Mine),
using each entity's live level and cost rather than assuming level 0 — so the check
remains valid even after the account has taken real actions, unlike the original
one-shot probe in `docs/NOTES.md` §12.4. All three checks agreeing is much stronger
evidence than any one alone, because each isolates a different divisor (research lab /
shipyard / robotics factory) while sharing the same `universe_speed` term. A mismatch on
any one check exits non-zero rather than being averaged away — confirmed by running it
live on 2026-08-12:

```
$ uv run --directory skills/veydrift-agent vd calc verify
       vd calc verify -- https://api.veydrift.com
    wallet=0x224aba5d489675a7bd3ce07786fada466b46fa0f
                       planet=664
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━━┓
┃ check                      ┃ computed ┃ live ┃ status ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━━┩
│ Energy Technology duration │     2880 │ 2880 │ match  │
│ Small Cargo duration       │     5760 │ 5760 │ match  │
│ Metal Mine duration        │      108 │  108 │ match  │
└────────────────────────────┴──────────┴──────┴────────┘
universe speed confirmed == 1 (all three duration formulas agree)
$ echo $?
0
```
