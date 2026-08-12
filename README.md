# Veydrift Agent Infrastructure

Two installable agent skills that read [Veydrift](https://veydrift.com) game state, propose
the next action for a Veydrift planet, and — only once a human has explicitly raised the
tier — build, allowlist, simulate and submit the transaction for it. Veydrift is an
on-chain space-strategy game on Base mainnet; nothing here is generic blockchain tooling.

- **`veydrift-agent`** (Python) — reads the game API, runs deterministic calculators,
  proposes zero or one action per tick. Never signs anything.
- **`veydrift-wallet`** (TypeScript) — the only thing here that builds real calldata,
  signs, or submits. Independently re-validates every transaction against its own
  allowlist regardless of what the agent skill already checked.

Built for and tested against a single account/planet, but generalized: planet traits are
read at runtime, not hardcoded, and the code is meant to work for any wallet/planet.

If you're an AI coding agent extending this codebase, read [`AGENTS.md`](AGENTS.md) instead
— it has build commands, conventions, and the invariants a change must not break. This file
is the product overview and the usage guide.

## Status: tier 1 — advisor

Nothing has ever been promoted. The agent proposes and pretty-prints a complete,
ready-to-submit transaction for every action it would take, but **nothing in this codebase
has ever caused a submission to Veydrift** — see [What this does not verify](#what-this-does-not-verify)
before assuming otherwise. No code path in this codebase advances the tier; only a human
editing `$VEYDRIFT_HOME/policy.json` does.

| Tier | May propose | May submit | Gate to enter |
| --- | --- | --- | --- |
| 1 `advisor` (current) | everything in scope | **nothing** | default |
| 2 `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `settlePlanet`, `startDefenseProduction`, `startShipProduction` | ≥24h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0)/Deploy(1)/Harvest(4) only | ≥7 days clean T2, human edit |

Combat (`Attack`, `AcsAttack`, `MissileAttack`, `Intercept`) is unreachable **in code**, at
every tier — `policy.json`'s `allow_combat` key is deliberately ignored everywhere it's
read. Enabling it requires a source change, not a config edit.

### Promoting a tier

`vd tick --readiness` reports tick count, uptime, proposals made, how many a human
actually executed, **divergences between proposal and human action**, which guardrails
fired and why, and cumulative gas spent. A clean report is necessary but not sufficient —
the steps below are what actually justifies a promotion.

**T1 → T2 (`advisor` → `economy`):**

1. At least 24 hours of continuous T1 ticks, through whichever harness.
2. A human reads `$VEYDRIFT_HOME/logs/strategy.md` in full — not just the latest tick —
   and confirms the reasoning, not just that proposals looked plausible one at a time.
3. Check `logs/proposals.jsonl` for guardrail fires: a run where guards fired correctly
   and the agent respected them is *stronger* evidence than a clean run with zero fires —
   a green tick count alone is a bad promotion signal on its own.
4. Only then, a human hand-edits `$VEYDRIFT_HOME/policy.json`, setting `tier: "economy"`.
   No command does this for you.
5. Confirm `walletctl verify-abi` passes immediately before the first real `send` — ABI
   drift is exactly the kind of thing that can happen silently between the review and the
   first live action.

**T2 → T3 (`economy` → `operator`):** the same shape, at a higher bar — at least **7 days**
of clean T2 operation (real submissions, not just ticks), before the same manual
`policy.json` edit. T3 additionally unlocks `launchFleetMission` for Transport/Deploy/
Harvest only; combat mission types remain unreachable regardless of tier.

**Never**: promote on tick count alone, promote without reading `strategy.md`, or promote
while any guard is failing intermittently rather than consistently.

## Install

```bash
npx skills add . -a claude-code -a hermes-agent
```

This is the only supported install path — there's nothing to hand-symlink. Re-run the same
command after editing anything under `skills/` to update; it's a fresh copy each time, not
a merge. `npx skills add . -l` lists both skills' descriptions without installing, useful
for confirming a `SKILL.md` frontmatter change parses correctly first.

`skills add` **copies** the skill into wherever the target harness keeps its skills — that
copy is a build artifact, not something to hand-edit or commit. It also does not respect
`.gitignore` when copying: clean `.venv/`, `node_modules/`, and other build caches out of
`skills/*/` *before* installing, or they get copied along with everything else (a copied
`.venv` is actively broken — `uv run` from the copy fails with a dyld error until you
`rm -rf` it and let `uv` rebuild a fresh one).

## Quick start: running a tick

| Harness | How |
| --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` |
| Claude Code, unattended | `claude -p "run a veydrift tick"` from `launchd` |
| Hermes | register `vd tick` on Hermes' own scheduler at `policy.cadence.economy_minutes` |
| Bare OS | `assets/com.veydrift.agent.plist.template` — launchd, `StartInterval`. Ships as a template with a documented install command; not installed by default |

```bash
$ uv run --directory skills/veydrift-agent vd tick init
wrote /Users/santteegt/.veydrift/policy.json
$ uv run --directory skills/veydrift-agent vd tick --dry-run
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

`guards: 13/16 pass (block)` is expected and correct at tier 1: the `tier` gate itself
blocks, which is exactly the mechanism that makes tier 1 safe by construction rather than
by discipline. Nothing downstream of the guard ever runs. `$VEYDRIFT_HOME` defaults to
`~/.veydrift` for every invocation on a given machine regardless of which harness runs it —
point `VEYDRIFT_HOME` at a different directory for any testing you don't want mixed into a
real account's history.

`vd tick --readiness` prints the promotion evidence above instead of running a tick;
`vd log --digest 24h`, `vd log tail-proposals`, `vd log tail-actions` and `vd log strategy`
read back what accumulates in `$VEYDRIFT_HOME/logs/` over many ticks.

The read → plan pipeline that `tick` wraps is also runnable standalone, without touching
`$VEYDRIFT_HOME` at all:

```bash
$ uv run --directory skills/veydrift-agent vd read snapshot \
    --wallet 0x224aba5d489675a7bd3ce07786fada466b46fa0f --summary
snapshot 0x224aba5d489675a7bd3ce07786fada466b46fa0f  health=ok  indexed=healthy  block=49877328
research: lab L0  all level 0
-- 7:181:14 (id 664)  fields 0/174
   levels: all level 0
   energy: 0/0 (scale 10000)
   production/hr: M 0 C 0 D 0
   hours-to-cap: M never (idle)  C never (idle)  D never (idle)
   affordable now: Metal Mine, Crystal Mine, Deuterium Synthesizer, Solar Plant, Metal Storage, Crystal Storage, Deuterium Tank
   queues: idle
incoming: none
```

Completed in ~2s, 466 bytes — well inside the design target of <10s / ≤2KB.

## The safety contract

**What this codebase will never do, by construction, not by configuration:**

- Submit a transaction without an explicit human `--confirm` on the exact `walletctl send`
  command line. No env var, no policy field, no flag makes this implicit.
- Propose or execute combat (`Attack`, `AcsAttack`, `MissileAttack`, `Intercept`) at any
  tier. `policy.json`'s `allow_combat` is read and then ignored everywhere.
- Sign or submit anything outside the live Veydrift contract address, or outside the
  current tier's allowed selector set — enforced twice, independently, by `vd guard`
  (agent-side) and `checkAllowlist` (wallet-side, always re-run regardless of what the
  agent already checked).
- Advance its own tier. Only a human edit of `policy.json`'s `tier` field does that.
- Write a private key, mnemonic, keystore or API secret to any tracked file, or to any log.

**Escalation list** — situations the agent is designed to hand to a human rather than act on:

| Trigger | What happens |
| --- | --- |
| Any incoming hostile fleet | ESCALATE, no proposal at all |
| Live `deploymentAbiHash` drifts from the pinned hash | Block every write; `walletctl verify-abi` surfaces this before any `send` |
| `/health` unhealthy for `on_health_unhealthy_minutes` (default 30) | ESCALATE |
| Same action reverts `on_revert_count` times (default 2) | ESCALATE, do not retry blindly |
| A single action's cost exceeds `escalate_above_pct_of_resources` (default 25%) of current holdings | ESCALATE rather than BLOCK — a judgment call, not a hard stop |

One limit worth stating plainly rather than implying more than is true: the wallet-side
tier check reads tier from `$VEYDRIFT_HOME/policy.json`, but falls back to a caller-
supplied `--tier` flag when no policy file exists. That fallback exists so `walletctl`
works standalone, but it means the check defends against an honest-but-misconfigured
caller, not a fully hostile one that controls its own environment. See
`skills/veydrift-wallet/references/tx-safety.md` for the full reasoning.

## Key custody — read this before touching a real wallet

**The wallet is the account; there is no recovery.** Losing the keystore password or the
key material loses the planet permanently — there is no password reset, no "forgot your
key" flow, nothing to appeal to.

That alone would be true of any EOA. What makes it a *harder* constraint here, verified
directly against the deployed contract:

- **Planet ownership is a plain struct field**, not a token. There is no `transferPlanet`
  function anywhere in the deployed contract, and planets are not ERC-721s, so there's no
  `transferFrom` escape hatch either.
- **`abandonPlanet` reverts for a home planet.** A single-planet account — the common
  case — can neither transfer nor abandon its planet through any contract mechanism. The
  only thing that can change is who holds the key.

**Consequence for wallet-provider choice:** any provider that mints a *new* address — Safe
multisig, ERC-4337 smart accounts, most hosted MPC/TEE wallets — categorically cannot hold
an existing planet. `docs/wallet-provider-research.md` evaluates every alternative against
this constraint in depth; the short version is that the shipped `keystore` provider (an
encrypted, locally-held EIP-2335/geth JSON keystore) is the correct default for a
single-planet hobby account, and EIP-7702 delegation (confirmed live on Base) is the one
path worth prototyping later, because it's the only mechanism found that adds
smart-account capability **without** changing the address.

**Password handling, concretely:** `VEYDRIFT_KEYSTORE_PASSWORD` env var, or an interactive
non-echoing prompt if unset. Never a CLI flag — a flag lands in shell history and `ps`
output, which a prompt or env var does not.

## Where logs live

All under `$VEYDRIFT_HOME/logs/`, never inside either skill's tree (skills get overwritten
on every install/update — see [Install](#install)):

| File | Contents | Mutability |
| --- | --- | --- |
| `proposals.jsonl` | every proposal, full guard verdict list, and the built calldata — whether or not it was ever submitted | append-only |
| `actions.jsonl` | **executed only** — tx hash, gas, block, before/after state, indexed-at | append-only |
| `ticks/<iso>.md` | the pretty report for one tick | one file per tick |
| `strategy.md` | rationale, plan revisions, escalations, human decisions | append-only |

`vd log --digest 24h` produces the daily rollup: builds, research, resources produced, gas
spent, and **everything refused, with reasons** — the refusals are the part worth reading
first when auditing a stretch of ticks, not the successes.

## What this does not verify

Read before trusting a claim about this project — it's the single easiest thing to overstate:

- **No transaction has ever been submitted to Veydrift from this codebase.** The write
  path is built, allowlisted, simulated and tested against fixtures (361 tests across both
  skills as of this writing) — never executed against mainnet. The first real submission
  is a human decision at the T1→T2 promotion, not something this codebase has done on its
  own initiative, ever.
- **Cost scaling, queue behaviour and lazy settlement above level 0 are unobserved.** The
  account this was built and tested against has taken zero on-chain actions since
  settlement — every building/tech level is 0, every queue is idle. Formulas are verified
  against contract source and against live level-0 data; nothing here has watched a live
  cost, queue, or lazy-settlement path respond to an actual level-up.
- **`protectedResources` semantics remain unconfirmed.** No loot or raid-profitability
  model is built on it anywhere in this codebase.
- **This is an advisor, not a proven autonomous system.** At tier 1 — the only tier this
  project has ever run at — the agent proposes and pretty-prints; a human reads the
  proposal and decides.

## Contributing / development

Build commands, test suites, coding conventions, and the invariants a change must not
break live in [`AGENTS.md`](AGENTS.md). Start there if you're modifying this codebase
rather than using it.

## Further reading

- [`docs/SPEC.md`](docs/SPEC.md) — the full implementation spec, work-package breakdown,
  and every acceptance criterion this repo is checked against.
- [`docs/RESEARCH-ADDENDUM.md`](docs/RESEARCH-ADDENDUM.md) — contract- and backend-source-
  derived corrections to the earlier notes: the real API route list, the real
  `Defense`/`FleetMissionType` enums, the ABI hash, the write-entrypoint list.
- [`docs/wallet-provider-research.md`](docs/wallet-provider-research.md) — every wallet-
  provider candidate evaluated against the address-binding constraint above.
- [`docs/NOTES.md`](docs/NOTES.md), `docs/veydrift-agent-prompt.md`,
  `docs/veydrift-agent-resources.md`, `docs/veydrift-briefing.html` — earlier research
  inputs this project was built from; superseded in places by the addendum but kept for
  provenance.
