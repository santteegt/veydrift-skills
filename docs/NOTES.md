# Veydrift — Research Notes for Future Agents

Findings from probing `api.veydrift.com` and the public docs on 2026-08-11 that are **not** in
`veydrift-briefing.html`, `veydrift-agent-resources.md`, or `veydrift-agent-prompt.md`.
Written for the next agent picking up this account.

Context: wallet `0x224aba5d489675a7bd3ce07786fada466b46fa0f`, planet id `664` at `7:181:14`, Base mainnet.

**If you only read one section:** §1 (the endpoint map) for what to call, and **§12 (Method)** for how
all of this was derived and how to redo it for a different planet or account.

---

## 1. The big correction: you probably don't need the ABI for reads

The earlier resource doc flagged "no published ABI" as the blocking gap. That was **understated
in one direction and overstated in the other**:

- The public read API exposes far more than the documented endpoint list suggests, including
  **live costs at your current level** for every building, ship and technology. An agent can plan
  an entire economy without ever touching an RPC node or knowing the cost-scaling factor.
- The ABI is still required to **write**. Nothing changes there.

Undocumented endpoints found by probing (all under `https://api.veydrift.com`, no auth):

| Endpoint | What it gives you |
| --- | --- |
| `/wallet/{addr}/infrastructure` | **The single most useful call.** Building levels + live costs + build durations, `energyBalance`, `productionPerHour`, `crawlerProduction`, `storageCaps`, `raidableResources`, `protectedResources`, current queue. |
| `/wallet/{addr}/research` | Technology levels + live costs + durations, `researchLabLevel`, `researchNetworkLabLevels`, queue. |
| `/wallet/{addr}/shipyard` | Ship counts + live costs + durations, `shipyardLevel`, `naniteLevel`, `fleetSlots {active, limit}`, `launchableShips`, queue. |
| `/wallet/{addr}/missions` | Mission archive, paginated (`page`, `pageSize`, default 25). Empty for a fresh account. |

Probed and **404/empty** (do not waste time): `/wallet/{addr}/buildings`, `/wallet/{addr}/defense`,
`/wallet/{addr}/fleet`, `/debug/config` (documented in the README but returns nothing in production).

There is almost certainly a defense endpoint under a name I didn't guess. Worth extracting the real
route list from the frontend bundle rather than probing blind.

## 2. Verified ID maps

These are stable ordinals used by the API and, presumably, the contract. Derived by matching the
API's `cost` objects against the base costs in `docs.md` — every one matched exactly.

**Buildings** (`/infrastructure` → `buildings[].id`)

| id | Building | id | Building |
| --: | --- | --: | --- |
| 0 | Metal Mine | 8 | Crystal Storage |
| 1 | Crystal Mine | 9 | Deuterium Tank |
| 2 | Deuterium Synthesizer | 10 | Fusion Reactor |
| 3 | Solar Plant | 11 | Nanite Factory |
| 4 | Robotics Factory | 12 | Terraformer |
| 5 | Shipyard | 13 | Alliance Depot |
| 6 | Research Lab | 14 | Missile Silo |
| 7 | Metal Storage | 15 | Rift Stabilizer |

**Technologies** (`/research` → `technologies[].id`)

| id | Tech | id | Tech |
| --: | --- | --: | --- |
| 0 | Energy | 8 | Hyperspace Technology |
| 1 | Laser | 9 | Impulse Drive |
| 2 | Ion | 10 | Hyperspace Drive |
| 3 | Combustion Drive | 11 | Plasma |
| 4 | Computer | 12 | Astrophysics |
| 5 | Weapons | 13 | Intergalactic Research Network |
| 6 | Shielding | 14 | Graviton |
| 7 | Armor | | |

Note the ordering is **not** the docs' table order for research — Impulse Drive is id 9, after the
combat techs. Don't infer ids from the docs; use the ones above.

**Ships** (`/shipyard` → `ships[].id`)

