# The jev decision engine

An alternative to the fixed-order ladder (`SKILL.md`, "The decision ladder"). It compares
**every legal candidate across every band** in one pass, using TypeSafe's Jev model to judge
each against a plain-language strategy, and falls back to the ladder whenever it cannot give
a confident answer. Everything it decides is still an ordinary `Action`, finalized the way the
ladder finalizes one, and checked by the same guard and the same wallet allowlist.

Sources: `src/veydrift_agent/engine.py` (dispatch, `vd engine`), `jev_engine.py` (state,
questions, composition, gates), `jev.py` (the TypeSafe client), `candidates.py`'s
`collect_pool` (the candidate pool), `plan.py`'s `veto_action`/`deadline_action`/
`RULE_BY_FAMILY`. Tests that pin each rule below are named where they matter.

## Contents

1. When to use it, and when not to
2. Enabling it
3. Decision flow
4. The candidate pool
5. What is sent to TypeSafe, and what never is
6. The questions and the composite score
7. Gates and fallbacks
8. What gets logged
9. CLI
10. Tuning
11. Limits
12. Why cross-planet scoring is acceptable here
13. Safety

## 1. When to use it, and when not to

The ladder is deterministic, offline, and checkable by hand (`strategy-playbook.md`). Its
cost is structural: bands run in a fixed order, so an idle building queue nearly always wins
before research, ships, defense or an unlock chain is even considered, and nothing in the
policy can weigh one band against another (`strategy-playbook.md` §13 and the diagnostic in
`opportunities.md` describe the symptom).

Use the jev engine when you want that weighing done for you, driven by a stated intent
("push research, keep defense modest, no attacks"), most often on an account with several
planets or several declared targets. Stay on the ladder when you want a decision you can
re-derive from the numbers alone, no network dependency, or byte-identical behaviour tick to
tick. The ladder is always the default (`policy.engine.kind = "ladder"`), and it remains the
fallback under the jev engine.

## 2. Enabling it

`policy.json` gains one top-level block. The default (`assets/policy.example.json`) is the
ladder; switch the kind and write an intent:

```json
"engine": {
  "kind": "jev",
  "jev": {
    "model": "jev-latest",
    "intent": "Grow research first, keep two planets balanced, keep a modest defense, never attack.",
    "weights": { "fit": 0.3, "urgency": 0.25, "focus": 0.15, "economy": 0.25, "threat": 0.05 },
    "min_confidence": 0.5,
    "min_confidence_high_stakes": 0.75,
    "min_margin": 0.03,
    "timeout_s": 5.0,
    "max_candidates": 24,
    "payback_reference_hours": 24.0,
    "high_stakes_only_when_idle": true,
    "allow_hold": false
  }
}
```

- The API key comes from the **`TYPESAFE_API_KEY` environment variable only**. It is never a
  `policy.json` field, and `log.py` scrubs it from every log by default. Unset, every jev
  decision falls back to the ladder with `fallback_reason: "missing_key"`.
- `typesafe-sdk` is a normal dependency of this skill (installed by `uv`); it is imported
  lazily on the first jev call, so a ladder user never loads it.
- The TypeSafe base URL is a constant in `jev.py`, not a policy field, and an environment
  override of it is ignored.
- `engine` is `extra="forbid"` throughout: an unknown key, including a typo inside `jev`, is a
  hard error, not a silent default. `intent` is capped at 1000 characters; empty uses a
  built-in rubric (balanced, energy-safe growth, no risky military action).
- `vd doctor` prints the configured kind, whether the key is set (never its value) and
  whether the SDK is importable.

## 3. Decision flow

`engine.decide` is what `vd tick` calls in place of `plan.plan_next_action`:

1. **Shared vetoes** (`plan.veto_action`: killswitch, health, game paused, unreconciled
   pending tx, a resolvable mission, an incoming hostile fleet) decide first, exactly as in
   the ladder. No request is made, and the killswitch halts before any network call.
2. **Storage-overflow deadline** (`plan.deadline_action`, the `5:` rules) decides next, also
   with no request. A loss-avoidance deadline is not a matter of taste.
3. The ladder's own pick is computed anyway (pure and cheap): it is the fallback and the
   reference for `agrees_with_ladder`.
4. **Pool**: `candidates.collect_pool` builds every legal, selectable candidate (section 4). An
   empty pool falls back with `empty_pool`, no request.
