# Veydrift Agent Infrastructure — Implementation Specification

**Status:** approved; built; judged; fixes in progress · **Version:** 2.1 · **Date:** 2026-08-12
**Planner:** Opus 5 · **Executors:** Sonnet 5 · **Judge:** Fable 5

> **v2.1** folds in the Fable 5 judge's findings. Corrections are marked inline with their date and
> the finding that prompted them, rather than silently rewritten — the errors this spec contained are
> part of its evidence. Three were load-bearing: `startShipProduction` belonged to no tier (§4), the
> pretty-format example claimed an impossible `16/16 pass` at tier 1 (§5.9), and §6.4 never said
> where `walletctl` learns the tier it enforces (§6.4).

> **v2 changes from v1** — Turnkey dropped from implementation and deferred to research; all config
> is JSON, not YAML; Python skills are uv projects using real libraries instead of stdlib-only;
> skills install via `npx skills add .` instead of symlinks; docs consolidated under `docs/`; the
> working directory is now a git repo; `references/` conventions aligned to `ethskills.com`.

---

## 0. Decisions taken

| Decision | Choice | Rationale |
| --- | --- | --- |
| API layer | Scripts now, MCP-ready | Zero ops; native skill fit; an MCP server later wraps the same module |
| Runtime | Python (uv) for reads/calc/loop · TypeScript for the wallet engine | viem lives in TS; the wallet engine is separately swappable by design |
| Scheduler | Idempotent `tick` entrypoint + harness adapters | One command; Claude Code, Hermes and launchd each drive it their own way |
| Config format | **JSON** | Kills the hand-rolled YAML parser — the weakest piece of v1, and it was parsing a *security policy* |
| Dependencies | **Real libraries, uv-managed** | Explicit instruction: do not reinvent the wheel |
| Wallet providers | **`envkey` + `keystore`, both real. No Turnkey** | Two working providers prove the interface better than one working + one stub |
| Distribution | `npx skills add .` | The `skills` CLI already targets both `claude-code` and `hermes-agent` |

### 0.1 Answering the packaging question directly

> *"Skills should be self contained (e.g. python project.yaml with uv as dependency manager). Is this
> correct based on create-skill best practices?"*

**Half correct, and the filename is off.**

- `skill-creator` does **not** prescribe a dependency manager. Its stated anatomy is only
  `SKILL.md` + `scripts/` + `references/` + `assets/`, with an optional `compatibility` frontmatter
  field for "required tools, dependencies (optional, rarely needed)". So uv is a free choice, not a
  deviation from best practice.
- The file is **`pyproject.toml`**, not `project.yaml` — uv has no `project.yaml`.
- Self-containment is the right instinct and uv is the right tool. Two uv idioms fit; this spec uses
  both:
  - **`pyproject.toml` + `uv.lock` per skill** for the multi-file skills, because `vdlib/` is shared
    across scripts and a shared package needs a project. `uv run` auto-creates and caches the venv,
    so there is still no install step for the user.
  - **PEP 723 inline metadata** (`# /// script` headers) for any genuinely standalone one-file
    utility, so it runs via `uv run script.py` with zero project context.

One correction to v1: this spec previously mandated stdlib-only Python. That is now **withdrawn** —
it was what forced the hand-rolled YAML parser, and JSON + pydantic is strictly better.

---

## 1. Goals and non-goals

### Goals

1. A **three-tier agent** advancing only on explicit human decision:
   **T1 Advisor** (read + propose, sign nothing) → **T2 Economy** (builds/research/resolve) → **T3 Operator** (adds non-combat fleet).
2. **Generalizable to any planet or account.** Planet traits are read at runtime. Planet 664 appears
   only as the default in the example policy.
3. **The skill builds transactions; it never submits them.** Submission is the wallet engine's sole
   job, behind its own independent allowlist.
4. **Everything proposed is pretty-printed; everything executed is logged immutably.** Strategy
   reasoning and a changelog are maintained continuously.
5. **One install command, two harnesses.** `npx skills add . -a claude-code -a hermes-agent`.

### Non-goals for this pass

- Combat, alliances, ACS, migration, referrals, NFT burns, the ERC-20 market bridge.
- A raid-profitability model — `protectedResources` semantics are still unconfirmed.
- A hosted/MPC/AA wallet provider. Deferred to `docs/wallet-provider-research.md` (§6).

### Phase 5 status note (2026-08-17) — Goal 1's T3 claim is still partly aspirational

Phase 5 of the general-strategy-engine program set out to make Goal 1's "T3 Operator
(adds non-combat fleet)" literally true, and to bring colonisation into scope (revising
the non-goals list above accordingly). **Only part of that landed.** What shipped:

- `resolveFleetMission` (already ECONOMY-tier, already implemented in `plan.py`) is now
  actually *reachable* — `tick.py` computes `resolvable_mission_ids` live and passes it
  through. This was always in scope; it was dormant, not aspirational.
- `PlanetSnapshot.archetype` is populated (opt-in, cadence-gated).
- `settlePlanet` — allowlisted capacity that could never do anything useful — is removed
  from both enforcement layers (a breaking change, `veydrift-wallet` v0.2.0).
- The real colonisation entrypoint was identified and verified against contract source
  (`launchFleetMission` mission type `Colonize`/2 — see §6.4 and
  `references/contract-writes.md`), correcting a plausible false lead (`startPlanet`,
  which is unrelated and out of scope regardless, being `payable`).

**What did NOT ship, and why** *(historical — see the very next section, "Phase 5c/5b status
update," which shipped exactly this; the mission-type list below is stale as a description of
the codebase today, kept verbatim as the record of what was true at the time this section was
written)*: non-combat fleet-mission planning (Transport/Deploy/Harvest) and real colonisation
both require a new `ActionKind.FLEET_MISSION` and new `Action` fields on `models.py` — the
frozen interface contract for the work package that attempted this phase (`AGENTS.md` §4). That
work package's own instructions treat `models.py` as not-to-be-edited, stop-and-report-if-
blocked, rather than a same-session decision it could make unilaterally. So T3's fleet capability
remains the operator tier *allowlisting* Transport(0)/Deploy(1)/Harvest(4) at the wallet-engine
layer (true since before this phase — see §6.4) with **no planner path that can ever produce
one** — the exact "allowlisted, unreachable" shape this phase found and fixed for
`resolveFleetMission`, still true for fleet missions generally. Colonisation is
correspondingly still out of scope in practice, not by non-goal but by this blocker. See
`veydrift-agent`'s `CHANGELOG.md` `[Unreleased]` entry and `docs/COVERAGE.md` §1.2 for
the full state and what a maintainer who can edit `models.py` needs to do to finish it.

### Phase 5c/5b status update (2026-08-17, this change) — Goal 1's T3 claim is now real

The orchestrator unfroze `models.py` for exactly the blocker described above and added
`ActionKind.FLEET_MISSION` plus the `Action` fields `launchFleetMission` needs. This
change is everything downstream of that:

