# Veydrift read API — route reference

**Probe date for everything in this document that says "confirmed live":** 2026-08-12,
against `https://api.veydrift.com`, wallet `0x224aba5d489675a7bd3ce07786fada466b46fa0f`,
planet `664` (coordinates `7:181:14`), an unauthenticated GET for every route. Where this
document cites the backend source instead of a live probe, it says so explicitly and gives
a `file:line` against the Veydrift backend repository at commit `84e468f` (`main` HEAD at
clone time — the *backend* is not the same drift risk as the *contract*; see §0 below).

Verified against this skill's source repository as of 2026-08-12; a small number of
field-level details in this document were found to differ from that repository's own
earlier backend-source-derived notes, and are called out explicitly in §9 where that
happened.

## Table of contents

- [0. A note on drift: contract vs backend](#0-a-note-on-drift-contract-vs-backend)
- [1. Health gating — the rule every consumer must follow](#1-health-gating--the-rule-every-consumer-must-follow)
- [2. `vd read` target → route map](#2-vd-read-target--route-map)
- [3. Route reference](#3-route-reference)
  - [3.1 `/health`](#31-health)
  - [3.2 `/runtime-config` (target: `config`)](#32-runtime-config-target-config)
  - [3.3 `/wallet/{addr}/settlement`](#33-walletaddrsettlement)
  - [3.4 `/wallet/{addr}/planets`](#34-walletaddrplanets)
  - [3.5 `/wallet/{addr}/queues`](#35-walletaddrqueues)
  - [3.6 `/wallet/{addr}/highscore`](#36-walletaddrhighscore)
  - [3.7 `/wallet/{addr}/infrastructure`](#37-walletaddrinfrastructure)
  - [3.8 `/wallet/{addr}/research`](#38-walletaddrresearch)
  - [3.9 `/wallet/{addr}/shipyard`](#39-walletaddrshipyard)
  - [3.10 `/wallet/{addr}/defenses`](#310-walletaddrdefenses)
  - [3.11 `/wallet/{addr}/moon`](#311-walletaddrmoon)
  - [3.12 `/wallet/{addr}/overview`](#312-walletaddroverview)
  - [3.13 `/wallet/{addr}/fleet-visibility`](#313-walletaddrfleet-visibility)
  - [3.14 `/wallet/{addr}/missions`](#314-walletaddrmissions)
  - [3.15 `/wallet/{addr}/activity`](#315-walletaddractivity)
  - [3.16 The three "universe" routes](#316-the-three-universe-routes)
  - [3.17 `/battle-reports`](#317-battle-reports)
  - [3.18 `/highscores`](#318-highscores)
  - [3.19 `/chain/events` — not exposed, and why](#319-chainevents--not-exposed-and-why)
  - [3.20 `/raid-finder/debris`](#320-raid-finderdebris)
  - [3.21 `/wallet/{addr}/attack-protection`](#321-walletaddrattack-protection)
- [4. Queue parsing (`QueueState`) — typed from source, not from a live sample](#4-queue-parsing-queuestate--typed-from-source-not-from-a-live-sample)
- [5. Incoming-fleet parsing (`FleetMissionSummary`) — same caveat](#5-incoming-fleet-parsing-fleetmissionsummary--same-caveat)
- [6. Entity ID → name tables](#6-entity-id--name-tables)
- [7. `snapshot`'s composition, and why it substitutes `overview` for `fleet-visibility`](#7-snapshots-composition-and-why-it-substitutes-overview-for-fleet-visibility)
- [8. Exit codes and the `bad planetId` gotcha](#8-exit-codes-and-the-bad-planetid-gotcha)
- [9. Corrections a full live probe found, over earlier backend-source-derived research](#9-corrections-a-full-live-probe-found-over-earlier-backend-source-derived-research)
- [10. The disk cache vs. the backend's own response cache](#10-the-disk-cache-vs-the-backends-own-response-cache)
- [11. Undocumented-but-live routes not wired into `vd read`](#11-undocumented-but-live-routes-not-wired-into-vd-read)

---

## 0. A note on drift: contract vs backend

The **contract** on `main` has drifted from the deployed one (`playerScore` exists on
`main`, reverts on the deployed implementation). That drift is specific to
`packages/contracts/`. The **backend** (`apps/backend/src/server.ts`, `evm.ts`, etc.)
is a live, currently-running service that this whole route table is read against
directly — every route below was both (a) read from `apps/backend/src/server.ts` at
`main` HEAD `84e468f` and (b) probed live and got the shape this document describes.
There is no equivalent "backend main has drifted from deployed backend" risk the way
there is for the contract, because the backend isn't proxy-upgraded the way the game
contract is — it's just the running server. Still, re-probe after any backend deploy.

---

## 1. Health gating — the rule every consumer must follow

```
health_ok = (data.ok is True) AND (data.readiness.ready is True)
```

**Nothing else.** Confirmed live (`/health`, 2026-08-12, landed on a reader worker):

```json
{
  "ok": true,
  "backend": { "worker": { "role": "reader", "index": 3, "count": 10 } },
  "readiness": {
    "ready": true, "degraded": false, "degradationReasons": [],
    "chainSyncConnected": null, "subscribedToHeads": null, "subscribedToLogs": null,
    "indexedState": null, "safeToServeIndexedState": null,
    "missionResolutionStatus": null
  },
  "chainSync": null, "missionResolution": null, "indexer": null, "rpc": null
}
```

Every one of those `null`s is present *while `ok` and `readiness.ready` are both
`true`* — this is `apps/backend/src/server.ts:558-604`'s `backendReadiness()` reporting
that this specific worker process (`role: "reader"`) doesn't run chain sync or hold an
indexer instance; only worker 0 (`role: "writer"`) does. **Do not treat any of those
nulls as an outage signal**, and do not gate on `chainSync`/`indexer`/`rpc` being
non-null. `read.py`'s `_health_ok()` implements exactly the two-field check above.

Freshness (separate from health) comes from the `indexer` block riding on every
**wallet** route (not `/health`): `indexedState: "healthy"` and
`safeToServeIndexedState: true`. `lastReconciledBlock` sitting ~1.5M blocks behind
`latestIndexedBlock` is expected — full reconciliation is an
explicit operator action, not a background loop. Never gate on that gap.

---

## 2. `vd read` target → route map

| `vd read` target | Route | Method | Extra flags used |
| --- | --- | --- | --- |
| `health` | `/health` | GET | — |
| `config` | `/runtime-config` | GET | — |
| `settlement` | `/wallet/{addr}/settlement` | GET | `--wallet` |
| `planets` | `/wallet/{addr}/planets` | GET | `--wallet` |
| `queues` | `/wallet/{addr}/queues` | GET | `--wallet`, optional `--planet-id` |
| `highscore` | `/wallet/{addr}/highscore` | GET | `--wallet` |
| `infrastructure` | `/wallet/{addr}/infrastructure` | GET | `--wallet`, **required** `--planet-id` |
| `research` | `/wallet/{addr}/research` | GET | `--wallet`, **required** `--planet-id` |
| `shipyard` | `/wallet/{addr}/shipyard` | GET | `--wallet`, **required** `--planet-id` |
| `defenses` | `/wallet/{addr}/defenses` | GET | `--wallet`, **required** `--planet-id` |
| `moon` | `/wallet/{addr}/moon` | GET | `--wallet`, **required** `--planet-id` |
| `overview` | `/wallet/{addr}/overview` | GET | `--wallet`, **required** `--planet-id` |
| `fleet-visibility` | `/wallet/{addr}/fleet-visibility` | GET | `--wallet` only (no `--planet-id`; see §3.13) |
| `missions` | `/wallet/{addr}/missions` | GET | `--wallet`, optional `--planet-id` |
| `activity` | `/wallet/{addr}/activity` | GET | `--wallet` |
| `universe` | `/universe/galaxies/{g}/systems/{s}` (two-step; see §3.16) | GET | `--wallet`, **required** `--planet-id` |
| `battle-reports` | `/battle-reports` | GET | **`--out` mandatory**, no `--wallet`/`--planet-id` |
| `highscores` | `/highscores` | GET | **`--out` mandatory**, no `--wallet`/`--planet-id` |
| `snapshot` | composed — see §7 | GET ×6 | `--wallet`, optional `--planet-id` (else discovers all) |

Every command additionally accepts `--json/--summary` (default `--summary`) and
`--max-age` — except `battle-reports`/`highscores`, which don't expose `--json`/
`--summary` at all: `--out` is mandatory for both and stdout is refused
unconditionally; see §8.

---

## 3. Route reference

Each entry: query params, a trimmed real response (2026-08-12), and payload-shape
notes. Full untrimmed captures are in `tests/fixtures/` (used by `test_read.py`'s
`respx` mocks — see that directory's own provenance note).

### 3.1 `/health`

No params. Shape and gating rule: §1. Server-side response cache TTL: 10s
(`apps/backend/src/server.ts:2604`) — separate from `vd`'s own 15s disk cache; see §10.

**`gameMaintenance` / `readiness.degradationReasons` — confirmed live 2026-08-20/21, not
in the original 2026-08-12 capture above.** An agent session checking live status found
the game genuinely paused for chain-side maintenance; a follow-up live fetch the same day
(game no longer paused) confirmed the field's normal, not-paused shape:

```json
"readiness": {
  "ready": true, "degraded": false, "degradationReasons": [],
  "gamePaused": false, "gamePauseAgeSeconds": 0
},
"gameMaintenance": {
  "paused": false, "observedAt": "2026-08-20T22:59:26.727Z",
  "pausedSince": null, "pauseAgeSeconds": 0
}
```

Key findings:

- **`gameMaintenance` is always present**, not absent when not paused — `{"paused":
  false, ...}` is the normal shape. `veydrift_agent.models.GameMaintenance` on `Snapshot`
  is `None` only for a malformed/future-changed response (or an older backend that
  predates the field, e.g. this file's own `health.json` fixture, captured 2026-08-12) —
  never read `None` as "confirmed not paused."
- **`gameMaintenance.pauseAgeSeconds`** is an int, seconds-paused-so-far, straight from
  the API — preferred over computing duration from `pausedSince` client-side (avoids
  clock-skew/timezone math).
- **`readiness` carries its own flattened `gamePaused`/`gamePauseAgeSeconds`**, redundant
  with `gameMaintenance`. `read.py`'s `_game_maintenance` deliberately reads only from
  `gameMaintenance` as the single source of truth, not both.
- **`degradationReasons` is genuinely free-form**, now actually consumed (previously
  documented but unused — see the "Undocumented-but-live" style caveats elsewhere in this
  file). `tests/fixtures/health_unhealthy.json` already carries a *different* real reason
  ("Upstream RPC unfinished requests are growing or stale."), so a consumer must never
  assume `"game_paused"` is the only possible entry.
- **`ok: false` and a game pause are independent signals.** The same 2026-08-20 live check
  found `/health`'s top-level `ok: false` for a reason unrelated to any pause
  (`randomnessReadiness.ready: false`, a randomness-safety-check issue) while
  `readiness.ready`/`degraded` were both fine — confirming `_health_ok`'s existing
  `ok`/`readiness.ready` check is a broad, multi-cause signal, and exactly why a dedicated
  `gameMaintenance.paused` check (`plan.py` rung `1b`, `guard.py`'s `game_paused` gate)
  needed to be a separate signal rather than folded into `health_ok`.
- `tests/fixtures/health_paused.json` is a hand-edited copy of the 2026-08-20 live capture
  with `gameMaintenance.paused: true`, a real-shaped `pausedSince`/`pauseAgeSeconds`, and
  `readiness.degradationReasons: ["game_paused"]` — synthesized rather than captured
  during an actual pause, per this file's own existing convention for `health_unhealthy.json`
  and the wallet-route synthetic fixtures (§4/§5).
- **`randomnessReadiness`-only degradation confirmed persistent, and confirmed served via
  HTTP 503 (2026-08-22).** Re-observed live (`curl`, twice, moments apart): still
  `ok: false`, `randomnessReadiness.ready: false`, everything else fine — this is not a
  one-off. New this observation: the HTTP status was **503**, not 200 — this backend
  apparently signals `ok:false` via a non-2xx status on `/health` specifically, not only
  via a 200-with-`ok:false` body (the 2026-08-20 observation didn't record HTTP status,
  so it's unconfirmed whether that one was also a 503). `randomnessReadiness` is its own
  top-level object, with its own `reasons` array (plural) — **not** the same list as
  `readiness.degradationReasons`, which was empty (`[]`) in this same response:
  ```json
  "randomnessReadiness": {
    "ready": false,
    "reasons": ["The randomness safety check is unavailable. New attacks are temporarily paused."],
    "updatedAt": "2026-08-22T05:37:42.301Z"
  }
  ```
  `read._recover_health_body()` defensively parses a `/health` 5xx's captured error body
  (JSON-shaped, has a `readiness` key) instead of hard-aborting, narrowly scoped to this
  one route; `Snapshot.combat_only_degradation()` (`skills/veydrift-agent/references/
  guardrails.md`'s `health` gate section has the full design) is what then decides
  whether that's safe to proceed past. `tests/fixtures/health_randomness_degraded.json`
  is this exact live capture, used directly (not hand-edited) as the mocked 503 body in
  the new tests.

### 3.2 `/runtime-config` (target: `config`)

No params. Confirmed live 2026-08-12:

```json
{
  "chainId": 8453, "network": "Base",
  "contractAddress": "0xf397910F005151b09644228573a4353818D3755d",
  "backend": { "build": {
    "deploymentAbiHash": "sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99",
    "deploymentCommit": "701bed3578cff4d134657c714c599dbdb55a4b6a"
  }},
  "featureSupport": { "researchEndpoint": true, "highscoresEndpoint": true, "...": "..." }
}
```

This ABI hash and deployment commit match `skills/veydrift-wallet/abi/PINNED.json` exactly.

### 3.3 `/wallet/{addr}/settlement`

No params. Player identity + home planet event data: `displayName`, `homePlanetId`,
`planet.{galaxy,system,position,fields,temperature,*MultiplierBps,resources,
resourcesAsOfNow}`. **No `archetype` field** — that only appears on the universe routes
(§3.16). No `fieldsUsed`/`fieldsCapacity` either — those are on `planets` (§3.4).

### 3.4 `/wallet/{addr}/planets`

No params. One row per owned planet, richer than `settlement`'s embedded planet:
`coordinates` (pre-formatted `"7:181:14"` string), `fieldsUsed`, `fieldsCapacity`,
`keyLevels` (a *subset* of buildings: metalMine/crystalMine/deuteriumSynthesizer/
solarPlant/roboticsFactory/shipyard/researchLab/terraformer only — not the full 16),
`queues.{building,defense,ship}` (planet-scoped; **not** `research`, which is
player-scoped and lives at the row's `queues` sibling — see the real payload's top-level
`"queues": {"research": null}` alongside `"planets": [...]`), and a `tactical` block
(`raidableResources`, `combatPower`, `combatTechLevels`, ship/defense counts+power).
`vd read universe` uses this route to resolve `--planet-id` → `galaxy`/`system` (§3.16).

### 3.5 `/wallet/{addr}/queues`

Query: `planetId` (optional). Shape: `{wallet, homePlanetId, building, defense, ship,
research}`, each a `QueueState | null` (§4). `apps/backend/src/server.ts:147` lists
`planetId` as an accepted cache-fragmenting param for this route, confirming it *does*
filter `building`/`defense`/`ship` to the given planet — unlike `fleet-visibility`
(§3.13), where the same-shaped param is silently ignored. `research` is player-scoped
and returned regardless of `planetId`. On the probed zero-state account all four are
always `null`; queue-parsing is verified against the backend source type instead (§4).

### 3.6 `/wallet/{addr}/highscore`

No params. **Singular** — this wallet's own score row, not the leaderboard (that's
`/highscores`, plural, §3.18; don't confuse the two commands). Confirmed live:

```json
{
  "formula": { "summary": "Veydrift Score uses the contract-parity totalUserScore formula...", "...": "..." },
  "entry": {
    "wallet": "0x224a...fa0f", "homePlanetId": "664", "planetCount": 1,
    "totalUserScore": "1000",
    "score": {"total": "0", "economy": "0", "research": "0", "researchLevels": "0",
              "military": "0", "fleet": "0", "fleetCount": "0", "defense": "0"}
  },
  "source": "contract-state-indexer"
}
```

`totalUserScore` (1000) already counts the un-upgraded starting grant; the category
breakdown (`entry.score.*`) is all zero until something is built/researched.

### 3.7 `/wallet/{addr}/infrastructure`

Query: `planetId` (**required** — the route 500s without it in practice, since nothing
downstream can resolve a planet). Confirmed live 2026-08-12 (trimmed):

```json
{
  "wallet": "0x224a...fa0f", "homePlanetId": "664", "planetId": "664",
  "resources": {"metal": "1000", "crystal": "1000", "deuterium": "0"},
  "resourcesAsOfNow": {"metal": "1000", "crystal": "1000", "deuterium": "0"},
  "energyBalance": {
    "produced": "0", "required": "0", "scaleBps": "10000",
    "sources": {"solarPlant": "0", "fusionReactor": "0", "solarSatelliteEnergy": "4"}
  },
  "productionPerHour": {"metal": "0", "crystal": "0", "deuterium": "0"},
  "protectedResources": {"metal": "0", "crystal": "0", "deuterium": "0"},
  "raidableResources": {"metal": "500", "crystal": "500", "deuterium": "0"},
  "storageCaps": {"metal": "10000", "crystal": "10000", "deuterium": "10000"},
  "buildings": [{"id": 0, "level": 0, "cost": {"metal": "60", "crystal": "15", "deuterium": "0"}, "durationSeconds": 108}, "... 15 more, ids 0-15"],
  "queue": null,
  "indexer": {"indexedState": "healthy", "safeToServeIndexedState": true, "latestIndexedBlock": "49861181"}
}
```

Notes:
- `buildings[]` items are `{id, level, cost, durationSeconds}` — **no `name` field**.
  The API never sends entity names on any route (confirmed across all four
  infrastructure/research/shipyard/defenses routes); `read.py` fills `Entity.name` from
  `ids.py`/local tables (§6), not from the wire.
- `energyBalance.sources.solarSatelliteEnergy` is the contract's own precomputed value —
  read it directly, never recompute from temperature.
  `energyBalance.produced`/`required`/`scaleBps` arrive as **strings** ("0", "10000");
  pydantic coerces them, per `models.py`'s module docstring convention.
- `raidableResources`/`protectedResources` are present and populated even at zero state.
  What `protectedResources` actually means is still an open question — `models.py` still
  won't build a loot model on it.
- No `coordinates`/`fields`/`temperature`/`archetype` on this route at all — that's why
  `snapshot` also needs `overview` (§7).

### 3.8 `/wallet/{addr}/research`

Query: `planetId` (**required**, same reason as §3.7, even though the data itself —
`technologies[]`, `researchLabLevel`, `researchNetworkLabLevels` — is per-*player*, not
per-planet; `resources`/`resourcesAsOfNow` in the response are the given planet's).
`technologies[]` items: `{id, level, cost, durationSeconds}`, ids 0-14, same
no-name-field pattern as buildings.

### 3.9 `/wallet/{addr}/shipyard`

Query: `planetId` (**required**). `ships[]` items: `{id, count, cost, durationSeconds}`
(note: `count`, not `level` — matches `models.Entity`'s `count` field for ships/
defenses). Also carries `fleetSlots: {active, limit}` (confirmed `{active: 0, limit: 1}`
at zero state — 1 slot at Computer Technology 0), `launchableShips` (identical to `ships` at zero state; presumably filters
zero-fuel-range ships when a wallet has any), `shipyardLevel`, `naniteLevel`.

### 3.10 `/wallet/{addr}/defenses`

Query: `planetId` (**required**). This route was previously unconfirmed by earlier
backend-source-derived research — confirmed live here at exactly this name, no auth, no
trick to it. `defenses[]` items:
`{id, count, cost, durationSeconds}`, ids 0-9 (§6's `Defense` enum). **Shape quirk**:
this is the one wallet route in this whole set whose top level has `homePlanetId` but
**no `planetId` field** — infrastructure/research/shipyard/moon/overview all echo both.
Cosmetic (doesn't affect parsing, since `read.py` never reads a `planetId` echo off the
body), but worth knowing if you're eyeballing a raw dump. Also carries
`missileSiloLevel` alongside `shipyardLevel`/`naniteLevel`.

### 3.11 `/wallet/{addr}/moon`

Query: `planetId` (**required** — this is the *parent planet's* id; the route reports
`parentPlanetId` echoing it back, plus `bodyKind: "moon"`). At zero state:
`moonAvailable: true` (misleadingly — read `unavailableReason: "No moon exists for this
home planet yet."` instead of trusting the boolean alone) with `moon: null`.

**Notable shape difference from every other production route**: `moon.buildings[]`
items carry `{id, key, label, level, cost, durationSeconds}` — `key` (e.g.
`"lunarBase"`) and `label` (e.g. `"Lunar Base"`) are present here and **nowhere else**.
If a moon-specific renderer is ever added, it can use `label` directly instead of an ID
table; `read.py` doesn't currently parse moon buildings into `PlanetSnapshot` (moon
state isn't part of `Snapshot` in `models.py`), so this is documented for the next
consumer rather than acted on now.

### 3.12 `/wallet/{addr}/overview`

Query: `planetId` (**required**). Bundles `settlement` + `planetsResponse` (= the
`planets` route's body) + `queues` + `fleetVisibility` — confirmed live to be
byte-identical in shape to calling those three/four routes separately and assembling
them by hand. Does **not** include `infrastructure`/`research`/`shipyard`/`defenses`.
This is the route `snapshot` uses
to get planet metadata (coordinates/fields/temperature) and incoming-fleet data in one
call — see §7 for why.

### 3.13 `/wallet/{addr}/fleet-visibility`

Query: `archive` (per `apps/backend/src/server.ts:141`'s cache-param table) — `vd read
fleet-visibility` does not expose an `--archive` flag, so it always fetches the default.
**No `--planet-id`**: confirmed both
by source comment (`server.ts:139-140`: *"The endpoint is wallet-scoped; `planetId` is
currently ignored by its handler"*) and empirically (`?archive=none` and no query at all
returned byte-identical bodies on the probed account). Shape: `{incoming, outgoing,
returning, joinableAttacks, completedMissions, battleReports, indexedRevision,
indexedBlock, generatedAt}`, each a `FleetMissionSummary[]` (§5) except `battleReports`.
`incoming` is the hostile-fleet escalation surface — but see
§5's caveat that "incoming" isn't provably hostile-only from the source alone.

### 3.14 `/wallet/{addr}/missions`

Query: `filter`, `missionNumber`, `missionType`, `planetId`, `status`, `page`,
`pageSize` (default `page=1`, `pageSize=25`). `vd read missions` wires through
`--planet-id` only. Shape: `{wallet, homePlanetId, rows,
pagination}`; `rows[]` is `FleetMissionArchiveEntry` — a tagged union of `{kind:
"mission", mission, report?}` or `{kind: "battleReport", report}`
(`apps/backend/src/evm.ts:559-561`). Empty (`rows: []`) at zero state.

Note: there is *also* a global, non-wallet-scoped `/missions` route
(`apps/backend/src/server.ts:1416`) — different shape
(`{missions: FleetMissionSummary[]}` for `status=active`, or a
`GlobalMissionArchiveResponse` otherwise), not wired into `vd read` since it isn't
wallet/planet-scoped like the rest of this tool.

### 3.15 `/wallet/{addr}/activity`

Query: `includeProjected`, `page`, `pageSize`, `since` — `vd read activity` uses none of
these, always fetching page 1 defaults. Shape:
`{wallet, items, summary, through, pagination}`. `items[]` is a chronological event feed
(`{transactionAt, transactionHash, category, kind, direction, title, detail,
occurredAt, metadata: {galaxy, planetId, position, system}, ...}`) — e.g. `{"kind":
"planet-started", "title": "Home planet settled", "detail": "Planet #664 · 7:181:14"}`
for the account's one lifetime event so far — **still the only `/activity` item ever
actually observed by this project**, live or fixture. No routine building/research/ship/
defense-completion item has ever been seen here, so its `kind`/shape is unconfirmed
(the pinned ABI's `BuildingCompleted`/`ResearchCompleted`/etc. events strongly suggest
one exists, but that's inference from contract source, not observation — keep that
distinction in mind before trusting any code that assumes a specific `kind` value for
those).

Also consumed internally, bypassing this CLI command entirely: `read.fetch_activity()`
is called directly by `tick.py`'s `_maybe_check_human_activity` (the best-effort
"did a human execute my proposal by hand" check) with a
`since` param this CLI command has never exercised — see that function's docstring for
the resulting caveat about `since`'s wire format being an unverified assumption.

### 3.16 The three "universe" routes

This is the biggest point of confusion in the whole surface, and this project's earlier
backend-source-derived research only listed two of the three that actually exist:

| Route | Real indexed data? | Params | Confirmed live |
| --- | --- | --- | --- |
| `/universe/system` | **No — procedurally generated** (`generateSystem()`, `apps/backend/src/server.ts:1517-1519`) | `galaxyId`, `systemId`, `seed` | yes, 200 |
| `/universe/systems` | Yes (`cachedGalaxySystemPayload`) | `galaxy`, `center`, `radius` (≤10) — scans a *range* of systems | yes, 200 |
| `/universe/galaxies/{g}/systems/{s}` | Yes (same `cachedGalaxySystemPayload` backing as above) | `detail` (optional); galaxy/system are **path** segments, not query params | yes, 200 — **not previously documented anywhere in this project's own research** |

`vd read universe` uses the **third** one. Rationale: `vd read`'s CLI surface exposes no
galaxy/system flags for the `universe` target, only the standard
`--wallet`/`--planet-id`/etc. set, so this command derives galaxy:system by first
calling `/wallet/{addr}/planets` (§3.4) and matching `--planet-id` against
`planets[].galaxy`/`.system`, then fetches that one real, indexed system — which is
what an agent actually wants ("what's around my planet"), not a synthetic preview
(`/universe/system`) or a wider scan requiring its own radius parameter this command
doesn't expose (`/universe/systems`). Confirmed live response (trimmed, `galaxy=7,
system=181`):

```json
{
  "generatorVersion": "veydrift-universe-v1", "chainId": 8453,
  "galaxy": 7, "system": 181,
  "planets": [
    {"position": 3, "key": "7:181:3", "fields": 173, "temperature": 59,
     "archetype": "scorching-molten", "occupiedBy": null, "debrisField": null,
     "hasMoon": false, "moonChance": null},
    "... 9 more slots, only position 14 (this wallet's planet) occupied"
  ]
}
```

`archetype` lives **only** on this family of routes — never on any wallet route
(§3.3-§3.12). If a future planner needs archetype for a specific owned planet, it has to
call this route (or `/universe/systems`), not `overview`/`planets`.

**`debrisField` is confirmed live-populated, not always `null`.** The sample above happens
to show `null` for that particular slot, but a probe against a different system
(`/universe/galaxies/5/systems/200`) confirmed a real, non-null value:
`{"metal": "2400", "crystal": "2400"}` at an occupied slot (2026-08-27). `read.py` exposes
a general-purpose fetcher for this route, `fetch_universe_system(galaxy, system)`, used by
two callers: `_universe_archetype_for_planet` (reads only `archetype`, cadence-gated via
`policy.cadence.universe_hours`) and `tick.py`'s `_own_planet_debris` (reads `debrisField`
for the wallet's own planets, uncached — no cadence knob of its own, since debris changes
faster than archetype). `migrationReservation` is also present per slot and is not yet
read by anything in this codebase.

### 3.17 `/battle-reports`

Query: `page`, `pageSize` (default `page=1`, `pageSize=25`, capped at 100 —
`apps/backend/src/server.ts:3658-3665`'s `missionArchivePagination()`). `vd read
battle-reports` uses the defaults (no `--page`/`--pageSize` flags exposed).
**Returns a bare JSON array**, not an object with a `pagination`
wrapper (unlike `/highscores`, §3.18, and unlike `/wallet/{addr}/missions`, §3.14 — an
inconsistency across the API worth knowing before writing a generic paginator). Measured
**61,543 bytes** for the default 25-row page on 2026-08-12. `--out` is mandatory; see §8.

Each row: `{missionId, attacker, targetPlanetId, outcome, rounds, randomSeed, loot,
transactionHash, blockNumber, attackerLosses, defenderLosses, debris,
defenderSnapshot, roundReports[]}` — `roundReports` has one entry per combat round
(≤6 observed), which is most of the byte weight.

### 3.18 `/highscores`

Query: `category`, `currentWallet`, `includeAttackProtection`, `limit`, `live`, `page`,
`pageSize` (default `page=1`, `pageSize=50`). `vd read highscores` uses the defaults —
**and this is the single biggest correction in this document**: measured **2,269,161
bytes (~2.2 MB)** for the default page on 2026-08-12, not the ~86 KB figure this project's
earlier research had estimated. The reason: the default response
returns **8 ranking categories** (`total`, `economy`, `research`, `researchLevels`,
`military`, `fleet`, `fleetCount`, `defense`) × 50 rows each, and — unlike a typical
leaderboard — **every row embeds that player's full `homePlanet` object**, including a
nested `tactical` block (current/raidable resources, ship/defense unit breakdowns,
combat power). The ~86 KB figure in prior research likely came from a much smaller
universe population (the indexed-planet count grows ~4/day, and the account population has
grown since the figure was first measured) or from probing with `?category=` set to
a single category. **`--out` is mandatory regardless of size** (§8); this document flags
the size purely so nobody assumes "under 100 KB, safe-ish to eyeball via `--json` in a
pinch" — it is not.

Shape: `{generatedAt, durationMs, formula, pagination, currentPlayer, rankings, source}`
— `rankings` is a **dict keyed by category name**, each value a `HighscoreEntry[]`, not
a flat array. `pagination` describes the *page* (rows per category), not a global row
count.

**`category` does NOT filter `rankings` down to one key** — confirmed live 2026-08-28:
passing `?category=economy` still returned all 8 category keys in `rankings`, unchanged;
only `pageSize` bounds the per-category row count. A caller wanting one category's rows
must still index `rankings["economy"]` (or whichever) itself.

`includeAttackProtection=true` (combined with a `currentWallet` set to the caller's own
address — omitted, that field is `null` on every row) adds a per-row `attackProtection`
block: `{allowed, blockedReason, blockedReasonLabel, defenderInactive,
scoreComparison: {scoreType, attackerScore, defenderScore, attackerVisibleScore,
defenderVisibleScore, protected}, targetAlliance}`. This is an ACCOUNT-level check
(score protection + same-alliance only — no `targetPlanetId` is given, so it cannot see
the bashing-limit dimension `/wallet/{addr}/attack-protection`, §11, can). Each row also
carries `homePlanet.tactical.raidableResources`/`.coordinates` and a `homePlanetId`.

Moved out of §11 below 2026-08-28 (commit 6 of the launch-actions plan): `read.
fetch_highscores()` is a live caller now (`tick._attack_targets`), the same "bypass the
CLI/`_emit` layer, raw dict" posture `fetch_raid_finder_debris`/`fetch_fleet_visibility`
already take — not wired into `vd read`'s own CLI target list (that command stays the
mandatory-`--out` one described above), same as those two.

### 3.19 `/chain/events` — not exposed, and why

This project's earlier backend-source-derived research flagged this route as "200 but
slow (>2 min uncapped) — needs paging params; do not call naively," with an open
question to find those params in `server.ts` before using it. **Correction**: there are
no paging params to find. Reading the handler (`apps/backend/src/server.ts:650-661`):

```ts
if (request.method === "GET" && url.pathname === "/chain/events") {
  if (!chainSync) return unavailableResponse(loaded.problems);
  return new Response(chainSync.eventStream(request.signal), {
    headers: { ...corsHeaders, "cache-control": "no-cache", connection: "keep-alive",
               "content-type": "text/event-stream; charset=utf-8" }
  });
}
```

This is a **Server-Sent Events stream**, not a paginated JSON resource. Confirmed live
with a bounded (3s) probe:

```
HTTP/2 200
content-type: text/event-stream; charset=utf-8
cache-control: no-cache

event: sync-status
data: {"connected":true,"eventsReceived":4061,"lastConnectedAt":"...","latestHeadBlock":"49860816", ...}
```

It opens immediately and then **keeps the connection open indefinitely**, pushing
`sync-status` events as chain sync progresses. There is no page/limit/cursor query
param that would make it "finish" — an unparameterised call "not returning within 2
minutes" is exactly correct behaviour for an SSE endpoint being fetched like a normal
JSON GET, not evidence of a slow paginator. `httpx.Client.get()` (what `http.py` uses)
would block until the 30s read timeout and then raise, once per retry attempt (3×,
compounding to ~90s+ of dead time) — which is worse than merely "not useful," so this
route is correctly left off the `vd read` target list, now for a documented reason
rather than an open question.

---

### 3.20 `/raid-finder/debris`

Not wallet-scoped (no `wallet` path segment or query param — confirmed live 2026-08-27:
identical body regardless of caller). Query: `page`, `pageSize` (default `page=1,
pageSize=250`, unconfirmed whether larger values are honoured). Shape: `{targets:
[{planetId, name, owner, coordinates: {galaxy, system, position}, archetype, hasMoon,
debris: {metal, crystal}, updatedAtBlock, transactionHash}], pagination, detail, stale,
source, indexer}`. All numeric-looking values (`planetId`, `debris.metal/.crystal`) are
decimal strings, same convention as every other route in this document.

Moved out of §11 below 2026-08-28 (commit 3 of the launch-actions plan): `read.
fetch_raid_finder_debris()` is a live caller now (`tick._foreign_debris_targets`), the
same "bypass the CLI/`_emit` layer, raw dict" posture `fetch_fleet_visibility`/
`fetch_activity` already take — not wired into `vd read`'s own CLI target list, same as
those two.

**Confirmed incomplete, not the authoritative debris source.** A live probe (2026-08-27)
returned 2 `targets` while the same response's own `indexer.indexedDebrisFields` reported
3 — this route's filtering criteria beyond that are undocumented. Acceptable for
`_foreign_debris_targets`'s purpose (a missed candidate is a missed opportunity, not a
wrong answer); explicitly **not** used for the wallet's *own* planets' debris
(`_own_planet_debris` uses `/universe/galaxies/{g}/systems/{s}` instead, §3.16) — the same
incompleteness there would risk excluding an owned planet and silently killing that rung.

A sibling route, `/raid-finder/rifters`, exists with the same shape (confirmed live,
empty `targets` on the probed universe) — the Rift Stabilizer building's mechanics are
unpublished (no formula for what it produces or protects has been found anywhere in the
pinned contract source), so this codebase has no consumer for it.

### 3.21 `/wallet/{addr}/attack-protection`

Wallet-scoped, one specific target per call. Query: `targetPlanetId` (required). Shape,
confirmed live: `{allowed, blockedReason, plunderBps, defenderInactive,
transportAllowed, attackerScore, defenderScore, ...}` — `blockedReason` is present only
when `allowed` is `false`, one of `"score_protection"` / `"bashing"` / `"not_allied"`.
This is the PER-PLANET, PER-(attacker,defender) form — richer than `/highscores`'s
embedded `attackProtection` block (§3.18), which has no `targetPlanetId` to key off and
so cannot see the bashing-limit dimension at all. The contract itself
(`VeydriftAntiRaidPrimitives.sol`) re-evaluates all of this again at mission IMPACT, not
at launch — so even a fresh call to this route is a best-effort pre-flight check, not a
guarantee that a launched Attack will actually land a battle.

Moved out of §11 below 2026-08-28 (commit 6 of the launch-actions plan): `read.
fetch_attack_protection()` is a live caller now (`tick._attack_protection_allowed`,
feeding `guard._gate_attack_protection`), the same "bypass the CLI/`_emit` layer, raw
dict" posture `fetch_raid_finder_debris`/`fetch_highscores` already take — not wired
into `vd read`'s own CLI target list, same as those two.

---

## 4. Queue parsing (`QueueState`) — typed from source, not from a live sample

The probed account (planet 664) is zero-state: every `queue`/`building`/`research`/
`ship`/`defense` field on every route above is `null`. `read.py`'s `_queue_entry()` is
therefore typed against the backend source rather than exercised against a live
populated response — `apps/backend/src/evm.ts:170-190`:

```ts
export type QueueState = {
  active: boolean;
  kind: string | null;
  planetId?: string;
  itemId?: number;
  targetLevel?: number;
  quantity?: number;
  readyAt: string | null;
  startedAt?: string | null;
  cost: Resources;
  backlog?: QueueState[];
  asOfNow?: { secondsRemaining: number; complete: boolean; ... };
};
```

Mapping to `models.QueueEntry`: `kind` → `QueueKind` (values already match:
`"building"|"research"|"ship"|"defense"`), `itemId` → `entity_id` (+ name lookup via §6),
`targetLevel` → `target_level`, `quantity` → `quantity`, `readyAt` → `ready_at`,
`asOfNow.secondsRemaining` → `seconds_remaining`. `tests/fixtures/
wallet_infrastructure_active_queue.json` is a synthetic fixture built from this type
(not a live capture) so `test_read.py` exercises the parse path; see that file's
sibling note in `test_read.py`'s module docstring.

---

## 5. Incoming-fleet parsing (`FleetMissionSummary`) — same caveat

Same situation: `fleet-visibility.incoming` is `[]` on the probed account. Typed from
`apps/backend/src/evm.ts:640-679`'s `FleetMissionSummary`. The one detail worth calling
out explicitly: **`missionType` arrives as a string, not the contract's numeric id** —
e.g. `"Attack"`, `"AcsDefend"`, `"MissileAttack"` (PascalCase, no spaces — the enum
member name, not a display label). `read.py`'s `_incoming_fleet()` resolves the paired
int via a local `FLEET_MISSION_TYPE_IDS` table keyed on those exact wire strings — see
`read.py`'s module docstring for why this is deliberately *not* sourced from `ids.py`'s
differently-formatted display-name table (`"ACS Defend"` vs. `"AcsDefend"`).

One more thing worth flagging for whoever builds `plan.py`'s escalation logic:
this project's earlier research calls `fleet-visibility.incoming` "the hostile-fleet
detection surface," and `models.py`'s `IncomingFleet.hostile` defaults to `True` on that
basis. But `FleetMissionType` includes `AcsDefend` (5) and
`DefenseHold` (9) — both allied-reinforcement mission types, not attacks — and nothing
in the backend source rules out an `AcsDefend` mission appearing in *your own*
`incoming` array when an ally stations a fleet to defend your planet. This module
doesn't attempt to disambiguate (that's `plan.py`'s decision, not a read-layer one), but
`mission_type_name` is populated specifically so a consumer can check it before treating
every `incoming` row as hostile.

**Action item, flagged but deliberately not guessed at (judge review, 2026-08-12):** this
is unverifiable against the probed account (zero incoming fleets, always `[]`), so nobody
has ever seen what a live `AcsDefend`/`DefenseHold` row in *your own* `incoming` array
actually looks like. `read.py`'s `_incoming_fleet()` carries a matching `# TODO` at the
`hostile=True` line. **Before the first tier-3 policy edit** (the only tier that unlocks
`launchFleetMission`), someone needs to either (a) observe a real
allied-reinforcement `incoming` row against a live account and confirm whether it's
distinguishable from an attack by `mission_type_name` alone, or (b) get a definitive answer
from the backend source/team on whether `fleet-visibility.incoming` can ever contain
non-hostile entries. Do not ship a guessed disambiguation rule for this — a wrong guess in
either direction is bad: silently ignoring a real attack, or self-escalating a tier-3
policy on every tick because an ally keeps a defensive fleet stationed at your planet.

---

## 6. Entity ID → name tables

The live API **never sends entity display names** — confirmed across all four of
`infrastructure`/`research`/`shipyard`/`defenses`: every entity is `{id, level|count,
cost, durationSeconds}`, bare integer id only. `read.py` imports
`BUILDING_NAMES`/`TECHNOLOGY_NAMES`/`SHIP_NAMES`/`DEFENSE_NAMES` from `ids.py`, built by
reading the deployed contract source directly at commit `701bed35` — the single
authoritative source for these names. It corrects "Dreadstar" (used throughout
`docs.md`'s rapidfire tables and prior notes) to "Deathstar," the contract's actual enum
member name.

Fleet-mission-type resolution is the one exception kept local regardless of `ids.py`'s
presence — §5 explains why (wire-format string matching, not display-name matching).

---

## 7. `snapshot`'s composition, and why it substitutes `overview` for `fleet-visibility`

`read.py`'s `snapshot` command fetches **health + overview + infrastructure + research +
shipyard + defenses** — six calls, `overview` in place of a bare `fleet-visibility` call
that an earlier, more literal design intent had called for. Two independent reasons,
both load-bearing:

1. **`overview` already contains `fleetVisibility`, byte-identical in shape**
   (confirmed live, §3.12). Fetching both `overview`
   and a bare `fleet-visibility` would be a wasted seventh call for data already in
   hand.
2. **The digest this command needs to produce requires "fields used/total"**, and *none*
   of health/infrastructure/research/shipyard/defenses/fleet-visibility carries planet
   coordinates, fields, or temperature (confirmed by reading every one of those six
   payloads directly, §3.3-§3.13). Only `settlement`/`planets`/`overview` do. The more
   literal composition cannot produce its own required summary content —
   `overview` resolves that inconsistency at zero extra cost.

Consequence for `models.Snapshot`: `PlanetSnapshot.archetype` is **always `None`** from
`snapshot`'s output, because `archetype` lives only on the universe routes (§3.16),
which `snapshot` deliberately does not call (that would cost a 7th request per planet
for one cosmetic string). `Snapshot.eth_balance_wei` is also always `None` — no read
route reports wallet ETH balance; that's `veydrift-wallet`'s domain (WP4a), not this
one's. Both are documented as legitimate `None`s per `models.py`'s own convention
("`None` means the API did not report this, never zero").

---

## 8. Exit codes and the `bad planetId` gotcha

Exit codes: `0` ok · `2` API unhealthy · `3` network · `4` bad args. `http.py`/
`read.py` implement this as: a 4xx from the API → `VeydriftHTTPError` → exit `4`; a 5xx
surviving all retries → `VeydriftServerError` → exit `2`; a connection/timeout failure
surviving all retries → `VeydriftNetworkError` → exit `3`; a missing `--wallet`/
`--planet-id`/`--out` → exit `4` directly, no network call attempted.

**Gotcha, confirmed live**: passing a non-existent `planetId` (e.g. `--planet-id
99999999`) does **not** get a clean `400`/`404`. It gets a **`503`**:

```json
{"error": "indexed_read_not_ready",
 "detail": "infrastructure is not available from indexed contract state yet. Refresh shortly."}
```

Under this project's "5xx = retry" contract, that means a bad planet id
costs **3 retry attempts with exponential backoff** (~1.5-3.5s of added latency,
confirmed by timing a live call) before the CLI gives up — and it then exits `2` ("API
unhealthy"), not `4` ("bad args"), even though the actual mistake was a bad argument.
There is no way to distinguish this from a genuinely overloaded backend at the HTTP
layer alone; the API's error contract conflates the two. This is a real, load-bearing
quirk for anyone scripting against `vd read`, not just a curiosity — worth knowing
before assuming a `2` always means "the backend is down."

`battle-reports`/`highscores` without `--out`: exit `4`, confirmed. Passing `--json` to
either (a flag they don't define) is rejected by click's own argument parser before this
module's code runs at all — exit `2` (click's usage-error convention), which happens to
collide with this project's own "API unhealthy" `2`. Noted for completeness; not fixed,
since it isn't this module's exit code being produced, and typer/click don't expose a
way to override the usage-error exit code per-app without a larger refactor than this
edge case warrants.

---

## 9. Corrections a full live probe found, over earlier backend-source-derived research

This project's earlier research was written from backend source, not from probing every
route — it explicitly invited this kind of correction ("field-level details may
differ"). What actually differed on 2026-08-12:

1. **`/highscores` size.** Earlier research didn't give a figure directly (a separately
   cited "~86 KB," itself likely stale); measured **~2.2 MB** for the
   default page. See §3.18.
2. **`/chain/events` is SSE, not "needs paging params."** Earlier research framed the
   2-minute hang as a missing-parameters problem. It is a
   `text/event-stream` response that never terminates by design; no parameter would
   change that. See §3.19.
3. **A third real "universe" route exists**: `/universe/galaxies/{g}/systems/{s}`. Earlier
   research's route table lists only `/universe/system` and `/universe/systems`.
   See §3.16.
4. **`/battle-reports` returns a bare array**, not an object with a `pagination`
   wrapper — earlier research didn't specify this either way; noted because it breaks the
   pattern every other paginated route in this API follows (`/highscores`,
   `/wallet/{addr}/missions`).
5. **Everything else matched.** The wallet route list, the two enums (`Defense`,
   `FleetMissionType`), the ABI hash, and the health-gating rule all confirmed
   exactly as documented — this is not a "the earlier research was wrong" finding so much
   as "a probe finds detail a source-read alone can't," which is exactly what that
   earlier pass asked the next one to do.

---

## 10. The disk cache vs. the backend's own response cache

Two independent caching layers exist; don't confuse them when debugging a stale read:

- **`vd`'s own disk cache** (`http.py`, this WP): `$VEYDRIFT_HOME/cache/`, keyed by
  route+params, 60s default / 15s for `/health`, controllable per-call via `--max-age`.
  This is the layer §1 describes.
- **The backend's own in-process response cache** (`apps/backend/src/server.ts`,
  `enableResponseCache`/`sharedResponseCache`), with its own TTL table
  (`server.ts:2600-2632`) independent of anything this skill controls: `/health` 10s,
  `/highscores` 1s (`live=true`) or 300s otherwise, `/wallet/*/fleet-visibility`,
  `/wallet/*/missions`, `/wallet/*/missile-attacks`, `/mission/*` **0** (always live),
  `/wallet/*/(infrastructure|moon|shipyard|defenses)` **0**, `/wallet/*/overview` **0**,
  `/wallet/*` (general) 5s, `/universe/systems` and `/universe/galaxies/*/systems/*` 30s.
  `--max-age 0` on the `vd` side only forces *this skill* to re-fetch; it cannot force
  the backend's own cache to miss. In practice this rarely matters (most of the routes
  this skill calls per-planet have a 0s backend-side TTL already), but it explains why
  `/highscores --max-age 0` can still return a response up to ~1s stale.

---

## 11. Undocumented-but-live routes not wired into `vd read`

Confirmed live 2026-08-12, in scope of neither this skill's own `vd read` target list nor
this work package, listed here so the next pass doesn't have to re-discover them:

| Route | What it is |
| --- | --- |
| `/wallet/{addr}/missile-attacks` | Paginated missile-attack archive |
| `/wallet/{addr}/referrals/history` | Referral history; write-adjacent, out of this skill's mandate |
| `/wallet/{addr}/alliance`, `/alliance/{id}` | Alliance state |
| `/wallet/{addr}/rift` | Rift Stabilizer balances (building id 15; mechanics unpublished) |
| `/wallet/{addr}/watched-planets` | Player-configured planet watchlist (GET/POST/DELETE) |
| `/wallet/{addr}/profile`, `/profile/display-name` | Player profile (GET/POST) |
| `/raid-finder/rifters` | Server-side target selection for Rift Stabilizer mechanics (unpublished, no consumer) — see §3.20's note; `/raid-finder/debris` moved to its own §3.20 entry 2026-08-28, now a live caller |
| `/randomness-readiness` | Randomness-engine commit/reveal readiness — still not a live caller as of this row (`generate_attack_candidates`/`_gate_health` consult `Snapshot.randomness_readiness`, sourced from `/health`'s own `randomnessReadiness` block, not this separate route) |
| `/planets/{id}` | Single-planet detail, not wallet-scoped |
| `/missions` (global) | Non-wallet-scoped mission feed — see §3.14's note |
| `/mission/{id}`, `/battle-report/{id}` | Single mission / single battle report by id |
| `/cca` | Chicken-burn-auction state (unrelated subsystem) |
| `/graphql` | Status-only — same payload as `/runtime-config` wrapped in `{data:{service:...}}`; no game schema |

None of these are in this skill's own `vd read` target list, so none are wired into
`vd read`. Recorded here per this work package's "mark undocumented-but-live routes"
mandate, not as a recommendation to add them.
