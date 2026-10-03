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
3. Adaptive intent
4. Decision flow
5. The candidate pool
6. What is sent to TypeSafe, and what never is
7. The questions and the composite score
8. Gates and fallbacks
9. What gets logged
10. CLI
11. Tuning
12. Limits
13. Why cross-planet scoring is acceptable here
14. Safety

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
    "proactive_storage_hours": 24.0,
    "high_stakes_only_when_idle": true,
    "allow_hold": false,
    "adaptive_intent": false,
    "adaptive_intent_default_hours": 6.0
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
- **The intent is validated at load, under the jev engine only.** It is sent to TypeSafe verbatim,
  so a policy with `kind: "jev"` whose `intent` contains any of these **fails to load** (every `vd`
  command that reads the policy then reports the problems, each quoting the matched text, for example
  `contains planet id 664 ("p664")`): an address (`0x`/`0X` plus 40 hex digits, or a bare run of 40),
  the policy's `wallet` or `signer` address (also split by spaces or invisible characters, or any
  token of 8 or more hex characters that is part of it, such as "ending 30553aa1", or a shortened form
  such as `0x4e15...3aa1` or "ends in 0553aa1"), three numbers
  joined by colons (`7:291:1`, and so also `3:2:1` or `14:00:00`), or a planet id from
  `policy.planets`: with three or more digits as a number token (`664`, `p664`, `planet664`, `#664`,
  `6,64`; `16640` is fine), with one or two digits only when introduced as a planet (`planet 10`, `p10`,
  `id 10`, `#10`), so "level 10 mines" loads.
  Matching runs on a normalised copy (Unicode NFKC, invisible format characters dropped, every Unicode
  digit read as ASCII, case-folded), so fullwidth digits or a zero-width space do not hide anything.
  Write counts as words ("two planets") and ratios as "3 to 2 to 1". Describe the strategy without
  identifiers. Under the ladder (`kind` not `"jev"`) the policy loads whatever the intent says: it never
  leaves the machine. The same text is checked again at `vd engine intent set`, at tick resolution and
  at send time (section 3, section 6). `tests/test_models_engine.py` pins each case
  (`test_a_policy_whose_intent_identifies_the_account_fails_to_load`,
  `test_a_planet_id_is_flagged_as_a_standalone_number`, `test_every_realistic_bypass_is_caught`,
  `test_a_ladder_policy_with_identifying_looking_intent_text_still_loads`,
  `test_the_same_intent_under_jev_fails_to_load_naming_the_snippet`).
- `adaptive_intent` (default `false`) lets an agent set a standing, expiring replacement for
  `intent` (section 3). `adaptive_intent_default_hours` (default 6, in `(0, 72]`) is the lifetime
  of an override set without `--ttl`. Neither affects a tick unless `kind` is `"jev"`.
- Numbers are validated at load: no `Infinity`/`NaN` anywhere in the `engine` block; each weight
  is `0..100`; `payback_reference_hours` is in `(0, 10000]`; `proactive_storage_hours` is in
  `(0, 720]`; and at least one of `fit`, `urgency`
  or `focus` must be above 0, because those are the judgments that carry a confidence (an
  economy/threat-only weighting would make every confidence gate pass vacuously).
- `vd doctor` prints the configured kind, whether the key is set (never its value) and
  whether the SDK is importable.

## 3. Adaptive intent

A standing, expiring **override of `policy.engine.jev.intent`**. While one is live it *replaces*
the policy intent for every tick (scheduled ones included), so the engine follows a change of
situation (a raid, a new colony, saving for a target) without anyone editing `policy.json`. When it
expires or is cleared, the policy intent applies again. Only the jev engine reads it; the ladder
has no intent.

**Enabling.** Set `policy.engine.jev.adaptive_intent` to `true` (section 2). With it off, a stored
override is ignored by every tick (an expired one is still cleaned up) and `set` refuses to write one. `adaptive_intent_default_hours`
is the lifetime when `--ttl` is omitted (default 6); the maximum lifetime is **72 hours**,
whatever the policy says (`models.ADAPTIVE_INTENT_MAX_HOURS`). The override lives in
`$VEYDRIFT_HOME/intent-override.json`: one slot, replaced by each `set`, written atomically.

**Commands** (`vd engine intent`; `--policy FILE` overrides `$VEYDRIFT_HOME/policy.json`):

```bash
vd engine intent set "Defense first while raids continue; keep research going slowly." \
    --reason "three attacks on the colonies today" --ttl 6h
vd engine intent show [--json]
vd engine intent clear [--reason "raids stopped"]
```

