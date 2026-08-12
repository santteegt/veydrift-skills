# AGENTS.md — Veydrift agent infrastructure

This is the primary, harness-agnostic operating document for this repository. If you are
an agent (Claude Code, Hermes, or anything else) picking up this repo cold, read this file
first — `CLAUDE.md` at the repo root is a one-paragraph pointer to it, nothing more.

## 1. What this is

Two installable skills that together read Veydrift game state, propose the next action for
a Veydrift planet, and — only once a human has explicitly raised the tier — build, allowlist,
simulate and submit the transaction for it:

- **`veydrift-agent`** (Python/uv) — reads the game API, runs deterministic calculators,
  proposes zero or one action per tick. Never signs anything; never imports `viem`/`ethers`/
  `web3` (grep-verifiable — acceptance criterion 15 of `docs/SPEC.md`).
- **`veydrift-wallet`** (TypeScript/Node) — the only thing in this repo that ever builds real
  calldata, signs, or submits. Independently re-validates every transaction against its own
  allowlist regardless of what the agent skill already checked.

Full spec: `docs/SPEC.md`. Contract/backend research this was built from:
`docs/RESEARCH-ADDENDUM.md`. Wallet-provider evaluation: `docs/wallet-provider-research.md`.

### Current tier: **1 — advisor**

Nothing has ever been promoted. `assets/policy.example.json` (copied to
`$VEYDRIFT_HOME/policy.json` by `vd tick init` — verified in §5) ships with
`tier: "advisor"`, and `docs/SPEC.md` §4 is explicit that **no code path in this
codebase ever advances the tier field** — only a human editing `$VEYDRIFT_HOME/policy.json`
does. At tier 1 the agent proposes and pretty-prints a complete, ready-to-submit transaction
for every action it would take, but nothing in this codebase has ever caused a submission —
see §8's honesty section before assuming otherwise.

| Tier | May propose | May submit | Gate to enter |
| --- | --- | --- | --- |
| 1 `advisor` (current) | everything in scope | **nothing** | default |
| 2 `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `settlePlanet`, `startDefenseProduction`, `startShipProduction` | ≥24h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0)/Deploy(1)/Harvest(4) only | ≥7 days clean T2, human edit |

Combat (`Attack`, `AcsAttack`, `MissileAttack`, `Intercept`) is unreachable **in code**, at
every tier — `policy.json`'s `allow_combat` key is deliberately ignored everywhere it's
read. Enabling it requires a source change, not a config edit.

## 2. Repository map

```
/Users/santteegt/Verydrift/                     # this repo
├── AGENTS.md, CLAUDE.md, CHANGELOG.md           # this file, its pointer, and the changelog
├── .gitignore                                   # secrets + $VEYDRIFT_HOME-adjacent paths excluded
├── docs/
│   ├── SPEC.md                                  # the implementation spec this repo follows
│   ├── RESEARCH-ADDENDUM.md                     # contract + backend findings, source of truth
│   ├── wallet-provider-research.md              # every wallet-provider candidate evaluated, and why
│   └── NOTES.md, veydrift-agent-prompt.md, veydrift-agent-resources.md, veydrift-briefing.html
│                                                 # earlier inputs this project superseded in places
├── skills/
│   ├── veydrift-agent/          # SKILL.md, pyproject.toml/uv.lock, references/, src/, tests/
│   └── veydrift-wallet/         # SKILL.md, package.json, abi/, references/, src/, tests/
└── $VEYDRIFT_HOME/                              # runtime state -- OUTSIDE this repo entirely
    ├── policy.json, agent-state.json, KILLSWITCH
    └── logs/{actions.jsonl, proposals.jsonl, strategy.md, ticks/}