| id | Ship | id | Ship |
| --: | --- | --: | --- |
| 0 | Small Cargo | 8 | Bomber |
| 1 | Light Fighter | 9 | Solar Satellite |
| 2 | Recycler | 10 | Destroyer |
| 3 | Colony Ship | 11 | Dreadstar |
| 4 | Large Cargo | 12 | Battlecruiser |
| 5 | Heavy Fighter | 13 | Reaper |
| 6 | Cruiser | 14 | **Pathfinder (undocumented)** |
| 7 | Battleship | 15 | Crawler |

## 3. Doc bugs worth knowing

- **Pathfinder exists but is missing from the ships catalog.** `docs.md` lists it once in the
  rapidfire table (Pathfinder → Recycler, factor 3) but omits it from the ship table entirely.
  It is live on-chain as id 14, costing 8,000 metal / 15,000 crystal / 8,000 deuterium.
- **"Deathstar" appears in the rapidfire tables** (Reaper → Deathstar ×10; Deathstar → any defense
  ×200) but the ship is called **Dreadstar** everywhere else. Same unit, stale naming.
- **Per-building cost factors are never published.** `docs.md` gives `cost = base × factor^level`
  but no factor per building. Moot now — read live costs from `/infrastructure`.
- The README describes a Base **Sepolia** (84532) deployment throughout. The live game is **Base
  mainnet (8453)**. The README is stale relative to production; trust `/runtime-config`.
- `https://api-test.veydrift.com/runtime-config` returns **production mainnet config**, not test
  config. Do not assume the `api-test` host points at a testnet universe.

## 4. Confirmed constants

- **Universe speed = 1.** Verified three independent ways against the published formulas:
  - Energy Technology at Lab 0: `(0 + 800) × 3600 / (1000 × 1 × 1) = 2880 s` — API says 2880.
  - Small Cargo at Shipyard 0: `(2000 + 2000) × 3600 / (2500 × 1 × 1 × 1) = 5760 s` — API says 5760.
  - Metal Mine 1 at Robotics 0: `(60 + 15) × 3600 / 2500 = 108 s` — API says 108.
  This means every duration formula in `docs.md` can be used as-is with `universe speed = 1`.
- **Base fleet slots = 1** at Computer Technology 0 (`fleetSlots.limit: 1`).
- **Base storage cap = 10,000** per resource at storage level 0.
- **Settlement start price = 0.012 ETH** (`12000000000000000` wei), sourced from an event, not config.

## 5. Live confirmation of the Solar Satellite finding

`/infrastructure` returns `energyBalance.sources.solarSatelliteEnergy: "4"` for planet 664.
This is the contract's own value and it matches the hand calculation
`clamp(trunc((−111 + 140) / 6), 1, 65) = 4` exactly. The "don't build Solar Satellites here"
advice is confirmed by the game itself, not just inferred from the formula.

Generalisation for any future planet: `solarSatelliteEnergy` is returned directly in
`energyBalance.sources` — just read it rather than recomputing.

## 6. Raidability observation (unresolved)

On a fresh planet holding 1,000 metal / 1,000 crystal / 0 deuterium, `/infrastructure` returned:

```
protectedResources: { metal: "0",   crystal: "0",   deuterium: "0" }
raidableResources:  { metal: "500", crystal: "500", deuterium: "0" }
storageCaps:        { metal: "10000", crystal: "10000", deuterium: "10000" }
```

Raidable is exactly 50% of held. `protectedResources` reading 0 while 500 of 1,000 is
non-raidable is **not explained** by anything in `docs.md`. Two plausible readings: the loot cap is
a flat 50% and `protectedResources` tracks something else entirely (storage-based shielding that
only kicks in at higher storage levels), or the field is simply not populated yet.

**Do not build a raid-profitability model on these two fields until the semantics are confirmed**
by observing a real battle report. This is the highest-value open question for anyone doing offense.

## 7. Universe structure (not in the docs)

- Systems are **sparse**. `7:181` has 10 planet slots (positions 3,4,5,6,7,8,9,10,11,14) and
  `1:250` has 9. Positions are not contiguous 1–15. Don't iterate positions blindly.