- **`set TEXT --reason R [--ttl T] [--json]`**. `--reason` is required (at most 280 characters);
  `TEXT` is at most 1000. `T` is `<number><unit>` with unit `m`, `h` or `d` (`90m`, `6h`, `1.5h`,
  `2d`), a bare number meaning hours; it must be above zero and at most 72 hours. It is refused,
  exit **2**, with every problem listed, **nothing written and no `strategy.md` line**, when:
  `adaptive_intent` is off, the text or reason is empty or too long, the text fails the
  validation of section 2 (address, coordinates, wallet or signer, planet id; applied here whatever
  the engine kind), or the TTL does not parse or is out of range. On success it replaces any existing
  override (an unreadable file too) and appends `intent override set until <expiry>: "<text>" --
  <reason>` to `strategy.md`; under the ladder (`kind` not `"jev"`) it still writes but warns that the
  override has no effect. With `--policy FILE` naming a file other than `$VEYDRIFT_HOME/policy.json` it
  also warns that ticks read the home policy, not that file: the override was validated against one
  policy but is judged against the other. `--json` prints the stored override plus `replaced` and
  `warnings`.
- **`show [--json]`** is read-only: the intent a tick would judge against now (`intent`),
  `source` (`policy`, `default` or `agent`), the override's reason, set time and expiry, and a
  `note` when a stored override is not in use. It reports an expired override and never deletes it
  (the tick does). Under the ladder it prints the stored override marked unused. It always adds a
  `send-time check:` line (json `send_time_check`): ticks re-check this text against the account's
  planets and targets right before sending and may still substitute it, which `show` cannot predict.
  `--json` keys: `engine_kind`, `adaptive_intent`, `intent`, `source`, `note`, `expired`, `override`,
  `send_time_check`.
- **`clear [--reason R]`** removes the file (an unreadable one too) and appends `intent override
  cleared -- <reason>`. It never reads the policy, works with the flag off, and exits 0 whether or
  not an override existed.
- Exit codes: **0** done, **2** `set` refused, **4** the policy failed to load (`set` and `show`).

**Resolution.** `engine.resolve_effective_intent` runs once per jev tick, in this order; the first
case that applies wins, and **none of them raises or fails a tick**. Every case but the last
returns the policy intent (the built-in rubric when the policy intent is empty) and records its
note on the trace:

| Case | Result | `intent_note` |
| --- | --- | --- |
| `policy.engine.kind` is not `"jev"` | policy intent; the file is not read | none |
| no override file, or an empty one | policy intent | none |
| the file cannot be read: bad JSON (including a pathologically nested file), wrong shape, text or reason over the limits, or a timestamp without a timezone (a hand-edited file) | policy intent | `override file unreadable` |
| `now` is at or past `expires_at` (the boundary is exclusive) | policy intent; the tick removes the file, **even with the flag off** | `override expired` |
| `adaptive_intent` is off | policy intent; the file is kept | `override ignored: adaptive_intent is off` |
| the stored `version` is not 1 | policy intent | `override rejected: unsupported version` |
| the lifetime is not honest: more than 72 hours between `set_at` and `expires_at`, more than 72 hours left from now, `expires_at` not after `set_at`, or `set_at` more than 5 minutes in the future (a hand-edited file) | policy intent | `override rejected: lifetime` |
| the stored text fails section 2's validation, or is blank (a hand-edited file) | policy intent | `override rejected: <problems>` |
| otherwise | **the agent intent**, whitespace stripped | none |

An empty policy intent falls to the built-in rubric (`intent_source: "default"`). The lifetime and
version are enforced here rather than trusted from the file, so a hand-edited override cannot outlive
the 72-hour ceiling. The text validation runs again at every tick, and once more, against the real
planet ids and coordinates of the account, right before each request (section 6). A naive `now` is
read as UTC. `tests/test_intent.py` pins each row (for example
`test_an_override_is_ignored_when_the_flag_is_off`, `test_the_expiry_boundary_is_exclusive`,
`test_a_hand_edited_override_with_identifying_text_is_rejected`,
`test_a_hand_edited_override_with_naive_datetimes_does_not_raise`,
`test_an_override_with_an_impossible_lifetime_is_rejected`,
`test_an_override_of_exactly_the_maximum_lifetime_is_honoured`,
`test_a_set_at_a_few_minutes_ahead_is_clock_skew_not_rejected`,
`test_an_override_of_an_unsupported_version_is_rejected`,
`test_an_expired_override_is_reported_expired_even_with_the_flag_off`,
`test_a_pathologically_nested_file_is_ignored_with_the_unreadable_note`).

**What a tick does.** After the killswitch check (which does no extra work) and only under a
configured jev engine:

- An **expired** override is deleted and one `strategy.md` line is appended, `intent override
  expired: "<text>" -- back to the policy intent` (`default intent` when the policy has none), so
  the expiry is logged exactly once, because the file is then gone
  (`tests/test_tick.py::test_an_expired_override_is_deleted_and_logged_exactly_once`). This happens
  with `adaptive_intent` off too (`test_an_expired_override_is_cleaned_up_even_when_adaptive_intent_is_off`).
  Only the override the tick read is deleted: the file is re-read and compared (intent, `set_at`,
  `expires_at`) and unlinked only if it still matches, so one a concurrent `vd engine intent set` wrote
  since stays. This is a compare-then-unlink, not an atomic claim: an override written in the instant
  between the compare and the unlink is removed too, and the agent re-sets it. A
  file that has already vanished is neither an error nor logged
  (`test_expiry_cleanup_leaves_an_override_set_after_the_tick_read_the_old_one`,
  `test_expiry_cleanup_survives_the_file_vanishing_and_logs_nothing`). An unreadable file, a
  pathological one included, is ignored with a note and never fails the tick
  (`test_e2e_a_pathological_override_file_does_not_fail_the_tick`).