```

**`$VEYDRIFT_HOME` defaults to `~/.veydrift`** and is created on first run. This is not a
stylistic choice: `npx skills add .` **copies** the skill tree into wherever the target
harness keeps its skills, so anything written inside `skills/veydrift-agent/` or
`skills/veydrift-wallet/` at runtime is destroyed the next time you update. Every script in
both skills resolves its own bundled files (references, ABI, schemas) relative to `__file__`,
never `cwd` — verified by running the installed copy from `/tmp`, nowhere near this repo
(§4 has the one real gotcha this surfaced). `assets/policy.example.json` is copied to
`$VEYDRIFT_HOME/policy.json` on first init; nothing under `$VEYDRIFT_HOME` is ever tracked
by this git repo.

**Status note, as of this writing:** `skills/veydrift-agent/src/veydrift_agent/{state,guard,tick,log}.py`,
`schemas/`, and `assets/policy.example.json` are WP3's deliverables, built in parallel with
this document. Earlier in this same session `vd doctor` failed with `ModuleNotFoundError`
and `vd --help` listed only `read`/`calc`/`plan`; by the time this document was finished,
WP3 had landed and `vd doctor` reports `wired: read, calc, plan, guard, tick, log` — every
example in §5 below, including a full `vd tick --dry-run` run, was re-verified against the
completed tree, including `assets/com.veydrift.agent.plist.template` and both generated
`schemas/*.schema.json` files, all present by the end of this session.

## 3. Key custody — read this before touching a real wallet

**The wallet is the account; there is no recovery.** Losing the keystore password or the
key material loses the planet permanently — there is no password reset, no "forgot your
key" flow, nothing to appeal to.

That alone would be true of any EOA. What makes it a *harder* constraint here, verified
directly against the deployed contract at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(`git show 701bed35:packages/contracts/src/VeydriftPlanetManagementModule.sol`,
`git show 701bed35:packages/contracts/src/VeydriftGameStorage.sol`):

- **Planet ownership is a plain struct field** (`_planets[planetId].owner`), not a token.
  There is no `transferPlanet` function anywhere in the deployed contract, and planets are
  not ERC-721s, so there's no `transferFrom` escape hatch either.
- **`abandonPlanet` reverts for a home planet:**
  ```solidity
  // VeydriftPlanetManagementModule.sol:150
  if (homePlanetOf[msg.sender] == planetId) revert CannotAbandonHomePlanet();
  ```
  Planet 664 — the example planet in `policy.json` — is this wallet's home planet and its
  only planet. So it can be neither transferred (no function exists) nor abandoned (the call
  reverts). The account cannot "reset" or "give up" the planet through any contract
  mechanism; the only thing that can change is who holds the key that controls
  `0x224a…fa0f`, which is custody transfer, not a game action.

**Consequence for wallet-provider choice:** any provider that mints a *new* address —
Safe multisig, ERC-4337 smart accounts, most hosted MPC/TEE wallets — categorically cannot
hold this planet. `docs/wallet-provider-research.md` evaluates every alternative against
this constraint in depth; the short version is that the shipped `keystore` provider (an
encrypted, locally-held EIP-2335/geth JSON keystore) remains the correct default for a
single-planet hobby account, and EIP-7702 delegation to Base's audited `EIP7702Proxy` →
`CoinbaseSmartWallet` (confirmed live on Base, `wallet-provider-research.md` §2-§3.3) is the
one path worth prototyping later, because it's the only mechanism found that adds
smart-account capability **without** changing the address.

**Password handling, concretely:** `VEYDRIFT_KEYSTORE_PASSWORD` env var, or an interactive
non-echoing prompt if unset. Never a CLI flag — a flag lands in shell history and `ps`
output, which a prompt or env var does not. Never logged; decrypted key material lives only
inside `signAndSend`'s local scope for the duration of one signing call.

## 4. Install and update

**The only supported install path.** There are no symlinks to hand-maintain in the sense
that matters — you never edit an installed copy and hope it syncs back to `skills/`; you
always edit `skills/<name>/` and re-run this command.

```bash
npx skills add . -a claude-code -a hermes-agent
```

**Verified 2026-08-12, from this repo's root, output reproduced verbatim (trimmed of
spinner frames):**

```
$ npx skills add . -a claude-code -y
◇  Installation Summary
   ~/Verydrift/.agents/skills/veydrift-agent    copy → Claude Code
   ~/Verydrift/.agents/skills/veydrift-wallet   copy → Claude Code
◇  Installed 2 skills
   ✓ veydrift-agent (copied)  → ~/Verydrift/.claude/skills/veydrift-agent
   ✓ veydrift-wallet (copied) → ~/Verydrift/.claude/skills/veydrift-wallet
```

Both skills installed successfully with a **single agent** (`-a claude-code` alone): a full,
independent copy landed at `<repo>/.claude/skills/<name>/`, no symlink, exactly as
`docs/SPEC.md` §2.2 describes.

**A correction to `docs/SPEC.md` §2.2, found while verifying this**: adding a *second*
agent changes the mechanism. Re-running with both agents named —

```
$ npx skills add . -a claude-code -a hermes-agent -y
◇  Installation Summary
   ~/Verydrift/.agents/skills/veydrift-agent    symlink → Claude Code, Hermes Agent
   ~/Verydrift/.agents/skills/veydrift-wallet   symlink → Claude Code, Hermes Agent
◇  Installed 2 skills
   ✓ ~/Verydrift/.agents/skills/veydrift-agent   symlinked: Claude Code
   ✓ ~/Verydrift/.agents/skills/veydrift-wallet  symlinked: Claude Code
```

...installs a real copy once into a **new, shared** `<repo>/.agents/skills/<name>/`
directory, then makes `<repo>/.claude/skills/<name>` a **symlink** into it
(`.claude/skills/veydrift-agent -> ../../.agents/skills/veydrift-agent`, confirmed with
`readlink`). `docs/SPEC.md` §2.2's claim "there are no symlinks to keep in sync" is only
true for the single-agent form; the exact two-agent command the spec recommends **does**
create a symlink (of the shared-copy-plus-symlink-fanout shape, not a symlink straight back
into `skills/`, which matters less for staleness but is still a symlink). Hermes Agent
itself was not installed on the machine this was verified on (`which hermes` /
`hermes-agent` found nothing), so only the Claude Code target could be confirmed working
end-to-end; the final summary line silently drops Hermes Agent from "symlinked:" for that
reason, not because the command failed.

Both runs' installed copies (including their `node_modules`/`.venv`, ~260 MB combined) were
removed after verification — they are build artifacts of running this command, not
deliverables of this repo, and nothing under `.claude/skills/` or `.agents/` should ever be
hand-edited or committed. If you run this command yourself and see it in `git status`, that
is expected; it is not meant to be tracked.

**One real gotcha this verification found, worth knowing before you hit it yourself.** The
`skills` CLI's copy step does not respect `.gitignore` — it copies `.venv/`, `__pycache__/`,
`.pytest_cache/` and `.ruff_cache/` right along with everything else if they exist in the
source tree at install time (confirmed: they showed up inside `.claude/skills/veydrift-agent/`
after a plain `npx skills add . -a claude-code`, the very first time this was run in this
session, immediately after `uv run pytest` had populated them in `skills/veydrift-agent/`).
A copied `.venv/` is not just dead weight — **it's actively broken**: `uv run --directory
<installed-copy> vd ...` from an unrelated directory failed immediately with
`dyld[…]: Library not loaded: @executable_path/../lib/libpython3.14.dylib`, because copying
a venv (rather than recreating one) breaks its internal relative references to the Python
framework it was built against. The fix is trivial once you know it: `rm -rf
<installed-copy>/.venv` and re-run — `uv run` recreates a fresh, working venv automatically
and everything works (verified: `vd calc verify` and `vd read snapshot` both ran correctly
from `/tmp` afterward, confirming acceptance criterion 13). Practical takeaway: **run `uv
run pytest` or any other command that populates `.venv`/`__pycache__` in `skills/veydrift-agent/`
*before* deciding those directories are safe to leave around, or clean them out of the
source tree before installing** — `.gitignore` keeps them out of version control, but it
does not keep them out of what `skills add` copies.

**Confirm discoverability**: `ls <target>/skills/` (or, for a single-agent install,
`ls .claude/skills/`) should list `veydrift-agent` and `veydrift-wallet` with a `SKILL.md`
inside each. `npx skills add . -l` (list mode, no install) also prints both skills' full
descriptions without touching disk — useful for confirming the frontmatter parses
correctly before installing anything.

To update after editing anything under `skills/`, re-run the same command — it's a fresh
copy (or a fresh symlink target), not a merge.

## 5. Running one tick, per harness

| Harness | How |
| --- | --- |
| Claude Code, interactive | `/loop 10m` driving `vd tick --format md` (`references/scheduling.md`, WP3) |
| Claude Code, unattended | `claude -p "run a veydrift tick"` from `launchd` |
| Hermes | register `vd tick` on Hermes' own scheduler at `policy.cadence.economy_minutes` |
| Bare OS | `assets/com.veydrift.agent.plist.template` — launchd, `StartInterval`. Ships as a template with a documented install command; not installed by default |

**First-time setup**, then the tick itself — verified end to end against the live API on
2026-08-12, output reproduced exactly (Rich's box-drawing characters included):

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
$ echo $?
0
```

`guards: 13/16 pass (block)` is expected and correct at tier 1: the `tier` gate itself
blocks (`"startBuildingUpgrade requires tier >= economy; policy tier is advisor"`), which
is exactly the mechanism that makes tier 1 safe by construction rather than by discipline —
the decision was `BLOCK`, not `ALLOW`, so nothing downstream of guard ever runs. This one
run also independently confirmed acceptance criterion 5 in full: it wrote the pretty report
above, appended one entry to `$VEYDRIFT_HOME/logs/proposals.jsonl` (full guard verdict list
and the built calldata included) and one to `logs/strategy.md`, and **`logs/actions.jsonl`
did not exist afterward** — nothing is ever logged as executed at tier 1, because nothing
ever executes.

`vd tick`'s own docstring lays out the nine steps this wraps: load+validate policy →
killswitch check → reconcile pending txs → snapshot → plan → guard → (if `ALLOW` and
`tier >= economy` and not `--dry-run`: `walletctl build → confirm → send`, await receipt,
then await indexed) → log → pretty report. `--dry-run` is the default at tier 1 and cannot
be turned off there — `vd tick --help` confirms this directly ("Always true at tier 1").
`vd tick --readiness` prints the promotion evidence described in §9 instead of running a
tick; `vd log --digest 24h`, `vd log tail-proposals`, `vd log tail-actions` and `vd log
strategy` read back what accumulates in `$VEYDRIFT_HOME/logs/` over many ticks. Note that
`$VEYDRIFT_HOME` defaults to the same path (`~/.veydrift`) for every invocation on a given
machine regardless of which harness or session runs it — if more than one agent session is
exercising this skill against the same machine at once (as happened while this document was
written), their tick counts, proposal logs and any `KILLSWITCH` file are genuinely shared
state, not isolated per-session. Point `VEYDRIFT_HOME` at a different directory for any
testing you don't want mixed into the real account's history.

**Before `tick`/`guard`/`log` existed** in this tree (earlier in the session this document
was written in), the read → plan pipeline below was already independently runnable and
verified — it's what `tick` wraps internally, and is still the right thing to run manually
when you want a proposal without touching `$VEYDRIFT_HOME` at all:

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

Completed in ~2s, 466 bytes — well inside the acceptance criteria's <10s / ≤2KB. Feed a
`--json` snapshot into the planner (offline from here on, no network calls):

```bash
$ uv run --directory skills/veydrift-agent vd read snapshot --wallet 0x224a...fa0f --json --out /tmp/snap.json
$ uv run --directory skills/veydrift-agent vd plan run --snapshot /tmp/snap.json --policy $VEYDRIFT_HOME/policy.json
╭──────────────────────────── vd plan run -- build ────────────────────────────╮
│ rule:      6:building-queue-empty                                            │
│ kind:      build                                                             │
│ function:  startBuildingUpgrade(planet=664, entity=3)                        │
│ target:    level 1                                                           │
│ cost:      M 75  C 30  D 0                                                   │
│ why:       Metal Mine 0->1 would need 11 energy against 0 produced.          │
│ Energy-first invariant: Solar Plant's marginal cost per energy point is      │
│ cheaper here than one more Solar Satellite (satellite energy/unit=4).        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

This is exactly what `docs/SPEC.md` §9 acceptance criterion 4 asks for: an energy-first
opener, never a Solar Satellite, on planet 664's real current state
(`skills/veydrift-agent/references/strategy-playbook.md` §6 is the full derivation by
hand). Note: `vd plan run` resolves `--snapshot`/`--policy` relative to the shell's `cwd`,
not `__file__` — those are user-supplied paths, unlike the skill's own bundled references.

Cross-checking the formula layer against live data, independent of any snapshot:

```bash
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

And on the wallet side, without configuring a real key (`walletctl verify-abi` needs no
provider at all):

```bash
$ cd skills/veydrift-wallet && npx tsx src/cli.ts verify-abi
pinned commit:          701bed3578cff4d134657c714c599dbdb55a4b6a
pinned ABI hash:        sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99
live deploymentCommit:  701bed3578cff4d134657c714c599dbdb55a4b6a
live deploymentAbiHash: sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99
commit match:           true
ABI hash match:         true
$ echo $?
0
```

matching `docs/SPEC.md` §9 acceptance criterion 6 exactly. `walletctl status` correctly
refuses to run without `VEYDRIFT_KEYSTORE` set — that's the expected failure mode with no
provider configured, not a bug:

```bash
$ npx tsx src/cli.ts status
status failed: "keystore" provider selected but VEYDRIFT_KEYSTORE is not set (path to an
encrypted EIP-2335/geth JSON keystore file).
```

Both skills' test suites were run in full, more than once, while writing this document:
`uv run pytest -q` (from `skills/veydrift-agent/`) → **187 passed**, consistently across
repeated runs — one earlier run mid-session showed 2 failures in `test_guard.py` that did
not reproduce on immediate re-run or in isolation, most likely a transient artifact of a
concurrent build session editing that file at the same moment, not a real defect; `npx
vitest run` (from `skills/veydrift-wallet/`) → **83 passed**, including the test literally
named for acceptance criterion 11 ("both providers return the SAME address for the SAME
key material").

## 6. The safety contract

**What this codebase will never do, by construction, not by configuration:**

- Submit a transaction without an explicit human `--confirm` on the exact `walletctl send`
  command line. No env var, no policy field, no flag makes this implicit
  (`skills/veydrift-wallet/references/tx-safety.md`).
- Propose or execute combat (`Attack`, `AcsAttack`, `MissileAttack`, `Intercept`) at any
  tier. `policy.json`'s `allow_combat` is read and then ignored everywhere.
- Sign or submit anything outside the live Veydrift contract address, or outside the
  current tier's allowed selector set — enforced twice, independently, by `vd guard`
  (agent-side, WP3) and `checkAllowlist` (wallet-side, always re-run regardless of what the
  agent already checked).
- Advance its own tier. Only a human edit of `policy.json`'s `tier` field does that.
- Write a private key, mnemonic, keystore or API secret to any tracked file, or to any log
  (`log.py` scrubs `0x[0-9a-fA-F]{64}` patterns that aren't a known tx hash, per
  `docs/SPEC.md` §5.9).

**Escalation list** — situations `vd guard`/`vd plan` are designed to hand to a human
rather than act on (`docs/SPEC.md` §4, §5.5):

| Trigger | What happens |
| --- | --- |
| Any incoming hostile fleet (`fleet-visibility.incoming`) | ESCALATE, no proposal at all |
| Live `deploymentAbiHash` drifts from the pinned hash | Block every write; `walletctl verify-abi` surfaces this before any `send` |
| `/health` unhealthy for `on_health_unhealthy_minutes` (default 30) | ESCALATE |
| Same action reverts `on_revert_count` times (default 2) | ESCALATE, do not retry blindly |
| A single action's cost exceeds `escalate_above_pct_of_resources` (default 25%) of current holdings | ESCALATE rather than BLOCK — this one is a judgment call, not a hard stop |

## 7. Where logs live, and how to read them

All under `$VEYDRIFT_HOME/logs/`, never inside either skill's tree:

| File | Contents | Mutability |
| --- | --- | --- |
| `proposals.jsonl` | every proposal, full guard verdict list, and the built calldata — whether or not it was ever submitted | append-only |
| `actions.jsonl` | **executed only** — tx hash, gas, block, before/after state, indexed-at | append-only |
| `ticks/<iso>.md` | the pretty report for one tick | one file per tick |
| `strategy.md` | rationale, plan revisions, escalations, human decisions | append-only |

`vd log --digest 24h` (WP3) produces the daily rollup: builds, research, resources
produced, gas spent, and **everything refused, with reasons** — the refusals are the part
worth reading first when auditing a stretch of ticks, not the successes.

## 8. What this repository does not verify — read before trusting a claim about it

Carried forward from `docs/SPEC.md` §11, because it is the single easiest thing to overstate
about this project:

- **No transaction has ever been submitted to Veydrift from this codebase.** The write path
  is built, allowlisted, simulated and fixture-tested (270 passing tests across both
  skills as of this writing) — never executed against mainnet. The first real submission is
  a human decision at the T1→T2 promotion, not something this codebase has done on its own
  initiative, ever.
- **Cost scaling, queue behaviour and lazy settlement above level 0 are unobserved.** The
  account backing planet 664 has taken zero on-chain actions since settlement — every
  building/tech level is 0, every queue is `null`. Formulas are verified against source and
  against live level-0 data; nothing here has watched a live cost, queue, or lazy-settlement
  path respond to an actual level-up.
- **`protectedResources` semantics remain unconfirmed.** No loot or raid-profitability model
  is built on it anywhere in this codebase.
- **This is an advisor, not a proven autonomous system.** At tier 1 — the only tier this
  account has ever run at — the agent proposes and pretty-prints; a human reads the
  proposal and decides. Nothing in the commands verified in §5 above required or exercised
  any autonomous write.

## 9. Promotion procedure: T1 → T2 → T3

`vd tick --readiness` is the tool for this — verified running (§5): it reports tick count,
uptime, proposals made, how many a human actually executed, **divergences between proposal
and human action**, which guardrails fired and why, and cumulative gas spent. Read its
`divergence` line carefully — it counts proposals with no matching `actions.jsonl` entry,
and its own output says plainly that a human executing a T1 proposal *by hand, outside this
tool* is not observable through it. A clean `vd tick --readiness` report is necessary but
not sufficient evidence; the steps below are what actually justifies the edit.

**T1 → T2 (`advisor` → `economy`):**

1. At least 24 hours of continuous T1 ticks, run through whichever harness (§5).
2. A human reads `$VEYDRIFT_HOME/logs/strategy.md` in full — not just the latest tick — and
   confirms the *reasoning*, not just that proposals looked plausible one at a time.
3. Check `logs/proposals.jsonl` for guardrail fires: a clean run with zero fires is weaker
   evidence than a run where guards fired correctly and the agent respected them — a green
   tick count alone is explicitly called out in the spec as a bad promotion signal.
4. Only then, a human hand-edits `$VEYDRIFT_HOME/policy.json`, setting `tier: "economy"`.
   No command does this for you.
5. Confirm `walletctl verify-abi` passes immediately before the first real `send` — ABI
   drift is exactly the kind of thing that can happen silently between the review and the
   first live action.

**T2 → T3 (`economy` → `operator`):** the same shape, at a higher bar — **at least 7 days**
of clean T2 operation (real submissions, not just ticks), reviewed the same way, before the
same manual `policy.json` edit to `tier: "operator"`. T3 additionally unlocks
`launchFleetMission` for Transport/Deploy/Harvest only; combat mission types remain
unreachable regardless of tier (§1).

**Never**: promote on tick count alone, promote without reading `strategy.md`, or promote
while any guard is failing intermittently rather than consistently passing.

## 10. Subagent-definition caveat

`.claude/agents/veydrift-builder.md` and `.claude/agents/veydrift-judge.md` define two
Claude Code subagents (`model`/`effort` in frontmatter) used to build and review this
repository's work packages.

**A new definition is not available immediately, but it does not require a restart
either.** Observed directly during the build this repo was produced by: both files were
written mid-session, and an `Agent` call naming `veydrift-builder` failed moments later
with `Agent type 'veydrift-builder' not found`, listing only the agents present at session
start. Later in the same session, with no restart, both types appeared and became usable.
So the registry does refresh — just not synchronously with the write.

Practical consequence for an orchestrator: **do not block on a definition you just
wrote.** Either create subagent definitions before the session that will use them, or fall
back to `subagent_type: general-purpose` with an explicit `model` override, folding the
definition's system prompt into the task prompt. That fallback costs you the `effort`
frontmatter — the `Agent` tool's parameters can set the model but not the reasoning effort,
which only a definition file can pin — so a fallback run inherits the parent session's
effort level instead.

An earlier revision of this section stated flatly that the registry "does not hot-reload"
and that a fresh session was required. That was written from the first half of the
evidence and is wrong; it is corrected here rather than deleted, because the failure mode
it describes is real and the misdiagnosis is the easy one to repeat.

## 11. Pointers into `docs/`

- `docs/SPEC.md` — the full implementation spec, work-package breakdown, and every
  acceptance criterion this repo is checked against.
- `docs/RESEARCH-ADDENDUM.md` — contract- and backend-source-derived corrections to
  everything written before it: the real API route list, the real `Defense`/
  `FleetMissionType` enums, the ABI hash, the write-entrypoint list.
- `docs/wallet-provider-research.md` — every wallet-provider candidate evaluated against
  the address-binding constraint in §3 above, and why the shipped `keystore` provider is
  still the recommendation.
- `docs/NOTES.md`, `docs/veydrift-agent-prompt.md`, `docs/veydrift-agent-resources.md`,
  `docs/veydrift-briefing.html` — earlier inputs this project was built from; superseded
  in places by the addendum (see that document's own inline corrections) but kept for
  provenance, not deleted.
- `skills/veydrift-agent/SKILL.md` and `skills/veydrift-wallet/SKILL.md` — the two skills'
  own entry points, each with a routing table into its `references/` directory.