5. **One request** to TypeSafe (section 5), all questions in parallel.
6. **Composition**: a weighted score per candidate (section 6).
7. **Gates** (section 7). The winner is finalized with the rule literal of its family
   (`plan.RULE_BY_FAMILY`, the same `6:`/`7:`/`8...` rules the ladder uses, so brief goals,
   narration and the storage gate keep working) and tagged `Action.engine = "jev"`. Any
   failure of a gate, any `JevError`, or any unexpected exception returns the **ladder's**
   action instead.

`plan_next_action` itself never calls the network; `vd plan run` stays offline unless you pass
`--engine policy|jev`.

## 4. The candidate pool

The pool is the only thing the model chooses from; it cannot invent an action or an argument.
`collect_pool` runs every candidate generator on **every** target planet (unrotated;
research once, through the first target planet, the planet `startResearch` is submitted
through), then applies these hard filters. Each rejection is counted under its reason code and
reported as `rejected` in the trace and by `vd engine pool`. Fixed order:

| Reason code | Rejects |
| --- | --- |
| `locked` | a visibility-only alternative: unmet prerequisite or defense cap (`score_basis` starts `locked:`) |
| `allow_flag` | the Solar Satellite the generator emits when `allow_ships` is off; and any action whose kind's flag is off (below) |
| `non_selectable` | a Crawler already at its boost cap |
| `planet_missing` | the action's planet is not in the snapshot |
| `queue_busy` | the queue the action would occupy is busy: the planet's building queue, the account-wide research queue, the ship or defense lane (a batch needs every lane it orders from) |
| `storage_cap` | a building whose cost exceeds the planet's storage cap (a storage upgrade itself is exempt) |
| `spend_unverifiable` | a production action whose live unit cost cannot be verified |
| `unaffordable` | live spend not covered by `resourcesAsOfNow` |
| `reserve` | holdings after the spend would fall below `policy.reserves` |
| `fields` | the planet has no free field, or field data is missing |
| `energy_unknown` | the planet reports no energy balance |
| `fleet_slots` | a fleet mission with no known free slot (unknown counts as none) |
| `batch_vs_scored_single` | a production batch on a planet that has a scored single ship order |
| `high_stakes_not_idle` | colonize/attack/missile while anything else survived (see below) |
| `duplicate` | the same action reached twice (same call identity; research dedups by technology and prefers the first target planet) |

The flag each kind needs: `allow_building` (upgrades), `allow_research`, `allow_ships`,
`allow_defense`, both for a batch that orders both, `allow_combat` (Attack, Missile),
`strategy.colonize` (Colonize), `allow_fleet_noncombat` (Transport, Deploy, Harvest). An
unrecognised kind or mission type is refused.

**Why this filter set exists.** The pool mirrors the guard's BLOCK gates that a snapshot alone
can decide (prerequisites, energy, affordability, reserve, fields, production batch, mission
type, fleet slots, missile target) and adds the checks the guard does not make: the
`allow_building`/`allow_research`/`allow_fleet_noncombat` flags and queue idleness. The guard
would otherwise let a busy-queue upgrade through to a contract revert.
`tests/test_pool.py`'s two property tests pin this across fixtures and a policy matrix: every
pooled entry passes the guard's snapshot-decidable gates, and a ladder winner missing from
the pool is refused for a reason a guard gate mirrors.

**Facts, not filters.** These reach the model as context and never remove a candidate:
whether the economy is already active (`situation.economy_active`), the value-ceiling
warning (the guard still escalates a large spend), a candidate's position among declared
priorities, and an energy-limited planet.

**High stakes.** Colonize, Attack and Missile are pooled only when nothing else survived
(`policy.engine.jev.high_stakes_only_when_idle`, default `true`); switching it off lets them
compete, still subject to their flags and to the stricter confidence floor (section 7).

**Deterministic pre-trim.** With more survivors than `max_candidates`, each family is ranked
(scored ascending by payback, then generation order) and families take one entry each per
round in band order, so every family present keeps a slot before any gets a second. The
number dropped is reported as `pre_trim`, not a rejection. The pool is ordered by band, then
generation index.

## 5. What is sent to TypeSafe, and what never is

One request: a `state` object and `2N + 2` questions for `N` pooled candidates. `vd engine
pool` prints the exact request offline and an estimated size; the ceiling is 48,000 estimated
tokens (`request_too_large` beyond that), far above a full pool.

**Never sent:** the wallet, the signer, any address, any coordinates, any raw planet id,
any resource amount, cost, rate, timestamp or fleet composition. Planets appear only as
labels (`planet A`, `planet B`, ... in target-planet order; `home` for the first, `colony`
for the rest); the label-to-id map never leaves the process. A transport names its endpoints
by label; a harvest, colonize or attack names no coordinates at all.
`tests/test_jev_engine.py::test_the_payload_carries_no_wallet_signer_address_coordinate_or_raw_planet_id`
pins this.