- The effective intent goes to the engine and onto every trace (section 9). A manual-override tick
  (`vd tick --action`) runs the same engine for its comparison, so it carries the intent too.
- The report panel gains **one** line, only when an agent intent drove the tick or a stored
  override was not used: `intent: agent -- "<text, 80 characters at most>" (until <date time> UTC;
  reason: <reason>)`, or `intent: policy (override expired)`. A plain policy or default intent
  prints nothing, so default reports are unchanged.
- A jev narration line in `strategy.md` ends `[engine=jev intent=agent]` when an agent intent was
  in force, `[engine=jev]` otherwise.
- `vd tick --readiness` adds `agent-intent proposals: N of M jev proposals`, so promotion evidence
  separates ticks an agent steered from ticks the policy steered. It is absent until a jev
  proposal exists.

Under the ladder none of this happens: the tick never reads the file, and the record's `engine`
block stays `null` (`tests/test_tick.py::test_a_ladder_policy_never_resolves_or_touches_an_override`).

**Guidance for an agent setting one.** Only with `adaptive_intent` on, and only to follow the
user's goals through a change of situation, never to change them.

- **Stay inside the user's standing intent and `policy.actions`.** Derive the override from the
  policy intent: it narrows or re-weights what the user already asked for, for a while; it does not
  invent a strategy. An override cannot widen what is permitted: the `allow_*` flags, `reserves`,
  `limits` and the tier decide what is legal before the model sees anything, and the guard and the
  wallet re-check the winner. Do not add wording the user never asked for, above all offence,
  colonizing or deploying (see the high-stakes note below).
- **Always give a concrete reason**, naming what you observed ("three raids on the colonies today",
  "saving for the fourth colony ship"). It is logged to `strategy.md`, the trace and the report,
  and it is what a human reads to decide whether to keep the override.
- **Prefer short TTLs and re-evaluate on expiry.** Pick the shortest lifetime that covers the
  situation (a few hours, not the 72-hour ceiling); when it lapses, look at the account again and
  set a new one only if the situation still holds. Do not renew on reflex.
- **Write one clear sentence naming kinds of development** ("defense", "research", "colony growth",
  "ships", "mines", "storage") in priority order, with what to avoid, as the policy intent does
  (section 11). No numbers or thresholds.
