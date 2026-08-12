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
(`npx skills add . -a claude-code -a hermes-agent` — see `README.md` at the repo root).
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
| 2 `economy` | everything in scope | `startBuildingUpgrade`, `startResearch`, `resolveFleetMission`, `settlePlanet`, `startDefenseProduction`, `startShipProduction` | ≥24h of T1 ticks, human review of `strategy.md`, human edit of `policy.json` |
| 3 `operator` | everything in scope | T2 + `launchFleetMission` for Transport(0)/Deploy(1)/Harvest(4) only | ≥7 days clean T2, human edit |

**Combat is unreachable at every tier by code, not by config.** `policy.json` has an
`allow_combat` key that every code path deliberately ignores — enabling `Attack`,
`AcsAttack`, `MissileAttack` or `Intercept` requires editing source, not flipping a flag.

Even at tier 1, `vd plan run` produces a complete, ready-to-submit transaction description
— that's what makes a T1→T2 promotion decision evidence-based instead of a guess. See
`README.md`'s promotion procedure for what evidence a human should actually look at before
editing `tier`.

## The tick contract

One tick is meant to run as: `load policy → killswitch check → reconcile pending txs →
snapshot → plan → guard → (if allow: walletctl build/simulate/send, tier ≥ 2 only) → log →
pretty report`. `vd tick` is the entrypoint that runs all of this atomically and
idempotently, lockfile-protected under `$VEYDRIFT_HOME` (WP3's `state.py`/`tick.py`/
`guard.py`). **As of this writing, `guard`/`tick`/`log` may not be wired into `vd --help`
yet** in whatever copy you're running against — that's expected if this document reached
you before that work package landed; `vd doctor` reports exactly what's live. Until then,
the read → plan pipeline is fully usable standalone:

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

`vd plan run` evaluates these in order; the **first match wins**, and rung 9 always fires
if nothing above it did (`Action.rationale` is never empty):

0. KILLSWITCH present → HALT
1. `/health` not ok → NO-OP, reason recorded
2. pending tx unreconciled → NO-OP, reconcile first
3. a mission has been Resolving > 60s → `resolveFleetMission` (permissionless, free)
4. incoming hostile fleet → ESCALATE, no proposal
5. a resource is within N hours of its storage cap → spend it, or build the matching storage
6. building queue empty → next build
7. research queue empty → next research
8. shipyard idle AND economy on track → ships/defense, only if policy allows
9. otherwise → NO-OP with an explicit reason

Rungs 6-8's actual choices — which mine, which energy source, which research — are
**derived from the planet's live traits** (temperature, multipliers, current levels), not
hardcoded per planet. `references/strategy-playbook.md` is the full walkthrough of that
derivation, worth reading before second-guessing a specific proposal.

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
`health` · `index_lag` · `affordability` (live `cost` vs `resourcesAsOfNow`) · `energy`
(post-action `produced ≥ required`) · `storage_overflow` · `fields` · `reserve` ·
`gas_per_tx` / `gas_per_day` · `eth_floor` · `value_ceiling` (spend over
`escalate_above_pct_of_resources` → ESCALATE, not BLOCK) · `idempotency` · `revert_streak`

Two of these are re-checked independently by `veydrift-wallet`'s own allowlist
(`skills/veydrift-wallet/references/tx-safety.md`) — `tier`/selector and `address` — on
purpose: a fully compromised copy of this skill still cannot make `walletctl` sign
something outside Veydrift or outside its tier. Defense in depth, not redundancy for its
own sake.

## Non-goals — things this skill will never propose, at any tier

Combat and ACS (code-level block, not config), alliances, migration, referrals, NFT
burns, the ERC-20 market bridge, and any raid-profitability recommendation —
`protectedResources`' semantics are unconfirmed (`docs/RESEARCH-ADDENDUM.md` §6), so
nothing here builds a loot model on it. If asked to plan a raid or an attack, say plainly
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
| Exact guardrail rules, current wiring status, what blocks vs. escalates | `references/guardrails.md` (WP3) |
| How `vd tick` is driven under Claude Code, Hermes, and bare launchd | `references/scheduling.md` (WP3) |
| Contract vs. API vs. `docs.md` disagreements, the full write-entrypoint list, canonical enums | `docs/RESEARCH-ADDENDUM.md` |
| The full spec this codebase was built against | `docs/SPEC.md` |
| Repo map, setup/test commands, invariants, ABI-pinning procedure, known gaps | `AGENTS.md` (repo root) |
| Install, usage, promotion procedure, key custody, what's never been verified | `README.md` (repo root) |

If a reference file doesn't answer the question and you're tempted to compute a cost
formula, stop: **`calc.py` deliberately contains no cost-scaling function.** Live cost at
the current level always comes from the API's `cost` field
(`references/formulas.md`'s header explains why recomputing it is exactly how affordability
checks go wrong).