**Sent:** `strategy_intent` (your text, or the default rubric) and `intent_source`;
`situation`; and one entry per candidate: `id` (`c0`, `c1`, ...), `group`, `planet` label,
`what` (a plain sentence) and `facts` (a few descriptive phrases).

`situation` holds: whether an economy build or research is in progress, the research queue
(`idle`/`busy`), fleet slots (`free`/`all in use`/`unknown`), threat buckets, the declared
targets as text, and per planet its label and role, energy, per-resource hours-to-cap,
queue state and defense posture.

**Every game number is turned into a descriptive bucket by code**, because Jev cannot count
or judge numeric closeness. The only plain integers are the action's own level or quantity
("Upgrade X to level 5", "Build 3 X") and declared-target counts ("have 2 of 5").

| Bucket | Values (upper bound exclusive) |
| --- | --- |
| Payback (`Candidate.score`, hours) | none: "no direct production gain"; under 6 hours; 6-24 hours; 1-3 days (under 72 h); 3-7 days (under 168 h); more than a week |
| Cost share (weighted spend over weighted holdings of that planet, `strategy.resource_weights`) | a small share (<10%); a moderate share (10-35%); a large share (35-70%); most (>70%) |
| Build time (research, upgrade, ship, defense, batch; per-unit duration times quantity) | under 15 minutes; 15 minutes-2 hours; 2-8 hours; 8-24 hours; over a day; unknown |
| Hours to cap, per resource | under 2 hours (including already full); 2-6 hours; 6-24 hours; more than a day; not filling; unknown |
| Energy, per planet | deficit: production throttled (scale below 100% or produced under required); surplus (spare at least 10% of required); balanced; unknown |
| Defense posture, per planet (planetary defense units, missiles excluded) | none (0); light (1-19); moderate (20-99); strong (100+) |
| Threat counts (incoming hostile fleets, recent attacks on you, debris on your planets) | none; one; several; the last two are `unknown` when no radar report exists (`vd plan run`, `vd engine pool`) |

A candidate's `facts` combine these with fixed sentences per family ("moves resources between
your own planets; nothing is gained overall", "commits combat ships to battle; they can be
lost", "consumes a Colony Ship") and its declared-priority position ("named research priority
#2", "next step toward unlocking a declared target").

`vd engine pool` and `vd engine compare` fetch no live targets, so the Colonize, Attack,
Missile and debris-harvest families never appear there; a real tick supplies them.

## 6. The questions and the composite score

| Question | Kind | What it asks |
| --- | --- | --- |
| `tick_focus` | Choice | which **group** of activity best serves the strategy now: `economy`, `research`, `fleet_defense`, `unlock`, `logistics`, `expansion`, `offense` (only groups present in the pool), plus `hold`. Each option carries a `what`/`not_for` description. |
| `threat` | Noul | probability that a planet is attacked within a few hours, from `situation.threats` and defense posture only |
| `fit_c<i>` | Score, 5 levels | how well candidate `i` serves the strategy. Levels, low to high: works against it; unrelated; supports it indirectly; directly supports it; is its top priority or immediate next step |
| `urgency_c<i>` | Score, 4 levels | what is lost by postponing it. Levels: can wait many hours; doing it soon helps a little; time-sensitive (wastes production, leaves a queue idle long, blocks a declared target); critical now (risks losing resources or assets) |

Scores are normalised to 0..1 from the API's own level legend. The composite for candidate
`i`, each term in 0..1:

```
C_i = ( w_fit * fit_i + w_urgency * urgency_i + w_focus * P(focus = group_i)
      + w_economy * economy_i + w_threat * P(threat) * [i is defense] ) / (sum of weights)
economy_i = H0 / (H0 + payback_hours_i)      (0.3 when the candidate has no payback score)
```

`H0` is `payback_reference_hours` (default 24, so a 24-hour payback scores 0.5). The
`economy` term is the only one computed wholly by code. The threat term applies only to
defense candidates (a defense order, or a batch containing one). Default weights: fit 0.30,
urgency 0.25, focus 0.15, economy 0.25, threat 0.05; only the ratios matter, and a weight of 0
drops its term (and its confidence, below). Ties break by ladder band order, then generation
order (`test_ties_break_by_band_then_generation_index`).

**Confidence** is the **minimum** confidence over the judgments with a positive weight that
fed the winner: its `fit`, its `urgency` and `tick_focus`. (A Noul carries no confidence.)

## 7. Gates and fallbacks

The winner must pass, in this order, or the ladder decides:

