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
3. [Install the skills](#3-install-the-skills)
4. [Create your policy](#4-create-your-policy)
5. [Set up your wallet](#5-set-up-your-wallet)
6. [Your first tick](#6-your-first-tick)
7. [Reading what the agent tells you](#7-reading-what-the-agent-tells-you)
8. [Running on a schedule](#8-running-on-a-schedule)
9. [Reading the logs](#9-reading-the-logs)
10. [Evolving through the tiers](#10-evolving-through-the-tiers)
11. [Troubleshooting](#11-troubleshooting)
12. [Safety reminders, one more time](#12-safety-reminders-one-more-time)

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
yourself, is the actual point. §10 below covers when and how to move past it.

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
the read API is `https://api.veydrift.com`, both public and unauthenticated for reads.

## 3. Install the skills

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

**One thing worth knowing before you run this yourself:** the installer copies whatever is
in `skills/veydrift-agent/` and `skills/veydrift-wallet/` at that moment, including build
artifacts if you happen to have run tests recently (`.venv/`, `node_modules/`,
`__pycache__/`). If you're installing from a checkout where you've run `uv run pytest` or
`npm test`, clean those out first (`rm -rf skills/veydrift-agent/.venv
skills/veydrift-wallet/node_modules`) or the install will carry dead weight — a copied
`.venv` in particular is actively broken until you delete it and let `uv` rebuild a fresh
one. If you're installing fresh from a clone, this doesn't apply.

## 4. Create your policy

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
  "tier": "advisor",                 // <-- start here. See §10 before ever changing this.
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
    "allow_combat": false             // ignored everywhere on purpose -- see §12
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
the agent propose things under the defaults for a while — see §10.

`policy.json` is validated strictly on every tick: an unrecognized key is a hard error, not
a silent ignore, and a missing required field stops the tick rather than guessing a
default. If you break the JSON, the next `vd tick` will tell you exactly what's wrong
rather than doing something unexpected.

## 5. Set up your wallet

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
`VEYDRIFT_KEYSTORE_PASSWORD` instead, but understand what you're trading away: see §12.

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

## 6. Your first tick

With `policy.json` written and the wallet skill verified, run one tick:

```bash
$ uv run --directory skills/veydrift-agent vd tick --dry-run
```

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

## 7. Reading what the agent tells you

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
  guess (§10).

## 8. Running on a schedule

One tick by hand is enough to see how it works; the useful mode is many ticks over time so
you (and later, §10's promotion evidence) have something to look at.

| Harness | How |
| --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` — you'll see each tick's report land in your chat |
| Claude Code, unattended | Schedule `claude -p "run a veydrift tick"` via `launchd` or a scheduled task |
| Hermes | Register `vd tick` on Hermes' own scheduler at `policy.cadence.economy_minutes` (10 minutes by default) |
| Bare OS, no agent harness | `skills/veydrift-agent/assets/com.veydrift.agent.plist.template` — a launchd template with a documented install/uninstall recipe in its own header comment. Not installed automatically |

Whichever you pick, `$VEYDRIFT_HOME` is shared across every invocation on the same
machine — if you're testing something and don't want it mixed into your real history, set
`VEYDRIFT_HOME` to a scratch directory for that session.

## 9. Reading the logs

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

## 10. Evolving through the tiers

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

## 11. Troubleshooting

| Symptom | What's actually happening |
| --- | --- |
| `vd tick` says `readiness.ready` is not true, or health nulls | Almost always transient backend replica lag, not an outage — the agent already treats this correctly and will retry. If it persists past `on_health_unhealthy_minutes` (default 30), it escalates instead of retrying forever. |
| `walletctl status` refuses to run | Expected if no provider is configured yet — it's telling you `VEYDRIFT_KEYSTORE` (or `VEYDRIFT_PRIVATE_KEY` for `envkey`) isn't set. Not a bug. |
| `walletctl verify-abi` shows a mismatch | The deployed contract's ABI has changed since this repo's pin. **Every write is blocked until this is resolved** — that's deliberate, not overly cautious. See `skills/veydrift-wallet/references/abi-pinning.md` for the re-pin recipe. |
| Guards read `13/16 pass (block)` and nothing was submitted, at tier 1 | Correct and expected — see §7. This is not an error state. |
| Two agent sessions on the same machine seem to share tick counts / a killswitch | They do — `$VEYDRIFT_HOME` is per-machine, not per-session, unless you override it. |
| `policy.json` edits get rejected | The schema is validated strictly — an unrecognized key or a missing required field is a hard stop, not a warning. Read the error; it names the exact field. |

## 12. Safety reminders, one more time

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
  extra attention there, and re-read §10's checklist before you do.
- **`--confirm` can never become automatic.** No environment variable, no policy field,
  and no flag combination makes `walletctl send` skip that explicit flag. If you ever see
  a transaction submit without you having typed `--confirm` on that exact command
  yourself (or a tier-2+ tick doing so per `wallet_engine.require_confirmation`, which you
  set), something is wrong and you should stop and investigate before doing anything else.