- **Never include identifiers.** No address, coordinates, wallet or signer, planet id (write "the
  colonies", not `planet 665`), and no three-number ratio or time like `3:2:1` or `14:00:00`
  (write "3 to 2 to 1"): `set` refuses them, a tick rejects them again from a hand-edited file, and
  the send-time check replaces the text with the policy or default intent if one still gets through.
  Ordinary numbers ("level 10", "30%") are fine.
  Say "the colonies" or "the home planet".
- **Clear it when the situation passes**, with a reason (`vd engine intent clear --reason ...`),
  rather than leaving a stale override to run out.
- Run `vd engine intent show` first: it says what is in force and why.

**High stakes under an agent intent.** An agent intent changes nothing about the gates. A high-stakes
pick (colonize, attack, missile, deploy) needs the same confidence floor, ladder idleness, margin and
model endorsement as under a policy intent (section 8), plus its allow flag (`allow_combat`,
`strategy.colonize`, `allow_fleet_noncombat`) and the tier. What does change is what the model
*endorses*: an intent that favours offence can make it endorse an attack the policy intent would
not, so **with `allow_combat` on, an agent intent can lead to a combat pick that every gate allows**.
Keep offence out of an override unless the standing intent already asks for it. The trace records
`intent_source`, and the override's reason, set time and expiry, on every engine record, so
`proposals.jsonl` shows which picks an agent steered
(`tests/test_jev_engine.py::test_an_endorsed_high_stakes_pick_is_decided_identically_under_a_policy_and_an_agent_intent`).

## 4. Decision flow

`engine.decide` is what `vd tick` calls in place of `plan.plan_next_action`:

1. **Shared vetoes** (`plan.veto_action`: killswitch, health, game paused, unreconciled
   pending tx, a resolvable mission, an incoming hostile fleet) decide first, exactly as in
   the ladder. No request is made, and the killswitch halts before any network call.
2. **Storage-overflow deadline** (`plan.deadline_action`, the `5:` rules) decides next, also
   with no request. A loss-avoidance deadline is not a matter of taste.
3. The ladder's own pick is computed anyway (pure and cheap): it is the fallback and the
   reference for `agrees_with_ladder`.
4. **Pool**: `candidates.collect_pool` builds every legal, selectable candidate (section 5). An
   empty pool falls back with `empty_pool`, no request.
5. **One request** to TypeSafe (section 6), all questions in parallel.
6. **Composition**: a weighted score per candidate (section 7).
7. **Gates** (section 8). The winner is finalized with the rule literal of its family
   (`plan.RULE_BY_FAMILY`, the same `6:`/`7:`/`8...` rules the ladder uses, so brief goals,
   narration and the storage gate keep working) and tagged `Action.engine = "jev"`. Any
   failure of a gate, any `JevError`, or any unexpected exception returns the **ladder's**
   action instead.

`plan_next_action` itself never calls the network; `vd plan run` stays offline unless you pass
`--engine policy|jev`.

## 5. The candidate pool

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
| `economy_not_on_track` | the undeclared default Rocket Launcher (the filler used only when `defense_targets` is empty) while `economy_on_track` does not hold, as in the ladder; declared ship and defense targets are unaffected |
| `storage_not_needed` | a proactive storage upgrade whose resource does not fill within `proactive_storage_hours` (default 24) at current production, unless the current cap blocks another building on that planet. The ladder never lets proactive storage win; without this filter the engine, when nothing else is legal, would spend on storage nowhere near full |
| `batch_vs_scored_single` | a production batch on a planet that has a scored single ship order |
| `high_stakes_not_idle` | colonize/attack/missile/deploy while anything else survived (see below) |
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

**High stakes.** Colonize, Attack, Missile and Deploy (`candidates.HIGH_STAKES_FAMILIES`; Deploy
moves the whole fleet to another planet for good) are pooled only when nothing else survived
(`policy.engine.jev.high_stakes_only_when_idle`, default `true`); switching it off lets them
compete, still subject to their flags and to the gates in section 8 (confidence floor, ladder
idleness, model endorsement).

**Deterministic pre-trim.** With more survivors than `max_candidates`, each family is ranked
(scored ascending by payback, then generation order) and families take one entry each per
round in band order, so every family present keeps a slot before any gets a second. The
number dropped is reported as `pre_trim`, not a rejection. The pool is ordered by band, then
generation index.

## 6. What is sent to TypeSafe, and what never is

One request: a `state` object and `2N + 2` questions for `N` pooled candidates. `vd engine
pool` prints the exact request offline and an estimated size; the ceiling is 48,000 estimated
tokens (`request_too_large` beyond that). A 24-candidate pool measures about 11,000 estimated
tokens and the 60-candidate maximum about 27,000; state plus the longest single question stays
under 4,000, far below the API's 32k cap for that pair.

**Never sent:** the wallet, the signer, any address, any coordinates, any raw planet id,
any resource amount, cost, rate, timestamp or fleet composition. Planets appear only as
labels (`planet A`, `planet B`, ... in target-planet order; the role sent with each is `listed
first` for the first target planet and `other` for the rest); the label-to-id map never leaves the process. A transport names its endpoints
by label; a harvest, colonize or attack names no coordinates at all.
`tests/test_jev_engine.py::test_the_payload_carries_no_wallet_signer_address_coordinate_or_raw_planet_id`
pins this.

**Sent:** `strategy_intent` (the effective intent: the policy text, an agent override, or the
default rubric) and `intent_source` (`policy`, `default` or `agent`);
`situation`; and one entry per candidate: `id` (`c0`, `c1`, ...), `group`, `planet` label,
`what` (a plain sentence) and `facts` (a few descriptive phrases).

**The intent is the only free text that reaches the model, so it is validated.** The policy intent
is checked at load (under the jev engine; the ladder never sends it), an override at `set` and
again at every tick (section 3); an address, coordinates, the wallet or signer, or a planet id in it
means the policy does not load, `set` refuses, or the tick uses the policy intent instead.

**The send-time check is the final guard.** Those checks cannot see the account: `policy.planets`
may be empty or stale, and the targets are known only once the pool exists. So
`jev_engine.sendable_context` re-checks the effective intent immediately before every TypeSafe request
(it runs once a pool exists, so a veto, the deadline or an empty pool, which send nothing, never reach it) against the
wallet and signer, **every planet the account owns** (the snapshot's `owned_planet_ids` and
`owned_planet_coordinates`, taken from `/wallet/{addr}/planets`, so a planet outside `policy.planets`
counts, plus `policy.planets` and the own-debris targets) and **the ids of the targets** the pool was
built from (attack, missile, foreign debris). Own and foreign ids are held to different rules, because
live targets are other players' planets with small ids (1, 10, 13, 24, 30...) that are everyday numbers
in prose: an own id of three or more digits matches as a bare number, an own id of one or two digits
only introduced as a planet (`planet 10`, `p10`, `id 10`, `#10`), and a foreign id only introduced that
way, at any length. The account's own coordinates match in any spelling; a foreign target's coordinates
(attack, missile, debris, colonize) match only as `g:s:p` with colons, which the generic rule catches, so
"Build 6 solar plants, 9 mines, 1 lab" is never rejected because a target sits at 6:9:1. A text that
fails is replaced and the request carries the replacement:

- a failing agent override gives way to the policy intent when that passes the same check, else to
  the built-in default rubric;
- a failing policy intent gives way to the default rubric;
- the trace's `intent` and `intent_source` show the text actually sent, and `intent_note` says why, for
  example `override rejected at send time: contains planet id 664 ("664")` (or `policy intent
  rejected at send time: ...`; an earlier resolution note is kept after it);
- if the check itself cannot run, the default rubric is sent with the note `intent check failed:
  built-in default used`, never the unchecked text. Nothing here raises or fails a tick;
- an unexpected exception after the substitution (building the request, the backend call, scoring)
  is an `engine_error:<Class>` ladder fallback whose trace still describes the text that was sent, not
  the text that was rejected (`test_an_unexpected_error_after_substitution_records_what_was_sent`).

`vd engine pool` prints the request *before* this substitution, so it shows what the policy intent
alone would send. `tests/test_jev_engine.py` pins the check
(`test_an_agent_override_that_names_the_account_is_replaced_before_the_backend_call`,
`test_a_policy_intent_naming_a_snapshot_planet_is_replaced_by_the_default`,
`test_target_planet_ids_and_coordinates_are_checked_too`,
`test_everyday_numbers_are_not_rejected_because_of_realistic_foreign_targets`,
`test_a_planet_the_account_owns_outside_policy_planets_is_known_to_the_check`,
`test_a_failing_check_sends_the_default_and_never_raises`,
`test_build_request_alone_does_not_substitute`), and `tests/test_tick.py` runs the real engine from an
override file to the backend (`test_e2e_a_planet_id_not_listed_in_the_policy_never_reaches_the_backend`,
`test_e2e_the_accounts_coordinates_in_any_spelling_never_reach_the_backend`).

**What the matching catches** (`models.intent_text_problems`, on a normalised copy: NFKC, invisible
format characters dropped, every Unicode digit read as ASCII, case-folded). Each problem quotes the
matched snippet (at most 48 characters; a hex run of 32 or more characters is shown as `<hex value>`,
never quoted even in part).

- **Addresses:** `0x` or `0X` plus 40 hex digits, or a bare run of 40.
- **Wallet and signer:** their hex found in the text with all non-hex characters removed (so spaces and
  zero-width characters do not split it), any token of 8 or more hex characters that is part of them, and
  abbreviations: the first or last 4-7 hex characters beside an ellipsis (`0x4e15...3aa1`, `...0553aa1`),
  the last 4-7 after "ends in", "ending" or "ending in", or a standalone token of 5-7 hex characters
  equal to their last characters.
- **Coordinates:** any `n:n:n` (spaces around the colons allowed), which also flags a ratio or a clock
  time; and, for the coordinates of the planets the account owns, any spelling: `7/181/14`, `7-181-14`,
  `galaxy 7 system 181 position 14`, `G7 S181 P14`, or the three numbers with at most 15 non-digit
  characters between them. That last rule can flag a coincidence ("7 mines, then 181 energy, then 14
  ships"); the cost is one tick on a substituted intent.
- **Planet ids:** an own id of three or more digits matches as a number token, including letters or `#`
  glued to the front (`p664`, `planet664`, `#664`), digit groups (`6,64`, `6_64`) and leading zeros. An
  own id of one or two digits, and any foreign (target) id, matches only right after an introducer:
  `planet`, `p`, `id` or `#`, then optional space, `-` or `:` (`planet 10`, `p10`, `id: 10`, `#10`; not
  `step 10`). A longer number (`16640`) does not match.

**Deliberately not caught:** spelled-out numbers ("six hundred sixty-four"), two-part coordinates
(`7:181`), ids with digits spaced apart, and a wallet that is both split by spaces and shortened below
8 hex characters. These cannot be told from prose without a language model; the check is a filter for
realistic leaks, not a proof (`test_deliberately_not_caught`, `test_two_part_coordinates_are_not_a_leak`).

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

## 7. The questions and the composite score

| Question | Kind | What it asks |
| --- | --- | --- |
| `tick_focus` | Choice | which **group** of activity best serves the strategy now: `economy`, `research`, `fleet_defense`, `unlock`, `logistics`, `expansion`, `offense` (only groups present in the pool), plus `hold`. Each option carries a `what`/`not_for` description. |
| `threat` | Noul | probability that a planet is attacked within a few hours, from `situation.threats` and defense posture only |
| `fit_c<i>` | Score, 5 levels | how well the candidate embedded in the question serves the strategy. Levels, low to high: works against it; unrelated; supports it indirectly; directly supports it; is its top priority or immediate next step |
| `urgency_c<i>` | Score, 4 levels | what is lost by postponing the embedded candidate. Levels: can wait many hours; doing it soon helps a little; time-sensitive (wastes production, leaves a queue idle long, blocks a declared target); critical now (risks losing resources or assets) |

**Each `fit_c<i>` / `urgency_c<i>` question carries its own candidate inline**: its instructions
are `{"question": "...the candidate below...", "candidate": {id, group, planet, what, facts}}`,
and no question refers to a candidate by array position. Positional lookups (`candidates[16]`)
proved unreliable in a live run: the model resolved an index one off and scored a neighbouring
candidate, which flipped the pick. Named fields (`strategy_intent`, `situation`) are still
referenced by name, and `state.candidates` stays in the state so `tick_focus` sees what is on
offer. The cost is size: each candidate appears three times, about 450 estimated tokens per
candidate.

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

**Two stages.** The decision is split the way the model's judgments are reliable:

1. *Group stage: which kind of development.* The winning group is the group of the top composite.
   Its **confidence** is the minimum over the group-deciding judgments with a positive weight:
   `tick_focus` and the group's best candidate's `fit` (urgency counts only when neither is
   weighted). Its **margin** is the group's best composite minus the best composite of any other
   group.
2. *Item stage: which candidate of that group.* Every candidate of the winning group whose composite
   is within `min_margin` of the group's best is an acceptable alternative. The first of them in pool
   order (band, then generation index: the ladder's own priority order, which honours declared
   priorities such as `research_priority`) is taken. This stage has no confidence gate: several
   equally rated legal options of the same kind are a harmless preference, not uncertainty worth a
   fallback. Only candidates with the same high-stakes status as the top one are eligible.

A **high-stakes** winner does not use the group-stage shortcuts: its confidence is the minimum over
*every* weighted judgment (its `fit`, its `urgency` and `tick_focus`), and it must also lead every
other *kind* of move by `min_margin` (below). (A Noul carries no confidence.)

## 8. Gates and fallbacks

A hold (below) is checked first. Then the winner must pass, in this order, or the ladder decides:

| Gate | Setting (default) | Falls back with |
| --- | --- | --- |
| group-stage confidence at least `min_confidence` (inclusive; for a high-stakes winner, the strict all-judgment confidence) | 0.5 | `low_confidence` |
| group lead over the best other group at least `min_margin` (skipped when only one group is on offer) | 0.03 | `low_margin` |
| a high-stakes winner (colonize, attack, missile, deploy) needs the higher floor | `min_confidence_high_stakes` 0.75 | `low_confidence_high_stakes` |
| a high-stakes winner must lead every other *kind* of move (skipped when no entry is of a different kind) | `min_margin` 0.03 | `low_margin` |
| a high-stakes winner needs the ladder to be idle (below) | `high_stakes_only_when_idle` (on) | `high_stakes_not_idle` |
| a high-stakes winner is refused when `tick_focus` chose hold | none; applies even with `allow_hold` off | `high_stakes_hold` |
| a high-stakes winner needs model endorsement (below) | normalized fit at least 0.75 | `high_stakes_not_endorsed` |

**High-stakes gates.** A high-stakes winner is taken only when all of these hold; the three
checks after the confidence floor run in the order listed.

- *Ladder idle.* With `high_stakes_only_when_idle` on, the ladder reaches its deploy, colonize,
  attack and missile rungs (`8c:logistics-deploy`, `8d:colonize`, `8e:attack`, `8f:missile`) only
  when every earlier band proposed nothing at all, affordable or not. So if the ladder's own pick is
  an on-chain action under any other rule, something ordinary is pending and the ladder's pick
  stands (`high_stakes_not_idle`). A ladder pick that is itself one of those four rules, or a
  non-on-chain pick, passes.
- *Not hold.* `tick_focus` must not have chosen `hold` (`high_stakes_hold`), whatever `allow_hold` is.
- *Endorsed.* The winner's normalized `fit` is at least 0.75 ("directly supports" on the five-level
  scale) **and** `tick_focus` chose the winner's own group; otherwise `high_stakes_not_endorsed`.

**Endorsement and split answers.** The fit at least 0.75 test reads the *expected* (probability-weighted)
fit, so a split answer can average past it: half the probability on "directly supports" and half on
"unrelated" can still land above 0.75 on the five-level scale. What catches a split answer is the
high-stakes confidence floor (`min_confidence_high_stakes`), because the confidence of a split
answer is low. Keep that floor high.

**Margins.** The trace's `margin` is the group margin. With only one group on offer there is no
rival kind of development: the group gate is skipped and the trace reports `margin: null`
(omitted from `vd engine compare`'s output and from the panel line), never a made-up number. A
high-stakes winner is additionally held to a *kind* margin: its composite minus the best composite
among entries of a different kind. Kind is `(family, function, entity)`, and a fleet or missile
launch also carries its mission type, origin planet and target, so two attacks from different
origins, or on different targets, are different kinds and must clear `min_margin`; the same move on
two symmetric planets is one kind.

Why two stages: live runs showed Jev deciding *which kind of development* with near-certainty while
spreading its per-item judgments (urgency above all) across several equally good options of that
kind. Gating on those spread judgments sent clear-cut ticks to the ladder, which is exactly the
starvation this engine exists to avoid.

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
| `timeout`, `connection` | the request timed out or could not connect (a timeout is not retried; a connection error may retry once within the `timeout_s` budget; see Tuning) |
| `rate_limited`, `auth`, `bad_request`, `server` | the matching TypeSafe API error class |
| `malformed` | an answer missing, of the wrong type, non-finite or out of range for any asked question: an empty or mis-sized score legend, a score more than 5% of the legend's span past either end (a smaller overshoot is clamped to 0..1), a confidence, noul or probability outside 0..1, choice probabilities that do not sum to 1 (tolerance 0.05) or name an option that was not asked; also a non-finite composite or margin, or a weight vector with no confidence-bearing judgment |
| `request_too_large` | estimated request over 48,000 tokens |
| `empty_pool` | nothing legal to choose from |
| `low_confidence`, `low_confidence_high_stakes`, `high_stakes_not_idle`, `high_stakes_hold`, `high_stakes_not_endorsed`, `low_margin` | the gates above |
| `engine_error:<Class>` | an unexpected exception inside the engine (after a send-time substitution the trace still shows the text that was sent); the ladder ran instead, the tick did not fail |

A veto or the deadline is not a fallback: it is recorded as `pre_empted_by`, holding the rule
that decided (for example `4:incoming-hostile-fleet` or `5:storage-overflow-spend`), and no
request was made. SDK error messages are never kept (they can echo request content): only the
class name, HTTP status and request id.

`tests/test_jev_engine.py` pins every code, including
`test_every_jev_error_falls_back_to_the_ladder_action` (each error returns exactly the
ladder's action) and `test_vetoes_and_the_deadline_never_call_the_backend`.

## 9. What gets logged

- **`proposals.jsonl` `engine` block**: the full trace (engine that decided, configured kind,
  model, request id, latency, input tokens, `fallback_reason`, `pre_empted_by`, `pool_size`,
  `rejected`, `ladder_pick`, `agrees_with_ladder`, `winner_confidence`, `margin`,
  `focus_probabilities`, `threat`, the top five judgments with their composite scores, and the
  **intent fields**, below). `null` under the ladder. It is **excluded from the dedup fingerprint** because latency,
  request id and probabilities jitter between otherwise identical ticks; what the engine chose
  is already in the fingerprinted fields. On a manual-override tick it holds the trace of the
  comparison call (below), not a decision of the engine.
- **The intent fields, on every engine record**, whichever way the tick went (a decision, a hold,
  any fallback, an engine error, or a veto or deadline pre-emption):
  `intent` (the effective text), `intent_source` (`policy`, `default` or `agent`), and, only when an
  override is attached, `intent_reason`, `intent_set_at` and `intent_expires_at`; `intent_note`
  says why a stored override was not used (the table in section 3) or why the text was replaced at
  send time (section 6). `intent` and `intent_source` always show the text that was actually sent.
  They are inside the excluded `engine`
  block, so an override changing the text or its expiry alone never makes a repeat pick a new
  proposal (`tests/test_tick.py::test_two_ticks_differing_only_in_override_text_with_the_same_pick_are_deduped`;
  every path: `tests/test_jev_engine.py::test_every_jev_error_fallback_carries_the_intent` and
  siblings).
- **The chosen action** carries `Action.engine` (`"jev"`, or `"ladder"` after a fallback and
  for every manual override). Its rationale is the ladder-style rationale plus "Selected by the
  jev engine from N legal candidates (<group> focus)", and its alternatives are the other
  pooled candidates in band order, never composite order, so neither carries a probability and
  a repeat pick still dedups while the pick is stable (`test_a_second_jev_tick_differing_only_in_probabilities_and_latency_is_deduped`;
  see section 12 for when it is not).
- **`actions.jsonl`**: the sent-action record gains an `engine` field.
- **`strategy.md`**: a narrated line for a jev action ends with `[engine=jev]`, or
  `[engine=jev intent=agent]` when an agent override was in force. `vd engine intent set` and
  `clear`, and the tick that removes an expired override, append their own lines (section 3).
- **Report panel**, one line, only when jev is configured:
  `engine: jev (<model>, 140ms, confidence 0.71, agrees with ladder)` (or `differs from
  ladder`), `engine: jev -> ladder fallback (timeout)`, or `engine: jev pre-empted by
  1b:game-paused`. When an agent override drove the tick, or a stored one was not used, one more
  line follows it (section 3).
- **A manual override** (`vd tick --action`) is never an engine decision, but the
  "planner would have proposed" comparison runs the configured engine, so under jev it costs
  one request. The `planner_would_have_proposed` record keeps the ladder's shape for both
  engines (`rule`, `kind`, `function`, `rationale`; no `engine` or `fallback_reason` key), because
  anything derived from the TypeSafe call would defeat dedup. For that reason the rationale is
  recorded without the "Selected by the jev engine" sentence (it names the pool size and focus
  group), so a jev pick and the ladder fallback for the same candidate record the same bytes. Under jev the comparison's trace goes
  into the proposal's fingerprint-excluded `engine` field, and the panel line shows it; that line
  describes the comparison, not the operator's action.

## 10. CLI

- `vd engine pool --snapshot F --policy F [--json]`: offline, no key. Prints the pool
  (band, group, family, planet, entity, score basis), the rejection counts, the exact `state`
  and `questions`, and an estimated token count. `--json` prints `pool`, `rejected` and
  `request` (`state`, `questions`, `estimated_tokens`). The request is shown as built, before the
  send-time intent check of section 6 could substitute the intent text.
- `vd engine compare --snapshot F --policy F [--json]`: runs the ladder and the jev engine on
  the same input (jev needs the key and the network for a real answer). Exit codes: `0` the
  picks agree (also when a veto or the deadline decided both), `1` they disagree, `3` jev fell
  back to the ladder, `4` a load error. Prints the confidence, margin and top judgments.
  Both use the intent of the policy file you give them, never an override: the override lives in
  `$VEYDRIFT_HOME`, not in the policy, and these commands are offline and reproducible. Use `vd
  engine intent show` to see what a tick would judge against.
- `vd engine intent set|show|clear`: the adaptive-intent override (section 3). Exit codes: `0`
  done, `2` `set` refused, `4` policy load error.
- `vd plan run --engine ladder|policy|jev`: default `ladder` (offline, unchanged). `policy`
  follows `policy.engine.kind`; `jev` forces it. The panel gains the engine line; `--json`
  prints the `Action` only (its `engine` field says who chose it). Any other value exits `2`.
- `vd doctor`: three added lines: `engine: <kind>`, `TYPESAFE_API_KEY: set|unset`,
  `typesafe-sdk: importable|missing`.

Suggested rollout: keep `tier` at `advisor`, run `vd tick` on the jev engine for a while, and
read the `engine` blocks: the fallback rate, `agrees_with_ladder`, and whether the disagreements
are ones you would have made yourself. `vd engine compare` on a saved snapshot answers "would
jev have chosen differently here" without a tick.

## 11. Tuning

- **Write the intent as priorities and exclusions**, in plain sentences: name the kinds of
  development you want ("research", "defense", "colony growth", "ships"), their order, and what
  to avoid ("no attacks", "do not spend on logistics"). The model reads the intent against each
  candidate's `what` and `group`, so use those words (research, defense, colony, mines,
  storage, ships). Do not put numbers or thresholds in it (it cannot count); numbers belong in
  `reserves`, `limits` and `policy.strategy`.
- **Weights** (each `0..100`, only ratios matter). Raise `fit` to follow the intent more; raise `economy` to favour fast payback
  (this also favours already-developed planets, section 13); raise `urgency` to favour
  work that prevents waste; `focus` is the group-level nudge; `threat` only ever lifts defense.
- **Thresholds.** Raise `min_confidence`/`min_margin` to hand more ticks to the ladder; lower
  them to trust the model more. A high fallback rate under `low_margin` means two *groups* keep
  scoring alike (e.g. economy versus research): sharpen the intent so it says which matters more.
  Near-equivalent candidates within one group never cause it.
- **`timeout_s`** (0.5-30) is a time budget, not a hard deadline. One attempt may take up to
  `timeout_s` per phase (connect, read, write, pool) and a timeout is never retried; a fast
  failure (connection error, 429, 5xx) may retry once, and only if the retry still fits the
  budget. A peer that trickles bytes is bounded per chunk, not overall, so the wall-clock time can
  exceed `timeout_s`. **`max_candidates`** (2-60) caps the pool after the pre-trim.
- **Declared targets still matter**: `ship_targets`, `defense_targets`, `research_priority` and
  `building_priority` shape which candidates exist and appear in the facts.

## 12. Limits

- Jev is not a calculator. It cannot count, judge whether two numbers are close, or read dates;
  that is why every number is bucketed, why the economy term is computed in code, and why no
  question asks it to compare quantities.
- Numbers stay in code: costs come from the live API's `cost` object, affordability and reserve
  checks are the pool's and the guard's, and no cost-scaling function exists.
- The model judges qualitative fit; it does not generate actions or arguments.
- One request per tick (two under a manual override with jev configured). A pool of a couple of
  dozen candidates is roughly 11,000 estimated tokens (each candidate is sent inline with its
  two questions), still a small per-tick cost; `vd engine pool` prints the
  estimate for your own policy.
- Decisions are not reproducible bit for bit: the model may answer slightly differently for the
  same state. The dedup fingerprint is stable under probability jitter only while the pick itself
  is stable. A result near a gate threshold (`min_confidence`, `min_margin`, the high-stakes
  floors) can flip between the jev pick and the ladder fallback from one tick to the next, and
  there is no hysteresis: such a flip is a different proposal, not a duplicate.
- The ladder's diagnostics (`opportunities:` and the playbook's derivations) describe the
  ladder's bands; under jev they remain accurate as descriptions of what each band would pick.

## 13. Why cross-planet scoring is acceptable here

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

## 14. Safety

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
  enter the pool when nothing else is legal. Like Colonize and Deploy, a winner of these also needs
  the higher confidence floor, ladder idleness and model endorsement, else the ladder decides.
- A jev failure never fails a tick and never blocks one: it is a ladder decision with a reason.
- An agent intent override is honoured only with `adaptive_intent` on, before its expiry, with a
  supported version and a lifetime of at most 72 hours, and after the same validation as the policy
  intent; anything else is the policy intent, never an error. It changes no gate, flag or tier
  (section 3 covers what it can change: what the model endorses).
- The intent text is checked once more against the account's real planet ids, coordinates, wallet and
  signer immediately before each TypeSafe request (every planet the account owns, and other players'
  target ids only with a planet introducer), and replaced by the policy or default intent if it
  fails (section 6). That check, not the load-time one, is what a stale or empty `policy.planets`
  cannot defeat.