| Gate | Setting (default) | Falls back with |
| --- | --- | --- |
| confidence at least `min_confidence` (inclusive) | 0.5 | `low_confidence` |
| a colonize/attack/missile winner needs the higher floor | `min_confidence_high_stakes` 0.75 | `low_confidence_high_stakes` |
| composite lead over the runner-up at least `min_margin` (skipped for a one-candidate pool) | 0.03 | `low_margin` |

**Hold.** With `allow_hold` on, a `tick_focus` of `hold` with probability at least 0.5 and
confidence at least `min_confidence` returns a NOOP with rule `9j:hold` (`engine: "jev"`)
instead of the best candidate. It is checked before the gates above. Off by default, and a
weak hold is ignored.

Every `fallback_reason` the trace can carry (`None` when jev decided or the ladder is
configured):

| Code | Meaning |
| --- | --- |
| `missing_key` | `TYPESAFE_API_KEY` unset or blank |
| `sdk_missing` | `typesafe-sdk` not importable |
| `timeout`, `connection` | the request timed out or could not connect (one retry at most, inside `timeout_s`) |
| `rate_limited`, `auth`, `bad_request`, `server` | the matching TypeSafe API error class |
| `malformed` | an answer missing, of the wrong type or non-finite for any asked question |
| `request_too_large` | estimated request over 48,000 tokens |
| `empty_pool` | nothing legal to choose from |
| `low_confidence`, `low_confidence_high_stakes`, `low_margin` | the gates above |
| `engine_error:<Class>` | an unexpected exception inside the engine; the ladder ran instead, the tick did not fail |

A veto or the deadline is not a fallback: it is recorded as `pre_empted_by`, holding the rule
that decided (for example `4:incoming-hostile-fleet` or `5:storage-overflow-spend`), and no
request was made. SDK error messages are never kept (they can echo request content): only the
class name, HTTP status and request id.

`tests/test_jev_engine.py` pins every code, including
`test_every_jev_error_falls_back_to_the_ladder_action` (each error returns exactly the
ladder's action) and `test_vetoes_and_the_deadline_never_call_the_backend`.

## 8. What gets logged

- **`proposals.jsonl` `engine` block**: the full trace (engine that decided, configured kind,
  model, request id, latency, input tokens, `fallback_reason`, `pre_empted_by`, `pool_size`,
  `rejected`, `ladder_pick`, `agrees_with_ladder`, `winner_confidence`, `margin`,
  `focus_probabilities`, `threat`, and the top five judgments with their composite scores).
  `null` under the ladder. It is **excluded from the dedup fingerprint** because latency,
  request id and probabilities jitter between otherwise identical ticks; what the engine chose
  is already in the fingerprinted fields.
- **The chosen action** carries `Action.engine` (`"jev"`, or `"ladder"` after a fallback and
  for every manual override). Its rationale is the ladder-style rationale plus "Selected by the
  jev engine from N legal candidates (<group> focus)", and its alternatives are the other
  pooled candidates in band order, never composite order, so neither carries a probability and
  a repeat pick still dedups (`test_a_second_jev_tick_differing_only_in_probabilities_and_latency_is_deduped`).
- **`actions.jsonl`**: the sent-action record gains an `engine` field.
- **`strategy.md`**: a narrated line for a jev action ends with `[engine=jev]`.
- **Report panel**, one line, only when jev is configured:
  `engine: jev (<model>, 140ms, confidence 0.71, agrees with ladder)` (or `differs from
  ladder`), `engine: jev -> ladder fallback (timeout)`, or `engine: jev pre-empted by
  1b:game-paused`.
- **A manual override** (`vd tick --action`) is never an engine decision, but the
  "planner would have proposed" comparison runs the configured engine, so under jev it costs
  one request and its record gains `engine` and `fallback_reason` (never probabilities). Under
  the ladder the record is unchanged.

## 9. CLI

- `vd engine pool --snapshot F --policy F [--json]`: offline, no key. Prints the pool
  (band, group, family, planet, entity, score basis), the rejection counts, the exact `state`
  and `questions`, and an estimated token count.
- `vd engine compare --snapshot F --policy F [--json]`: runs the ladder and the jev engine on
  the same input (jev needs the key and the network for a real answer). Exit codes: `0` the
  picks agree (also when a veto or the deadline decided both), `1` they disagree, `3` jev fell
  back to the ladder, `4` a load error. Prints the confidence, margin and top judgments.
- `vd plan run --engine ladder|policy|jev`: default `ladder` (offline, unchanged). `policy`
  follows `policy.engine.kind`; `jev` forces it. The panel gains the engine line; `--json`
  prints the `Action` only (its `engine` field says who chose it). Any other value exits `2`.
- `vd doctor`: three added lines: `engine: <kind>`, `TYPESAFE_API_KEY: set|unset`,
  `typesafe-sdk: importable|missing`.

Suggested rollout: keep `tier` at `advisor`, run `vd tick` on the jev engine for a while, and
read the `engine` blocks: the fallback rate, `agrees_with_ladder`, and whether the disagreements
are ones you would have made yourself. `vd engine compare` on a saved snapshot answers "would
jev have chosen differently here" without a tick.

## 10. Tuning

- **Write the intent as priorities and exclusions**, in plain sentences: name the kinds of
  development you want ("research", "defense", "colony growth", "ships"), their order, and what
  to avoid ("no attacks", "do not spend on logistics"). The model reads the intent against each
  candidate's `what` and `group`, so use those words (research, defense, colony, mines,
  storage, ships). Do not put numbers or thresholds in it (it cannot count); numbers belong in
  `reserves`, `limits` and `policy.strategy`.
