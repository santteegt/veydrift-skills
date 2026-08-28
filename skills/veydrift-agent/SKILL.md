---
name: veydrift-agent
description: Reads Veydrift game state, proposes the next build/research/fleet action for a Veydrift planet, and explains build orders and the energy-first strategy. Use this whenever the user is playing Veydrift, mentions a Veydrift planet (e.g. planet 664), asks "what should I build next," "run a tick," "check my queues/energy/resources," "is my planet under attack," or anything about Veydrift's tier system (advisor/economy/operator), guardrails, or promotion from T1 to T2. Trigger even if the user just says "check Veydrift" or pastes a planet id/coordinates like "7:181:14" without naming the skill. Do NOT use this for OGame or other browser-strategy games in general, or for generic "how does blockchain gaming work" questions that aren't about this specific project — this skill is Veydrift-specific and knows nothing generic. For signing, submitting, or wallet-provider questions, hand off to `veydrift-wallet` instead; this skill never signs anything.
---

# veydrift-agent

Reads Veydrift's HTTP API, runs deterministic calculators against it, and proposes
**zero or one** next action per tick — a pydantic `Action`, never signed calldata sent
anywhere. `veydrift-wallet` (a separate skill) is the only thing in this project that ever
builds real transaction bytes or submits anything onchain. If a user's request involves
signing, `walletctl`, private keys, keystores, or transaction submission, route to
`veydrift-wallet` and its `references/` instead of guessing here.