- Planets have an **`archetype`** string never mentioned in the docs. Observed values:
  `scorching-molten`, `lush-temperate`, `temperate-ocean`, `cold-tundra`, `frozen-ice`.
  It correlates with temperature, and therefore with the deuterium multiplier.
- **`migrationReservation`** is a live, undocumented mechanic. `7:181:8` currently shows
  `{ status: "quantum-unstable", label: "Quantum-unstable planet", wallet: "0x4c82…ff58", planetId: "7" }`.
  Treat reserved slots as unavailable for colonisation and investigate before targeting them.
- Each universe planet object carries `occupiedBy` (with `ownerDisplayName` and `alliance`),
  `debrisField`, `hasMoon` and `moonChance` — this is the whole raid/harvest targeting surface,
  available with one unauthenticated GET per system.

## 8. Population and competitive context

From the `indexer` block (returned on most wallet routes, and more complete there than on `/health`):

| Metric | 2026-08-07 | 2026-08-11 |
| --- | --: | --: |
| Indexed planets | 179 | 195 |
| Indexed moons | 81 | 85 |
| Indexed debris fields | 1 | 2 |
| Indexed event logs | 302,197 | 337,159 |

**This is a very small universe** — roughly 195 planets total, growing ~4/day. Strategic reading:

- Almost nobody is at war. Two debris fields across the whole game means combat is rare.
- 85 moons against 195 planets is a startlingly high ratio, and moon-chance reports number only 2.
  Nearly all moons therefore came from **burning Burning Chicken NFTs**, not from combat debris.
  Moons are being bought, not fought for.
- `7:181` has exactly one occupied slot: yours. The neighbourhood is empty, which is good for
  quiet growth and bad for nearby raid targets.
- With this few players, **score-based attack protection is the dominant constraint**, and the
  reputational cost of raiding is high in a community this size.

## 9. Backend behaviour that will confuse an agent

- The backend runs a **multi-worker pool** (10 workers observed in production). Worker 0 is the
  sole writer and runs chain sync; the rest are read-only replicas over a shared SQLite index.
- **Consequence: `/health` returns `null` for `chainSync`, `missionResolution`, `indexer`, `rpc`,
  and most of `readiness` when your request lands on a reader worker.** These nulls are **not
  errors** and must not be treated as an outage. Confirmed by `worker.role: "reader"` in the same
  response. An agent that hard-fails on `readiness.indexedState === null` will refuse to ever run.
  Gate on `ok === true` and `readiness.ready === true` instead.
- Richer indexer telemetry (`latestIndexedBlock`, `indexedState`, `safeToServeIndexedState`,
  `lastRebuiltAt`) is embedded in the `indexer` block of the **wallet routes**, which is where you
  should read it from.
- `lastReconciledBlock` sits at `48295091` while `latestIndexedBlock` is `49839333` — over 1.5M
  blocks apart, and `lastReconciledAt` is from 2026-07-06. This is expected: full reconciliation is
  an explicit operator action (`bun run index:sync`), not a background loop. **Do not read the gap
  as staleness.** `indexedState: "healthy"` and `safeToServeIndexedState: true` are the fields
  that matter.
- `/graphql` is **status-only**. It returns the same runtime config as `/runtime-config` wrapped in
  a `service` object. There is no game schema. Don't build against it.
- `/highscores` is ~86 KB and will blow up a context window. Fetch to a file and filter.

## 10. Undocumented systems visible in `/runtime-config`

These contracts are live but appear nowhere in `docs.md`. Each is a potential surface an agent
should be told to stay away from unless explicitly authorised:

- **Referral system** `0x3246Df19…` + a referral signer, with `referralStartPriceWei` = 0.012 ETH.
- **Paid alliance invite** `0xD11be372…` + its own signer. Alliance membership has a paid path.
- **Migration** `0x33A56B6f…`, tied to the `migrationReservation` / "quantum-unstable" mechanic above.
- **Rift Stabilizer** is in the building catalog (id 15) and `docs.md` mentions "Rift resource
  movement", but publishes no mechanics. `indexedRiftBalances: 0` — nobody is using it, or it
  isn't live. Ignore for now.