- **Weights.** Raise `fit` to follow the intent more; raise `economy` to favour fast payback
  (this also favours already-developed planets, section 12); raise `urgency` to favour
  work that prevents waste; `focus` is the group-level nudge; `threat` only ever lifts defense.
- **Thresholds.** Raise `min_confidence`/`min_margin` to hand more ticks to the ladder; lower
  them to trust the model more. A high fallback rate under `low_margin` usually means the pool
  holds several near-equivalent candidates, which is fine.
- **`timeout_s`** (0.5-30) is the total budget, retry included; a tick waits at most that long.
  **`max_candidates`** (2-60) caps the pool after the pre-trim.
- **Declared targets still matter**: `ship_targets`, `defense_targets`, `research_priority` and
  `building_priority` shape which candidates exist and appear in the facts.

## 11. Limits

- Jev is not a calculator. It cannot count, judge whether two numbers are close, or read dates;
  that is why every number is bucketed, why the economy term is computed in code, and why no
  question asks it to compare quantities.
- Numbers stay in code: costs come from the live API's `cost` object, affordability and reserve
  checks are the pool's and the guard's, and no cost-scaling function exists.
- The model judges qualitative fit; it does not generate actions or arguments.
- One request per tick (two under a manual override with jev configured). A pool of a couple of
  dozen candidates is a few thousand tokens, a tiny per-tick cost; `vd engine pool` prints the
  estimate for your own policy.
- Decisions are not reproducible bit for bit: the model may answer slightly differently for the
  same state. Dedup stays stable (section 8) but the pick itself is not guaranteed to repeat.
- The ladder's diagnostics (`opportunities:` and the playbook's derivations) describe the
  ladder's bands; under jev they remain accurate as descriptions of what each band would pick.

## 12. Why cross-planet scoring is acceptable here

`strategy-playbook.md` §13 argues against scoring candidates across planets for the *ladder*:
a colony's first upgrades are cheap and low in absolute value, so a payback ranking keeps
choosing the developed planet and the colony starves. `planet_rotation` fixes that fairness
problem, and it is a ladder-only device: it rotates which planet three ladder rungs walk
first, which has no meaning when candidates are compared rather than walked.

The jev engine differs in what it ranks and against what. The pool is a set of already-legal
candidates; the ranking is against an explicit intent you wrote (which can say "grow the
colony"), with planet roles and queues in the state; the ladder is the fallback. Payback is
one term among five, and only the code-computed one, so it can be down-weighted. The
trade-off is real, though: with default weights the economy term still favours established
planets, and nothing guarantees fair turns. If a colony starves under jev, say so in the
intent, lower `weights.economy`, or return to the ladder with `planet_rotation` on.

## 13. Safety

- `guard.py` and `walletctl` re-check everything independently. The engine chooses among
  candidates the guard would already accept on snapshot data; the guard still evaluates every
  gate on the chosen action with live data, and the wallet re-validates it against its own
  allowlist and the on-chain ABI pin. `Action.engine` is provenance only and is never read by
  the guard.
- Tier rules are unchanged. At `advisor` a jev proposal is never executed. At `economy` and
  above a jev-chosen action is sent exactly like a ladder action: `wallet_engine.
  require_confirmation` governs whether `tick` sends automatically, and `walletctl send`
  still needs `--confirm`.
- Combat stays gated: Attack and Missile need `allow_combat` (and `operator` tier) and only
  enter the pool when nothing else is legal, under the higher confidence floor.
- A jev failure never fails a tick and never blocks one: it is a ladder decision with a reason.