Everything this skill knows about Veydrift's contract and API was read from the deployed
contract source and live API responses, not from `docs.md` (which has confirmed errors —
see `references/entity-ids.md` §9) or genre convention (OGame's Defense/Technology
ordering does **not** match Veydrift's — `references/entity-ids.md` §4).

Single entrypoint: `uv run --directory <path-to-this-skill> vd <subcommand>`. Paths are
resolved from `__file__`, so this works from any cwd once installed
(`npx skills add . -a claude-code -a hermes-agent`, run from the source repository).
Run `vd doctor` first if unsure which subcommands are wired up in the copy you're running
against — this skill is built across parallel work packages and a partially-built tree is
expected to still run the parts that exist.

## The tier model — the single most important thing to get right

Tier lives in one field, `policy.json`'s `tier`. **No code path in this skill ever
advances it.** Only a human editing that file moves the account forward. Read the tier
before trusting any answer about what the agent "will do" — the same proposal means very
different things at each tier.

| Tier | May propose | May submit (via `walletctl`, separately gated) | Gate to enter |
| --- | --- | --- | --- |
| 1 `advisor` (default) | everything in scope | **nothing, ever** | — |
| 2 `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `startDefenseProduction`, `startShipProduction` | ≥24h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0)/Deploy(1)/Colonize(2)/Harvest(4) unconditionally, plus Attack(3) with `policy.actions.allow_combat=true` | ≥7 days clean T2, human edit |

**Most of combat is unreachable at every tier by code, not by config.** `AcsDefend`,
`Intercept`, `MissileAttack`, `AcsAttack` and `DefenseHold` require editing source, not
flipping a flag, regardless of `policy.json`. `Attack` is the one exception since
2026-08-28: `policy.json`'s `allow_combat` key is a real gate for it at `operator` tier —
though no ladder rung proposes an Attack action, so this widens what can be sent by hand,
not what the agent does on its own.

Even at tier 1, `vd plan run` produces a complete, ready-to-submit transaction description
— that's what makes a T1→T2 promotion decision evidence-based instead of a guess.
`vd tick --readiness` prints the evidence directly: tick count, uptime, proposals made, how
many a human actually executed, **divergences between proposal and human action**, which
guardrails fired and why, and cumulative gas spent. A clean report is necessary but not
sufficient — before hand-editing `policy.json`'s `tier` field:

1. At least 24 hours of continuous T1 ticks.
2. A human reads `$VEYDRIFT_HOME/logs/strategy.md` **in full**, not just the latest tick,
   and confirms the reasoning — not just that individual proposals looked plausible.
3. Check `logs/proposals.jsonl` for guardrail fires: a run where guards fired correctly
   and the agent respected them is *stronger* evidence than a clean run with zero fires —
   a green tick count alone is a bad promotion signal on its own.
4. Only then, hand-edit `tier` to `"economy"`. No command does this automatically.
5. Confirm `walletctl verify-abi` passes immediately before the first real `send` — ABI
   drift can happen silently between the review and the first live action.

T2 → T3 is the same shape at a higher bar: at least **7 days** of clean T2 operation (real
submissions, not just ticks). Never promote on tick count alone, without reading
`strategy.md`, or while any guard fails intermittently rather than consistently.

## The tick contract

One tick runs as: `load policy → killswitch check → reconcile pending txs → snapshot →
plan → guard → (if allow: walletctl build/simulate/send, tier ≥ 2 only) → log → pretty
report`. `vd tick` is the entrypoint that runs all of this atomically and idempotently,
lockfile-protected under `$VEYDRIFT_HOME`. `vd doctor` reports which subcommands are wired
in the copy you're running — useful if you're working from a partially-updated checkout.
The read → plan pipeline underneath is also fully usable standalone, without a policy file
or `$VEYDRIFT_HOME` at all:

```bash
# 1. Get a snapshot (network — needs a live wallet address)
uv run --directory skills/veydrift-agent vd read snapshot --wallet 0x224a...fa0f --json --out /tmp/snap.json

# 2. Feed it to the planner with a policy file (offline, no network)
uv run --directory skills/veydrift-agent vd plan run --snapshot /tmp/snap.json --policy $VEYDRIFT_HOME/policy.json
```

`vd plan run` prints the rule that fired, the function name, target, live-quoted cost, and
a rationale that states the exact numbers behind the decision — read it before trusting
it; the reasoning is meant to be checkable by hand (`references/strategy-playbook.md` §11
is a checklist for exactly that).

`--dry-run` is the default at tier 1 and cannot be disabled there once `tick` is wired.
Nothing this skill produces ever reaches the chain without `walletctl send --confirm`,
which is `veydrift-wallet`'s decision, not this skill's.

## The decision ladder

`vd plan run` evaluates these in order; the **first match wins**, and the pipeline always
falls back to an explicit NO-OP if nothing matched (`Action.rationale` is never empty).
Rungs 0-4 are vetoes; rungs 5-9 are a four-band candidate pipeline (`candidates.py`,
2026-08-16 — see `references/strategy-playbook.md` for the full derivation):

0. KILLSWITCH present → HALT
1. `/health` not ok → NO-OP, reason recorded
1b. game paused → ESCALATE (or NO-OP if `escalation.on_game_paused` is false) —
    `gameMaintenance.paused` from `/health`; any write would revert during a
    chain-side maintenance pause
2. pending tx unreconciled → NO-OP, reconcile first
3. a mission has been Resolving > 60s → `resolveFleetMission` (permissionless, free)
4. incoming hostile fleet → ESCALATE, no proposal
5-9. generate → filter → score → select, four bands in order:
     1. deadline-driven — storage overflow: spend it, or build the matching storage
     2. economically scored — building upgrade, ascending payback hours
     3. policy-declared — research, then ships/defense, gated on economy-on-track
     4. unlock-chain (rung 8b) — the shallowest buildable prerequisite toward a locked
        `ship_targets`/`defense_targets`/`research_priority` entry, only when nothing
        above found anything at all — see `references/strategy-playbook.md` §12
     else → NO-OP with an explicit reason

The economic band's actual choices — which mine, which energy source — are **derived from
the planet's live traits** (temperature, multipliers, current levels), not hardcoded per
planet. `references/strategy-playbook.md` is the full walkthrough of that derivation,
worth reading before second-guessing a specific proposal. Every proposal also carries
`alternatives`: the runner-up options considered and why each lost — informational only,
never a decision input.

**The one invariant worth internalizing on its own:** before proposing any mine upgrade,
the planner computes energy `required` at the *post-upgrade* level and compares it to
current `produced`. If the upgrade would push `required` past `produced`, it proposes an
energy building instead — never the mine. This is why a completely fresh planet's very
first proposal is a Solar Plant, not a mine (`references/strategy-playbook.md` §3, §6).
The gap between mine level and the Solar Plant level needed to support it **widens** as
levels climb (`references/formulas.md` §8's crossover table) — there is no fixed offset
that stays correct, which is exactly why this is computed fresh every tick instead of
hardcoded.

## Safety gates, in summary

`vd guard` (WP3) evaluates every gate below and reports a full per-gate verdict list —
never short-circuited, so a passing tick is as auditable as a blocked one. Full rules and
current wiring status: `references/guardrails.md`.

`killswitch` · `tier` (action's function ∈ tier's allowed set) · `address` (destination ∈
live `/runtime-config`) · `abi_hash` (live hash == pinned, else block every write) ·
`health` · `game_paused` (`gameMaintenance.paused` — BLOCKs unconditionally, fail-closed
on missing data) · `index_lag` · `affordability` (live `cost` vs `resourcesAsOfNow`) · `energy`
(post-action `produced ≥ required`) · `storage_overflow` · `fields` · `reserve` ·
`gas_per_tx` / `gas_per_day` · `eth_floor` · `value_ceiling` (spend over
`escalate_above_pct_of_resources` → ESCALATE, not BLOCK) · `idempotency` · `revert_streak`

Two of these are re-checked independently by `veydrift-wallet`'s own allowlist — `tier`/
selector and `address` — on purpose: a fully compromised copy of this skill still cannot
make `walletctl` sign something outside Veydrift or outside its tier. Defense in depth, not
redundancy for its own sake. (If the `veydrift-wallet` skill is also installed, its
`references/tx-safety.md` has the exact checks; that skill enforces this independently of
whether it's present, so nothing here depends on it.)

## Non-goals — things this skill will never propose, at any tier

Combat and ACS (code-level block, not config), alliances, migration, referrals, NFT
burns, the ERC-20 market bridge, and any raid-profitability recommendation —
`protectedResources`' semantics are unconfirmed, so nothing here builds a loot model on it. If asked to plan a raid or an attack, say plainly
that this skill cannot do that by design, not just "wasn't asked to."

## Routing table — read these, don't guess

Formulas, ID tables, and route tables live in `references/` on purpose — they don't
belong inlined here, and re-deriving them from memory is exactly how prior docs got the
Defense enum order and the Deathstar/Dreadstar naming wrong (`references/entity-ids.md`
§9). Scripts under `src/` are run, never read into context.

| Question | Read |
| --- | --- |
| Which API route does `vd read <target>` call, what does it return, what's the health-gating rule, what are the exit codes? | `references/api-routes.md` |
| What's the exact formula behind a number `vd calc`/`vd plan` produced — energy, production, duration, distance, fuel, storage cap? | `references/formulas.md` |
| Building/Technology/Ship/Defense/FleetMissionType/Resource id → name, and the fleet-tuple index shift | `references/entity-ids.md` |
| Why did the planner propose *this specific* action — the full derivation, worked examples for a cold and a hot planet, what's unobserved | `references/strategy-playbook.md` |
| Which deployed contract function does a given `Action.function` map to, and the traps in calling it (overloads, revert conditions, functions that look like reads but aren't) | `references/contract-writes.md` |
| Exact guardrail rules, current wiring status, what blocks vs. escalates | `references/guardrails.md` |
| How `vd tick` is driven under Claude Code, Hermes, and bare launchd | `references/scheduling.md` |
| When and how to supply your own `Action` instead of the planner's choice (`vd tick --action`) | `references/manual-action-override.md` |

Every row above is a file bundled with this skill — it travels with the install and is all
you need. (Any `docs/*.md` mention elsewhere in this skill's files is a build-time
provenance note from this skill's source repository, not a file this install carries.)

If a reference file doesn't answer the question and you're tempted to compute a cost
formula, stop: **`calc.py` deliberately contains no cost-scaling function.** Live cost at
the current level always comes from the API's `cost` field
(`references/formulas.md`'s header explains why recomputing it is exactly how affordability
checks go wrong).