## 11. Suggested next steps

1. **Extract the route list and ABI from the frontend bundle** at `veydrift.com`. This resolves both
   the missing defense endpoint and the ABI in one pass, and is easier than Basescan.
2. **Confirm `protectedResources` semantics** (§6) before any offensive modelling.
3. **Re-probe for the defense endpoint** — `/infrastructure`, `/research` and `/shipyard` follow a
   clear naming convention, so it exists under some name.
4. **Snapshot `indexedPlanets` weekly.** Growth rate is the best available proxy for whether this
   universe is worth investing in.
5. When the account has real state, capture one `/missions` row and one battle report — the row
   shape is the missing piece for automating fleet actions.

---

# 12. Method — how this analysis was produced

Documented so it can be repeated for another planet, another account, or after a contract upgrade.
The whole thing is five passes, roughly 25 HTTP GETs and one short Python script. No RPC node, no
API key, no wallet access was used at any point.

## 12.1 The five passes

| Pass | Question | Source |
| --: | --- | --- |
| 1 | What are the rules? | `veydrift.com/docs.md` |
| 2 | What's actually deployed, and where? | repo README → `/runtime-config` |
| 3 | What does this specific account hold? | wallet routes |
| 4 | What do the rules *imply* for this planet? | independent reimplementation of the formulas |
| 5 | What's the competitive context? | universe + indexer telemetry |

The ordering matters. Passes 1–2 are cheap and constrain everything after them — in particular,
pass 2 caught that the README describes Base Sepolia while the game is on mainnet, which would have
poisoned every subsequent read had I trusted the README's chain ID.

## 12.2 Finding the undocumented endpoints

The docs and README list a partial route set. The four most useful routes are unlisted and were
found by **naming-convention inference**, not by scraping:

1. Observe that documented wallet routes follow `/wallet/{addr}/{noun}` — `settlement`, `queues`.
2. Note that `/runtime-config` advertises `featureSupport.researchEndpoint: true` and
   `highscoresEndpoint: true`. **Feature flags name routes.** `researchEndpoint` implied a research
   route existed; `/wallet/{addr}/research` was a guess that paid off immediately.
3. Once `/research` returned a rich payload (levels + costs + durations + queue), generalise: the
   game's other three domains are infrastructure, shipyard and missions. `/infrastructure`,
   `/shipyard`, `/missions` all hit on the first try.

Guesses that returned empty: `/buildings`, `/defense`, `/fleet`, `/debug/config`. Note the pattern —
the working names are the *domain* nouns the UI would use for a page, not the entity plural. That
heuristic predicts the still-missing defense route is something like `/defenses` or a section of a
planet-scoped route, and is the first thing to try next.

**Cheaper alternative I did not use:** the frontend bundle at `veydrift.com` contains the real route
list and the ABI. Extracting it would have replaced this entire pass. Recommended for whoever
continues.

## 12.3 Deriving the ID maps by cost fingerprinting

The API returns entities as bare integer IDs with a `cost` object and no names. `docs.md` publishes
base costs by name. At level 0, **live cost == base cost**, so the two tables join on the cost triple:

```
API  id 2  -> { metal: 225, crystal: 75, deuterium: 0 }
docs        -> Deuterium Synth: 225 metal, 75 crystal
=> id 2 = Deuterium Synthesizer
```

All 16 buildings, 15 technologies and 14 of 16 ships matched exactly and uniquely — no cost triple
collided, which is what makes the method sound. Two ship IDs did **not** match:

- id 14 (8,000 / 15,000 / 8,000) matched nothing in the catalog. It's the Pathfinder, which the docs
  reference once in the rapidfire table but omit from the ship list.
- id 15 matched Crawler, which the docs list *before* the unknown. So the contract inserts Pathfinder
  ahead of Crawler and the docs simply dropped a row.

**This method only works on a fresh account.** Once you have levels above 0, live cost ≠ base cost
and the join breaks. Capture the ID maps early, or from a throwaway wallet.