- **Goal 1's "T3 Operator (adds non-combat fleet)" is no longer aspirational.**
  `candidates.py` gained a logistics family (`generate_transport_candidates`,
  `generate_harvest_candidates`, wired into `plan.py` as a new band, gated on
  `policy.actions.allow_fleet_noncombat`, default `false`) that can actually produce a
  `launchFleetMission` `Action`; `guard.py` gained an 18th gate (`mission_type`,
  default-deny, independent of `tier`); `tick.py` can now build calldata for it,
  resolving the overload by full signature (AGENTS.md §7 trap #2) and the 14-slot fleet
  tuple correctly (trap #1). See §5.4, §5.5, §9 below and `veydrift-agent`'s
  `CHANGELOG.md`.
- **Colonisation is now in scope and implemented**, revising the non-goals list below:
  `launchFleetMission` mission type `Colonize` (2) is allowed at both enforcement
  layers (`guard.py`'s `mission_type` gate and `veydrift-wallet`'s
  `OPERATOR_ALLOWED_MISSION_TYPES`, widened together in the same change, never one
  before the other — see §6.4). No planner rung *proposes* a colonisation `Action`
  yet (that would need a "where to colonise" target-selection policy this phase's
  brief did not ask for) — the entrypoint is implemented and both-layers-gated, ready
  for a future colonisation-target generator, the same "capacity exists, planner
  doesn't reach for it yet" shape `resolveFleetMission` had before the prior pass of
  this phase revived it.
- **Revised non-goals, restated**: combat, alliances, ACS, migration, referrals, NFT
  burns, and the ERC-20 market bridge remain fully out of scope, unconditionally
  (unchanged from the original list above). A raid-profitability model remains out of
  scope (`protectedResources` semantics still unconfirmed). Colonisation is **no
  longer** a non-goal, per the above. Non-combat fleet logistics (Transport/Harvest) is
  **no longer** a non-goal either, though the generators are intentionally
  conservative: Transport only ever considers the wallet's own planets, using
  already-built ships; Harvest only ever considers a planet's own local debris field
  (never a foreign one). **Live since 2026-08-28** (correction 67, §9) —
  `tick._own_planet_debris` now wires a confirmed-populated debris-field source into
  `candidates.generate_harvest_candidates`'s `own_planet_debris` parameter.

---

## 2. Repository layout

```
/Users/santteegt/Verydrift/                     # git repo
├── AGENTS.md                       # harness-agnostic operating doc (primary)
├── CLAUDE.md                       # thin pointer -> AGENTS.md
├── CHANGELOG.md                    # Keep a Changelog
├── .gitignore                      # written; secrets + state/ excluded
│
├── docs/
│   ├── SPEC.md                     # this file
│   ├── RESEARCH-ADDENDUM.md        # contract + backend findings (done)
│   ├── wallet-provider-research.md # NEW, WP4b — deferred provider evaluation
│   ├── NOTES.md · veydrift-agent-prompt.md · veydrift-agent-resources.md
│   └── veydrift-briefing.html      # prior inputs, unmodified
│
├── skills/                         # discovered natively by `npx skills add .`
│   ├── veydrift-agent/
│   │   ├── SKILL.md
│   │   ├── pyproject.toml · uv.lock
│   │   ├── references/
│   │   │   ├── api-routes.md · entity-ids.md · formulas.md
│   │   │   ├── contract-writes.md · guardrails.md
│   │   │   ├── strategy-playbook.md · scheduling.md
│   │   ├── src/veydrift_agent/
│   │   │   ├── cli.py              # typer app; single `vd` entrypoint
│   │   │   ├── read.py · calc.py · plan.py · guard.py · tick.py · log.py
│   │   │   ├── models.py           # pydantic: Policy, Action, Snapshot, GuardVerdict
│   │   │   ├── ids.py · http.py · state.py · fmt.py
│   │   ├── schemas/                # GENERATED from pydantic, committed
│   │   │   ├── policy.schema.json · action.schema.json
│   │   ├── assets/
│   │   │   ├── policy.example.json
│   │   │   └── com.veydrift.agent.plist.template
│   │   └── tests/
│   │
│   └── veydrift-wallet/
│       ├── SKILL.md
│       ├── references/providers.md · abi-pinning.md · tx-safety.md
│       ├── abi/VeydriftGame.701bed3.json · abi/PINNED.json
│       ├── package.json · tsconfig.json
│       ├── src/
│       │   ├── cli.ts · abi.ts · allowlist.ts · tx.ts · fleet.ts
│       │   └── providers/{types,envkey,keystore,index}.ts
│       └── tests/
│
└── $VEYDRIFT_HOME/                 # runtime state — OUTSIDE the repo (see §2.1)
    ├── policy.json · agent-state.json · KILLSWITCH
    └── logs/{actions.jsonl, proposals.jsonl, strategy.md, ticks/}
```

### 2.1 State lives outside the skill — a direct consequence of `npx skills add`

`skills add` **copies** the skill into the agent's skills directory. Anything written inside the
skill tree is destroyed on the next install/update. So:

- State root is `$VEYDRIFT_HOME`, defaulting to **`~/.veydrift`**. Created on first run.
- Every script resolves bundled paths relative to `__file__`, never `cwd` — a skill installed to
  `~/.claude/skills/veydrift-agent/` must work when invoked from anywhere.
- The repo does not ship a `state/` directory; `assets/policy.example.json` is copied to
  `$VEYDRIFT_HOME/policy.json` by `vd init`.

### 2.2 Install and test

```bash
npx skills add . -a claude-code -a hermes-agent      # install/update both skills
npx skills add .                                     # auto-detect agents, prompt
```

`skills add .` walks `skills/<name>/SKILL.md`, which this layout already satisfies — no manifest is
required. `AGENTS.md` documents this as the only supported install path.

> **Correction, 2026-08-12 (verified by WP5 against the real CLI).** v2.0 claimed "there are no
> symlinks to keep in sync". That is true only for a **single** `-a` target, where the skill is
> copied to `.claude/skills/<name>/`. With **two or more** targets, the CLI installs once to a
> shared `.agents/skills/<name>/` and symlinks each agent's directory into it. The practical
> consequence is unchanged — one command, nothing hand-maintained — but the mechanism is not
> copy-only, and anything reasoning about install layout must handle both shapes. Only the
> `claude-code` target was confirmed end-to-end; Hermes Agent is not installed on this machine.
>
> **Also found: `skills add` does not honour `.gitignore` when copying.** A `.venv/`,
> `node_modules/` or `__pycache__/` present in the source tree at install time is copied verbatim,
> and a copied `.venv` is actively broken (`dyld: Library not loaded`), crashing `uv run` from the
> installed copy until it is removed — after which uv rebuilds cleanly. This makes §2.1's
> "state lives outside the skill tree" rule load-bearing for *build* artifacts too, not just
> runtime state. `AGENTS.md` documents the workaround.

---

## 3. Dependencies

Chosen to eliminate hand-rolled infrastructure. Each earns its place by replacing code v1 would have
written by hand.

### `veydrift-agent` — Python ≥3.11, uv

| Package | Replaces |
| --- | --- |
| `httpx` | hand-rolled `urllib` + timeout/retry logic |
| `typer` | hand-rolled `argparse` subcommand tree |
| `pydantic` v2 | **the hand-rolled YAML parser and hand-written JSON Schemas** — schemas are generated via `model_json_schema()` |
| `rich` | hand-rolled table/panel formatting for the pretty report |
| `tenacity` | hand-rolled exponential backoff |
| dev: `pytest`, `respx`, `ruff` | — |

### `veydrift-wallet` — Node ≥22, npm

| Package | Purpose |
| --- | --- |
| `viem` | encoding, simulation, gas estimation, send, receipt, `getAddress` checksums |
| `ethers` v6 | **keystore decryption only** (`Wallet.fromEncryptedJson`) — scrypt+AES is not something to hand-roll |
| `commander` | CLI |
| dev: `vitest`, `typescript`, `tsx` | — |

Foundry (`cast`) is already on this machine and is used **in tests** to independently cross-check
function selectors, rather than trusting our own encoder to check itself.

---

## 4. Tier model

Tier is one field in `policy.json`. **No code path advances it** — only a human edit does.

| Tier | Name | May propose | May submit | Gate to enter |
| --- | --- | --- | --- | --- |
| 1 | `advisor` | everything in scope | **nothing** | default |
| 2 | `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `startDefenseProduction`, `startShipProduction` | ≥24 h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 | `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0) / Deploy(1) / Colonize(2) / Harvest(4) only | ≥7 days clean T2, human edit |

> **Correction, 2026-08-17 (judge review of the general-strategy-engine program).** The T2 row
> above previously still listed `settlePlanet` — removed from both enforcement layers in Phase 5
> (see the correction below AC46, §9) — and the T3 row previously omitted Colonize(2), added at
> both enforcement layers in Phase 5b (§6.4). Both are fixed here to match the code and
> `docs/COVERAGE.md` §1.6/§1.2.

Combat (`Attack`, `AcsAttack`, `MissileAttack`, `Intercept`) is **unreachable in code at every tier**.
`policy.json` carries an `allow_combat` key that is deliberately ignored; enabling attacks requires a
code change. With two debris fields across ~195 planets the expected return does not justify the
downside, and the friction is cheap.

> **Fix, 2026-08-12 (spec defect found by WP5).** `startShipProduction` was missing from the tier
> table in v2.0, while §5.4's ladder rung 8 proposes ships when `actions.allow_ships` is enabled.
> The result was dead configuration: setting `allow_ships: true` produced proposals that `walletctl`
> would refuse at *every* tier, forever. Ship production is added to tier 2 rather than removed from
> the ladder, because producing ships is a resource spend on your own planet — the same risk profile
> as `startDefenseProduction`, which tier 2 already permits. Combat remains gated separately, at the
> mission-type level on `launchFleetMission`, and is unreachable in code regardless. `allow_ships`
> still defaults to `false`, so this widens nothing until a human opts in.

**Tier 1 still builds calldata** — it renders a complete, ready-to-submit transaction and prints it
for manual execution. That is what makes the T1→T2 decision evidence-based.

`vd readiness` prints the promotion evidence: tick count, uptime, proposals made, how many the human
actually executed, **divergences between proposal and human action**, which guardrails fired and why,
and gas spent. A green tick count alone is a bad promotion signal; divergence is the useful one.

Divergence's stated blind spot ("a human executing a T1 proposal by hand, outside this tool, leaves
no trace this command can see") is narrowed, not closed, by a best-effort check: whenever the previous
tick's proposal was on-chain and unresolved (tier 1, or `require_confirmation` stopped the send), the
next tick fetches `/wallet/{addr}/activity` since that proposal's timestamp and surfaces whatever raw
items come back — titles, kinds, transaction hashes — for a human to read. This is deliberately **not**
a confirmed match/diverge verdict: the only `/activity` item ever actually observed in this project is
a one-time "planet settled" milestone, so the shape of a routine building/research-completion item is
unconfirmed. `vd readiness`'s `human_activity_checked`/`human_activity_hits` counts summarise this;
never a guard input, never affects `Decision`.

---

## 5. The `veydrift-agent` skill

Single `vd` entrypoint (typer), invoked as `uv run --directory <skill> vd <subcommand>`.

### 5.1 SKILL.md

Frontmatter `description` must trigger on Veydrift play, planet management, build-order questions and
the agent loop — and **not** on generic blockchain or generic strategy-game questions. Per
skill-creator guidance the description should lean slightly pushy, since skills under-trigger.

Body ≤500 lines: tier model, tick contract, decision ladder, safety gates in summary, and a routing
table into `references/`. Formulas, ID tables and route tables live in `references/` only. Scripts
are executed, never read into context.

### 5.2 `vd read` — API access

```
vd read <target> [--wallet 0x..] [--planet-id N] [--json|--summary] [--out FILE] [--max-age S]
```

Targets: `health · config · settlement · planets · queues · highscore · infrastructure · research ·
shipyard · defenses · moon · overview · fleet-visibility · missions · activity · universe ·
battle-reports · highscores · snapshot`.

- `--summary` (default) emits a ≤2 KB digest: levels, affordable-now set, energy balance + `scaleBps`,
  production/hr, **hours-to-cap** per resource, queue ETAs, incoming fleets, fields used.
- `battle-reports` and `highscores` **refuse stdout**; `--out` is mandatory. They are 60–86 KB.
- `snapshot` = health + infrastructure + research + shipyard + defenses + fleet-visibility.
- Disk cache under `$VEYDRIFT_HOME/cache/`, `--max-age` default 60 s (`health` 15 s). Never cache non-200.
- `tenacity`: 3 attempts, exponential backoff; 10 s connect / 30 s read.
- `/chain/events` is **not exposed** — its paging contract is unknown and an unparameterised call did
  not return within 2 minutes.
- Exit codes: `0` ok · `2` API unhealthy · `3` network · `4` bad args.

**Health gating.** Gate on `ok === true` **and** `readiness.ready === true` only. `null` for
`chainSync` / `indexer` / `rpc` is a read-replica artifact, not an outage. Never gate on the
`lastReconciledBlock` ↔ `latestIndexedBlock` gap — it is ~1.5M blocks by design. Freshness comes from
`indexedState: "healthy"` + `safeToServeIndexedState: true` on the wallet routes.

> **Correction, 2026-08-22.** `_health_ok()`'s raw `ok`/`readiness.ready` definition above is
> unchanged — this adds a narrowly-scoped exception layered on top, not a redefinition.
> `Snapshot.combat_only_degradation()` positively confirms `ok === false` is caused *solely* by
> `randomnessReadiness` (a combat-only subsystem this codebase can never touch, since
> `allow_combat` is read-and-ignored everywhere) while everything else — `readiness.ready`, no
> other `degradationReasons`, `gameMaintenance.paused` — is confirmed fine; only then does
> `plan.py`'s rung 1 / `guard.py`'s `health` gate proceed instead of blocking. Confirmed live,
> persistent (not one-off): `/health` currently returns this via HTTP 503, not only a
> 200-with-`ok:false` body — `read._fetch_or_exit()` defensively recovers a parseable `/health`
> 5xx body specifically (every other route's 5xx behaviour is unaffected) so this check can even
> run. See §9's new acceptance criteria (62-63) and `skills/veydrift-agent/references/
> guardrails.md`'s `health` gate section for the full design.

> **Phase 3 of the general-strategy-engine program, 2026-08-16.** `PlanetSnapshot` gains two fields,
> both sourced from routes `snapshot` already fetches (no new HTTP call): `missile_silo_level` ←
> `/defenses`'s `missileSiloLevel` (needed for the defense-target missile-slot cap, §5.4), and
> `crawler_production` ← `/infrastructure`'s `crawlerProduction` block (`total`/`effective`/
> `maxEffective`/`boostBps`/`capped`, preferred over recomputing wherever present, same posture
> `energy.solar_satellite_energy` already takes). Both default `None`, and `None` means unverifiable
> — never `0` — for every consumer (`AGENTS.md` §5).

### 5.3 `vd calc` — deterministic calculators

Pure functions, no network, fully unit-tested, each docstring citing its source (`docs.md`,
`RESEARCH-ADDENDUM.md` §5, or contract `file:line`).

```
scaled_level(base, L)                     production_per_hour(...)
energy_balance(...) -> (produced, required, scale_bps)
solar_satellite_energy(max_temp)          deuterium_multiplier_bps(max_temp)
max_temp_from_bps(bps)                    fusion_energy(L, energy_tech)
fusion_deuterium_upkeep(L)                crawler_boost_bps(count, mine_levels)
build_seconds/research_seconds/ship_seconds(...)
storage_cap(level)                        hours_to_cap(current, per_hour, cap)
distance(a, b) · travel_seconds(...) · mission_fuel(...) · available_cargo(...)
solar_crossover_table(max_mine_level)     max_planets(astrophysics)
```

**Costs are never computed.** Live cost at the current level comes from the API's `cost` object.
Per-building factors are unpublished rationals; recomputing them is how affordability checks go
wrong. `calc.py` must contain **no** cost-scaling function.

`vd calc verify` re-runs the three duration checks from `NOTES.md` §12.4 against the live API and
asserts universe speed is still 1. Non-zero exit on drift.

### 5.4 `vd plan` — decision engine

Input: snapshot + policy. Output: **zero or one** `Action` (pydantic model) plus a machine-readable
rationale. Rungs 0-4 are vetoes (safety, not strategy) and always run first, first match wins; rungs
5-9 are a four-band candidate pipeline (below — a fourth band, "unlock-chain," was added in Phase 4;
see the Phase 4 note further down):

```
0. KILLSWITCH present                -> HALT
1. /health not ok                    -> NO-OP, reason recorded
2. pending tx unreconciled           -> NO-OP, reconcile first
3. mission Resolving > 60s           -> resolveFleetMission   (permissionless, free)
4. incoming hostile fleet            -> ESCALATE, no proposal (fleet-visibility.incoming)
5-9. generate -> filter -> score -> select over four bands, in order:
     1. deadline-driven      -- storage overflow: spend it, or build the matching storage
     2. economically scored  -- building upgrade, ascending payback hours
        (also carries policy.strategy.building_priority infrastructure picks, which win
        outright over a scored candidate when declared)
     3. policy-declared      -- research, then ships/defense, gated on economy-on-track
     4. unlock-chain (rung 8b) -- the shallowest buildable prerequisite toward a locked
        ship_targets/defense_targets/research_priority entry, only when bands 1-3 found
        nothing at all
     else                    -> NO-OP with an explicit reason
```

> **Phase 2 of the general-strategy-engine program, 2026-08-16.** Before this change,
> each of rungs 5-9 both decided the action *family* and hardcoded *which entity* inside
> one function. A new module, `skills/veydrift-agent/src/veydrift_agent/candidates.py`,
> splits that in two: one pure generator function per family (`mine`, `energy`,
> `storage`, `research`, `ship`, `defense` — `infrastructure` is reserved for a future
> family, unused so far), each `(snapshot, policy, planet) -> list[Candidate]`, plus a
> `select_*` function per rung that replays the exact priority order the pre-Phase-2
> ladder used. `plan.py`'s own `plan_next_action` now only calls the three `select_*`
> functions in band order and attaches the runner-up `Candidate`s to the winning
> `Action.alternatives` (§9 AC23). **This phase's own acceptance criterion is zero
> behaviour change** — every pre-existing `test_plan.py`/`test_guard.py`/`test_tick.py`
> test still passes, unmodified.

**Scoring rule.** A `Candidate` is scored (`score: float | None`, payback hours) if and only if its
level change moves `calc.production_per_hour`'s output — computed by calling that function twice
(current levels, then with the candidate's one level incremented) and differencing, weighted by
`policy.strategy.resource_weights` (default 1:1:1) and divided into the live `Entity.cost` (never
recomputed — `calc.py`'s cost-scaling ban applies here too). Everything else — a storage building, a
locked entity, every research/ship/defense pick — is `score=None` and ranked after every scored
candidate within the same band, never above one.

**Energy-first invariant, restated for the pipeline: a hard filter, not a score.** A mine candidate
whose post-upgrade `required` energy would exceed `produced` is **never generated** by
`generate_mine_candidates` — the cheaper of Solar Plant / Solar Satellite (`generate_energy_candidates`)
is generated in its place. Compute `required`/`produced` explicitly at the *post-upgrade* level; never
use a fixed solar-level offset — the gap widens from 2 levels at mine 3 to 4 at mine 10.

**Build order is derived, not hardcoded.** `strategy-playbook.md` documents the derivation;
`candidates.py` implements it parametrically from planet traits (temperature, multipliers,
`solarSatelliteEnergy`, fields, levels). Planet 664's deuterium-lean, no-satellite opener must *fall
out of* its traits.

**`alternatives` is informational only.** It is never an ROI verdict, never consulted by `guard.py` or
any `Decision` logic — the winning `Action` is decided exactly the way it always was.

> **Phase 3 of the general-strategy-engine program, 2026-08-16 — every planet-local entity
> reachable.** Before this change the planner could only ever propose 13 of the 51 entities in
> `ids.py`. `candidates.py` gains three new/extended families, all driven by declared
> `policy.strategy` targets rather than an invented doctrine (the governing principle: *the engine
> computes what is legal, affordable and economically comparable; the policy declares intent for
> everything else*):
>
> - **ships** — `generate_ship_target_candidates` stock-keeps toward `policy.strategy.ship_targets`:
>   the first declared target below its live `Entity.count`, filtered through `techtree.unmet()`.
>   **Solar Satellite keeps its separate energy-driven scored path** (`_generate_satellite_ship_
>   candidates`) untouched — the two mechanisms never merge. **Crawler** (`generate_crawler_
>   candidates`) is new and *scored*: `calc.crawler_boost_bps`'s own internal caps (8 per combined
>   mine level, 5,000 bps) make an already-saturated crawler count score `None` automatically
>   (`score_payback` sees a zero marginal delta), and the live `PlanetSnapshot.crawler_production.
>   capped` flag short-circuits the same conclusion without recomputing when present.
> - **defenses** — `generate_defense_target_candidates` is the same shape against
>   `policy.strategy.defense_targets`, plus `techtree`'s caps: shield domes at 1 per planet, and
>   missiles against `missile_silo_level * 10` slots (ABM 1 slot, Interplanetary 2). Declaring
>   `defense_targets` supersedes the pre-Phase-3 hardcoded Rocket-Launcher-only default entirely — an
>   empty list reproduces that default exactly. **The contract counts queued quantity toward both
>   caps**; `PlanetSnapshot` carries only one `QueueEntry` per queue kind (no backlog), so a queued
>   amount beyond that single entry is under-counted, never over-counted — the safe direction. A
>   `missile_silo_level` (or a built/queued count) the snapshot didn't report fails closed (a
>   `"locked: ..."` candidate, never silently treated as `0`).
> - **research** — `generate_research_candidates` orders by `policy.strategy.research_priority`
>   first (technology names, resolved case-insensitively), then falls back to the pre-Phase-3
>   lowest-level-then-id order for everything not named — and that fallback pick's `score_basis` is
>   explicitly prefixed `"default: ..."` so a reader can tell a derived-looking pick apart from an
>   actual declared preference.
> - **infrastructure** (the family reserved, unused, in Phase 2) — Robotics Factory, Nanite Factory,
>   Shipyard, Research Lab, Terraformer, Missile Silo, `score=None`, ordered by
>   `policy.strategy.building_priority`. Empty `building_priority` means this family never generates
>   anything — it is the family's sole reachability switch. When set, it takes precedence ahead of
>   the ordinary mine walk in `select_building_candidate` (an explicit `building_priority` is a
>   declared human intent, which wins outright under the governing principle above). **Fusion
>   Reactor does NOT belong here** — it moves `production_per_hour`, so `generate_energy_candidates`
>   scores it in the economic band instead, deliberately without touching the pre-Phase-3
>   `_cheapest_energy_choice` substitution comparison (pinned by AC4/the hot-planet counterfactual to
>   Solar Plant vs. Solar Satellite only — see criterion 66 for its 2026-08-27 replacement, a
>   three-way comparison that also wires Fusion Reactor into this substitution).
> - **storage, proactively** — `generate_proactive_storage_candidates` activates `calc.storage_cap`
>   (previously dead) as a Band-2 candidate, always `score=None`, visible in `alternatives` well
>   before `generate_storage_candidates`' reactive overflow trigger fires. Never changes which
>   candidate *wins* Band 2 — additive to the alternatives pool only.
>
> **This phase's own acceptance criterion, again zero behaviour change**: every one of the four new
> `StrategyCfg` fields (`ship_targets`, `defense_targets`, `research_priority`, `building_priority`)
> defaults to empty, and empty reproduces Phase 2's planner output exactly — pinned directly in
> `tests/test_candidates.py`/`tests/test_plan.py` and implicitly by every pre-Phase-3 test passing
> unmodified (§9 AC25-31).

> **Phase 4 of the general-strategy-engine program, 2026-08-16 — the engine works out the
> build-up, not just the final entity.** Phase 3 made every entity *reachable when its
> prerequisites are already met*, but a declared `ship_targets`/`defense_targets`/
> `research_priority` entry the account cannot build **yet** (e.g. Small Cargo, which
> needs Shipyard 2 and Combustion Drive 2 on a fresh planet) was declared, legal to want,
> and permanently unreachable — every generator correctly refuses to propose it (never
> propose an entity the contract would revert on), and nothing ever proposed the
> *prerequisite* that would unlock it. Phase 4 closes that loop:
>
> - **`techtree.next_step_toward(family, entity_id, *, building_levels, technology_levels)
>   -> UnlockStep | None`** — new pure function, `techtree.py`. Breadth-first walk of
>   `unmet()`'s output *backwards*: given a locked target, finds the shallowest requirement
>   in its dependency chain that is itself buildable right now (its own `unmet()` is empty
>   AND its own current level is known — an `UnmetRequirement(have=None)` can never become
>   a confidently-chosen step, "surface it as unresolvable rather than acting on it").
>   Cycle-safe (a `visited` node set) and depth-bounded (`_MAX_UNLOCK_DEPTH = 32`) against a
>   malformed table, though the real tables are asserted acyclic by test rather than merely
>   assumed so. Returns `None` when the target is already unlocked, or when every branch
>   bottoms out unresolvable. No cost math, no `resource_weights` — this function only
>   compares levels, same discipline `unmet()` itself already follows.
> - **`candidates.generate_unlock_chain_candidates`** — new generator. For every *locked*
>   declared target in `ship_targets`/`defense_targets`/`research_priority` (not
>   `building_priority`, which already has its own first-class reachability path), walks
>   `next_step_toward` and emits the result as a `Candidate`, `score=None` always — an
>   unlock step's value is entirely in what it eventually enables, exactly the unbounded-
>   future-plan assumption this codebase has already refused three times over (no cost-
>   scaling function, no ROI verdict, no activity-classification score). Two declared
>   targets sharing one unmet prerequisite are proposed once, deduplicated by the step's
>   own `(family, entity_id)`. Gated on the matching `policy.actions.allow_building`/
>   `allow_research` flag and the matching queue being idle, same as every other family
>   that can emit that action kind. Ordered by weighted cost ascending
>   (`policy.strategy.resource_weights`, live `Entity.cost` only, never recomputed) when
>   more than one locked target resolves to a different step; a step whose cost is
>   unavailable sorts after every known-cost one, never guessed.
> - **`candidates.select_unlock_chain_candidate` / `plan.py` rung `8b`** — a fourth ladder
>   band, reached *only* when bands 1-3 (storage overflow, economically-scored building/
>   infrastructure, policy-declared research/ships/defense) produced nothing at all for any
>   target planet. This is deliberate and load-bearing, not incidental: an unlock-chain
>   candidate must never outrank the deadline-driven storage-overflow band and must never
>   displace a scored economic candidate — folding it into rung 6's `building_priority`
>   branch (which *does* win outright over a scored candidate, by design, for a declared
>   `building_priority`) would violate that. Giving it its own rung, checked last, makes the
>   precedence a property of the ladder's control flow rather than a flag this function
>   would have to remember to check.
> - **`Action.expected_effect`** carries the *remaining* chain after this step (e.g. what
>   else is still needed toward the declared target), so `strategy.md`/`proposals.jsonl`
>   show the multi-tick plan implied by a declared target without the engine ever
>   committing to it — every tick re-derives from live state from scratch, same as every
>   other rung.
> - **`guard.py`'s `prerequisites` gate needed no change.** It derives `family` from
>   `Action.kind` (`BUILD -> BUILDING`, `RESEARCH -> RESEARCH`), not from which
>   `candidates.py` generator produced the action, so it already independently re-verifies
>   an unlock-chain step's legality — confirmed by test, not merely assumed
>   (`tests/test_guard.py::test_prerequisites_gate_passes_an_unlock_chain_step_from_
>   candidates_py`).
>
> **This phase's own acceptance criterion, again zero behaviour change with empty
> targets**: with no declared `ship_targets`/`defense_targets`/`research_priority`,
> `generate_unlock_chain_candidates` returns `[]` and rung 8b never fires — pinned directly
> in `tests/test_candidates.py`/`tests/test_plan.py`, and every pre-Phase-4 test in
> `test_plan.py`/`test_candidates.py`/`test_guard.py`/`test_tick.py` passes unmodified
> (§9 AC33-41).
>
> **Not delivered by this phase, on purpose**: no ROI number for an unlock step (see
> above); no commitment to the rest of a multi-tick plan (re-derived live every tick, never
> queued); no change to `policy.strategy.building_priority`'s own reachability path or
> precedence.

> **Phase 5c, 2026-08-17 (this change) — a fifth band, logistics.** Rungs 5-9 are now a
> **five**-band pipeline, not four: a new `plan.py` rung `8c` (`candidates.
> select_logistics_candidate`) runs after band 4 (unlock-chain), reached only when bands
> 1-4 produce nothing at all for any target planet — the same "never outranks a scored
> economic pick or the storage-overflow deadline" precedence rule §5.4's Phase 4 note
> above already states for band 4, extended by one more band on the same principle.
>
> - **`generate_transport_candidates`** — `FleetMissionType.Transport` (0) between the
>   player's own planets. Moves whichever single resource is furthest above
>   `policy.reserves` on the origin planet to whichever other own planet currently holds
>   the least of it (a simple, deterministic heuristic, not a claimed-optimal multi-
>   resource allocation), using only already-built cargo-capable ships (never proposes
>   building a ship to enable a mission). Cargo amount is bounded by `calc.
>   available_cargo` (capacity minus `calc.mission_fuel`'s own fuel cost at the computed
>   `calc.distance`/`calc.ship_movement_stats`-derived slowest speed), never by surplus
>   alone.
> - **`generate_harvest_candidates`** — `FleetMissionType.Harvest` (4), restricted to the
>   contract's own local special case (`originPlanetId == targetPlanetId`,
>   `LOCAL_HARVEST_DISTANCE = 5`): a planet's own debris field, never a foreign one.
>   Requires an already-built Recycler (the contract reverts on `ships.recycler == 0`).
>   The frozen `Snapshot` model carries no debris-field data on any route this codebase
>   reads, so this generator takes an explicit `own_planet_debris` parameter rather than
>   fetch-or-guess it. **Live since 2026-08-28** (correction 67, §9): `tick.py`'s
>   `_own_planet_debris` wires it from `/universe/galaxies/{g}/systems/{s}`
>   (`read.fetch_universe_system`), confirmed live-populated — closing the "never
>   observed with a populated sample" gap this row previously described.
> - Both gated, independently, once each, on **`policy.actions.allow_fleet_noncombat`**
>   (defaults `false`) — with the default policy this band produces nothing at all,
>   identical to pre-Phase-5c behaviour, the same safety property every prior phase's
>   new capability has shipped with.
> - `calc.py` gained the ship-movement-stats formula layer this band needed:
>   `SHIP_CARGO_CAPACITY` (a fixed table), `ship_fuel_consumption`, `ship_speed`,
>   `ship_movement_stats` — all read directly from `VeydriftCatalog.sol` at the pinned
>   commit. **Not the banned "cost-scaling function" category** (§5.3/calc.py's own
>   module docstring): that ban is specifically about per-building/tech/ship/defense
>   *cost* factors, which really are unpublished rationals; cargo capacity, fuel
>   consumption and speed are a small, fully-published, `pure` lookup table with no
>   live/per-account state, and no live API route ever reports them (`/shipyard` gives
>   only `cost`/`durationSeconds`/`count`) — there is no "prefer the live value" option
>   the way `Entity.cost` has.
> - `guard.py`'s `mission_type` gate and `tick.py`'s `launchFleetMission` encoder (§5.5,
>   §6.7) are what let a `launchFleetMission` `Action` this band produces actually reach
>   `walletctl` — see those sections for the enforcement and encoding side of this
>   capability.

### 5.5 `vd guard` — guardrail evaluation

Returns `ALLOW` / `BLOCK` / `ESCALATE` with a per-gate verdict list. **Every gate is evaluated and
reported**, never short-circuited — the full verdict list is the audit artifact.

| Gate | Rule |
| --- | --- |
| `killswitch` | `$VEYDRIFT_HOME/KILLSWITCH` absent |
| `tier` | action's function ∈ tier's allowed set |
| `mission_type` | (Phase 5c, 2026-08-17) for `launchFleetMission` only: `Action.mission_type` ∈ the allowed set {Transport, Deploy, Colonize, Harvest} — default-deny, `mission_type is None` **BLOCK**s (never "nothing to check"); mirrors `veydrift-wallet`'s `OPERATOR_ALLOWED_MISSION_TYPES` exactly, independent of and in addition to `tier` |
| `prerequisites` | proposed entity's on-chain requirements (`techtree.py`) are met on the target planet — a level the snapshot didn't report is treated as unmet, never assumed high enough; also enforces shield-dome/missile-slot caps |
| `address` | destination ∈ **live** `/runtime-config` address set |
| `abi_hash` | live `deploymentAbiHash` == pinned → else **BLOCK all writes** |
| `health` | `ok && readiness.ready` |
| `index_lag` | receipt indexed within `max_index_wait_s`, else halt rather than act on stale state |
| `affordability` | `resourcesAsOfNow` ≥ the API's live `cost` |
| `energy` | post-action `produced ≥ required`, `scaleBps == 10000` |
| `storage_overflow` | no resource hits cap before the next tick unspent |
| `fields` | fields used < 100%, warn at `field_warn_pct` |
| `reserve` | resource floors preserved |
| `gas_per_tx` / `gas_per_day` | within ceilings; daily counter in `agent-state.json` |
| `eth_floor` | wallet ETH ≥ `eth_gas_floor_wei` |
| `value_ceiling` | spend > `escalate_above_pct_of_resources` (default 25%) → **ESCALATE** |
| `idempotency` | no pending tx for the same `(planet, action, entity)` |
| `revert_streak` | same action reverted < 2× |

> **18 gates total as of Phase 5c (2026-08-17)**, not 17 — `mission_type` (above) is the
> addition. `AGENTS.md` §5's "the two tier-enforcement layers must agree" now covers two
> independent duplications: `guard._MIN_TIER_FOR_FUNCTION` vs. `allowlist.ts`'s
> `ECONOMY_SIGNATURES`/`LAUNCH_FLEET_MISSION_SIGNATURES` (function-level, unchanged), and
> `guard._ALLOWED_MISSION_TYPES` vs. `allowlist.ts`'s `OPERATOR_ALLOWED_MISSION_TYPES`
> (mission-type-level, new) — both checked by the same test,
> `test_tier_map_agrees_with_the_wallet_engines_allowlist`, extended in this change.
> Every place this spec (and `references/guardrails.md`, `README.md`,
> `docs/PLAYER-GUIDE.md`) previously said `14/17 pass (block)` for a routine tier-1
> proposal now reads `15/18 pass (block)` — `mission_type` PASSes trivially for every
> non-`launchFleetMission` action, so it adds one more PASS, never new noise.

> **Gap closed, 2026-08-16 (legality layer, phase 1 of the general-strategy-engine
> program).** Nothing in `plan.py` or `guard.py` checked an on-chain prerequisite of any
> kind before this change. `plan.py`'s rung 7 (`_next_research_action`) picked
> `min(snapshot.technologies, key=lambda t: ((t.level or 0), t.id))`, which on a fresh
> planet resolves to Energy Technology (id 0) — but Energy requires Research Lab ≥ 1
> (`VeydriftDependencies.sol`'s `requireResearch`, composed from
> `VeydriftCatalog.researchLabRequirement`). On a fresh planet at tier 2 that is a
> guaranteed on-chain revert, paid in real gas, the first time the ladder's own default
> pick was ever submitted. The same hole let rung 8 propose a Rocket Launcher on a planet
> with no Shipyard (`requireDefense`'s unconditional `if (shipyardLevel == 0) revert`).
>
> Fixed with a new pure-data module, `skills/veydrift-agent/src/veydrift_agent/techtree.py`
> — the full building/ship/defense/research prerequisite table transcribed from
> `VeydriftDependencies.sol`/`VeydriftCatalog.sol` at the pinned commit, plus the
> shield-dome and missile-silo-slot hard caps — wired in twice, independently: `plan.py`
> now filters every candidate through it before returning an `Action` (a locked first
> choice is skipped in favour of the next unlocked candidate, never silently dropped to a
> rung-9 NOOP when an unlocked alternative exists), and the new `prerequisites` gate above
> independently re-derives the same check from `Snapshot` rather than trusting what
> `plan.py` already filtered — the same defense-in-depth posture `_gate_energy` already
> takes toward the energy invariant. Both sides fail closed on absent data: a building or
> technology level the snapshot didn't report is treated as *not* satisfying the
> requirement, never assumed high enough (`AGENTS.md` §5's no-vacuous-pass rule applied to
> a new class of check). The tech-tree table itself is **transcribed from contract source
> and has never been validated against a live revert** — see §11.

> **Re-verified, 2026-08-16 (Phase 3 of the general-strategy-engine program).** Phase 3 makes
> `Action.quantity` routinely > 1 for the first time (ship/defense stock-keeping toward a declared
> count) and makes the shield-dome/missile-silo cap check reachable through paths other than the old
> hardcoded single Rocket Launcher. `_gate_prerequisites`/`_defense_cap_violation` already read
> `action.quantity` generically (defaulting to `1` only when absent) and already sum
> `techtree.MISSILE_SLOTS` across every missile id, so **no code change was needed** — verified by
> two new tests requesting a multi-unit Small Shield Dome and a multi-unit Interplanetary Missile
> purchase that a `quantity=1` request from the same starting count would have passed
> (`tests/test_guard.py::test_prerequisites_blocks_a_multi_unit_shield_dome_request_even_at_zero_built`,
> `::test_prerequisites_blocks_a_multi_unit_missile_request_over_remaining_silo_capacity`). `candidates.py`
> gains its own, independently-written cap check (`_defense_capacity_reason`) for the same contract
> rule — deliberate duplication, not shared code, matching the defense-in-depth posture `_gate_energy`
> already takes toward `plan.py`'s energy invariant — using the new `PlanetSnapshot.missile_silo_level`
> field (§5.2); `guard.py`'s own check is untouched and keeps reading the Missile Silo *building*
> level from `Snapshot.planets[].buildings`, an independent source for the same number.

### 5.6 `policy.json`

A pydantic `Policy` model. `schemas/policy.schema.json` is **generated** from it and committed;
`vd init` writes `$VEYDRIFT_HOME/policy.json` from `assets/policy.example.json`. An invalid policy is
a hard stop — never a silent fallback to defaults.

```json
{
  "version": 1,
  "tier": "advisor",
  "wallet": "0x224aba5d489675a7bd3ce07786fada466b46fa0f",
  "planets": [664],
  "chain_id": 8453,
  "cadence": { "economy_minutes": 10, "research_minutes": 10, "fleet_minutes": 10, "universe_hours": 24 },
  "limits": {
    "gas_per_tx_wei": "3000000000000000",
    "gas_per_day_wei": "20000000000000000",
    "eth_gas_floor_wei": "2000000000000000",
    "escalate_above_pct_of_resources": 25,
    "max_index_wait_s": 300,
    "field_warn_pct": 80
  },
  "reserves": { "metal": 0, "crystal": 0, "deuterium": 0 },
  "storage": { "hours_to_cap_trigger": 2 },
  "actions": {
    "allow_building": true, "allow_research": true,
    "allow_defense": false, "allow_ships": false,
    "allow_fleet_noncombat": false, "allow_combat": false
  },
  "escalation": {
    "on_incoming_fleet": true, "on_abi_hash_change": true,
    "on_health_unhealthy_minutes": 30, "on_revert_count": 2
  },
  "wallet_engine": { "provider": "keystore", "require_confirmation": true },
  "strategy": {
    "resource_weights": { "metal": 1, "crystal": 1, "deuterium": 1 },
    "max_alternatives": 5,
    "ship_targets": [],
    "defense_targets": [],
    "research_priority": [],
    "building_priority": []
  }
}
```

`"planets": []` means discover via `/wallet/{addr}/planets`.

**`strategy`** (Phase 2 of the general-strategy-engine program, 2026-08-16): config for the
`candidates.py` generate/filter/score/select pipeline (§5.4). `resource_weights` is the exchange rate
`score_payback` uses to collapse a metal/crystal/deuterium cost triple to a scalar for payback-hours
scoring — default 1:1:1 preserves the assumption the pre-Phase-2 `_energy_candidate` already made
implicitly (it summed the three unweighted). `max_alternatives` caps `Action.alternatives` so
`proposals.jsonl` stays bounded. Both fields are additive for an existing `policy.json` (absent
`strategy` key -> default), but because `Policy` is `extra="forbid"`, a new policy file that sets
`strategy` will not load on an agent build predating this field.

**`ship_targets` / `defense_targets` / `research_priority` / `building_priority`** (Phase 3 of the
general-strategy-engine program, 2026-08-16 — see §5.4's Phase 3 note for the full behaviour). Each
entry of `ship_targets`/`defense_targets` is an `EntityTarget`: `{"name": "Crawler", "count": 20}` or
`{"id": 15, "count": 20}` (exactly one of `name`/`id`) — `name` is resolved case-insensitively against
`ids.py`'s `ship_name`/`defense_name` tables, and **an unresolvable name raises loudly** (`ValueError`,
surfacing as a failed `vd plan`/`vd tick`) rather than silently proposing nothing — the same "typo must
never mean silence" posture `Policy`'s `extra="forbid"` already takes at the key level, extended to
target *values*. `research_priority`/`building_priority` are plain ordered name lists, resolved the
same way. All four default to `[]`, and `[]` reproduces Phase 2's behaviour byte-for-byte — this is
Phase 3's own acceptance criterion (§9 AC25).

Since Phase 4 (2026-08-16), a `ship_targets`/`defense_targets`/`research_priority` entry the account
cannot build *yet* is no longer a dead end: `plan.py` rung `8b` proposes the shallowest unmet
prerequisite toward it instead, when nothing else on the ladder found anything (see §5.4's Phase 4
note). `building_priority` is unaffected — it already has its own reachability path and does not feed
rung `8b`.

### 5.7 `vd tick` — the loop entrypoint

```
vd tick [--policy PATH] [--dry-run] [--readiness] [--format md|json]
```

Idempotent, lockfile-protected in `$VEYDRIFT_HOME`:

```
1. load + validate policy         6. guard
2. killswitch check               7. if ALLOW and tier>=2 and not --dry-run:
3. reconcile pending txs                 walletctl build -> confirm -> send
4. snapshot                              await receipt, THEN await INDEXED
5. plan                           8. log: proposal, unless content-identical to the
                                     immediately-previous logged proposal (dedup);
                                     action only if executed
                                  9. pretty report -> stdout + logs/ticks/
```

Step 8's dedup: a repeat `vd tick` invocation whose full proposal record (everything
except `ts`/`tick`) is byte-identical to the immediately-previous logged proposal is not
new evidence -- most commonly a human/agent re-running `vd tick` seconds later just to
re-inspect a different `--format`. `tick_count`/`proposals_count` don't advance and
nothing is appended to `proposals.jsonl`/`strategy.md` on that repeat; `last_tick_at`
still updates, and the printed/`--format json` report always shows the full, accurate
current state with a `duplicate`/`note` marker. This is content-based, not time-window
based: live guard-evaluation figures drift over real elapsed time even when the
recommendation itself is unchanged, so a genuine re-evaluation hours later still logs
normally.

Step 8 also runs a best-effort human-activity check, gated on there being anything
unresolved from the *previous* tick to check (an on-chain proposal this tool itself did
not execute): a `/wallet/{addr}/activity` fetch, embedded into this tick's own
`proposals.jsonl` entry (`human_activity_check`, excluded from the dedup fingerprint
above -- its content legitimately varies tick to tick even when this tick's own proposal
is a genuine repeat) and surfaced on the printed report as an `activity:` line. See §4's
promotion-evidence note for the full framing (raw evidence, never a verdict).

`--dry-run` is the default at tier 1 and cannot be disabled there. The indexed-wait in step 7 is
mandatory: a confirmed receipt is not indexed state, and no dependent action may follow until the
index reflects it.

### 5.8 Scheduling adapters (`references/scheduling.md`)

The skill owns `tick`; the harness owns cadence.

| Harness | Adapter |
| --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` |
| Claude Code, unattended | `claude -p "run a veydrift tick"` from launchd |
| Hermes | register `vd tick` on Hermes' scheduler at `cadence.economy_minutes` |
| Bare OS | `assets/com.veydrift.agent.plist.template` — launchd, `StartInterval`, logs to `$VEYDRIFT_HOME/logs/` |

The plist ships as a template with a documented install command. It is not installed.

### 5.9 Logging, pretty formatting, changelog

Rendered with `rich`:

```
[2026-08-11T19:42:03Z] TICK #142  tier=advisor  planet 664 (7:181:14)
  state:    M 1,842  C 1,201  D 318   | energy 79/44 (scale 10000) | fields 7/174
  queues:   build idle · research idle · ship idle · defense idle
  incoming: none
  PROPOSE   startBuildingUpgrade(664, 3 SolarPlant)  ->  level 3 → 4
    cost:   M 225  C 75  D 0        (affordable, 12% of holdings)
    why:    mines at 3 require 157 energy; solar 3 produces 79. Energy-first invariant.
    guards: 17/20 pass (block)
    tx:     to 0xf397…755d  data 0x…  (NOT SUBMITTED — tier advisor)
  next:     Metal Mine 3→4, blocked on energy until Solar 5
```

> **Correction, 2026-08-12 (judge finding).** v2.0's example showed `guards: 16/16 pass` at
> `tier=advisor`, which is impossible: for an onchain proposal the tier gate *must* fail at tier 1 —
> that is the entire meaning of the tier. Beyond being wrong in the example, it hid a real design
> problem: because every tier-1 onchain proposal therefore reports `decision=block`, `strategy.md`
> accrued a near-identical entry every tick and `--readiness`'s "which guardrails fired" statistic
> was swamped by structural noise — degrading precisely the promotion evidence §4 calls the useful
> signal.
>
> **A structural tier block must therefore be reported separately from a substantive guardrail
> firing** (affordability, energy, storage, gas, reserve…). The full verdict list still goes to
> `proposals.jsonl` — that remains the audit artifact — but the human-facing summary must not treat
> "tier 1 declined to submit, as designed" as evidence of anything.
>
> **Correction, 2026-08-17.** The example above previously read `guards: 15/16 substantive pass ·
> tier: block (structural — advisor never submits)` and `tx: ... (NOT SUBMITTED — tier 1)`. Both
> numbers and the wording were stale on two counts. First, the gate total has grown from 16 to 18
> since this example was last touched (`prerequisites` added Phase 1, `mission_type` added Phase
> 5c — §5.5). Second, `tick.py`'s actual renderer (`tick.py:985`,
> `f"  guards: {guard_report.passed}/{guard_report.total} pass ({guard_report.decision.value})"`)
> has never split "substantive" from "structural" in the printed line itself — that distinction
> lives in `guard.is_structural_tier_block`, not in this string. Confirmed against a live
> `vd tick --dry-run` run 2026-08-17 (prints `guards: 12/18 pass (block)` and
> `NOT SUBMITTED -- tier advisor` for a real, differently-shaped proposal) and against
> `guard.py`'s own `is_structural_tier_block` docstring, which documents the canonical routine
> tier-1 case on an unlocked entity as exactly `guards: 15/18 pass (block)` — `tier` (BLOCK),
> `gas` and `eth_floor` (ESCALATE, no estimate/balance available yet at tier 1) are the 3 gates
> that don't pass. The example above now uses that exact figure and the real `tier {value}`
> wording rather than the historical `16/16`-derived `15/16`/`tier 1` phrasing.

| Sink | Contents | Mutability |
| --- | --- | --- |
| `logs/proposals.jsonl` | every proposal + full guard verdicts + calldata | append-only |
| `logs/actions.jsonl` | **executed only**: tx hash, gas, block, before/after, indexed-at | append-only |
| `logs/ticks/<iso>.md` | the block above | one per tick |
| `logs/strategy.md` | rationale, plan revisions, escalations, human decisions | append-only |
| `CHANGELOG.md` | Keep a Changelog; an entry per tier promotion or capability change | curated |

`vd log --digest 24h` produces the daily summary: builds, research, resources produced, gas spent,
and **everything refused, with reasons**.

**Secrets never enter a log.** `log.py` scrubs any `0x[0-9a-fA-F]{64}` that is not a known tx hash,
and refuses to write any value matching a configured secret env var.

---

## 6. The `veydrift-wallet` skill

Built after reading `https://ethskills.com/SKILL.md` and `https://ethskills.com/wallets/SKILL.md`;
`references/tx-safety.md` records which of their rules this implements and which it consciously
skips. Note their convention: **"onchain" is one word**, used throughout our docs.

### 6.1 The constraint that governs every provider decision

**A Veydrift planet is permanently bound to the EOA that settled it.** Ownership is
`_planets[planetId].owner` — a plain struct field. There is no `transferPlanet`, planets are not
NFTs, and `abandonPlanet` destroys rather than transfers (`NOTES.md` §13).

The consequence for wallet architecture is severe and easy to miss:

> **Any provider that issues a new address cannot hold planet 664.** Safe multisig, ERC-4337 smart
> accounts, Cobo, Coinbase CDP Server Wallets and Turnkey-generated wallets all create a *new*
> address. Moving to one means abandoning the planet and re-settling from scratch.

That reduces the viable option space to two shapes, and this is what §6.5's research must evaluate:

1. **Providers that adopt the existing key** — encrypted keystore, HSM/KMS import, or an MPC service
   supporting key import.
2. **EIP-7702 delegation** — the EOA gains smart-account behaviour (session keys, spending caps,
   batching) **while keeping its address**, hence keeping the planet. **Confirmed live on Base**
   (2026-08-12): transaction `0xba45e2808d60302f4dbc7f63ab5d4e8cf914789eab289c358788c194d8c1d4db`
   in block `49860849` has `type: 0x4` with a one-entry `authorizationList`, verified directly via
   `eth_getTransactionByHash`. An earlier draft inferred this from the Pectra `requestsHash` block
   header and flagged it as unproven; that caveat is retired.

The ethskills recommendation of a 2-of-3 Safe for agent wallets is sound in general and **not
applicable here**, for exactly this reason. Recording the disagreement is the point.

### 6.2 CLI

```
walletctl status                      # provider, address, chainId, ETH balance, ABI pin state
walletctl verify-abi                  # live deploymentAbiHash vs pinned -> exit 1 on drift
walletctl build   --action a.json     # -> unsigned {to, data, value, chainId, gas}
walletctl simulate --tx tx.json       # eth_call + estimateGas; surfaces reverts
walletctl send    --tx tx.json --confirm
walletctl receipt --hash 0x…
```

`send` without `--confirm` exits non-zero and prints the transaction it *would* have sent. No env var
or flag makes `--confirm` implicit.

Per ethskills' transaction-safety pattern, `send` prints before prompting: checksummed destination
(`viem.getAddress`), decoded function + args, value, estimated gas and total cost in ETH, and the
purpose string from the action.

### 6.3 Providers

```ts
export interface WalletProvider {
  readonly name: string;
  getAddress(): Promise<`0x${string}`>;
  signAndSend(tx: UnsignedTx): Promise<`0x${string}`>;
  capabilities(): { canSign: boolean; canSimulate: boolean; remotePolicy: boolean };
}
```

| Provider | Status | Notes |
| --- | --- | --- |
| `keystore` | **implemented — default** | EIP-2335 / geth encrypted JSON keystore, decrypted via `ethers.Wallet.fromEncryptedJson`. Path from `VEYDRIFT_KEYSTORE`, password from `VEYDRIFT_KEYSTORE_PASSWORD` or an interactive prompt. Held in memory only for the signing call |
| `envkey` | **implemented — testing only** | Raw `VEYDRIFT_PRIVATE_KEY` via viem. Prints a startup warning; ethskills ranks a plaintext `.env` key as testing-grade storage. Refuses to start if the key is also found in any file under the repo |

Selected by `policy.wallet_engine.provider`, overridable by `WALLET_PROVIDER`.

Two working providers — rather than one working provider and one stub — is what actually proves the
interface is swappable.

### 6.4 Allowlist — enforced in the wallet engine, not only in the agent skill

Defence in depth: a fully compromised `veydrift-agent` still cannot make `walletctl` sign elsewhere.

1. `tx.to` ∈ addresses from a **live** `/runtime-config` fetch — never a hardcoded list.
2. `tx.data`'s 4-byte selector ∈ the tier's allowed selector set, computed from the **pinned ABI**.
3. `tx.value == 0` unless the action is an explicitly whitelisted payable (none are reachable here).
4. `tx.chainId == 8453`.
5. Any failure → non-zero exit, log the rejection, sign nothing.

> **Gap closed, 2026-08-12 (judge finding).** v2.0 specified *that* the selector must be in "the
> tier's allowed set" but never said **where the engine learns the tier**. The implementation took it
> from `--tier`/`VEYDRIFT_TIER`, supplied by the agent — so the check defended against a caller by
> trusting that caller's own claim about its privilege level. A compromised agent running under a
> tier-1 policy could simply pass `--tier operator`.
>
> **Requirement:** `walletctl` reads `tier` from `$VEYDRIFT_HOME/policy.json` itself. If `--tier` is
> also supplied and disagrees, it **refuses** rather than preferring either. A malformed or
> unparseable policy refuses too — a security policy must never fail open to a permissive default.
>
> **Residual limit, stated honestly:** when signing credentials are readable from the environment
> (`VEYDRIFT_PRIVATE_KEY`, `VEYDRIFT_KEYSTORE_PASSWORD`), a fully compromised agent can bypass
> `walletctl` entirely and sign directly with viem. The two-layer defence is only airtight with the
> interactive keystore prompt, where the human supplies the password per signing session. Anyone
> reading §6.4 as "the agent cannot sign outside the allowlist" should read it as "the agent cannot
> sign outside the allowlist *through this tool*".
>
> **And a second, cheaper bypass of the tier check** (second judge pass, 2026-08-12, confirmed by
> execution): the no-policy fallback means a caller that controls its own environment can point
> `VEYDRIFT_HOME` at an empty directory and pass `--tier operator`. The fallback is deliberate —
> `walletctl` must work standalone before `vd tick init` has run — so **the tier check defends
> against an honest-but-misconfigured caller, not a hostile one.** The checks that do survive a
> hostile caller are the ones that are properties of the *transaction* rather than claims by the
> caller: live-config address, chainId, `value == 0`, selector set, mission type, mandatory
> `--confirm`, and refusal to send a nonpayable-read. State the distinction that way; a tier check
> that reads as a security boundary when it is really a misconfiguration guard is worse than none.

> **Allowlist change, Phase 5 (2026-08-17, docs/SPEC.md §5.4/§9), breaking, `veydrift-wallet`
> v0.2.0.** `settlePlanet(uint256)` removed from `ECONOMY_SIGNATURES` — its body at the pinned
> commit is byte-identical to `collectResources`, a disguised read `sendTx` already refuses, and no
> `veydrift-agent` planner rung ever produced this action; it was allowlisted capacity that could
> only ever burn gas. Removed from `guard.py`'s `_MIN_TIER_FOR_FUNCTION` in the same change.
>
> **`OPERATOR_ALLOWED_MISSION_TYPES` was deliberately NOT widened this phase**, despite Phase 5's
> brief asking for Colonize (2) to be added. The colonisation entrypoint was verified first, per
> that brief's own instruction: `VeydriftGame.sol`'s facade `launchFleetMission` (both overloads)
> reads `missionType` via inline assembly and dispatches to `VeydriftColonizationModule` when it
> equals `Colonize`, whose `_validateColonyCreation` calls `_requireShips(originPlanetId,
> Ship.ColonyShip, 1)` — confirming `launchFleetMission` is genuinely the entrypoint, not a
> different function. The widening itself was withheld because its Python-side counterpart (an
> independent mission-type gate in `guard.py`, per AGENTS.md §5's two-layer-agreement invariant)
> could not be built — it needs `Action` fields that don't exist on `models.py`, frozen for the
> work package that attempted this. Widening this allowlist alone would have left the wallet engine
> as the *sole* enforcement layer for a launchable mission type, which is exactly the asymmetry this
> project's two-layer design exists to avoid. See `veydrift-wallet`'s `CHANGELOG.md` v0.2.0 entry.

> **`OPERATOR_ALLOWED_MISSION_TYPES` widened to include Colonize (2), 2026-08-17 (Phase
> 5b, this change), `veydrift-wallet` `[Unreleased]`.** This is the widening the note
> above describes as withheld — done now, in **the same change** as `guard.py`'s new
> `mission_type` gate (§5.5), never before it: `models.py` was unfrozen and extended
> with `ActionKind.FLEET_MISSION` and the `Action` fields `launchFleetMission` needs, so
> the Python-side counterpart the prior note names as the blocker now exists.
> `OPERATOR_ALLOWED_MISSION_TYPES` is `{0, 1, 2, 4}`; `guard.py`'s `_ALLOWED_MISSION_TYPES`
> is the same set, and `test_tier_map_agrees_with_the_wallet_engines_allowlist`
> (agent-side) now parses and compares both, extended from function-name sets to also
> cover mission-type sets. Combat types (3, 5, 6, 7, 8, 9) are unaffected — never added
> to either set, by design (this is still the *only* widening; every other mission type
> stays refused unconditionally at both layers, per AGENTS.md §5's "combat stays
> unreachable by code, not by config").

### 6.5 Deferred research — `docs/wallet-provider-research.md` (WP4b)

A document, not code. Deliverable of this pass; the decision comes later.

- Frame every candidate against §6.1: **does it preserve the planet-owning address?**
- Evaluate against: open source, free tier, self-hostable, key-import support, policy enforcement,
  Base support, and operational burden for a single-planet hobby account.
- Candidates to cover: encrypted keystore (baseline), **EIP-7702 delegation on the existing EOA**,
  Web3Signer / HashiCorp Vault (open source, self-hosted), Cobo CAW, Coinbase CDP Server Wallets,
  Turnkey, OKX OnchainOS. Record that Cobo and CDP are **hosted MPC, not open source or
  self-hostable** — which directly conflicts with the stated aim.
- Survey skills.sh for prior art. Already found: `austintgriffith/ethereum-wingman`,
  `paulrberg/agent-skills@cli-cast`, `starchild-ai-agent/official-skills@wallet-policy`,
  `coinbase/agentic-wallet-skills@*`, `okx/onchainos-skills@okx-agentic-wallet`.
- End with a recommendation and the open questions blocking it. No provider is implemented in this pass.

### 6.6 ABI pinning

- Pin `VeydriftGame.json` built at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`.
- `PINNED.json` records `{commit, abiHash, foundry:{solc, optimizer_runs, via_ir, cbor_metadata, bytecode_hash}, fetchedAt, source}`.
- `verify-abi` computes `sha256(JSON.stringify(artifact.abi))` — compact separators, forge key order —
  and compares to live. Expected `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99`.
- On drift: block every write, escalate, print the rebuild recipe.
- `abi-pinning.md` must state plainly that **`main` is not the deployed contract**, and list the
  divergent functions (`RESEARCH-ADDENDUM.md` §1.1) — `playerScore` foremost, since prior notes
  recommend it and it reverts.

### 6.7 Two traps the encoder must handle

Silent-corruption bugs, not crashes. Each gets a dedicated function and a dedicated test.

1. **The 14-slot fleet tuple.** `shipCountsToFleetTuple()` throws on non-flyable ships
   (SolarSatellite 9, Crawler 15); tuple indices 9–13 map to Ship ids 10–14. Test asserts a Destroyer
   lands at tuple index 9, not 10.
2. **`launchFleetMission` is overloaded** — 7-arg and 6-arg forms both live on the deployed ABI.
   Always select by full signature, never by name.

Plus: `attackProtectionStatus`, `collectResources`, `debrisField`, `maxRaidLoot`,
`protectedResources`, `raidableResources` are `nonpayable` but semantically reads. Route them through
`simulate`; `send` must refuse them.

---

## 7. AGENTS.md and CLAUDE.md

`AGENTS.md` — primary, harness-agnostic:

1. What this is, the tier model, the current tier
2. Repository map and `$VEYDRIFT_HOME`
3. **Install/update: `npx skills add . -a claude-code -a hermes-agent`**
4. Running one tick in each harness
5. The safety contract — what the agent will never do; the escalation list
6. Where logs live and how to read them
7. Promotion procedure T1→T2→T3 and the evidence required
8. Key custody: **the wallet is the account; there is no recovery**, and §6.1's address-binding constraint
9. Pointers into `docs/`

`CLAUDE.md` is a pointer only: a short paragraph plus `@AGENTS.md`. It must not duplicate content.

---

## 8. Work packages

Contracts in §5 and §6 are frozen, so Wave A packages are independent.

### Wave A — parallel (Sonnet 5, high)

| WP | Scope | Deliverables |
| --- | --- | --- |
| **WP1** | Read layer | `read.py`, `http.py`, `fmt.py`, `models.py` (Snapshot), `pyproject.toml`, `references/api-routes.md`, tests with recorded `respx` fixtures |
| **WP2** | Calculators + planner | `calc.py`, `plan.py`, `ids.py`, `references/{formulas,entity-ids,strategy-playbook}.md`, tests incl. the hot-planet counterfactual |
| **WP4a** | Wallet engine | all of `skills/veydrift-wallet/` — both providers, ABI pin, allowlist, both trap functions, `references/{providers,abi-pinning,tx-safety}.md`, vitest suite. **Must read both ethskills docs first** |
| **WP4b** | Provider research | `docs/wallet-provider-research.md` per §6.5. Research only — no code |

### Wave B — parallel, after Wave A

| WP | Scope | Deliverables |
| --- | --- | --- |
| **WP3** | Guardrails + logging + tick | `guard.py`, `log.py`, `tick.py`, `state.py`, `cli.py`, generated schemas, `policy.example.json`, plist template, `references/{guardrails,scheduling}.md`, tests |
| **WP5** | Docs + skill authoring | both `SKILL.md` files (skill-creator best practices), `AGENTS.md`, `CLAUDE.md`, `CHANGELOG.md`, `references/contract-writes.md`, install verification |

### Wave C — judgement

Fable 5 (high) reviews the implementation **and this spec**, reporting correctness, spec violations,
silent-failure risks, guardrail bypasses and spec defects. Triage, fix, repeat until clean.

---

## 9. Acceptance criteria

**Functional**
1. `vd read snapshot --summary` returns in <10 s, ≤2 KB, for `0x224a…fa0f`.
2. `vd read battle-reports` without `--out` exits non-zero rather than printing 60 KB.
3. `vd calc verify` passes the three duration checks against the live API.
4. `vd plan` on the current 664 snapshot proposes an energy-first opener and **never** a Solar
   Satellite; the same planner on a hot-planet fixture **does** propose satellites.
5. `vd tick --dry-run` completes end-to-end, writes a pretty report, `proposals.jsonl` and
   `strategy.md`, and writes **nothing** to `actions.jsonl`.
6. `walletctl verify-abi` prints `sha256:62cdedb7…6bc99` and matches live.
7. `walletctl send` without `--confirm` exits non-zero.
8. `walletctl` rejects a tx to a non-Veydrift address, and a selector outside the tier set.
9. `shipCountsToFleetTuple` places Destroyer at index 9; throws on SolarSatellite and Crawler.
10. `touch $VEYDRIFT_HOME/KILLSWITCH` → the next tick halts before any network call beyond health.
11. Both providers return the **same address** for the same key material, one from keystore, one from env.
11a. `vd plan` never proposes an entity whose contract prerequisites are unmet; `vd guard` BLOCKs one
    that is unmet **and** one whose level the snapshot did not report.

**Structural**
12. `npx skills add .` installs both skills to `claude-code`; they load and are invocable by name.
13. Scripts work when invoked from an arbitrary cwd after install (paths resolved from `__file__`).
14. `uv run` works with no prior install step; `uv.lock` committed.
15. `veydrift-agent` never imports viem/ethers/web3 and never signs. Grep-verifiable.
16. No private key, mnemonic, keystore or API secret in any tracked file. `git log -p | grep -iE 'private.?key|0x[a-fA-F0-9]{64}'` clean.
17. Nothing is written inside the skill tree at runtime; all state under `$VEYDRIFT_HOME`.

**Documentary**
18. `AGENTS.md` documents the promotion procedure and required evidence per gate.
19. `CHANGELOG.md` exists with an initial entry.
20. Every reference file cites provenance per claim (docs.md / contract `file:line` / live probe date).
21. Nothing in `references/` contradicts `RESEARCH-ADDENDUM.md`: no `playerScore`, defense route is
    `/defenses`, Defense and FleetMissionType enums match the contract.
22. `wallet-provider-research.md` leads with the address-binding constraint (§6.1) and states plainly
    that Cobo and CDP are hosted, not open source.
23. `vd tick`'s printed report and `proposals.jsonl` carry the winning `Action`'s ranked
    `alternatives`, each with a stated reason it lost (a payback-hours comparison, or a
    `techtree.describe()` lock reason) — added 2026-08-16 (Phase 2 of the
    general-strategy-engine program).
24. Two content-identical `vd tick` invocations (`alternatives` included) still dedup to one logged
    `proposals.jsonl` entry; a tick whose only real change is a different runner-up in
    `alternatives` is logged as a new tick, not suppressed — `tests/test_tick.py`'s
    `test_two_identical_ticks_with_alternatives_attached_still_dedup_once` and
    `test_ticks_whose_only_difference_is_alternatives_are_not_deduped` pin both halves. Added
    2026-08-16 (Phase 2 of the general-strategy-engine program).

**Phase 3 of the general-strategy-engine program, added 2026-08-16 — every planet-local entity
reachable (§5.4/§5.6):**

25. With every `policy.strategy` target field empty (the default), `plan_next_action`'s output is
    byte-identical to Phase 2's on the same fixtures —
    `tests/test_plan.py::test_empty_strategy_targets_reproduce_phase_2_planner_output_exactly` /
    `::test_empty_strategy_targets_reproduce_phase_2_hot_planet_output_exactly`, plus every
    Phase-1/Phase-2 test in `test_plan.py`/`test_candidates.py`/`test_guard.py` passing unmodified.
26. A declared `ship_targets`/`defense_targets` entry below its live count is proposed; at or above
    count is not; a locked entry is skipped with `techtree.describe()`'s text in the reason —
    `tests/test_candidates.py::test_ship_target_below_count_is_proposed`,
    `::test_ship_target_at_count_is_not_proposed`,
    `::test_locked_ship_target_is_skipped_with_techtree_describe_in_the_reason`.
27. A second Small Shield Dome is refused (1-per-planet cap), and a missile request exceeding
    `missile_silo_level * 10` remaining slots is refused — both independently, in `candidates.py`
    (planner side) and `guard.py` (unchanged, re-verified generalizes to `quantity > 1`) —
    `tests/test_candidates.py::test_second_small_shield_dome_is_refused`,
    `::test_missiles_over_silo_capacity_are_refused`;
    `tests/test_guard.py::test_prerequisites_blocks_a_multi_unit_shield_dome_request_even_at_zero_built`,
    `::test_prerequisites_blocks_a_multi_unit_missile_request_over_remaining_silo_capacity`.
28. An unknown entity name in `ship_targets`/`defense_targets`/`research_priority`/`building_priority`
    raises rather than silently proposing nothing —
    `tests/test_candidates.py::test_unknown_ship_target_name_fails_loudly` (+ the defense/research
    siblings).
29. A crawler candidate is scored via `calc.crawler_boost_bps` and respects the 8-per-mine-level cap
    (a saturated boost scores `None`, never a spurious positive payback) —
    `tests/test_candidates.py::test_crawler_candidate_is_scored_when_boost_has_room_to_grow`,
    `::test_crawler_candidate_respects_the_eight_per_mine_level_cap`.
30. Proactive storage is a scored-band (Band 2) candidate, always `score=None`, present regardless of
    overflow urgency — `tests/test_candidates.py::test_proactive_storage_candidate_scored_none_and_
    present_regardless_of_urgency`.
31. `building_priority` orders the new `infrastructure` family, taking precedence over the ordinary
    mine walk when set; `research_priority` overrides the fallback order (originally
    lowest-level-first; see criterion 64 for its 2026-08-22 replacement), and the fallback
    pick's reason is explicitly labelled `"default: ..."` —
    `tests/test_candidates.py::test_building_priority_orders_infrastructure_candidates`,
    `::test_building_priority_selects_first_unlocked_declared_building`,
    `::test_research_priority_overrides_lowest_level_first`,
    `::test_research_fallback_is_explicitly_labelled_default`.
32. `missile_silo_level is None` is never read as `0` by the new `candidates.py` cap-check code (a
    separate failure mode from `guard.py`'s own, already-covered, building-level-sourced check) —
    `tests/test_candidates.py::test_defense_target_missile_silo_level_none_fails_closed_not_as_zero`.

**Phase 4 of the general-strategy-engine program, added 2026-08-16 — a locked declared target
proposes its own unlock chain (§5.4):**

33. `techtree.next_step_toward` returns the shallowest currently-buildable prerequisite for a
    hand-worked chain (Small Cargo -> Shipyard 2 + Combustion Drive 2 -> Shipyard needs Robotics
    Factory 2 -> Robotics Factory needs nothing), not the locked immediate requirement and not the
    original target — `tests/test_techtree.py::test_next_step_toward_hand_worked_small_cargo_chain`.
34. Once the deeper prerequisite is already satisfied, the walk returns the *nearer* unmet link
    instead, proving it is "shallowest," not "deepest" or "first-declared" —
    `tests/test_techtree.py::test_next_step_toward_returns_first_step_not_final_target_when_shallower`.
35. An already-unlocked target returns `None` —
    `tests/test_techtree.py::test_next_step_toward_already_unlocked_target_returns_none`.
36. The real requirement graph (all four `_TABLES`) is asserted acyclic by test, not merely assumed;
    a synthetic cycle injected into a *copy* of `BUILDING_REQUIREMENTS` (monkeypatched, never
    mutating the real table) cannot hang the walk — `_MAX_UNLOCK_DEPTH` bounds it and it returns
    `None` — `tests/test_techtree.py::test_real_requirement_graph_is_acyclic`,
    `::test_next_step_toward_is_depth_bounded_against_a_synthetic_cycle`.
37. When two locked declared targets resolve to two different unlock steps,
    `generate_unlock_chain_candidates` orders them by weighted cost ascending
    (`policy.strategy.resource_weights`, live `Entity.cost` only); a step whose cost cannot be
    determined sorts after every known-cost one, never interleaved —
    `tests/test_candidates.py::test_generate_unlock_chain_candidates_ties_broken_by_weighted_cost_ascending`,
    `tests/test_candidates.py::test_unlock_weighted_cost_orders_unknown_cost_after_every_known_cost`.
38. A chain that bottoms out in a node whose own current level was never reported yields no
    confidently-chosen step (`None`), even though the node's own requirements (or lack thereof) would
    otherwise qualify it —
    `tests/test_techtree.py::test_next_step_toward_absent_level_data_yields_no_confidently_chosen_step`.
39. The walk correctly switches between a building lookup and a technology lookup mid-chain
    (`ReqSource.BUILDING -> EntityFamily.BUILDING`, `ReqSource.TECHNOLOGY -> EntityFamily.RESEARCH`)
    for both a ship target and a defense target —
    `tests/test_techtree.py::test_next_step_toward_cross_family_walk_can_resolve_to_a_technology`,
    `::test_next_step_toward_defense_target_crosses_into_technology`.
40. With no declared `ship_targets`/`defense_targets`/`research_priority`,
    `generate_unlock_chain_candidates` returns `[]` and `plan_next_action` never reaches rung `8b`;
    when a scored economic candidate exists on any target planet, rung `8b` is never even consulted —
    `tests/test_candidates.py::test_generate_unlock_chain_candidates_empty_with_no_declared_targets`,
    `tests/test_plan.py::test_unlock_chain_rung_never_fires_with_no_declared_targets`,
    `::test_unlock_chain_rung_never_reached_when_an_economic_candidate_exists`.
41. `guard.py`'s `prerequisites` gate independently re-derives an unlock-chain step's legality (it
    keys off `Action.kind`, not which generator produced the action) and PASSes it —
    `tests/test_guard.py::test_prerequisites_gate_passes_an_unlock_chain_step_from_candidates_py`.

**Phase 5 (2026-08-17, docs/SPEC.md §5.4/§9)**

42. `plan.py` rung 3 (`resolveFleetMission`) fires from the real `vd tick` entrypoint, not just
    from a directly-supplied `resolvable_mission_ids` argument: `tick._resolvable_mission_ids`
    reads `/wallet/{addr}/fleet-visibility` and finds an own `outgoing` mission that is `Outbound`,
    `needsResolution`, and >60s past `arrivalAt` —
    `tests/test_tick.py::test_resolvable_mission_ids_finds_an_arrived_needs_resolution_outbound_mission`,
    `::test_run_tick_wires_resolvable_mission_ids_into_the_planner`.
43. A mission within the 60s grace window, not `needsResolution`, not `Outbound`, or missing
    `arrivalAt` is never proposed for resolution — fails closed on absent/ambiguous data, the same
    posture every other gate in this codebase takes —
    `tests/test_tick.py::test_resolvable_mission_ids_skips_missions_within_the_grace_window`,
    `::test_resolvable_mission_ids_skips_missions_that_do_not_qualify`.
44. `PlanetSnapshot.archetype` is populated from `/universe/galaxies/{g}/systems/{s}` when
    `read.snapshot` is called with `universe_cadence_hours` set (as `vd tick` always does, from
    `policy.cadence.universe_hours`); a bare `vd read snapshot` with no flag makes no new network
    call and leaves `archetype` `None`, byte-for-byte the pre-Phase-5 behaviour —
    `tests/test_read.py::test_snapshot_populates_archetype_when_universe_cadence_is_set`,
    `::test_snapshot_leaves_archetype_none_when_universe_cadence_is_not_requested`.
45. A failed/unreachable universe-route fetch leaves `archetype` `None` rather than aborting the
    snapshot — an enrichment field is never load-bearing for a guard/plan decision —
    `tests/test_read.py::test_snapshot_archetype_stays_none_when_universe_route_errors`.
46. `settlePlanet` is rejected at every tier by both enforcement layers (`guard.py`'s
    `_MIN_TIER_FOR_FUNCTION` no longer contains it; `allowlist.ts`'s `tierSelectors` no longer
    contains its selector at any tier) — `tests/test_guard.py::test_tier_map_agrees_with_...`
    (agent-side, still passes after the removal),
    `veydrift-wallet/tests/allowlist.test.ts`'s `"settlePlanet is no longer allowlisted at any tier"`.
47. **Now met** (was "not met," documented as blocked on `models.py` above — see the "Phase 5
    status note" in §1). A non-combat fleet-mission `Action` (Transport/Harvest) can be constructed
    by the planner (`candidates.generate_transport_candidates`/`generate_harvest_candidates`,
    gated on `policy.actions.allow_fleet_noncombat`) and encoded by `tick.py`
    (`_action_to_walletctl_json`'s `launchFleetMission` branch) — see criteria 48-56 below for the
    specific tests. Deploy (mission type 1) is allowlisted at both layers but has no generator
    (no rung proposes it) — a deliberate scope limit of this pass, not a gap in enforcement.

**Phase 5c/5b of the general-strategy-engine program, added 2026-08-17 — non-combat fleet
missions and colonisation (§5.4/§5.5/§6.4):**

48. `guard.py`'s `mission_type` gate default-denies: `mission_type is None` on a `launchFleetMission`
    action **BLOCK**s (never treated as "nothing to check"), and every combat type (3, 5, 6, 7, 8, 9)
    **BLOCK**s independently of the `tier` gate (still `BLOCK`s at every tier, including operator) —
    `tests/test_guard.py::test_mission_type_blocks_when_mission_type_is_none_never_passes_vacuously`,
    `::test_mission_type_blocks_every_combat_type`,
    `::test_mission_type_blocks_independently_of_tier_at_every_tier`.
49. Transport (0), Deploy (1), Colonize (2), Harvest (4) all **PASS** the `mission_type` gate; every
    non-`launchFleetMission` action PASSes it trivially, adding no noise to a routine proposal
    (at the time of that change, 18 gates total, `mission_type` PASSing as the 15th of 15 passing
    in the routine tier-1 case, `15/18` not `14/17`; the `game_paused` gate added later made the
    same routine case `16/19`, and commit 2 of the launch-actions plan's `fleet_slots` gate makes
    it `17/20` as of 2026-08-28 — see criteria 58-60, 67) —
    `tests/test_guard.py::test_mission_type_allows_transport_deploy_colonize_harvest`,
    `::test_mission_type_passes_trivially_for_a_non_fleet_action`,
    `::test_all_nineteen_gates_always_present_even_when_blocked`.
50. `guard._ALLOWED_MISSION_TYPES` and `veydrift-wallet`'s `OPERATOR_ALLOWED_MISSION_TYPES` are
    identical sets, parsed from both real files (not hardcoded in the test), and neither contains a
    combat type — `tests/test_guard.py::test_tier_map_agrees_with_the_wallet_engines_allowlist`
    (extended this phase to cover mission-type sets, not just function-name sets).
51. `tick.py` resolves `launchFleetMission`'s overload by full canonical signature, never by name:
    `Action.speed_pct is not None` selects the 7-arg form; `None` selects the 6-arg form (the
    contract's own 100%-speed default) rather than fabricating a speed value at the encoder —
    `tests/test_tick.py::test_fleet_mission_uses_the_six_arg_overload_when_speed_pct_is_none`,
    `::test_fleet_mission_uses_the_seven_arg_overload_when_speed_pct_is_set`.
52. The 14-slot fleet tuple is built correctly: a Destroyer (Ship id 10) lands at tuple index 9, not
    10 (AGENTS.md §7 trap #1) — the Python-side mirror of `veydrift-wallet`'s `fleet.test.ts` pin —
    and a non-flyable ship id (SolarSatellite/Crawler) in `Action.ships` raises, even at count 0 —
    `tests/test_tick.py::test_fleet_mission_ship_tuple_pins_destroyer_at_index_nine_not_ten`,
    `::test_fleet_mission_ship_tuple_raises_on_non_flyable_ship_even_at_zero_count`.
53. Colonize encodes `targetPlanetId` as the packed `(1<<255) | (galaxy<<24) | (system<<8) |
    position` coordinate, not a real planet id, and its trailing `uint256` (`randomnessRequestId`
    in the deployed source, carried by `Action.randomness_request_id`) is always `0` — the contract
    hard-reverts (`InvalidId`) otherwise —
    `tests/test_tick.py::test_fleet_mission_colonize_encodes_the_packed_coordinate_target`.
54. A local Harvest (`target_coordinates` unset) resolves straight to `origin_planet_id` with no
    snapshot lookup, matching the contract's own `originPlanetId == targetPlanetId` special case; a
    Transport/Deploy target not among the wallet's own planets in the snapshot raises rather than
    building calldata against a guessed planet id —
    `tests/test_tick.py::test_fleet_mission_local_harvest_targets_the_origin_planet_with_no_coordinate_lookup`,
    `::test_fleet_mission_raises_when_target_coordinates_unresolvable`.
55. `candidates.generate_transport_candidates` returns `[]` with the default policy
    (`allow_fleet_noncombat=false`), without cargo-capable ships, or without surplus above
    `policy.reserves`; with all three satisfied it proposes a `launchFleetMission` Transport to
    whichever other own planet holds the least of the surplus resource, bounded by
    `calc.available_cargo` —
    `tests/test_candidates.py::test_generate_transport_candidates_empty_by_default_policy`,
    `::test_generate_transport_candidates_empty_without_cargo_ships`,
    `::test_generate_transport_candidates_empty_without_surplus`,
    `::test_generate_transport_candidates_moves_surplus_to_the_planet_that_needs_it_most`.
56. `candidates.generate_harvest_candidates` returns `[]` with the default policy, without a built
    Recycler, or without a caller-supplied `own_planet_debris` entry for the planet (absent debris
    data is never treated as "no debris," and never as "harvest anyway") —
    `tests/test_candidates.py::test_generate_harvest_candidates_empty_by_default_policy`,
    `::test_generate_harvest_candidates_empty_without_a_recycler`,
    `::test_generate_harvest_candidates_empty_without_known_debris`,
    `::test_generate_harvest_candidates_produces_a_local_harvest_action`.
57. With the default policy (`allow_fleet_noncombat=false`), a real `vd tick --dry-run` against the
    live API behaves identically to Phase 4: `plan.py`'s new band 5 never fires, and every
    pre-existing test in `test_guard.py`/`test_tick.py`/`test_candidates.py`/`test_plan.py` passes
    unmodified except the two documented, justified exceptions (the 17→18 gate-count assertion in
    `test_guard.py`, and the now-stale `allow_fleet_noncombat` "dead config" warning assertion in
    `test_tick.py` — both described in `veydrift-agent`'s `CHANGELOG.md`).

<!-- Game-pause detection (veydrift-agent 1.2.0). /health's `gameMaintenance` block, first
     observed live 2026-08-20 during a real maintenance pause. -->

58. A confirmed chain-side game pause (`/health`'s `gameMaintenance.paused == true`) never reaches
    the candidate pipeline: `plan.py`'s rung `1b` returns ESCALATE with `rule="1b:game-paused"`
    when `policy.escalation.on_game_paused` is true (the default), and NOOP with the same `rule`
    when it is false — the flag chooses escalate-vs-noop, never escalate-vs-proceed —
    `tests/test_plan.py::test_game_paused_escalates_when_flag_is_true`,
    `::test_game_paused_noops_when_flag_is_false`,
    `::test_game_paused_rung_does_not_fire_when_not_paused`.
59. `guard.py`'s `game_paused` gate re-checks the same fact independently of `plan.py` and
    **fails closed on absent data**: `snapshot.game_maintenance is None` is BLOCK ("a check that
    could not run, not one that passed"), never PASS — the flat `snapshot.game_paused` boolean is
    a convenience flag and is never read alone as confirmation the game is *not* paused —
    `tests/test_guard.py::test_game_paused_gate_blocks_when_game_maintenance_is_none`,
    `::test_game_paused_gate_blocks_when_paused`, `::test_game_paused_gate_passes_when_not_paused`.
60. `/health` is parsed by exactly one function (`read._game_maintenance`), shared by `read.py`'s
    full-snapshot path and `tick.py`'s minimal `_fetch_health_only` killswitch path, so the two can
    never drift apart (AGENTS.md §5). `readiness.degradationReasons` is carried through generically
    — it is a free-form list, not a pause-only flag, and is never assumed to contain only
    `"game_paused"` — `tests/test_read.py::test_snapshot_parses_game_maintenance_paused`,
    `::test_snapshot_parses_game_paused_false_and_none_maintenance_on_older_backend_shape`,
    `tests/test_tick.py::test_killswitch_health_paused_payload_still_reports_health_ok_and_game_paused`.
61. **Correction, 2026-08-21, superseding §5.4's Phase 3 "storage, proactively" note above** (the
    latter kept verbatim as history, not edited): a scored mine/energy winner, or a declared
    `building_priority` winner, whose cost exceeds the planet's *current* storage cap for a resource
    it needs is now replaced by the matching proactive-storage candidate (or, absent one, falls
    through to the next candidate) — `generate_proactive_storage_candidates` is no longer additive-
    to-`alternatives`-only; it can win Band 2. Before this fix, `guard.py`'s `_gate_affordability`
    would BLOCK such a pick forever ("never affordable: cost exceeds storage cap") while `plan.py`
    kept re-proposing it every tick, with the real fix demoted to an informational alternative —
    `tests/test_candidates.py::test_mine_winner_capped_by_storage_is_replaced_by_matching_storage_candidate`,
    `::test_mine_winner_capped_by_storage_falls_through_when_no_storage_substitute_available`,
    `::test_building_priority_winner_capped_by_storage_is_replaced_by_matching_storage_candidate`.
62. A confirmed combat-only `/health` degradation (`randomnessReadiness.ready == false`, with
    `readiness.ready == true`, no other `degradation_reasons`, and `gameMaintenance.paused ==
    false` all positively confirmed) does not reach a NOOP/BLOCK — `Snapshot.combat_only_
    degradation()` is fail-closed (any other combination, including `readiness_ready == false`,
    still blocks exactly as before `health_ok` itself became relevant) —
    `tests/test_plan.py::test_health_not_ok_falls_through_when_combat_only_degradation_is_confirmed`,
    `::test_health_not_ok_still_noops_when_readiness_itself_is_not_ready`,
    `::test_health_not_ok_still_noops_on_a_genuinely_different_degradation`,
    `tests/test_guard.py::test_health_passes_on_confirmed_combat_only_degradation`,
    `::test_health_still_blocks_when_readiness_itself_is_not_ready`,
    `::test_health_still_blocks_on_a_genuinely_different_degradation`.
63. `read._fetch_or_exit()` defensively recovers a `/health` HTTP 5xx whose captured error body
    parses as a real health-response shape, instead of hard-aborting — scoped narrowly to
    `/health`: every other route's 5xx still exits exactly as before, and an unparseable `/health`
    5xx body still exits too. `tick.py`'s killswitch-only `_fetch_health_only()` shares the same
    recovery (`read._recover_health_body`), functionally inert under `killswitch_active=True` but
    keeping the halted `Snapshot`'s audit record honest —
    `tests/test_read.py::test_fetch_or_exit_recovers_a_parseable_5xx_health_body`,
    `::test_fetch_or_exit_still_exits_2_on_an_unparseable_5xx_health_body`,
    `::test_fetch_or_exit_never_recovers_a_5xx_on_a_non_health_route`,
    `::test_snapshot_parses_randomness_readiness_and_readiness_ready_from_a_recovered_5xx`,
    `tests/test_tick.py::test_killswitch_recovers_a_5xx_health_body_and_reports_combat_only_degradation`.
64. **Correction, 2026-08-22, replacing criterion 31's "lowest-level-first" fallback description.**
    `research_priority`/`building_priority`'s undeclared fallback tail is ranked by
    `techtree.unlock_breadth` descending (fully-unlocked-count first, partially-advanced count as
    tiebreak, current level ascending, then id ascending only as the final tiebreak) instead of pure
    lowest-level-then-id. A new `techtree.unlock_breadth(family, entity_id, *, building_levels,
    technology_levels)` computes the ranking purely by re-calling the already-verified,
    already-tested `unmet()` against every known building/ship/defense/research id before and after
    a hypothetical +1 — a structural fact re-derived from data already used to check legality, never
    an invented value judgement, so this does not cross the "no ROI verdict" line drawn for economic
    scoring (§5.2, `candidates.py`'s own module docstring). Scoped narrowly: only the *ordering*
    computation inside `_infrastructure_priority_order`/`_research_priority_order`'s fallback
    branches changes — `select_building_candidate`/`select_research_candidate`'s first-unlocked-wins
    selection logic, `Candidate.score`, and `rank_candidates` are all untouched, and a declared
    priority list's own entries still take precedence exactly as before this change —
    `tests/test_techtree.py::test_unlock_breadth_robotics_factory_0_to_1_unlocks_research_lab_only`,
    `::test_unlock_breadth_robotics_factory_1_to_2_unlocks_shipyard`,
    `::test_unlock_breadth_entity_with_no_requirers_returns_zero`,
    `::test_unlock_breadth_counts_partial_when_a_conjunction_has_other_unmet_legs`,
    `::test_unlock_breadth_runs_over_the_full_real_graph_without_crashing`,
    `tests/test_candidates.py::test_research_fallback_order_prefers_unlock_breadth_over_level`,
    `::test_infrastructure_fallback_order_prefers_unlock_breadth_over_level`,
    `::test_infrastructure_fallback_order_reachable_without_any_declaration`.
65. **Correction, 2026-08-26.** `_mine_priority_order`'s exact-tie handling — previously
    a pure accident of Python dict-declaration order (`METAL_MINE` listed first,
    `docs/COVERAGE.md`'s "Mine selection ignores the payback score it computes" row) —
    now breaks an exact density tie by ascending `score_payback` hours (each mine's
    already-computed payback, the same number `Candidate.score` already carries for
    display) instead. New optional keyword-only `_mine_priority_order(planet, *,
    tie_break: Mapping[int, float] | None = None)`; `select_building_candidate` builds
    the map from data it already has (`mine_candidates`, built before the walk) and
    passes it; every other call site (including `generate_mine_candidates`'s own
    internal use, whose list order is never winner-load-bearing downstream) leaves
    `tie_break` at its default `None`, under which the secondary sort key is constant
    and the stable sort reproduces today's exact dict-order output — byte-identical. A
    mine missing from the map (locked, energy-unsafe, or a `score_payback`-returns-`None`
    edge case) sorts last, never preferentially winning an unknown value over a known
    one. This is the same move `generate_unlock_chain_candidates` already makes — its
    own docstring: weighted cost as "not an ROI comparison... just a tie-break among
    otherwise-incomparable proposals" — applied to a same-family, already-computed
    number, not an invented cross-family exchange rate, so it does not cross the "no ROI
    verdict" line either. Scoped narrowly: the primary `(level+1)/density` ranking is
    completely untouched, and criterion 23's byte-identical-to-pre-Phase-2 guarantee
    still holds for every existing fixture — checked directly (planet_664, planet_hot,
    `_ready_snapshot`, `_blocked_planet`): none of them reaches an exact density tie, so
    none of their pinned output changes.

    Two consequences accepted deliberately, not overlooked: **(a)** a tie between an
    energy-blocked mine and an energy-safe one now resolves to the safe mine directly
    (as a mine, not via the energy-first substitute) instead of the blocked mine
    forcing an energy-substitute proposal by dict-order luck — a genuine improvement,
    not just "a different mine wins," and pinned by its own test. **(b)** the winning
    mine's `Action.rationale` (generated inside `generate_mine_candidates`, before the
    tie-break is known) does not currently say a tie was broken by payback, even in the
    scenario that motivated this change — accepted rather than adding a second
    mechanism to thread that state through purely for UX polish.

    `tests/test_candidates.py::test_mine_priority_order_default_tie_break_is_dict_declaration_order`,
    `::test_mine_priority_order_tie_break_prefers_lower_payback`,
    `::test_mine_priority_order_tie_break_with_no_scores_falls_back_to_dict_order`,
    `::test_select_building_candidate_breaks_a_real_tie_by_computed_payback`,
    `::test_mine_tie_with_an_energy_blocked_twin_prefers_the_energy_safe_one_directly`,
    `::test_mine_tie_break_winner_still_defers_to_storage_precondition`.
66. **Correction, 2026-08-27.** `candidates._cheapest_energy_choice` — the comparison
    `select_building_candidate` uses to pick the energy-first *substitute* when a mine
    upgrade would be energy-unsafe — is now a three-way comparison (Solar Plant / Solar
    Satellite / Fusion Reactor) instead of two-way. Previously Fusion Reactor was
    deliberately excluded from this specific comparison (`docs/COVERAGE.md`'s "Fusion
    Reactor as an energy-first *substitute*" row, "partial (Phase 3)") even though it was
    already an ordinary scored `energy`-family candidate elsewhere — and that other path
    alone consistently undersold it: raising future energy capacity doesn't move current
    `production_per_hour` unless the planet is already throttled, and Fusion Reactor's own
    deuterium upkeep makes the delta strictly negative otherwise, so `score_payback`
    returns `None` for a build that can still be the objectively cheaper energy source.
    Reproduced live against `tests/fixtures/planet_hot.json`, the fixture this comparison
    is pinned against: Fusion Reactor 0→1 costs 43.64/energy point one-time versus Solar
    Satellite's 83.33 and Solar Plant 15→16's 211.88 — the pre-fix code already mis-picked
    Satellite over a ~2x-cheaper Fusion Reactor on its own canonical fixture, uncaught
    because no test asked whether Fusion Reactor existed.

    **Design decision, made explicitly:** unlike Solar Plant and Solar Satellite, Fusion
    Reactor carries an ongoing operating cost (deuterium upkeep,
    `calc.fusion_deuterium_upkeep`, recurring every hour it exists) on top of its
    one-time build cost.
    A raw one-time-cost comparison would favor it unfairly. Its cost is amortized over a
    new module-level constant, `_ENERGY_UPKEEP_AMORTIZATION_HOURS = 24`, before
    comparison: `(one_time_cost + upkeep_delta_per_hour * 24) / energy_gained`. This
    window is outcome-changing, not cosmetic: on `planet_hot.json` it still leaves Fusion
    Reactor the winner (51.64/point, versus Satellite's 83.33 and Solar Plant's 211.88),
    but a 7-day window would have flipped the winner back to Satellite (99.64/point) — a
    deliberately chosen, documented constant, not an invented cross-family exchange rate
    (`policy.strategy.resource_weights` plays no role here, matching this comparison's
    pre-existing flat 1:1:1 cost-sum scope, unchanged by this fix).

    Scoped narrowly: `generate_energy_candidates`'s own scoring of Fusion Reactor (the
    `energy`-family candidate path) is unchanged — it already correctly nets upkeep
    against production via `calc.production_per_hour` when a planet is throttled; this
    fix only touches the substitution comparison. `_generate_satellite_ship_candidate`
    (the shipyard-idle rung's separate Satellite path) needed no logic change — its
    existing "only propose a Satellite when `_cheapest_energy_choice` actually picked
    one" guard already declines correctly when Fusion Reactor wins instead. Both
    independent enforcement layers already anticipated Fusion Reactor as an energy-fixing
    build: `guard.py`'s `_ENERGY_FIX_BUILDINGS` already included it, and `allowlist.ts`
    tier-gates `startBuildingUpgrade` by function signature, not entity id — neither
    needed a change.

    One deliberate, accepted test-fixture consequence: because Fusion Reactor is a
    *building*, `policy.actions.allow_ships` (which only ever gated the Satellite
    fallback) has no bearing on whether it wins — so on the unmodified `planet_hot.json`
    fixture, the pre-fix pair of tests that demonstrated `allow_ships` gating Satellite
    versus Solar Plant now both resolve to Fusion Reactor regardless of that policy field,
    and no longer demonstrate that knob's effect at all. That coverage is preserved
    instead against a Fusion-locked variant of the same fixture (Energy Technology
    dropped below its unlock requirement — not a building level, since Fusion Reactor is
    at level 0 there and `calc.fusion_energy(0, ...)` is 0 regardless of technology
    level, so nothing else the energy-first check depends on is perturbed).

    `tests/test_plan.py::test_planet_hot_prefers_fusion_reactor_when_cheaper`,
    `::test_planet_hot_falls_back_to_solar_plant_when_ships_disallowed`,
    `::test_empty_strategy_targets_reproduce_phase_2_hot_planet_output_exactly`,
    `tests/test_candidates.py::test_cheapest_energy_choice_prefers_fusion_reactor_when_amortized_cost_is_lower`,
    `::test_cheapest_energy_choice_falls_back_to_two_way_when_fusion_reactor_is_locked`,
    `::test_select_building_candidate_names_fusion_reactor_in_the_rationale_when_it_wins`.

67. **Correction, 2026-08-28.** §1's claim above that Harvest "is not live-reachable yet
    because no live source for debris-field data is wired in" is now stale.
    `candidates.generate_harvest_candidates`'s `own_planet_debris` parameter is now
    supplied for real: `tick.py`'s new `_own_planet_debris()` reads
    `/universe/galaxies/{g}/systems/{s}` (`read.fetch_universe_system`, new this change) —
    the same route already fetched for `PlanetSnapshot.archetype` — and its `debrisField`
    per slot is confirmed live-populated (`{"metal": "2400", "crystal": "2400"}` at a real
    occupied slot, probed 2026-08-27), closing the "populated shape has never actually
    been seen" gap `generate_harvest_candidates`'s own docstring previously flagged.
    Deliberately **not** sourced from `/raid-finder/debris`: that route takes no wallet
    parameter, is independently confirmed to omit at least one indexed debris field (its
    own `indexer.indexedDebrisFields` outnumbers its `targets` array), and its filtering
    criteria are undocumented — using it would risk the exact vacuous-pass-on-absent-data
    failure mode AGENTS.md §5 warns against, if it turns out to exclude the caller's own
    planets. `_own_planet_debris` groups the wallet's owned planets by `(galaxy, system)`
    so a multi-planet wallet sharing a system fetches it once, and is best-effort
    end-to-end: a fetch failure for one system degrades that system's planets to "no
    debris this tick" without aborting the others or the tick itself, matching
    `_resolvable_mission_ids`'s existing contract exactly. This closes only the local-
    harvest half (`origin_planet_id == target`, `generate_harvest_candidates`'s only
    supported case) — foreign Harvest (a third party's debris field) remains unbuilt; see
    `docs/COVERAGE.md`'s Harvest row.

    `tests/test_tick.py::test_own_planet_debris_finds_a_populated_debris_field_on_an_owned_slot`,
    `::test_own_planet_debris_ignores_a_null_debris_field`,
    `::test_own_planet_debris_ignores_a_zero_debris_field`,
    `::test_own_planet_debris_skips_a_planet_with_no_coordinates`,
    `::test_own_planet_debris_degrades_to_empty_on_fetch_failure`,
    `::test_own_planet_debris_fetches_each_system_only_once`,
    `::test_run_tick_wires_own_planet_debris_into_the_planner`.

---

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Contract upgraded mid-build (UUPS) | `verify-abi` every tick; blocks writes on drift |
| API shape changes | Summaries degrade rather than crash; unknown fields ignored, missing required fields → explicit error |
| Formulas unverified by this codebase above level 0 | Cost scaling, queue behaviour and lazy settlement above level 0 are **unobserved by this system acting** — this codebase has never itself proposed, guarded or sent an action that resolved above level 0. Every planner path depending on level >0 is fixture-tested and marked unverified-against-live in `strategy-playbook.md` |
| `skills add` copy semantics bite | Criteria 13 and 17 test it directly |
| Keystore password handling | Never in argv, never logged, prompt by default; env var documented as the weaker option |
| Provider research goes stale | Dated, with the address-binding constraint as the durable filter |
| Overconfidence from advisory ticks | `vd readiness` reports guardrail fires and proposal/execution *divergence*, not a green count |

> **Correction, 2026-08-17.** This row previously read "Zero-state account" and asserted the
> account itself was at level 0 ("every level is 0, every queue is idle"). That is stale.
> Verified on-chain 2026-08-17 via `cast call buildingLevel(uint256,uint8)`/
> `technologyLevel(address,uint8)` against the deployed contract (planet 664, wallet
> `0x224aba5d489675a7bd3ce07786fada466b46fa0f`): Metal Mine 10, Crystal Mine 9, Deuterium
> Synthesizer 5, Solar Plant 11, Robotics Factory 2, Shipyard 1, Research Lab 1, Energy
> Technology 2, Computer 0 — the account has been played by hand through the game UI at tier
> 1, at the time this reading was taken. **Correction, 2026-08-25:** the distinct claim this
> paragraph made next — that *this codebase* had never submitted a transaction or observed
> its own proposals resolve above level 0 — no longer holds either; this codebase has since
> submitted real transactions through its own `veydrift-agent`/`veydrift-wallet` path at
> tier 2 (`economy`) and tier 3 (`operator`) — see `README.md`'s Status section and §11's
> first bullet below. `vd calc verify` does cross-check three duration formulas against live
> API data at the account's current (non-zero) level and passes, confirmed 2026-08-17,
> covering only those three formulas specifically, not cost scaling generally (see §11's
> bullet on this).

---

## 11. What this does not verify

- ~~No transaction has ever been submitted to Veydrift from this codebase~~ — **correction,
  2026-08-25**: no longer true. Real transactions have since been submitted to Veydrift
  from this codebase, at tier 2 (`economy`) and tier 3 (`operator`) — see `README.md`'s
  Status section for the current state. Promotion past tier 1 remains always a deliberate
  human decision, never automatic (§4).
- ~~EIP-7702 support on Base is inferred from a block header field~~ — **resolved 2026-08-12**:
  confirmed by a landed type-0x04 transaction (§6.1). Nothing in this codebase uses 7702; it is a
  future option, not a dependency.
- **Cost scaling above level 0 has never been observed by this codebase — queue behaviour
  and lazy settlement no longer belong on this list.** `vd calc verify` cross-checks three
  duration formulas (Energy Technology research, Small Cargo ship production, Metal Mine
  building) against live API data every run and passes, confirmed 2026-08-17; a local
  Anvil fork run additionally observed this codebase's own `build → simulate → send`
  path populate and lazily settle a real queue above level 0 (`startBuildingUpgrade`,
  Metal Mine 10 → 11 — `AGENTS.md` §10), and real transactions have since been submitted
  through this codebase to mainnet itself at tier 2/3 (**correction, 2026-08-25** —
  `README.md`'s Status section), so "no proposal this codebase generated has ever been
  submitted and watched resolve above level 0" no longer holds either. What still stands:
  no per-building cost-scaling *factor* has been observed or verified by this codebase at
  any level — the formula itself, not whether real actions have been taken.
- **`protectedResources` semantics remain unconfirmed**; no loot model is built on them.
- **No wallet provider beyond local key custody has been evaluated in depth** — that is WP4b's output,
  and it is a research document, not a recommendation to deploy.
- **`techtree.py`'s prerequisite table is transcribed from `VeydriftDependencies.sol`/
  `VeydriftCatalog.sol` at the pinned commit.** Every table entry was read from source,
  spot-checked in `tests/test_techtree.py` against the Solidity, and cross-checked for the
  two known transcription traps (the 9-arg vs. 5-arg `requireBuilding` overload;
  conjunction vs. disjunction in the source's `||` clauses). **Correction, 2026-08-25:**
  this bullet previously claimed no proposal this table declares "unlocked" was ever
  submitted through this codebase and observed to succeed or revert for the stated reason,
  reasoning that the account's non-trivial levels came only from hand-play outside this
  codebase. That distinction no longer holds — this codebase has since submitted real
  transactions through its own `veydrift-agent`/`veydrift-wallet` path at tier 2/3 (see
  `README.md`'s Status section); this document doesn't itself catalog which specific
  `techtree.py` entries those exercised. The shield-dome/missile-silo cap
  arithmetic carries the same caveat, plus a narrower one of its own: it is derived from a
  single `QueueEntry` per `PlanetSnapshot` (no backlog list — `models.py` is frozen), so a
  real queue backlog deeper than one entry would be undercounted, not overcounted.
- **Phase 3's newly-reachable families are legality-verified, never economically or
  live-verified.** `generate_ship_target_candidates`/`generate_defense_target_candidates`/
  `generate_infrastructure_candidates` are exercised only against synthetic fixtures
  (`tests/test_candidates.py`'s `_ready_snapshot`), never a live account with a Shipyard ≥ 5 or a
  Missile Silo ≥ 2 — the account this project was built against has neither (verified on-chain
  2026-08-17: Shipyard 1, Missile Silo 0; no longer the zero-state account of §10's original
  framing, but still short of both thresholds). The
  crawler boost formula (`calc.crawler_boost_bps`) and the 8-per-mine-level/5,000-bps caps are
  contract-derived and unit-tested, but no crawler has ever actually been produced and observed to
  move real `productionPerHour`.
