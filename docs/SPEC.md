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
| 2 | `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `settlePlanet`, `startDefenseProduction`, `startShipProduction` | ≥24 h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 | `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0) / Deploy(1) / Harvest(4) only | ≥7 days clean T2, human edit |

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
rationale. Ladder, first match wins:

```
0. KILLSWITCH present                -> HALT
1. /health not ok                    -> NO-OP, reason recorded
2. pending tx unreconciled           -> NO-OP, reconcile first
3. mission Resolving > 60s           -> resolveFleetMission   (permissionless, free)
4. incoming hostile fleet            -> ESCALATE, no proposal (fleet-visibility.incoming)
5. resource within N hours of cap    -> spend it, or build the matching storage
6. building queue empty              -> next build
7. research queue empty              -> next research
8. shipyard idle AND economy on track-> ships/defense per policy
9. otherwise                         -> NO-OP with an explicit reason
```

**Energy-first invariant.** Before any mine upgrade, compute `required` and `produced` explicitly at
the *post-upgrade* level; if it would drive `required > produced`, propose the energy building
instead. Never use a fixed solar-level offset — the gap widens from 2 levels at mine 3 to 4 at mine 10.

**Build order is derived, not hardcoded.** `strategy-playbook.md` documents the derivation; `plan.py`
implements it parametrically from planet traits (temperature, multipliers, `solarSatelliteEnergy`,
fields, levels). Planet 664's deuterium-lean, no-satellite opener must *fall out of* its traits.

### 5.5 `vd guard` — guardrail evaluation

Returns `ALLOW` / `BLOCK` / `ESCALATE` with a per-gate verdict list. **Every gate is evaluated and
reported**, never short-circuited — the full verdict list is the audit artifact.

| Gate | Rule |
| --- | --- |
| `killswitch` | `$VEYDRIFT_HOME/KILLSWITCH` absent |
| `tier` | action's function ∈ tier's allowed set |
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
  "wallet_engine": { "provider": "keystore", "require_confirmation": true }
}
```

`"planets": []` means discover via `/wallet/{addr}/planets`.

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
5. plan                           8. log: proposal always; action only if executed
                                  9. pretty report -> stdout + logs/ticks/
```

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
    guards: 15/16 substantive pass · tier: block (structural — advisor never submits)
    tx:     to 0xf397…755d  data 0x…  (NOT SUBMITTED — tier 1)
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

---

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Contract upgraded mid-build (UUPS) | `verify-abi` every tick; blocks writes on drift |
| API shape changes | Summaries degrade rather than crash; unknown fields ignored, missing required fields → explicit error |
| Zero-state account | Cost scaling, queue behaviour and lazy settlement are **unobserved**. Every planner path depending on level >0 is fixture-tested and marked unverified-against-live in `strategy-playbook.md` |
| `skills add` copy semantics bite | Criteria 13 and 17 test it directly |
| Keystore password handling | Never in argv, never logged, prompt by default; env var documented as the weaker option |
| Provider research goes stale | Dated, with the address-binding constraint as the durable filter |
| Overconfidence from advisory ticks | `vd readiness` reports guardrail fires and proposal/execution *divergence*, not a green count |

---

## 11. What this does not verify

- **No transaction has ever been submitted to Veydrift from this codebase.** The write path is
  constructed, allowlisted, simulated and fixture-tested — never executed against mainnet. The first
  real submission is a human decision at T1→T2.
- ~~EIP-7702 support on Base is inferred from a block header field~~ — **resolved 2026-08-12**:
  confirmed by a landed type-0x04 transaction (§6.1). Nothing in this codebase uses 7702; it is a
  future option, not a dependency.
- **Cost scaling, queue behaviour and lazy settlement are unobserved** — the account is at zero state.
- **`protectedResources` semantics remain unconfirmed**; no loot model is built on them.
- **No wallet provider beyond local key custody has been evaluated in depth** — that is WP4b's output,
  and it is a research document, not a recommendation to deploy.