## 12.4 Verifying the published formulas

Rather than trusting `docs.md`, I reimplemented the formulas and checked them against values the API
returns. This is what upgraded "the docs say" into "confirmed".

```python
import math
def sl(base, L):                      # scaled level value(base, L)
    return 0 if L == 0 else math.floor(base * L * 11**L / 10**L)
```

Three independent duration checks, each isolating a different constant, all at level 0:

| Entity | Formula | Computed | API | Isolates |
| --- | --- | --: | --: | --- |
| Energy Tech | `(0+800)×3600 / (1000×(lab+1)×speed)` | 2880 | 2880 | research divisor |
| Small Cargo | `(2000+2000)×3600 / (2500×(yard+1)×2⁰×speed)` | 5760 | 5760 | production divisor |
| Metal Mine 1 | `(60+15)×3600 / (2500×(robo+1)×2⁰×speed)` | 108 | 108 | building divisor |

All three agree only if **universe speed = 1**. Three separate formulas converging on the same
unknown is much stronger evidence than any one of them, and it means every duration formula in the
docs can be used as written. This is the single highest-leverage thing to re-verify after any
contract upgrade.

## 12.5 Reading the planet

The API gives `deuteriumMultiplierBps` but the *temperature* is what drives several other mechanics,
so I inverted the published relation to recover it and then cross-checked:

```
docs:      deut mult bps = 12,800 − 20 × maxTemp
observed:  15,020 bps
solve:     maxTemp = (12,800 − 15,020) / 20 = −111 °C
API field: temperature: -111          ✓ consistent
```

That consistency check is the point — it confirms the API's `temperature` *is* the "planet maximum
temperature" the formulas refer to, rather than a mean or a surface value. Only then is it safe to
feed it into the second formula:

```
solar satellite energy = clamp(trunc((maxTemp + 140) / 6), 1, 65)
                       = trunc(29 / 6) = 4
API field: energyBalance.sources.solarSatelliteEnergy: "4"    ✓ confirmed by the contract
```

So one input (temperature) drives two opposed effects on the same planet: **best-in-class deuterium,
worst-in-class satellites.** That tension is the whole strategic character of 664, and it fell out
of inverting one formula.

The energy-crossover table was then generated rather than estimated:

```python
for L in range(1, 11):
    demand = sl(10, L) * 2 + sl(20, L)      # metal + crystal + deuterium mine demand
    s = 1
    while sl(20, s) < demand: s += 1        # smallest Solar Plant that covers it
    print(L, demand, s)
```

Output: mines at 3 → Solar 5 · at 5 → Solar 8 · at 7 → Solar 11 · at 10 → Solar 14.

## 12.6 Reading the owner

Everything knowable about the account came from two routes, with no privileged access:

| Fact | Field | Route |
| --- | --- | --- |
| Display name `D@f7pUnK` | `player.displayName` | `/settlement` |
| Single planet, no colonies | `homePlanetId` present, one entry | `/settlement` |
| No alliance | `occupiedBy.alliance: null` | `/universe/...` |
| Never acted | all four queues `null`, `technologyLevels: {}`, all building levels 0 | `/queues`, `/infrastructure` |
| Untouched starting grant | 1,000 metal / 1,000 crystal / 0 deuterium | `/settlement` |
| Account age ≈ 4 days | see below | both |

Account age was established **two independent ways**, which matters because either alone could be
misread:

```
1. lastSettledAt = 1786121739  ->  2026-08-07T16:55:39Z          (contract clock)
2. settled at block 49666196; latest indexed 49839333
   delta 173,137 blocks x 2s Base block time = 4.01 days          (chain clock)
```

Both land on 2026-08-07. Agreement rules out a stale `lastSettledAt` — relevant because
`lastSettledAt` is a *lazy settlement* marker, not necessarily a creation time, and on an active
planet it would have advanced. On this account it hasn't moved since settlement, which is itself the
proof that nothing has happened.

**Deliberately not attempted:** clustering the owner's other on-chain activity, funding sources, or
linked addresses. It's an account I was asked to help operate, not to profile, and none of it would
change the build order.

