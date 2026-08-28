# Manual action override — `vd tick --action`

`plan_next_action` (`plan.py`) chooses the Action for every tick, and most of the time
that's exactly right — its candidate generation, scoring, and the energy-first invariant
are the whole point of this skill. `vd tick --action <file>` exists for the narrower case
where **your own reasoning about the best next move genuinely diverges from what the
planner would choose, and that divergence is blocking real strategy progress** — a
situational move the planner has no rung for at all (e.g. topping off storage while
blocked on a different resource), not a general substitute for its judgement. If you find
yourself reaching for this on every tick, that's a sign a real planner rung is missing,
not that the override is working as intended — consider it, or say so, rather than making
this your default path.

## What this is not

This is **not** the "call `walletctl` directly, bypass `vd tick` entirely" pattern. That
pattern skips every gate in `guard.py` (killswitch, affordability, energy, reserve floor,
gas ceilings, revert-streak escalation, idempotency, colony cap, and more) along with the
tick lockfile and the entire audit trail — `walletctl`'s own allowlist re-check
(`references/tx-safety.md` in the `veydrift-wallet` skill) covers signing-layer safety
only (contract address, selector-tier, `value==0`, chainId, mission-type), not any of
that. `--action` is the supported alternative: it substitutes **only** which `Action` is
evaluated, never anything about how it's evaluated.

## The gate: `policy.strategy.allow_agent_action_override`

Lives under `strategy`, alongside `ship_targets`/`research_priority`/`enable_crawler` —
a strategic-override lever, not a wallet-engine or top-level account setting. Default
`false`. `vd tick --action <file>` refuses outright (non-zero exit, nothing logged, no
state touched) unless this is `true` — set it deliberately, the same way you'd set
`wallet_engine.require_confirmation` or promote a tier, not as a default-on convenience.

```json
{
  "strategy": {
    "allow_agent_action_override": true
  }
}
```

## The file: an `Action` JSON

Same shape `plan_next_action` itself produces — see `schemas/action.schema.json` for the
full field list, or `models.py`'s `Action` class for the annotated version. A minimal
on-chain example:

```json
{
  "kind": "build",
  "function": "startBuildingUpgrade",
  "planet_id": 664,
  "entity_id": 3,
  "entity_name": "Solar Plant",
  "target_level": 11,
  "rule": "operator override",
  "rationale": "storage is about to overflow metal while blocked on deuterium for the mine upgrade"
}
```

**`Action`'s base model config is `extra="ignore"`, not `forbid`** (unlike `Policy`,
which is `extra="forbid"`) — a typo'd field name in your file is silently dropped, not
rejected. Validate against the schema by hand before relying on a field you added; the
gates downstream (§"What still runs" below) will fail closed on a field that's simply
missing, but a *misspelled* one looks identical to "not set" and won't warn you.

**`source` is not yours to set.** Whatever the file says, `tick.py` forcibly overwrites it
to `"manual_override"` after validation — a stray `"source": "planner"` in the file can
never spoof it. This is what makes `proposals.jsonl`/`actions.jsonl` auditable: every
record says plainly whether the planner or an operator chose it.

## What still runs — everything

Once your `Action` is loaded, it flows through the **exact same** `_run_tick` pipeline a
planner-chosen action does. Nothing below is skipped, softened, or specific to an
override:

- Every one of `guard.py`'s 20 gates (`references/guardrails.md`) — killswitch, tier,
  affordability, energy, fields, reserve, gas, eth_floor, value_ceiling, idempotency,
  revert_streak, colony cap, ship-availability, fleet slots, and the rest.
- `wallet_engine.require_confirmation` — a human still has to run the printed
  `walletctl send --confirm` command themselves if that's `true`.
- The tier ceiling — an override still can't send anything above what `policy.tier`
  allows, exactly like a planner-chosen action.
- `tick_lock()` — an override tick still holds the same lockfile a scheduled tick would,
  so it can't race a concurrent `vd tick` and corrupt `agent_state.json`.
- Full audit logging — `proposals.jsonl`, `actions.jsonl`, `logs/ticks/*.md`, dedup
  fingerprinting, revert/gas-ledger accounting.

The only thing that changes is which `Action` object guard evaluation and
build/simulate/send are handed.

## The disagreement is captured automatically — you don't have to write it down

Whenever `--action` fires, `tick.py` also calls `plan_next_action` itself, purely for
comparison (a pure, no-network call over the snapshot already in hand — costs nothing
extra, and its result is never executed). Both choices are reported together, in the same
tick, in three places:

1. **`logs/strategy.md`** — an `OVERRIDE:` line naming both the operator's action and what
   the planner would have proposed instead, unconditionally (an override is never routine
   enough to suppress, unlike the usual structural-tier-block narration).
2. **The tick's own printed report** — an `OVERRIDE:` line in the same stdout/`--format
   json` output as everything else that tick produced.
3. **`proposals.jsonl`** — an `"override"` key on the logged record, carrying both
   `operator_action` and `planner_would_have_proposed` (`rule`/`function`/`rationale`).

You only need to write a good `rationale` on your own `Action` explaining *why* you're
overriding — the comparison against what the planner would have done is never left to
your prose to capture correctly.

## When *not* to reach for this

- If the planner is simply wrong about something durable (a formula, a threshold, a
  missing rung) — fix `plan.py`/`candidates.py`, don't paper over it tick after tick with
  the same override.
- If you just want to skip ahead impatiently and the planner's choice wasn't actually
  blocking anything — that's not the divergence this exists for.
- If you want to bypass a *guard* gate specifically (not the planner's choice of action)
  — you can't, and shouldn't be able to: this only ever changes which `Action` is
  evaluated, never how.
