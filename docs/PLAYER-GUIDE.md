# Veydrift Agent — Player's 101

A start-to-finish walkthrough for someone who plays Veydrift and wants an agent to read
their planet's state and propose what to build next. No prior familiarity with this repo
assumed. If you already know what this project is and just want the product overview,
[`README.md`](../README.md) is shorter; if you're extending the code, read
[`AGENTS.md`](../AGENTS.md) and [`TECHNICAL-WALKTHROUGH.md`](TECHNICAL-WALKTHROUGH.md) instead.

**Read this in order — each section assumes the previous one is done.** Every command
below was run against this repository and its real output is shown; where output would be
account-specific (your wallet, your planet), that's called out.

## Table of contents

1. [What you're installing](#1-what-youre-installing)
2. [Prerequisites](#2-prerequisites)
3. [Terminology](#3-terminology)
4. [Install the skills](#4-install-the-skills)
5. [Bootstrap via an agent session](#5-bootstrap-via-an-agent-session)
6. [Create your policy](#6-create-your-policy)
7. [Set up your wallet](#7-set-up-your-wallet)
8. [Your first tick](#8-your-first-tick)
9. [Manual action override — `vd tick --action`](#9-manual-action-override-vd-tick---action)
10. [Reading what the agent tells you](#10-reading-what-the-agent-tells-you)
11. [Running on a schedule](#11-running-on-a-schedule)
12. [Reading the logs](#12-reading-the-logs)
13. [Evolving through the tiers](#13-evolving-through-the-tiers)
14. [Example prompt and looping for tier>=1 agent operators](#14-example-prompt-and-looping-for-tier1-agent-operators)
15. [Troubleshooting](#15-troubleshooting)
16. [Safety reminders, one more time](#16-safety-reminders-one-more-time)

---

## 1. What you're installing

Two things, working together:

- **`veydrift-agent`** reads your planet's state from Veydrift's API and tells you what to
  build next — a building upgrade, a research item, occasionally a ship or defense order.
  It never signs or sends anything. By itself it's a read-only advisor.
- **`veydrift-wallet`** is the only thing that ever touches your private key. It builds the
  actual transaction, checks it against an independent allowlist, and — **only when you
  explicitly type `--confirm`** — signs and submits it.

They start in **advisor mode**: the agent will tell you exactly what it would do and print
a ready-to-submit transaction, but nothing gets sent until you decide otherwise. That's not
a training-wheels mode you're meant to graduate out of quickly — running in advisor mode
for a real stretch of time, and reading what it proposed against what you'd have done
yourself, is the actual point. §13 below covers when and how to move past it.

## 2. Prerequisites

| Tool | Why | Check you have it |
| --- | --- | --- |
| [`uv`](https://docs.astral.sh/uv/) | Runs the Python skill; creates its own virtual environment automatically | `uv --version` |
| Node.js ≥ 22 | Runs the TypeScript wallet skill | `node --version` |
| `npx` (ships with Node) | Installs both skills | `npx --version` |
| Claude Code or a Hermes-compatible harness | Runs the skills | — |
| A Veydrift account with a settled planet | Nothing to read/manage without one | — |

Nothing else. You do **not** need `git`, a clone of the Veydrift contracts repo, or a Base
RPC key of your own — the wallet skill talks to `https://mainnet.base.org` by default and
the read API is `https://api.veydrift.com`, both public and unauthenticated for reads. (An
RPC key becomes *useful*, not required, once you're making a lot of calls — §7 covers it.)

## 3. Terminology

A handful of words carry specific meaning throughout this guide and in everything the
agent prints. Knowing them now means the rest of this guide — and the agent's own
output — reads as plain English instead of jargon.

| Term | Means |
| --- | --- |
| **Tick** | One complete, atomic run of the agent's pipeline: load your policy, check for a killswitch, reconcile any pending transaction, read your planet's live state, decide on zero or one next action, run every safety check against it, and — only at tier ≥2 with a real send — submit it. Nothing schedules a tick by itself; something else (you, typing a command; a loop; a scheduler) decides *when* to call one. §8 and §11 cover running them. |
| **Tier** | How much the agent is trusted to *submit*, not what it's allowed to *think about* — it always proposes the same way regardless of tier. Three tiers: `advisor` (propose only, never send — where you start), `economy` (can submit building/research/production actions), `operator` (also non-combat fleet missions). Advancing tiers is always a manual edit you make, never automatic — §13. |
| **Guard** | One of 19 independent safety checks the agent runs on every proposal before it's ever allowed to send — things like "can I actually afford this," "will this push energy negative," "does the destination address match the real contract." Every guard is evaluated and reported every tick, even after one has already said no, so a blocked tick is exactly as inspectable as an allowed one. §10 shows what this looks like in real output. |
| **Snapshot** | The live read of your planet — resources, queues, energy, incoming fleets — that a tick's decision is computed against. Always fetched fresh from Veydrift's own API at the start of the tick; a decision is never made against stale or cached numbers. |
| **Proposal vs. action** | A *proposal* is what the agent decided it would do. An *action* is a proposal that was actually submitted onchain. At tier 1, every tick produces only proposals — the distinction doesn't matter until tier 2, where it becomes the whole point of `logs/proposals.jsonl` vs. `logs/actions.jsonl` (§12). |
| **Policy** | The one file, `policy.json`, that holds every setting governing what the agent's allowed to do for your account — tier, wallet, planet(s), spending limits, which action types are enabled. §6 walks through every field. |
| **Alternatives** | The runner-up options the agent considered for a proposal but didn't pick, each with a one-line reason ("payback 47h vs 31h" for a worse economic option, or "locked: needs Shipyard 2 (have 0)" for one you can't build yet). Purely informational — it never overrides the actual proposal, and the agent never re-evaluates it as a decision. Shown in `vd tick`'s report and `proposals.jsonl` whenever there was more than one option to consider. |

### Entities have numbers, not just names

Veydrift's own API and contract identify buildings, ships, defenses, research, and fleet
missions by small integer IDs — not the names you see in the game's UI. The agent
translates these for you in its prose (`why: Metal Mine 0->1 would need...`), but the raw
`entity=` field in a tick's machine-readable output, and anything you read straight from
logs, will show the number. The building IDs are the ones you'll see most as a new player,
since building upgrades are almost always the agent's first proposal:

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

Ships, defenses, research, and fleet mission types have their own separate numbering — the
full canonical tables live in
[`skills/veydrift-agent/references/entity-ids.md`](../skills/veydrift-agent/references/entity-ids.md),
sourced directly from the deployed contract rather than from Veydrift's own docs (which
get two of these visibly wrong). Worth knowing if you've played OGame before: **Veydrift's
numbering does not follow OGame convention**, and it's easy to get two things wrong from
memory —

- **Defense order**: Small Shield Dome is id 3, sitting *before* Gauss Cannon (id 4) and
  Ion Cannon (id 5) — the reverse of OGame's usual order.
- **Ship 11 is officially "Deathstar,"** not "Dreadstar" — the contract's own enum spells
  it `Deathstar`. This codebase accepts "Dreadstar" as an alias when you type it, but
  anything it prints uses the contract's spelling.

You won't need to hand-type these often — the agent does the translation — but it's worth
knowing they exist so a raw number in a log line never looks like a mistake.

## 4. Install the skills

From this repository's root:

```bash
npx skills add . -g -a claude-code -a hermes-agent -y
```

Verified output (from this repo):

```
◇  Installed 2 skills
   ✓ veydrift-agent (copied)  → ~/.claude/skills/veydrift-agent
   ✓ veydrift-wallet (copied) → ~/.claude/skills/veydrift-wallet
```

That's the whole install. To update after this repo changes, re-run the same command —
it's a fresh copy each time, never a merge, so don't hand-edit anything under
`.claude/skills/`.

This install puts both skills side by side, which is what lets `veydrift-agent` find
`veydrift-wallet` automatically (it resolves it as a sibling directory) when it shells out
for tier ≥2 sends. If your harness installs skills into isolated per-skill roots instead,
set `VEYDRIFT_WALLET_DIR` to the wallet skill's install path as an escape hatch — most
installs never need this.

The repo also ships a `Makefile` — a **dev-repo convenience only**, for installing from
your own checkout, not part of what a real install does or needs:

```bash
$ make clean            # tidy skills/ — .venv, node_modules, etc.
$ make install-skills   # clean, then install globally, no prompts
```

### Environment variables at a glance

Every env var either skill reads, in one place — most of these won't matter until later
sections set them up; this is the reference to come back to:

| Variable | Skill | Purpose | Default |
| --- | --- | --- | --- |
| `VEYDRIFT_HOME` | both | Where policy, logs and cached state live — see §6 and §12. | `~/.veydrift` |
| `VEYDRIFT_RPC_URL` | wallet | Base RPC endpoint for every read/write. | `https://mainnet.base.org` |
| `WALLET_PROVIDER` | wallet | Which provider to sign with (`keystore`/`envkey`). | `keystore` |
| `VEYDRIFT_KEYSTORE` | wallet | Path to the encrypted keystore JSON (`keystore` provider). | — (required by that provider) |
| `VEYDRIFT_KEYSTORE_PASSWORD` | wallet | Keystore password. Leave unset for an interactive, non-echoing prompt instead — recommended, see §7. | unset (prompts) |
| `VEYDRIFT_PRIVATE_KEY` | wallet | Raw private key (`envkey` provider, testing only). | — (required by that provider) |
| `VEYDRIFT_TIER` | wallet | Fallback tier when no `policy.json` exists yet. Ignored (and flagged as a disagreement) once a policy file is present — `walletctl` always trusts the file over this. | `advisor` |
| `VEYDRIFT_WALLET_DIR` | agent | Escape hatch if the two skills aren't installed as siblings — see above. | sibling-directory auto-detect |
| `VEYDRIFT_SECRET_ENV_VARS` | agent | Comma-separated extra env var names to redact from logs, beyond the built-in `VEYDRIFT_PRIVATE_KEY`/`VEYDRIFT_KEYSTORE_PASSWORD`. Only needed if you've added your own secret-bearing env var into this system's environment. | unset (built-ins only) |

## 5. Bootstrap via an agent session

Everything in §6 and §7 below — writing `policy.json`, picking a wallet provider — can be
done by hand, or handed straight to your agent session: both skills ship a `SKILL.md` that
tells Claude Code or Hermes exactly when to trigger and what to do, so once installed (§4)
a plain-language request is enough.

Open a fresh session and say something like — keep the first three paragraphs as-is for
future sessions, and swap the last for whatever you actually want next (§11 covers this
ongoing use):

```
You're an agent that helps me play Veydrift, an on-chain space-strategy game on
Base, using the Veydrift skills installed in this environment. You read my
planet's state, propose what to build next, and explain your reasoning in
plain terms — but you never submit a transaction until I've explicitly raised
your tier past advisor.

Beyond the single next action a tick proposes, look at all four queues —
building, research, ship, defense — every time you check in, and treat an
idle one as something to investigate, not a routine state to report and move
past. Favor keeping every queue occupied, balanced against infra payback:
if the strongest economic pick is still a mine or infrastructure upgrade,
take it — don't fill a queue with a weak pick just to avoid idle time — but
if a queue is idle purely because nothing is declared for it to build, that's
waste worth closing.

When the planet's growth looks bottlenecked by policy rather than by what's
actually buildable — no ship_targets/defense_targets/research_priority
declared, or building_priority that hasn't kept pace with infrastructure
you've already unlocked (Shipyard, Research Lab, Nanite Factory, and so on)
— tell me directly and propose the specific policy.json edit, not just a
one-off action. I'd rather you flag a stagnating config than quietly work
around it tick after tick.

My wallet is <your address> and my settled planet is <your planet id or
coordinates>. Initialize my policy file for a cautious first run in advisor
mode, explaining each field you set. Then give me an initial overview: my
planet's profile and type, its strengths and limitations, current resources,
what's nearby in its neighborhood and the wider universe, and a suggested
strategy for my first 24 hours. Finally, run my first tick.
```

Coordinates (`7:181:14`, the format shown in-game) work as well as the raw id — the agent
resolves them via `vd read planets --wallet <address>`, which lists both.

This isn't magic: it's the agent running the same `vd tick init` and `vd tick --dry-run`
commands this guide documents by hand, then explaining what it finds. Read what it wrote
anyway — §6 is the same file, explained field by field, if you'd rather check its work or
do this yourself.

## 6. Create your policy

If you just used §5 to have an agent session do this for you, treat this section as the
reference for understanding exactly what it wrote — read it anyway, since it's the file
that governs everything downstream. If you're setting this up by hand instead, start here.

Everything the agent does — which planets it manages, how cautious it is, what tier it's
allowed to operate at — comes from one file: `$VEYDRIFT_HOME/policy.json`. It defaults to
`~/.veydrift/policy.json` and is created for you:

```bash
$ uv run --directory skills/veydrift-agent vd tick init
wrote /Users/santteegt/.veydrift/policy.json
```

(Adjust the path if you're not on macOS with the default `$HOME` — `$VEYDRIFT_HOME` always
wins if you've set it.)

Open the file it wrote. This is the full shape, with the values you'll actually want to
change called out:

```jsonc
{
  "version": 1,
  "tier": "advisor",                 // <-- start here. See §13 before ever changing this.
  "wallet": "0x224aba5d489675a7bd3ce07786fada466b46fa0f",   // <-- YOUR wallet address
  "planets": [664],                  // <-- YOUR planet id(s). [] auto-discovers all of them
  "chain_id": 8453,                  // Base mainnet. Leave this alone.
  "cadence": {
    "economy_minutes": 10, "research_minutes": 10,
    "fleet_minutes": 10, "universe_hours": 24
  },
  "limits": {
    "gas_per_tx_wei": "3000000000000000",     // 0.003 ETH ceiling per transaction
    "gas_per_day_wei": "20000000000000000",   // 0.02 ETH ceiling per day, cumulative
    "eth_gas_floor_wei": "2000000000000000",  // stop and escalate below 0.002 ETH balance
    "escalate_above_pct_of_resources": 25,    // a spend over 25% of holdings asks you first
    "max_index_wait_s": 300,
    "field_warn_pct": 80
  },
  "reserves": { "metal": 0, "crystal": 0, "deuterium": 0 },  // never spend below these
  "storage": { "hours_to_cap_trigger": 2 },  // "about to overflow" = within this many hours
  "actions": {
    "allow_building": true, "allow_research": true,
    "allow_defense": false,           // flip to true once you want defense proposals
    "allow_ships": false,             // flip to true once you want ship proposals
    "allow_fleet_noncombat": false,   // gates Transport/Harvest proposals -- operator tier, see §13
    "allow_combat": false             // ignored everywhere on purpose -- see §16
  },
  "escalation": {
    "on_incoming_fleet": true, "on_game_paused": true, "on_abi_hash_change": true,
    "on_health_unhealthy_minutes": 30, "on_revert_count": 2
  },
  "wallet_engine": { "provider": "keystore", "require_confirmation": true },
  "strategy": {
    "resource_weights": { "metal": 1, "crystal": 1, "deuterium": 1 },   // used to tie-break, not to pick a family -- see below
    "max_alternatives": 5,            // caps how many runner-up options each proposal lists
    "ship_targets": [{"name": "Small Cargo", "count": 1}],  // a target not yet buildable now drives its own build-up (see below)
    "defense_targets": [],            // same shape, e.g. [{"name": "Small Shield Dome", "count": 1}]
    "research_priority": [],          // ordered technology names, e.g. ["Energy Technology"]
    "building_priority": [],          // ordered infrastructure names (Robotics Factory etc.)
    "enable_crawler": false,          // opt-in for the scored Crawler family — see below
    "allow_agent_action_override": false  // vd tick --action opt-in -- see §9
  }
}
```

`policy.json` is validated strictly on every tick: an unrecognized key is a hard error, not
a silent ignore, and a missing required field stops the tick rather than guessing a
default. If you break the JSON, the next `vd tick` will tell you exactly what's wrong
rather than doing something unexpected.

<details>
<summary><strong>Useful tips</strong> — declaring targets and priorities, <code>enable_crawler</code>, and letting the agent build up to a locked target (click to expand)</summary>

**`ship_targets`/`defense_targets`/`research_priority`/`building_priority`.** All four
default to `[]`. Empty on all four means: energy still comes only from Solar
Plant/Solar Satellite/Fusion Reactor, defense still means only the Rocket Launcher,
research still walks its default order (see below — no longer purely
lowest-level-first), and nothing proposes
Robotics Factory/Nanite Factory/Shipyard/Research Lab/Terraformer/Missile Silo at all.
Declare a target to unlock the rest of the entity list:

- `ship_targets`/`defense_targets` are standing-count declarations: `{"name": "Crawler",
  "count": 20}` means "keep producing Crawlers, one at a time, until 20 are built or
  queued." Accepts either `"name"` (case-insensitive, e.g. `"crawler"`,
  `"Small Shield Dome"`) or a numeric `"id"` from `references/entity-ids.md`. Declaring
  `defense_targets` **replaces** the old hardcoded Rocket-Launcher-only default
  entirely — leave it empty if you're happy with that default.
- `research_priority`/`building_priority` are ordered name lists, but "ordered" means
  *preference*, not a queue that advances: the planner always proposes the first
  declared name that's currently buildable, and keeps proposing further levels of that
  *same* entry indefinitely — it only moves on to the next name if the first ever
  becomes locked (an unmet prerequisite), never because it decided the first one is
  "done." See the round-robin callout below before declaring more than one name
  expecting them to take turns. Names not declared fall back to a default order, ranked
  by how many other buildings/ships/defenses/research technologies a level-up would
  directly unlock (most first) — a real, structural fact re-derived from the same
  prerequisite tables `techtree.py` already uses to check legality, not an invented
  priority; level and id are only the tiebreak when that's equal. Infrastructure has no
  fallback candidates to rank at all unless you declare a priority — that family still
  only exists once you do.
- **A name that doesn't match anything is a hard error on the next tick** for
  `ship_targets`, `defense_targets`, and `research_priority` — the same "typo must never
  mean silence" posture the rest of `policy.json` already takes for an unrecognized key.
  If `vd tick` starts failing right after you edit one of these three lists, check the
  spelling first. **`building_priority` is the one exception to this rule** — see the
  callout in the field reference below before relying on the same guarantee there.

None of this is a fleet doctrine or a threat model. The planner still only enforces what
is legal (on-chain prerequisites, shield-dome/missile-silo caps) and, where a number is
genuinely comparable, what is economical (Crawler's production-boost payback) — *how
many* Crawlers or Small Shield Domes you actually want is your call, expressed here.

**`enable_crawler`.** Defaults `false`. This is a *separate* switch from
naming `"Crawler"` in `ship_targets` above: `ship_targets` is always an unscored "keep
building until N" declaration, no matter which entity you name. `enable_crawler` instead
turns on a second, scored path — the planner comparing Crawler's production-boost payback
against Solar Satellite and picking whichever is cheaper — that is *not* declared intent,
so it stays off unless you opt in. Leave it `false` and nothing changes; set it `true` if
you want the planner to consider Crawler on its own economic merits, not just when you've
named a standing count for it.

**Declaring a target you can't build yet, and the agent working out the build-up.**
You can name a target your account isn't ready for. The shipped example
policy above does exactly this: `ship_targets: [{"name": "Small Cargo", "count": 1}]`. On
a fresh planet, Small Cargo needs Shipyard level 2 and Combustion Drive level 2 — neither
of which exists yet. Before this addition, that entry would have sat there, legal to
want, doing nothing: the agent correctly refuses to propose a ship the contract would
reject, but nothing ever proposed what would unlock it. Now, once nothing more urgent or
more directly profitable is available to propose, the agent walks the requirement chain
backwards and proposes the nearest thing standing in the way instead — on a truly fresh
planet that's Robotics Factory (Shipyard's own prerequisite), then Shipyard itself once
Robotics Factory clears, and so on, tick by tick, until Small Cargo itself becomes
buildable and the ordinary `ship_targets` stock-keeping takes over.

Two things this deliberately is *NOT*: it is not a queued multi-step plan — every tick
re-derives the next step from your account's live levels, so if you build something by
hand in between ticks, the next proposal reflects that. And it is not scored against your
other options the way a mine or energy upgrade is (you'll see `score: null` and
`rule: "8b:unlock-chain"` on the proposal) — it only ever fires once nothing better is
available on the ordinary ladder, so it can't crowd out a genuinely profitable upgrade or
the storage-overflow safety check ahead of it. Read the proposal's `rationale` for which
target it's working toward and `expected_effect` for what's still left after this step.

</details>

**Two fields you must edit before anything downstream makes sense:**

- `wallet`: your actual player wallet address — the one that settled the planet(s) you
  want managed. `0x224a…fa0f` above is this repository's own reference example; it is not
  a wallet you should use.
- `planets`: your planet id(s), or `[]` to have the agent discover every planet the wallet
  holds automatically via `/wallet/{addr}/planets`. This field only ever holds numeric ids
  — if you only know your coordinates (the `galaxy:system:position` shown in the game's
  UI, e.g. `7:181:14`), not the id, editing this file by hand means finding the id first:
  `uv run --directory skills/veydrift-agent vd read planets --wallet <your address>` lists
  every planet you own with both, so you can match yours by coordinates.

Everything else in `limits`/`reserves`/`actions`/`escalation` is a reasonable starting
point. Don't loosen `limits` or flip on `allow_defense`/`allow_ships` until you've watched
the agent propose things under the defaults for a while — see §13.

<details>
<summary><strong>Full field reference</strong> — every <code>policy.json</code> field, its legal values, and its default (click to expand)</summary>

Every field `policy.json` accepts, with its real type and its real constraint — including
where there genuinely is none. This is the table that answers "what am I allowed to type
here," not the terse inline comments in the JSON example above. Where a field is marked
**unconstrained**, that's a confirmed fact about the schema (`models.py`), not a softened
guess — don't read it as "should be positive" or "should be sane." Only `version`,
`tier`, and `wallet_engine.provider` are genuinely closed enums at the schema level.

**Top level**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `version` | int (literal) | only the integer `1` | `1` |
| `tier` | string enum | exactly `"advisor"`, `"economy"`, `"operator"` (lowercase) | `"advisor"` |
| `wallet` | string | any string — **no address format or checksum validation at the schema level.** A malformed address is not caught until something downstream tries to use it. | required, no default |
| `planets` | list of int | any list of planet ids; `[]` is a special case meaning "auto-discover every planet this wallet owns," **not** "no planets" | `[]` |
| `chain_id` | int | **completely unconstrained by the schema** — any integer validates in `policy.json` itself. It's the separate `veydrift-wallet` skill's own allowlist that actually requires `8453` and refuses every write otherwise. Leave it at `8453`: there's no upside to changing it, only a wallet skill that then refuses to work. | `8453` |

**`cadence`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `economy_minutes` | int | unconstrained — no minimum; `0` or negative is schema-legal, though would presumably misbehave wherever it's consumed as an interval | `10` |
| `research_minutes` | int | same as above | `10` |
| `fleet_minutes` | int | same as above | `10` |
| `universe_hours` | int | unconstrained | `24` |

**`limits`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `gas_per_tx_wei` | int, as a JSON string | **required, no default** — `policy.json` fails to load without it. Unconstrained otherwise: no minimum, and nothing checks it against `gas_per_day_wei`. | none |
| `gas_per_day_wei` | int, as a JSON string | required, no default; unconstrained | none |
| `eth_gas_floor_wei` | int, as a JSON string | required, no default; unconstrained | none |
| `escalate_above_pct_of_resources` | int | unconstrained — **not clamped to 0–100 despite the name** (see footgun below) | `25` |
| `max_index_wait_s` | int | unconstrained | `300` |
| `field_warn_pct` | int | unconstrained — **not clamped to 0–100 despite the name** (see footgun below) | `80` |

> **Footgun — the "percent" fields aren't bounded.** `escalate_above_pct_of_resources` and
> `field_warn_pct` read like they should be schema-limited to 0–100, the way `tier` or
> `wallet_engine.provider` genuinely are closed enums. They are not. `150` or `-10` both
> pass `policy.json` validation without complaint; whatever downstream logic treats the
> value as a percentage does whatever it does with an out-of-range input. The schema
> provides no safety net here — get the number right yourself.

**`reserves`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `metal` | int | unconstrained — a negative reserve is schema-legal and meaningless | `0` |
| `crystal` | int | same | `0` |
| `deuterium` | int | same | `0` |

**`storage`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `hours_to_cap_trigger` | float | unconstrained | `2.0` |

**`actions`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `allow_building` | bool | `true`/`false` | `true` |
| `allow_research` | bool | `true`/`false` | `true` |
| `allow_defense` | bool | `true`/`false` | `false` |
| `allow_ships` | bool | `true`/`false` | `false` |
| `allow_fleet_noncombat` | bool | `true`/`false` | `false` |
| `allow_combat` | bool | `true`/`false` — legal to set, but **read and then unconditionally ignored by every code path.** Enabling `Attack`/`AcsAttack`/`MissileAttack`/`Intercept` requires an actual source change, not a config edit — see §16. | `false` |

**`escalation`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `on_incoming_fleet` | bool | `true`/`false` | `true` |
| `on_game_paused` | bool | `true`/`false` | `true` |
| `on_abi_hash_change` | bool | `true`/`false` | `true` |
| `on_health_unhealthy_minutes` | int | unconstrained | `30` |
| `on_revert_count` | int | unconstrained | `2` |

**`wallet_engine`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `provider` | string enum | exactly `"keystore"` or `"envkey"` — no other value is legal | `"keystore"` |
| `require_confirmation` | bool | `true`/`false` | `true` |

**`strategy`**

| Field | Type | Legal values / range | Default |
| --- | --- | --- | --- |
| `resource_weights.metal` | **int** | unconstrained, but **must be a whole number.** This reuses the same `Resources` model as `reserves`, so `1.5` is not legal even though you might reasonably want to weight deuterium higher than a plain integer allows. | `1` |
| `resource_weights.crystal` | int | same | `1` |
| `resource_weights.deuterium` | int | same | `1` |
| `max_alternatives` | int | unconstrained — `0` legally means "log no alternatives"; there's no upper cap either | `5` |
| `ship_targets` | list of `{name, id, count}` | `name` resolved case-insensitively against `references/entity-ids.md`'s Ship table (or use a numeric `id` instead); an unrecognized `name` is a hard error on the next tick. `count` defaults `0` and **is not constrained to be non-negative** (see footgun below). **Empty: this rule is off** — no standing ship target is proposed at all (Solar Satellite's separate energy-driven path is unaffected either way). | `[]` |
| `defense_targets` | list of `{name, id, count}` | same shape and same rules, against the Defense table. **Empty: falls back to the old hardcoded default** — a single Rocket Launcher, unconditionally — not off, just undeclared. | `[]` |
| `research_priority` | list of string | ordered Technology names; an unrecognized name is a hard error on the next tick. **Empty: falls back to an unlock-breadth-ranked default order** (most-directly-unlocking technology first; level then id only as the tiebreak — see callout below) across all technologies — research proposals still happen, just unprioritized by name. **Does not round-robin — same callout.** | `[]` |
| `building_priority` | list of string | ordered Building names — **asymmetric with the three fields above; see callout below**. **Empty: the infrastructure family never fires** — rung 6 falls through to its ordinary value-density mine/energy walk, which payback scoring does not drive except to break an exact tie between two mines. **Does not round-robin either — same callout.** | `[]` |
| `enable_crawler` | bool | `true`/`false` | `false` |
| `allow_agent_action_override` | bool | `true`/`false` — gates `vd tick --action <file>`. See §9. | `false` |

> **`resource_weights` is used to tie-break, not to pick a family — it only ever changes
> the winning proposal in three narrow places, and only ever changes a *displayed* number
> everywhere else.** Concretely:
>
> - **An exact tie between two mines.** Mine selection is normally decided purely by
>   value density (`(level+1) / (base_rate × multiplier)`) — `resource_weights` plays no
>   part in that walk at all. Only when two mines score *identically* on that primary
>   ranking does the planner fall back to each mine's weighted payback-hours score to
>   pick between them. This is a real, recurring case, not a hypothetical edge case — it
>   recurs any time the two mines' levels sit at the corresponding ratio (e.g. Metal
>   level 14 and Crystal level 9 at a 1x multiplier tie exactly), not just one specific
>   pair of levels.
> - **Multiple locked declared targets at once.** If more than one `ship_targets`/
>   `defense_targets`/`research_priority` entry is simultaneously locked, the cheapest
>   (weighted) unlock step across all of them wins the unlock-chain rung — again a
>   tie-break among otherwise-incomparable candidates, not a ranking against mines or
>   research.
> - **Crawler vs. Solar Satellite, only once `enable_crawler` is on.** With Crawler
>   enabled, its weighted payback score competes directly against Solar Satellite's for
>   the shipyard slot — the closest this field comes to a real ranking rather than a
>   pure tie-break, and it only applies when you've opted in.
>
> **Everywhere else, changing `resource_weights` changes a number in `alternatives`, not
> what gets proposed.** Research selection, `building_priority`'s infrastructure walk,
> defense selection, Fusion Reactor's displayed payback score, and a mine that *isn't*
> tied with another mine are all completely unaffected by this field — the weighted
> score still gets computed and shown for context, it just never wins anything. If you
> set `deuterium: 3` expecting the planner to start favoring deuterium-producing picks
> broadly, it won't — check the three bullets above for the only places it actually bites.

> **Footgun — a negative `count` is a silent no-op, not an error.** `ship_targets`/
> `defense_targets` entries are compared as `entity.count >= target.count`. For any
> negative `target.count`, that comparison is trivially true, so the whole entry becomes
> inert: the planner treats the target as already met and never proposes anything toward
> it. Nothing tells you this happened — it just quietly does nothing, forever.

> **`building_priority`'s hard-error rule is not the same as the other three list
> fields.** A genuinely misspelled name still hard-errors on `building_priority`, same as
> `ship_targets`/`defense_targets`/`research_priority` — the name-lookup step itself
> fails. But building-name resolution isn't restricted to the infrastructure family at
> that lookup step, so a **correctly spelled** building name that isn't one of the six
> infrastructure buildings (Robotics Factory, Nanite Factory, Shipyard, Research Lab,
> Terraformer, Missile Silo) resolves fine and then silently produces no candidate — no
> error, no warning, nothing in the logs. Add `"Metal Mine"` to `building_priority`
> expecting it to do something, and it will be quietly ignored forever with zero
> feedback. Stick to the six infrastructure names in this field.

> **The undeclared fallback order is ranked by what it unlocks, not by level.**
> For every name you *don't* declare, the planner ranks candidates by
> `techtree.unlock_breadth` — how many other buildings/ships/defenses/research
> technologies would have one of their own requirements satisfied by this specific
> level-up, computed by re-checking the same prerequisite tables `techtree.py` already
> uses to decide legality. A building/technology that unlocks something outright is
> preferred over one that doesn't, regardless of which one is numerically cheaper or
> lower-level; level and id only break a genuine tie. This is a structural fact, not an
> economic judgement — it never compares against a mine's resources/hour payback, and it
> only decides ordering *within* the research/infrastructure families, never whether they
> outrank a mine.

> **A multi-name `research_priority`/`building_priority` does not round-robin.** Both
> fields always propose the first declared name that's currently unlocked, and keep
> re-proposing further levels of that *same* entry every tick, forever — there's no
> "build it once, then move to the next name" logic, because neither field has a target
> level or count to complete against (unlike `ship_targets`/`defense_targets`'s `count`).
> Declaring `research_priority: ["Energy Technology", "Espionage Technology"]` permanently
> locks research onto Energy Technology, never touching Espionage Technology, until you
> edit the list yourself — leveling a technology or building doesn't un-satisfy its own
> prerequisites, so it never naturally cedes the slot to the next name. Treat these
> fields as "my #1 priority, with the rest as fallback names for if #1 ever becomes
> locked," not as a build order the planner works through.

</details>

## 7. Set up your wallet

You have two choices for `wallet_engine.provider`. **Use `keystore` unless you have a
specific reason not to** — it's the default, and it's the one the rest of this guide
assumes.

### `keystore` (recommended)

An encrypted, password-protected JSON file holding your key — the standard geth/EIP-2335
format, the same shape MetaMask and most wallet tooling can export or import. Nobody who
gets the file alone can use it; they'd also need the password, which is never stored
anywhere in this repo or its config.

**If you already have a keystore file** (exported from another wallet), skip to
"Point the skill at it" below.

**If you need to create one**, run this from `skills/veydrift-wallet/`:

```bash
$ npm run wallet:new

New address: 0x7Cd117B9a5e8E5e9E11a5Db0C1e489dF899eda9A
? Output directory: ~/.veydrift
? Keystore password: ********
? Confirm password: ********
Encrypting (this takes a few seconds)...
Wrote keystore to /Users/you/.veydrift/keystore.json
```

It prompts for the output directory (defaults to `~/.veydrift`, so it lands exactly where
`export VEYDRIFT_KEYSTORE=...` below expects it, if you take the default) and the
password — masked, typed twice to catch a typo, and rejected if empty. The password never
appears in your shell history, a script file, or a process list: unlike a one-liner that
takes the password as a literal, this is an interactive prompt (`scripts/gen-keystore.mjs`,
`@inquirer/prompts`) for exactly that reason.

**Read this before running it for real:** it generates a **brand new** address with no
history. It cannot access an existing planet — a Veydrift planet is permanently bound to
the address that settled it (no transfer function exists on the contract at all; see
[`README.md`](../README.md)'s key-custody section). So:

- If you're setting up a **new** account: generate a fresh keystore this way, fund it with
  a small amount of ETH on Base for gas, and use its address to settle your first planet.
- If you already have an **existing** planet: you need the private key that already
  settled it, not a freshly generated one. `wallet:new` only ever generates a fresh
  random key, so it can't help here — import that specific key into a keystore instead,
  via most wallet software (MetaMask, `cast wallet`, a hardware wallet's export flow).

**Point the skill at it**, wherever the file lives:

```bash
export VEYDRIFT_KEYSTORE=/path/to/your/keystore.json
```

Leave `VEYDRIFT_KEYSTORE_PASSWORD` **unset**. The skill will prompt you interactively,
without echoing what you type, every time it needs to sign. That's deliberate — a value in
an env var is one `printenv` or one compromised process away from being read; a prompt
requires a human at the keyboard for every single send. If you want the convenience of not
typing it every time (for example, unattended tier-2+ operation), you can set
`VEYDRIFT_KEYSTORE_PASSWORD` instead, but understand what you're trading away: see §16.

Verify it's wired up correctly:

```bash
$ npx tsx src/cli.ts status
provider:        keystore
address:         0x7Cd117B9a5e8E5e9E11a5Db0C1e489dF899eda9A
rpcUrl:          https://mainnet.base.org
chainId:         8453 (Base)
balance:         0 ETH
pinned ABI hash: sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99
pinned commit:   701bed3578cff4d134657c714c599dbdb55a4b6a
live ABI hash:   sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99
ABI pin match:   MATCH
game contract:   0xf397910F005151b09644228573a4353818D3755d
capabilities:    canSign=true canSimulate=false remotePolicy=false
```

(This exact transcript was captured against a real, throwaway test keystore created with
the recipe above — the address is that test wallet's, not a real account's. Yours will
show your own address and a real ETH balance.) The important lines: `ABI pin match: MATCH`
confirms the wallet skill's pinned contract ABI still matches what's actually deployed —
check this before your first real send, since a mismatch there blocks every write.
`balance: 0 ETH` is fine for now; you'll need a small amount of ETH on Base once you reach
tier 2, for gas.

### `envkey` (testing only — read this before using it)

A raw private key in an environment variable, `VEYDRIFT_PRIVATE_KEY`. It works, and the
skill prints a loud warning every time it's used, on purpose. This is ranked as
testing-grade storage, not something to run a real account through — an env var is
readable by anything with process access, with none of the password-prompt friction the
keystore provider has. Use it for local testing with a throwaway key, not for anything
holding a real planet.

### RPC endpoint (applies to either provider)

Every read and every write the wallet skill makes goes through one RPC endpoint,
configurable independently of which provider you picked above:

```bash
export VEYDRIFT_RPC_URL=https://base-mainnet.g.alchemy.com/v2/your-api-key
```

Left unset, it defaults to `https://mainnet.base.org` — Base's public endpoint, which
works but is shared and rate-limited. If you're running frequent ticks or doing anything
call-heavy (fork testing, repeated `status`/`build`/`simulate` calls), pointing this at a
dedicated endpoint like Alchemy avoids getting throttled. `walletctl status`'s `rpcUrl:`
line always shows whichever endpoint is actually configured, so you can confirm the
override took effect before relying on it.

§4 has the full list of environment variables both skills read, including the ones on
this page.

<details>
<summary><strong>Running multiple gameplay sessions on one machine</strong> (click to expand)</summary>

Playing more than one wallet from the same laptop — different accounts, different
sessions, running concurrently — is safe, but needs two separate settings per session,
not one:

- **`VEYDRIFT_HOME`** — a distinct directory per session (`~/.veydrift-alice`,
  `~/.veydrift-bob`, ...). This is what isolates policy, logs, cache, and the tick
  lockfile between sessions.
- **`VEYDRIFT_KEYSTORE`** (and `VEYDRIFT_KEYSTORE_PASSWORD`, if you're not using the
  interactive prompt) — a distinct keystore file per session. This is **not** derived
  from `VEYDRIFT_HOME` in any way; it's an independent path you set explicitly. It
  doesn't need to live inside `VEYDRIFT_HOME` either — anywhere on disk works, including
  right alongside it (`~/.veydrift-alice/keystore.json`) if you'd rather keep each
  wallet's files together.

In practice this isn't extra bookkeeping: different wallets already mean different
keystore files (a keystore encodes exactly one key), so you'd be setting
`VEYDRIFT_KEYSTORE` per session regardless. The thing to actually watch is remembering to
set **both** vars for each session — setting `VEYDRIFT_HOME` alone does not scope or
relocate the keystore.

Two caveats, neither a correctness risk: the first tick after a fresh install may trigger
`veydrift-wallet`'s one-time `npm install` self-heal (§4) — if two sessions' very first
ticks land in the same instant against a not-yet-installed shared skill copy, both could
kick off that install at once; harmless once `node_modules` exists, which is almost
always immediately. And both sessions share whatever RPC/API rate limits the default
endpoints impose unless you point each at its own via `VEYDRIFT_RPC_URL` above — a
throughput consideration, not a data-collision one.

</details>

## 8. Your first tick

With `policy.json` written and the wallet skill verified, run one tick:

```bash
$ uv run --directory skills/veydrift-agent vd tick --dry-run
```

Recall from §3: a tick is one atomic pass through the whole pipeline — load policy, check
the killswitch, reconcile any pending transaction, snapshot your planet, decide on an
action, run every guard, and (tier ≥2 only) send. `vd tick` is the single command that
does all of that; nothing about it schedules repetition — §11 covers running many of them.

At tier 1 (`advisor`, the default), `--dry-run` is **always on** — there's no flag to turn
it off at this tier. Real output, captured against this repo's reference planet:

```
╭───────────────────────────────── vd tick #1 ─────────────────────────────────╮
│ [2026-08-12T14:25:12Z] TICK #1  tier=advisor  planet 664 (7:181:14)          │
│   state:    M 1,000  C 1,000  D 0   | energy 0/0 (scale 10000) | fields      │
│ 0/174                                                                        │
│   queues:   building idle · research idle · ship idle · defense idle         │
│   incoming: none                                                             │
│   PROPOSE   startBuildingUpgrade(planet=664, entity=3)                       │
│     cost:   M 75  C 30  D 0                                                  │
│     why:    Metal Mine 0->1 would need 11 energy against 0 produced.         │
│ Energy-first invariant: Solar Plant's marginal cost per energy point is      │
│ cheaper here than one more Solar Satellite (satellite energy/unit=4).        │
│     alts:   1 considered and not selected --                                │
│              [energy] Solar Satellite (unscored) -- locked: needs Shipyard   │
│ 1 (have 0)                                                                   │
│     guards: 16/19 pass (block)                                               │
│     tx:     to 0xf397910F005151b09644228573a4353818D3755d  data              │
│ 0x165715e3... (NOT SUBMITTED -- tier advisor)                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Your own first tick will look different (your planet's actual state and traits), but the
shape is the same: current resources and queues, whether anything hostile is inbound, one
proposed action with its cost and reasoning, and confirmation that nothing was submitted.

### Options worth knowing

`vd tick` takes a handful of flags, and there are a couple of sibling commands worth
knowing before you move on to §11's scheduling options:

| Command | What it does |
| --- | --- |
| `vd tick` | Runs exactly one tick and prints the report shown above. |
| `vd tick --dry-run` | Never sends, no matter what tier allows — useful for checking what the agent *would* do without any possibility of a real submission. Always on automatically at tier 1. |
| `vd tick --format json` | Same tick, machine-readable output instead of the pretty panel — useful for piping into a script or another log. `md` (the pretty panel) is the default. |
| `vd tick --policy PATH` | Run against a specific policy file instead of `$VEYDRIFT_HOME/policy.json` — handy for testing a config change, or managing more than one account/planet from the same machine. |
| `vd tick --readiness` | Doesn't run a tick at all — prints your tier-promotion evidence instead (tick count, uptime, proposals vs. what you actually executed, guardrail fires). §13 covers reading this before promoting. |
| `vd tick init` | Writes a fresh `policy.json` from the template — the command §6 uses to bootstrap. Safe to re-run; won't overwrite an existing file without `--force`. |
| `vd doctor` | Reports which subcommands are actually wired up in the copy you're running — useful if you ever pull a checkout mid-update. |
| `vd log --digest 24h` | A rollup of the last day: what got built, resources produced, gas spent, and everything the agent refused to do and why. §12 covers this in full. |

## 9. Manual action override — `vd tick --action`

Most of the time, let `plan_next_action` choose. `vd tick --action <file>` exists for the
narrower case where the agent's own reasoning about the best next move genuinely
diverges from the planner's, and that divergence is blocking real strategy progress — a
situational move the planner has no rung for at all, not a general substitute for its
judgement. Full detail, the `Action` JSON shape, and what it does and doesn't skip:
`skills/veydrift-agent/references/manual-action-override.md`.

The short version:

- Gated by `strategy.allow_agent_action_override` (default `false`) — refused outright
  without it.
- Only substitutes **which** `Action` is evaluated. Every gate in `guard.py`, the tier
  ceiling, `wallet_engine.require_confirmation`, the tick lockfile, and the full audit
  trail all still apply exactly as they do to a planner-chosen action — this is not a way
  to bypass any of them.
- The disagreement with the planner is captured automatically: `vd tick` also computes
  what the planner would have proposed, purely for comparison, and reports both choices
  together in `logs/strategy.md`, the tick's own printed output, and `proposals.jsonl`'s
  `"override"` key — you don't have to write that comparison down yourself.
- This is **not** the same thing as calling `walletctl` directly and skipping `vd tick`
  entirely — that bypasses every one of the guarantees above and leaves no audit trail.
  If you ever see that pattern suggested, prefer `--action` instead.

## 10. Reading what the agent tells you

A few things worth understanding about that block before you trust it:

- **`guards: 16/19 pass (block)`** at tier 1 is expected, not a problem. The `tier` gate
  itself blocks — every onchain proposal is blocked at tier 1 by design, since advisor
  mode may never submit. That's what makes tier 1 safe *by construction*, not by
  discipline: the decision genuinely is `BLOCK`, so nothing past that point ever runs.
  (One of the 19 is `mission_type`, which only ever has anything to check on a fleet-mission
  proposal — see §13's note on operator tier below. It passes trivially for everything else,
  so a routine building or research tick simply shows it among the passes.)
- **`why:`** states the actual numbers behind the decision, not a canned explanation. If
  it says a mine upgrade needs 11 energy against 0 produced, that's a live comparison
  against your planet's current state, computed fresh every tick — not a rule of thumb.
- **The build order is derived from your planet's actual traits** — temperature,
  resource multipliers, current levels — not a generic build order copy-pasted for every
  planet. A cold planet gets pushed toward Deuterium Synthesizer earlier; a hot planet
  gets offered a different energy source (Fusion Reactor or a Solar Satellite) where a
  cold one never would. If the reasoning doesn't match your planet's traits, something's
  wrong — say so, don't just follow it.
- **The transaction shown is real and complete**, not a mockup — `tx: to ... data ...` is
  exactly what would be submitted if you were at a tier that could submit it. That's
  deliberate: it's what makes a later promotion decision evidence-based rather than a
  guess (§13).

## 11. Running on a schedule

One tick by hand is enough to see how it works; the useful mode is many ticks over time so
you (and later, §13's promotion evidence) have something to look at. Every option below
runs the exact same `vd tick` command with the exact same safety posture — the only thing
that differs between them is *who decides when to call it*. `vd tick`'s own lockfile makes
two overlapping calls a benign skip rather than a race, so running it from more than one
place at once on the same machine is safe, if occasionally redundant.

| Harness | How | Notes |
| --- | --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` | A human is present — each tick's report lands directly in your chat as it happens. |
| Claude Code, unattended | Schedule `claude -p "run a veydrift tick"` via `launchd` or a scheduled task | The agent invokes `vd tick` itself with nobody watching in real time — `strategy.md` and `proposals.jsonl` (§12) become what you review afterward, not the immediate output. |
| Hermes | Register `vd tick` on Hermes' own scheduler at `policy.cadence.economy_minutes` (10 minutes by default) | Hermes owns the interval; `vd tick` behaves identically regardless of who's calling it. |
| Bare OS, no agent harness | `skills/veydrift-agent/assets/com.veydrift.agent.plist.template` — a launchd template with a documented install/uninstall recipe in its own header comment | Not installed automatically — you fill in its four placeholders and run `launchctl load` yourself. A reasonable interval is `cadence.economy_minutes * 60` seconds, but nothing keeps the plist and `policy.json` in sync automatically; that's on you if you change one. |

### Or just ask, one tick at a time

You don't need a schedule to use the agent through a session — §5 already showed this for
setup, and the same thing works for ongoing use. In any Claude Code or Hermes session with
both skills installed, plain language like *"run a Veydrift tick"*, *"check my queues,"* or
*"is my planet under attack"* is enough to trigger `veydrift-agent` on its own — it's
listed directly in the skill's own trigger phrases. This is the same `vd tick` underneath,
just invoked conversationally instead of from a schedule; useful for a one-off check
without setting up any of the harnesses above.

Whichever you pick, `$VEYDRIFT_HOME` is shared across every invocation on the same
machine — if you're testing something and don't want it mixed into your real history, set
`VEYDRIFT_HOME` to a scratch directory for that session.

## 12. Reading the logs

Everything accumulates under `$VEYDRIFT_HOME/logs/`, never inside the skill's own
directory (which gets wiped on every reinstall):

| File | What's in it |
| --- | --- |
| `proposals.jsonl` | Every single proposal, ever — the full guard verdict list and the exact calldata, whether or not it was ever submitted. One line per tick that proposed something. |
| `actions.jsonl` | **Only what was actually executed** — tx hash, gas spent, block, before/after state. Empty at tier 1, always. |
| `ticks/<timestamp>.md` | The pretty report block, one file per tick, in case you want to look back at exactly what a specific tick said. |
| `strategy.md` | Rationale, plan revisions, and every escalation, in human-readable prose — the file to actually *read*, not just grep. |

```bash
$ uv run --directory skills/veydrift-agent vd log --digest 24h
```

prints a daily rollup: what was built/researched, resources produced, gas spent, and —
worth reading first, not last — **everything the agent refused to do, and why.** A quiet
day where nothing fired is not the same as a day where fires were checked and correctly
resolved; the digest tells them apart.

## 13. Evolving through the tiers

| Tier | What it can propose | What it can actually submit |
| --- | --- | --- |
| 1 `advisor` (you start here) | Everything in scope | Nothing, ever |
| 2 `economy` | Everything in scope | Building upgrades, research, defense/ship production, permissionless mission resolution |
| 3 `operator` | Everything in scope | Everything tier 2 can, plus non-combat fleet missions (Transport/Deploy/Colonize/Harvest) |

Nothing in this codebase ever advances the tier on its own. It is **always** a manual edit
of `policy.json`'s `tier` field, by you, and it should be treated as a real decision, not a
config toggle.

**What operator actually does, not just allows.** With `policy.actions.allow_fleet_noncombat`
set to `true` (it defaults to `false` — promoting to operator alone does **not** turn this
on), the agent can propose two kinds of fleet mission on its own:

- **Transport** — moving a surplus resource from a planet that has more of it than your
  configured `reserves` floor to whichever of your other planets currently holds the
  least, using ships you've already built. It never proposes building a ship to make this
  possible.
- **Harvest** — recovering debris sitting on one of your own planets, using a Recycler
  you've already built. This one is implemented but not live yet in practice: the agent
  has no reliable way to learn about debris on a planet from the live API today, so this
  path exists and is tested but won't actually fire until that's wired up.

**Colonisation** (`launchFleetMission` mission type `Colonize`) is allowed at both
enforcement layers — the wallet-engine allowlist and the agent's own
`mission_type` guard both accept it at operator tier — but the agent does not yet propose
*where* to colonise on its own. If you want to colonise, you'd build and send that
transaction through `walletctl` directly rather than waiting for a tick to suggest it;
picking a target is a judgement call this codebase leaves to you for now.

Both of these only ever fire at `operator` tier, behind the same guardrail evaluation as
everything else — including a new gate, `mission_type`, that independently re-checks the
mission type against the same allowed set the wallet engine enforces (§10 covers what
`guards: N/19` means).

**Before you promote from `advisor` to `economy`:**

```bash
$ uv run --directory skills/veydrift-agent vd tick --readiness
```

This prints the actual evidence: tick count, uptime, proposals made, how many *you*
executed by hand. Read the divergence line carefully — proposals with no matching
`actions.jsonl` entry. A clean report with zero guardrail fires is *weaker* evidence than a
report where a guard fired correctly and the agent respected it; a green tick count on its
own tells you the agent hasn't hit a wall yet, not that it's trustworthy.

The checklist:

1. **At least 24 hours** of continuous tier-1 ticks.
2. **Read `strategy.md` in full**, not just the latest few ticks — the reasoning across a
   stretch of time, not whether any single proposal looked fine in isolation.
3. **Check `proposals.jsonl` for guardrail fires.** A run with guards firing correctly and
   the agent respecting them is real evidence; a run with nothing to check is not.
4. **Edit `policy.json` yourself**, changing `"tier": "advisor"` to `"tier": "economy"`.
   No command does this for you, on purpose.
5. **Run `walletctl verify-abi` immediately before your first real send.** ABI drift — the
   deployed contract getting upgraded — is exactly the kind of thing that can happen
   silently in the gap between your review and the first live action.

**Before `economy` → `operator`:** the same shape, at a higher bar — at least **seven
days** of clean tier-2 operation (real submissions, not just ticks), read the same way,
before the same kind of manual edit.

**Never**: promote on tick count alone, promote without having actually read `strategy.md`,
or promote while a guard is failing *intermittently* rather than consistently passing —
intermittent failures are the ones worth understanding before you give the agent more
room, not less.

## 14. Example prompt and looping for tier>=1 agent operators

Everything so far assumes you're checking in on the agent yourself, one tick at a time.
This section is for the other mode: a standing agent "commander" that runs your planet
continuously via `/loop`, proposing — and, once you've promoted past `advisor`, actually
submitting — on its own between check-ins. Read §13 before using this for real; nothing
below changes what tier does. It's still the only thing that decides whether a proposal
can ever actually send.

**One field decides whether this is truly unattended: `wallet_engine.require_confirmation`.**
It defaults to `true`, and at `true`, a tier ≥2 tick never sends on its own — it prints
"AWAITING HUMAN CONFIRMATION" and hands you a `walletctl send --confirm` command instead,
every single time. That's the safe default, and a reasonable place to stay if you want a
human in the loop on every send. For the agent to actually run unattended, set it to
`false` yourself, deliberately — which is exactly why the agent prompt below carves that
field out from what it's allowed to change on its own, right alongside `tier`.

### Agent prompt

Paste this into a fresh session, filling in your own wallet address and planet
coordinates. Unlike §5's bootstrap prompt, this one assumes you've already promoted past
`advisor` (§13) — at tier 1 it's harmless (nothing can send regardless of what the prompt
claims, since the `tier` gate blocks every onchain function structurally), but it's
written for the tier where it actually does something:

```
You're Wayfinder Automata, an agent commander playing Veydrift, an on-chain
space-strategy game on Base, using the Veydrift skills installed in this
environment. Your wallet address is <your address> and your home planet's
coordinates are <your planet's coordinates, e.g. 7:291:4>.

You command this planet to grow its empire: prepare a resource surplus, and
prioritize infrastructure, research, and shipyard production, plus
non-combat fleet missions once you have the fleet to run them. Look at all
four queues — building, research, ship, defense — every time you act, and
treat an idle one as something to investigate, balanced against infra
payback: don't fill a queue with a weak pick just to avoid idle time, but
don't leave one idle just because nothing is declared for it either.
Combat, ACS, and alliances are out of scope for this skill at every tier,
by design — never propose or imply progress toward them, even if I ask.

Every time you act: read the planet's live state, propose what to build
next, execute it, and give me a final summary in plain terms, including a
link to the on-chain transaction (https://basescan.org/tx/<hash>, since
this is Base) whenever one was actually sent. You're allowed to submit
on-chain transactions unless I've explicitly lowered your tier to advisor.

You're free to update policy.json as your strategy evolves — including
declaring new ship_targets/defense_targets/research_priority/
building_priority as infrastructure unlocks new options, so growth doesn't
stagnate on a config that hasn't kept up — with two exceptions that are
mine to set, not yours: tier, and wallet_engine.require_confirmation.
Report any other change you make to policy in the same summary that made
it — I should never have to diff the file to find out what changed.

Maintain your own durable, cross-session memory of what should survive
between sessions — this planet's long-term strategy, its strengths and
constraints, and decisions you've already made and why — and keep it
current whenever anything changes. Keep it to what's genuinely durable:
never store data that's already live in the game state (resources, queue
timers, prices) — that's what the next tick is for.
```

### User prompt — kick off the loop

Once the agent prompt above is the standing context for the session, this is what
actually starts the loop. `/loop` with no fixed interval lets the agent self-pace between
iterations instead of ticking on a dumb timer:

```
/loop Run one Veydrift gameplay tick (not a dry run) and let your strategy
adjust each iteration per your standing instructions. Then send me a report
covering: the action taken this iteration (including the on-chain tx link,
if one sent), any policy.json change you made and why, and a proposal for
the next actionable moment — a busy queue's QueueEntry.seconds_remaining
(or ready_at), or the affordability-gate's ETA string ("affordable in
~Xh Ym") when the winning pick isn't affordable yet. Schedule the next
wakeup for whichever of those is soonest, floored at policy.cadence's
relevant *_minutes field so we never tick faster than the policy allows,
and capped at 3600s per wakeup — chain multiple wakeups for a longer wait
instead of one long sleep. If nothing gives an ETA (idle queue, no pending
mine target), fall back to the cadence default.
```

`QueueEntry.seconds_remaining`/`ready_at` and the affordability gate's ETA string are both
real fields the agent already has access to from a normal tick's output — nothing here
asks it to invent numbers it doesn't have.

## 15. Troubleshooting

| Symptom | What's actually happening |
| --- | --- |
| `vd tick` says `readiness.ready` is not true, or health nulls | Almost always transient backend replica lag, not an outage — the agent already treats this correctly and will retry. If it persists past `on_health_unhealthy_minutes` (default 30), it escalates instead of retrying forever. |
| `/health` reports `ok: false`, but the tick still runs normally | Expected: `ok:false` caused *solely* by a combat-related backend readiness issue (a "New attacks are temporarily paused"-style condition) no longer blocks the peaceful ladder — this codebase never touches combat regardless of policy, so that specific condition can't affect what it would propose. Any other cause of `ok:false` still blocks/escalates as before. |
| `walletctl status` refuses to run | Expected if no provider is configured yet — it's telling you `VEYDRIFT_KEYSTORE` (or `VEYDRIFT_PRIVATE_KEY` for `envkey`) isn't set. Not a bug. |
| `walletctl verify-abi` shows a mismatch | The deployed contract's ABI has changed since this repo's pin. **Every write is blocked until this is resolved** — that's deliberate, not overly cautious. See `skills/veydrift-wallet/references/abi-pinning.md` for the re-pin recipe. |
| Guards read `16/19 pass (block)` and nothing was submitted, at tier 1 | Correct and expected — see §10. This is not an error state. |
| Two agent sessions on the same machine seem to share tick counts / a killswitch | They do — `$VEYDRIFT_HOME` is per-machine, not per-session, unless you override it. |
| `policy.json` edits get rejected | The schema is validated strictly — an unrecognized key or a missing required field is a hard stop, not a warning. Read the error; it names the exact field. |

## 16. Safety reminders, one more time

- **The wallet *is* the account.** There is no password reset and no recovery path for a
  lost keystore password or lost key. A Veydrift planet cannot be transferred to a
  different address by any contract mechanism — see `README.md`'s key-custody section
  before you decide how seriously to treat key storage.
- **`allow_combat` in `policy.json` does nothing, on purpose.** Every code path that reads
  it ignores it. Enabling `Attack`/`AcsAttack`/`MissileAttack`/`Intercept` requires an
  actual source code change, not a config edit — that friction is deliberate.
- **Real transactions have already been submitted to Veydrift on mainnet from this
  codebase** — at tier 2 (`economy`) and tier 3 (`operator`), through the real
  `build → simulate → send` path, not a fixture or a fork. See `README.md`'s Status
  section for the current tier. That doesn't make your own T1→T2 promotion any less
  consequential — no code path advances the tier on its own; only you, editing
  `policy.json`, do — so budget the same care §13's checklist asks for regardless of what
  this account or any other has already done.
- **`--confirm` can never become automatic.** No environment variable, no policy field,
  and no flag combination makes `walletctl send` skip that explicit flag. If you ever see
  a transaction submit without you having typed `--confirm` on that exact command
  yourself (or a tier-2+ tick doing so per `wallet_engine.require_confirmation`, which you
  set), something is wrong and you should stop and investigate before doing anything else.