## 12.7 Deriving the strategy

Each recommendation traces to a specific observation. Nothing here is genre knowledge imported from
OGame — that was the main thing I tried to avoid.

| Observation | Inference | Recommendation |
| --- | --- | --- |
| deut ×1.502, near range top | The planet's comparative advantage is fuel and Hyperspace-tier research | Push Deuterium Synthesizer earlier than a generic opener would |
| satellite energy = 4 vs ~34 hot | The cheap energy option is unavailable here | Solar Plant early, Fusion later; **never** satellites |
| Solar curve lags 3-mine demand, gap widens | A fixed level-offset rule silently fails as you grow | Compute `required` vs `produced` before every mine upgrade |
| 174 fields | Ample; not a binding constraint for a long time | Don't optimise field usage yet; warn at 80% |
| Storage cap 10,000, overflow discarded | An unattended planet stalls at cap | Added an explicit overflow gate to the agent prompt |
| 2 debris fields across ~195 planets | Combat is effectively not happening in this universe | Economy compounds, raiding doesn't — offense off by default |
| 85 moons vs 2 moon-chance reports | Moons are bought via Chicken NFT burns, not won | Don't plan a moon around combat debris |
| 7:181 has 10 slots, 1 occupied (yours) | No nearby targets, and no nearby threats | Safe to grow tall before going wide |
| `fleetSlots.limit: 1` | One mission at a time until Computer Tech | Computer Technology earlier than combat tech |
| Score protection 1.5× under 50k | Even if you wanted to raid, the target pool is tiny | Reinforces economy-first |

The universe-scale figures came from the `indexer` block that rides along on most wallet responses —
`indexedPlanets`, `indexedMoons`, `indexedDebrisFields`, `indexedMoonChanceReports`. Sampling the
same block four days apart gave the growth rate (179 → 195 planets, ~4/day). **Two samples is a weak
trend**; treat the growth number as indicative only, and keep sampling.

## 12.8 Where the method corrected itself

Worth recording, because both errors were caught by the process rather than by luck:

1. **"Keep Solar Plant ~2 levels above your highest mine."** Written from the level-1–3 range, where
   it happens to hold. Generating the full crossover table (§12.5) showed the gap widens to 4 levels
   by mine level 10. Corrected in all three deliverables.
2. **"The missing ABI is the blocking gap."** True when written, false two passes later — once
   `/infrastructure` turned up returning live costs, the ABI turned out to be needed only for writes.
   The lesson: **exhaust the read surface before declaring a dependency.**

## 12.9 Limits of this method

- **Single-account, zero-state.** Every observation is of a fresh planet with all levels at 0. Cost
  scaling, queue behaviour under load, lazy settlement, and combat are entirely unobserved.
- **Two time samples.** Growth rates are indicative, not measured.
- **No write path tested.** Nothing here validates that a transaction constructed from these IDs
  actually succeeds. That's the obvious next milestone, and why the prompt mandates a dry run.
- **`protectedResources` unexplained** (§6) — carried forward as an open question rather than
  papered over.
- **Formulas verified only at level 0.** The duration checks isolate the constants cleanly, but a
  level-dependent bug would not have shown up.

## 12.10 Re-running this for another planet

```
1. GET /runtime-config                     # chain, addresses, deploymentAbiHash — trust this over any README
2. GET /health                             # gate on ok + readiness.ready ONLY (see §9)
3. GET /wallet/{addr}/settlement           # coords, fields, temperature, multipliers, player
4. GET /wallet/{addr}/infrastructure       # levels, live costs, energyBalance, storageCaps, production
5. GET /wallet/{addr}/research             # tech levels + live costs
6. GET /wallet/{addr}/shipyard             # ships, fleetSlots
7. GET /universe/galaxies/{g}/systems/{s}  # neighbours, debris, moons, reservations
8. Invert: maxTemp = (12,800 - deutMultBps) / 20; cross-check against `temperature`
9. Read energyBalance.sources.solarSatelliteEnergy — decides the whole energy strategy
10. Generate the energy-crossover table with the sl() snippet in §12.5
11. Sample the `indexer` block for universe scale; repeat weekly for a real trend
12. Re-run the three duration checks in §12.4 to confirm universe speed is still 1
```

