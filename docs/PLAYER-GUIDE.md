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
9. [Reading what the agent tells you](#9-reading-what-the-agent-tells-you)
10. [Running on a schedule](#10-running-on-a-schedule)
11. [Reading the logs](#11-reading-the-logs)
12. [Evolving through the tiers](#12-evolving-through-the-tiers)
13. [Troubleshooting](#13-troubleshooting)
14. [Safety reminders, one more time](#14-safety-reminders-one-more-time)

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
yourself, is the actual point. §12 below covers when and how to move past it.

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
| **Tick** | One complete, atomic run of the agent's pipeline: load your policy, check for a killswitch, reconcile any pending transaction, read your planet's live state, decide on zero or one next action, run every safety check against it, and — only at tier ≥2 with a real send — submit it. Nothing schedules a tick by itself; something else (you, typing a command; a loop; a scheduler) decides *when* to call one. §8 and §10 cover running them. |
| **Tier** | How much the agent is trusted to *submit*, not what it's allowed to *think about* — it always proposes the same way regardless of tier. Three tiers: `advisor` (propose only, never send — where you start), `economy` (can submit building/research/production actions), `operator` (also non-combat fleet missions). Advancing tiers is always a manual edit you make, never automatic — §12. |
| **Guard** | One of 16 independent safety checks the agent runs on every proposal before it's ever allowed to send — things like "can I actually afford this," "will this push energy negative," "does the destination address match the real contract." Every guard is evaluated and reported every tick, even after one has already said no, so a blocked tick is exactly as inspectable as an allowed one. §9 shows what this looks like in real output. |
| **Snapshot** | The live read of your planet — resources, queues, energy, incoming fleets — that a tick's decision is computed against. Always fetched fresh from Veydrift's own API at the start of the tick; a decision is never made against stale or cached numbers. |
| **Proposal vs. action** | A *proposal* is what the agent decided it would do. An *action* is a proposal that was actually submitted onchain. At tier 1, every tick produces only proposals — the distinction doesn't matter until tier 2, where it becomes the whole point of `logs/proposals.jsonl` vs. `logs/actions.jsonl` (§11). |
| **Policy** | The one file, `policy.json`, that holds every setting governing what the agent's allowed to do for your account — tier, wallet, planet(s), spending limits, which action types are enabled. §6 walks through every field. |

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
npx skills add . -a claude-code -a hermes-agent
```

Verified output (from this repo, 2026-08-12):

```
◇  Installed 2 skills
   ✓ veydrift-agent (copied)  → ~/Verydrift/.claude/skills/veydrift-agent
   ✓ veydrift-wallet (copied) → ~/Verydrift/.claude/skills/veydrift-wallet
```

That's the whole install. `npx skills add . -l` (list mode) prints both skills'
descriptions without touching disk, if you want to confirm they parse correctly first.
To update after this repo changes, re-run the same command — it's a fresh copy each time,
never a merge, so don't hand-edit anything under `.claude/skills/` or `.agents/`; edit
`skills/` in this repo and reinstall.

This install puts both skills side by side, which is what lets `veydrift-agent` find
`veydrift-wallet` automatically (it resolves it as a sibling directory) when it shells out
for tier ≥2 sends. If your harness installs skills into isolated per-skill roots instead,
set `VEYDRIFT_WALLET_DIR` to the wallet skill's install path as an escape hatch — most
installs never need this.

**One thing worth knowing before you run this yourself:** the installer copies whatever is
in `skills/veydrift-agent/` and `skills/veydrift-wallet/` at that moment, including build
artifacts if you happen to have run tests recently (`.venv/`, `node_modules/`,
`__pycache__/`). If you're installing from a checkout where you've run `uv run pytest` or
`npm test`, clean those out first or the install will carry dead weight — a copied `.venv`
in particular is actively broken until you delete it and let `uv` rebuild a fresh one. If
you're installing fresh from a clone, this doesn't apply.

The repo ships a `Makefile` for exactly this:

```bash
$ make clean            # just tidy skills/ — .venv, node_modules, dist, __pycache__, etc.
$ make install-skills   # clean, then install globally, no prompts (see below)
```

Both are safe to re-run any time — `clean` only ever removes known build-artifact directory
names under `skills/`, never source files, and both skills rebuild their own
`.venv`/`node_modules` automatically the next time you run their tests or `walletctl`.

`install-skills` doesn't just prepend `clean` to the command shown above — it runs
`npx skills add . -g -y -a claude-code -a hermes-agent`, which installs differently in two
ways worth knowing before you run it:

- **`-g` (global)**: both skills land once at `~/.agents/skills/veydrift-agent` and
  `~/.agents/skills/veydrift-wallet` — verified, that's genuinely where they end up — and
  get symlinked from there into each agent's own directory (`~/.claude/skills/`,
  `~/.hermes/skills/`, etc.), rather than copied separately into *this repo's*
  `.claude/skills/`. That makes them available to every project on your machine, not just
  this checkout — worth knowing since it's a bigger footprint than the plain command above.
- **`-y` (yes)**: skips the confirmation prompt the plain command would otherwise show you.

If you specifically want the install confined to this checkout, run the plain
`npx skills add . -a claude-code -a hermes-agent` command instead of the Makefile target.

## 5. Bootstrap via an agent session

Everything in §6 and §7 below — writing `policy.json`, picking a wallet provider — can be
done by hand, editing files yourself. It can also be done by just asking your agent
session to do it, since both skills were built specifically to be driven this way: each
one ships a `SKILL.md` that tells a Claude Code or Hermes session exactly when to trigger
and what it's allowed to do, so once the skills are installed (§4), a plain-language
request is enough to get moving.

Open a fresh session in your installed harness and say something like:

```
I just installed the Veydrift skills. My wallet is <your address> and my planet
is <your planet id>. Set up my policy file for a cautious first run in advisor
mode, explain each field you set, and get me ready to run my first tick.
```

Because `veydrift-agent`'s `SKILL.md` explicitly lists "planet id," "wallet," and
"policy.json" among the phrases that trigger it, a Claude Code or Hermes session with the
skill installed will pick this up on its own — you don't need to name the skill or type a
slash command. What actually happens next is not magic — it's the agent typing the same
commands this guide documents by hand:

1. It runs `vd tick init` to write a fresh `policy.json` from the shipped template
   (exactly the command shown in §6).
2. It opens the file and walks you through each field — tier, wallet, planet id(s),
   spending limits, reserves, which action types are enabled — the same fields §6 explains
   below.
3. It should ask you to confirm your wallet address and planet id specifically, since those
   are the two fields it cannot guess correctly on its own.
4. It will likely offer to run `vd tick --dry-run` once the file looks right, so you can
   see a first proposal before deciding anything further.

**Read what it wrote anyway.** Letting the agent do the typing doesn't change what ends up
in the file or skip any review step — it's the same `policy.json`, validated the same
strict way, that §6 walks through field by field. If you'd rather do this entirely by hand
instead — or want to understand exactly what the agent just set on your behalf — §6 is the
complete reference. This same plain-language approach isn't limited to setup, either: once
you're running, you can ask the same kind of session "run a Veydrift tick" or "check my
queues" at any point instead of typing the raw `uv run` commands yourself — §10 covers
this alongside the more formal scheduling options.

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
  "tier": "advisor",                 // <-- start here. See §12 before ever changing this.
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
    "allow_fleet_noncombat": false,   // reserved for tier 3; no proposer uses it yet
    "allow_combat": false             // ignored everywhere on purpose -- see §14
  },
  "escalation": {
    "on_incoming_fleet": true, "on_abi_hash_change": true,
    "on_health_unhealthy_minutes": 30, "on_revert_count": 2
  },
  "wallet_engine": { "provider": "keystore", "require_confirmation": true }
}
```

**Two fields you must edit before anything downstream makes sense:**

- `wallet`: your actual player wallet address — the one that settled the planet(s) you
  want managed. `0x224a…fa0f` above is this repository's own reference example; it is not
  a wallet you should use.
- `planets`: your planet id(s), or `[]` to have the agent discover every planet the wallet
  holds automatically via `/wallet/{addr}/planets`.

Everything else in `limits`/`reserves`/`actions`/`escalation` is a reasonable starting
point. Don't loosen `limits` or flip on `allow_defense`/`allow_ships` until you've watched
the agent propose things under the defaults for a while — see §12.

`policy.json` is validated strictly on every tick: an unrecognized key is a hard error, not
a silent ignore, and a missing required field stops the tick rather than guessing a
default. If you break the JSON, the next `vd tick` will tell you exactly what's wrong
rather than doing something unexpected.

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

**If you need to create one**, this is a genuine, verified recipe — run from
`skills/veydrift-wallet/`:

```bash
$ npx tsx -e '
import { Wallet } from "ethers";
import { writeFileSync } from "fs";
const wallet = Wallet.createRandom();
console.log("New address:", wallet.address);
const password = "choose a real passphrase here, not this one";
const json = await wallet.encrypt(password);
writeFileSync("veydrift-keystore.json", json);
console.log("Wrote veydrift-keystore.json");
'
```

**Read this before running it for real:** `Wallet.createRandom()` generates a **brand
new** address with no history. It cannot access an existing planet — a Veydrift planet is
permanently bound to the address that settled it (no transfer function exists on the
contract at all; see [`README.md`](../README.md)'s key-custody section). So:

- If you're setting up a **new** account: generate a fresh keystore this way, fund it with
  a small amount of ETH on Base for gas, and use its address to settle your first planet.
- If you already have an **existing** planet: you need the private key that already
  settled it, not a freshly generated one. Import that specific key into a keystore
  instead of generating a random one — most wallet software (MetaMask, `cast wallet`, a
  hardware wallet's export flow) can do this. The point of the recipe above is showing the
  *shape* of a working keystore, not prescribing where the key comes from.

**Point the skill at it**, wherever the file lives:

```bash
export VEYDRIFT_KEYSTORE=/path/to/your/keystore.json
```

Leave `VEYDRIFT_KEYSTORE_PASSWORD` **unset**. The skill will prompt you interactively,
without echoing what you type, every time it needs to sign. That's deliberate — a value in
an env var is one `printenv` or one compromised process away from being read; a prompt
requires a human at the keyboard for every single send. If you want the convenience of not
typing it every time (for example, unattended tier-2+ operation), you can set
`VEYDRIFT_KEYSTORE_PASSWORD` instead, but understand what you're trading away: see §14.

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

### Environment variables at a glance

Every env var either skill reads, in one place:

| Variable | Skill | Purpose | Default |
| --- | --- | --- | --- |
| `VEYDRIFT_HOME` | both | Where policy, logs and cached state live — see §6 and §11. | `~/.veydrift` |
| `VEYDRIFT_RPC_URL` | wallet | Base RPC endpoint for every read/write. | `https://mainnet.base.org` |
| `WALLET_PROVIDER` | wallet | Which provider to sign with (`keystore`/`envkey`). | `keystore` |
| `VEYDRIFT_KEYSTORE` | wallet | Path to the encrypted keystore JSON (`keystore` provider). | — (required by that provider) |
| `VEYDRIFT_KEYSTORE_PASSWORD` | wallet | Keystore password. Leave unset for an interactive, non-echoing prompt instead — recommended, see above. | unset (prompts) |
| `VEYDRIFT_PRIVATE_KEY` | wallet | Raw private key (`envkey` provider, testing only). | — (required by that provider) |
| `VEYDRIFT_TIER` | wallet | Fallback tier when no `policy.json` exists yet. Ignored (and flagged as a disagreement) once a policy file is present — `walletctl` always trusts the file over this. | `advisor` |
| `VEYDRIFT_WALLET_DIR` | agent | Escape hatch if the two skills aren't installed as siblings — see §4. | sibling-directory auto-detect |
| `VEYDRIFT_SECRET_ENV_VARS` | agent | Comma-separated extra env var names to redact from logs, beyond the built-in `VEYDRIFT_PRIVATE_KEY`/`VEYDRIFT_KEYSTORE_PASSWORD`. Only needed if you've added your own secret-bearing env var into this system's environment. | unset (built-ins only) |

## 8. Your first tick

With `policy.json` written and the wallet skill verified, run one tick:

```bash
$ uv run --directory skills/veydrift-agent vd tick --dry-run
```

Recall from §3: a tick is one atomic pass through the whole pipeline — load policy, check
the killswitch, reconcile any pending transaction, snapshot your planet, decide on an
action, run every guard, and (tier ≥2 only) send. `vd tick` is the single command that
does all of that; nothing about it schedules repetition — §10 covers running many of them.

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
│     guards: 13/16 pass (block)                                               │
│     tx:     to 0xf397910F005151b09644228573a4353818D3755d  data              │
│ 0x165715e3... (NOT SUBMITTED -- tier advisor)                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Your own first tick will look different (your planet's actual state and traits), but the
shape is the same: current resources and queues, whether anything hostile is inbound, one
proposed action with its cost and reasoning, and confirmation that nothing was submitted.

### Options worth knowing

`vd tick` takes a handful of flags, and there are a couple of sibling commands worth
knowing before you move on to §10's scheduling options:

| Command | What it does |
| --- | --- |
| `vd tick` | Runs exactly one tick and prints the report shown above. |
| `vd tick --dry-run` | Never sends, no matter what tier allows — useful for checking what the agent *would* do without any possibility of a real submission. Always on automatically at tier 1. |
| `vd tick --format json` | Same tick, machine-readable output instead of the pretty panel — useful for piping into a script or another log. `md` (the pretty panel) is the default. |
| `vd tick --policy PATH` | Run against a specific policy file instead of `$VEYDRIFT_HOME/policy.json` — handy for testing a config change, or managing more than one account/planet from the same machine. |
| `vd tick --readiness` | Doesn't run a tick at all — prints your tier-promotion evidence instead (tick count, uptime, proposals vs. what you actually executed, guardrail fires). §12 covers reading this before promoting. |
| `vd tick init` | Writes a fresh `policy.json` from the template — the command §6 uses to bootstrap. Safe to re-run; won't overwrite an existing file without `--force`. |
| `vd doctor` | Reports which subcommands are actually wired up in the copy you're running — useful if you ever pull a checkout mid-update. |
| `vd log --digest 24h` | A rollup of the last day: what got built, resources produced, gas spent, and everything the agent refused to do and why. §11 covers this in full. |

## 9. Reading what the agent tells you

A few things worth understanding about that block before you trust it:

- **`guards: 13/16 pass (block)`** at tier 1 is expected, not a problem. The `tier` gate
  itself blocks — every onchain proposal is blocked at tier 1 by design, since advisor
  mode may never submit. That's what makes tier 1 safe *by construction*, not by
  discipline: the decision genuinely is `BLOCK`, so nothing past that point ever runs.
- **`why:`** states the actual numbers behind the decision, not a canned explanation. If
  it says a mine upgrade needs 11 energy against 0 produced, that's a live comparison
  against your planet's current state, computed fresh every tick — not a rule of thumb.
- **The build order is derived from your planet's actual traits** — temperature,
  resource multipliers, current levels — not a generic build order copy-pasted for every
  planet. A cold planet gets pushed toward Deuterium Synthesizer earlier; a hot planet
  gets offered Solar Satellites where a cold one never would. If the reasoning doesn't
  match your planet's traits, something's wrong — say so, don't just follow it.
- **The transaction shown is real and complete**, not a mockup — `tx: to ... data ...` is
  exactly what would be submitted if you were at a tier that could submit it. That's
  deliberate: it's what makes a later promotion decision evidence-based rather than a
  guess (§12).

## 10. Running on a schedule

One tick by hand is enough to see how it works; the useful mode is many ticks over time so
you (and later, §12's promotion evidence) have something to look at. Every option below
runs the exact same `vd tick` command with the exact same safety posture — the only thing
that differs between them is *who decides when to call it*. `vd tick`'s own lockfile makes
two overlapping calls a benign skip rather than a race, so running it from more than one
place at once on the same machine is safe, if occasionally redundant.

| Harness | How | Notes |
| --- | --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` | A human is present — each tick's report lands directly in your chat as it happens. |
| Claude Code, unattended | Schedule `claude -p "run a veydrift tick"` via `launchd` or a scheduled task | The agent invokes `vd tick` itself with nobody watching in real time — `strategy.md` and `proposals.jsonl` (§11) become what you review afterward, not the immediate output. |
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

## 11. Reading the logs

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

## 12. Evolving through the tiers

| Tier | What it can propose | What it can actually submit |
| --- | --- | --- |
| 1 `advisor` (you start here) | Everything in scope | Nothing, ever |
| 2 `economy` | Everything in scope | Building upgrades, research, defense/ship production, permissionless mission resolution |
| 3 `operator` | Everything in scope | Everything tier 2 can, plus non-combat fleet missions (Transport/Deploy/Harvest only) |

Nothing in this codebase ever advances the tier on its own. It is **always** a manual edit
of `policy.json`'s `tier` field, by you, and it should be treated as a real decision, not a
config toggle.

**Before you promote from `advisor` to `economy`:**

```bash
$ uv run --directory skills/veydrift-agent vd tick --readiness
```

This prints the actual evidence: tick count, uptime, proposals made, how many *you*
executed by hand, and — the part worth reading most carefully — **divergences between
what was proposed and what you actually did.** A clean report with zero guardrail fires
is *weaker* evidence than a report where a guard fired correctly and the agent respected
it; a green tick count on its own tells you the agent hasn't hit a wall yet, not that it's
trustworthy.

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

## 13. Troubleshooting

| Symptom | What's actually happening |
| --- | --- |
| `vd tick` says `readiness.ready` is not true, or health nulls | Almost always transient backend replica lag, not an outage — the agent already treats this correctly and will retry. If it persists past `on_health_unhealthy_minutes` (default 30), it escalates instead of retrying forever. |
| `walletctl status` refuses to run | Expected if no provider is configured yet — it's telling you `VEYDRIFT_KEYSTORE` (or `VEYDRIFT_PRIVATE_KEY` for `envkey`) isn't set. Not a bug. |
| `walletctl verify-abi` shows a mismatch | The deployed contract's ABI has changed since this repo's pin. **Every write is blocked until this is resolved** — that's deliberate, not overly cautious. See `skills/veydrift-wallet/references/abi-pinning.md` for the re-pin recipe. |
| Guards read `13/16 pass (block)` and nothing was submitted, at tier 1 | Correct and expected — see §9. This is not an error state. |
| Two agent sessions on the same machine seem to share tick counts / a killswitch | They do — `$VEYDRIFT_HOME` is per-machine, not per-session, unless you override it. |
| `policy.json` edits get rejected | The schema is validated strictly — an unrecognized key or a missing required field is a hard stop, not a warning. Read the error; it names the exact field. |

## 14. Safety reminders, one more time

- **The wallet *is* the account.** There is no password reset and no recovery path for a
  lost keystore password or lost key. A Veydrift planet cannot be transferred to a
  different address by any contract mechanism — see `README.md`'s key-custody section
  before you decide how seriously to treat key storage.
- **`allow_combat` in `policy.json` does nothing, on purpose.** Every code path that reads
  it ignores it. Enabling `Attack`/`AcsAttack`/`MissileAttack`/`Intercept` requires an
  actual source code change, not a config edit — that friction is deliberate.
- **No transaction has ever been submitted to Veydrift from this codebase.** As of this
  writing, the entire tier-2+ write path is built, tested against fixtures, and verified
  piece by piece — but genuinely unexercised against a real chain. You will be the first
  person to actually exercise it for your account, whenever you promote past tier 1. Budget
  extra attention there, and re-read §12's checklist before you do.
- **`--confirm` can never become automatic.** No environment variable, no policy field,
  and no flag combination makes `walletctl send` skip that explicit flag. If you ever see
  a transaction submit without you having typed `--confirm` on that exact command
  yourself (or a tier-2+ tick doing so per `wallet_engine.require_confirmation`, which you
  set), something is wrong and you should stop and investigate before doing anything else.