Steps 8–9 are the ones that produce planet-*specific* advice rather than generic advice. Everything
else is bookkeeping.

---

# 13. Contract analysis — can a planet be transferred to another wallet?

**Short answer: no.** Planets are not transferable, not sellable, and not NFTs. Verified by reading
the deployed source, not inferred.

## 13.1 Where the source actually is

Correcting §5 and §12.2: **`raw.githubusercontent.com` serves the contracts fine.** The GitHub
*contents API* returns empty anonymously, which earlier led me to call the source un-browsable — but
raw file access works, and so does Blockscout. Three working routes to the code, none needing a key:

| Route | URL |
| --- | --- |
| Source (best) | [`raw.githubusercontent.com/Borodutch/veydrift/main/packages/contracts/src/VeydriftGame.sol`](https://raw.githubusercontent.com/Borodutch/veydrift/main/packages/contracts/src/VeydriftGame.sol) — for the pinned deployment commit specifically, use [the `701bed35` blob instead](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) |
| Verified contract + ABI | `base.blockscout.com/api/v2/smart-contracts/{implementation}` |
| Proxy → implementation | `base.blockscout.com/api/v2/addresses/{proxy}` → `implementations[0]` |

Game proxy `0xf397…755d` → implementation **`0xf210b66b23731971ac606fC2C5c29a96eA19A99d`**.
Migration proxy `0x33A5…f7d3` → implementation `0x5DbAA02383fBb44f48bd469078429b5aE4cBFEC7`.

Note: Blockscout's JSON for a large contract lands in a single ~70 KB line, which ripgrep silently
skips. Fetching the `.sol` from raw GitHub avoids that entirely — do that instead.

## 13.2 The evidence

[VeydriftGame.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol) is a facade that enumerates **every** external entrypoint before delegating to
modules. That makes it a complete, checkable inventory of what the game can do.

1. **There is no planet transfer function.** No `transferPlanet`, `sellPlanet`, `giftPlanet`,
   `setPlanetOwner`, or equivalent — anywhere in the facade.
2. **Planets are not tokens.** Ownership is a plain struct field, `_planets[planetId].owner`, plus
   `_ownedPlanetIds[player]` / `homePlanetOf[player]` / `planetCountOf[player]`. There is no ERC-721
   in the system; `/runtime-config` lists only the three ERC-20 resource tokens.
3. **`transferOwnership(address)` is a decoy — do not misread it.**
   ```solidity
   function transferOwnership(address nextOwner) external onlyOwner { ... _owner = nextOwner; }
   ```
   This is the **game contract admin**, not a planet. Calling it does nothing to your planet, and an
   ordinary player can't call it anyway. It is the single most likely thing for an agent or a hasty
   reader to mistake for planet transfer.
4. **Migration is not a transfer path.** `importMigratedState(address player, bytes payload)` looks
   promising until you read the module:
   ```solidity
   if (msg.sender != _migrationSettlement) revert Unauthorized(msg.sender);
   if (msg.value != startPrice) revert BadStartPayment();
   if (state.player != player || ...) revert InvalidId();
   ```
   Only the operator's migration-settlement contract may call it, and the payload's `player` must
   equal the destination. The module's own comment says *"Delegatecall target for signed
   testnet-to-mainnet state imports."* It is a one-way operator-run migration, not a player action.
   It also requires the destination wallet to be empty or hold exactly one untouched starter planet
   (`_discardSingleStartedPlanetBeforeMigration`, which reverts if `planetCountOf != 1` or any fleet
   mission is active).
5. **`abandonPlanet(uint256)` exists** — you can give a planet *up*, freeing the coordinate. That
   destroys it; it does not hand it to anyone.

## 13.3 What *can* move between wallets

- **Resources, via an undocumented market bridge.** The facade exposes
  `depositMarketResource(uint256 planetId, Resource, uint128)`,
  `requestMarketResourceWithdrawal(...)` and `finishMarketResourceWithdrawal(Resource)`. This is how
  in-game Metal/Crystal/Deuterium become the ERC-20 proxies in `/runtime-config` — and **ERC-20s are
  freely transferable.** `docs.md` never mentions this system. The withdrawal is two-phase
  (request → finish), so expect a delay or lock; `_lockedWithdrawalResources` appears in the reserve
  accounting.
- **Resources in-game**, via a Transport mission to another player's planet.
- **Not ships** — Deploy only moves fleets between bodies *you own*.

So the value on a planet is partially extractable, but the planet, its buildings, its research and
its fleet are permanently bound to the wallet that settled them.

## 13.4 The practical consequence

**The only way to hand this account to someone else is to hand over the private key.** That is not a
transfer — it's shared custody, irreversible, and it also hands over the ability to drain anything
else that wallet ever touches. This is precisely why the burner-wallet posture in
`veydrift-agent-resources.md` §1 matters: the wallet *is* the account, permanently.

If the goal is to let someone else (or an agent) operate the planet, key custody is the only
mechanism the game offers. If the goal is to exit, the options are: bridge resources out via the
market, Transport what you can to a friendly planet, then `abandonPlanet`.

## 13.5 Other findings from the source, worth recording

Things `docs.md` either omits or states imprecisely:

| Finding | Detail |
| --- | --- |
| Colony capacity formula | `maxPlanets(player) = 1 + Astrophysics level`. Docs only said Astrophysics "raises colony capacity". |
| Terraformer | `_planets[planetId].fields += 5` — exactly **+5 fields per level**. Docs said only "adds planet fields". |
| Dreadstar = Deathstar | The enum member is `Ship.Deathstar`. Confirms §3 — the rapidfire tables use the internal name. |
| Pathfinder confirmed | `Ship.Pathfinder` is a real enum member and is mission-capable. |
| Mission-capable ships | `_missionShipQuantity` excludes Solar Satellite and Crawler — they cannot fly, as expected. |
| Rift Stabilizer | `Building.InterdimensionalRiftStabilizer`, hard-capped at level 1. |
| Cost factors are rationals | `buildingCostFactor(building)` returns `(numerator, denominator)` per building — not a single float. Still read live costs. |
| Lazy building completion | `startBuildingUpgrade` settles first, so a finished upgrade completes automatically. **No separate finish tx is needed** — `finishBuildingUpgrade` is a back-compat no-op wrapper. Agents should not waste gas calling it. |
| Defense counts are on-chain | `defenseCount(planetId, Defense)` is a public view. This is the workaround for the missing defense API endpoint (§1). |
| Game can be paused | `setGamePaused(bool)`. Most gameplay delegations call `_requireGameNotPaused()`; first-planet settlement does not. An agent should handle a paused-game revert gracefully. |
| Admin powers | The contract owner can pause the game, repoint the moon/alliance/randomness systems, upgrade the proxy, and `releaseExcessResourceReserves` to a treasury. Normal for an alpha, but it is real centralisation — worth knowing before committing serious value. |

**Useful read functions for an agent** (all public views on the game proxy, no API needed):
`planet`, `buildingLevel`, `shipCount`, `defenseCount`, `technologyLevel`, `productionPerHour`,
`energyBalance`, `storageCaps`, `previewResources`, `buildingUpgradeCost`, `researchCost`,
`shipCost`, `defenseCost`, `maxPlanets`, `playerScore`, `isCoordinateAvailable`, `fleetMission`,
`researchQueue`, `activeBuildingConstruction`, `shipQueue`, `defenseQueue`.

---

*All figures read live from `api.veydrift.com` on 2026-08-11 unless dated otherwise. Contract
analysis in §13 is against `main` on GitHub and the deployed implementation at
`0xf210b66b23731971ac606fC2C5c29a96eA19A99d`; re-check after any proxy upgrade. Anything marked
unresolved is genuinely unresolved — please don't let it harden into an assumption.*
