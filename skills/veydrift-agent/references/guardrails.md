# Guardrails — `vd guard`, the 20 gates

Source of truth for *what* each gate checks, *why*, what data it needs, what it does when
that data is missing, and how to configure it. `guard.py` is the frozen contract this
document explains; nothing here should contradict the code, and if it ever appears to, the
code wins and this file has a bug.

## The rule every gate is built around

> A guard must not pass vacuously when the data it checks is absent. If `energy` is
> `None` because the API omitted it, that is not an energy check that passed — it is a
> check that could not run, and it must block or escalate rather than return `PASS`.

Two gates hit this on **every single tick**, not just as an edge case, because of how the
rest of this codebase is shaped:

- **`energy`** — a snapshot's `PlanetSnapshot.energy` is only populated when the
  `/infrastructure` route returns an `energyBalance` block (`read.py`). If it doesn't,
  `energy` BLOCKs, full stop — see "Vacuous-pass audit" below for the exact reasoning.
- **`eth_floor`** — `Snapshot.eth_balance_wei` is **always** `None` coming out of
  `read.py`'s `snapshot` command. Quoting `models.py`'s own comment on that field: *"no
  read route reports wallet ETH balance; walletctl's job."* If this gate read that field
  directly it would vacuously PASS on every tick, forever. Instead it takes
  `eth_balance_wei` as a separate, explicitly-supplied parameter that `tick.py`
  best-effort-populates from `walletctl status` — and BLOCKs/ESCALATEs whenever that
  parameter is `None`, which is most of the time at tier 1 (nothing needs to send, so
  `tick.py` doesn't even bother calling `walletctl status` there — see its own gate row
  below).

Every gate is evaluated on every tick, regardless of what earlier gates decided —
`GuardReport.verdicts` is a fixed-length list of exactly 20 entries every time (17
through Phase 4; Phase 5c, 2026-08-17, added `mission_type`; a later change, 2026-08-20,
added `game_paused`; commit 2 of the launch-actions plan, 2026-08-28, added
`fleet_slots` — see its own row below). A blocked
proposal is exactly as informative as an allowed one, which is the entire point of never
short-circuiting: `logs/proposals.jsonl` is the audit trail, not just the last mile. (A
content-identical repeat of the immediately-previous proposal is not re-persisted to
`proposals.jsonl` — `tick.py`'s `_fingerprint_proposal` dedup — but every gate still runs
to completion on every single call; only the already-complete result's *write* is
conditional.)

## `evaluate_guardrails()` is pure — `tick.py` gathers the live facts

`guard.py` makes zero network calls and never shells out to `walletctl`. Everything a
gate needs beyond the `Action`/`Snapshot`/`Policy`/`AgentState` it's handed comes in as an
explicit keyword parameter:

| Parameter | Who supplies it, and how | Used by |
| --- | --- | --- |
| `killswitch_active` | `state.killswitch_active()` | `killswitch` |
| `live_addresses` | `tick.py`'s `_live_addresses()` — a live `GET /runtime-config`, extracting `gameContractAddress`/`contractAddress` (mirrors `veydrift-wallet/src/allowlist.ts` exactly) | `address` |
| `unsigned_tx` | `tick.py`'s `_walletctl_build()` — shells out to `walletctl build --action ... --out ...` | `address`, implicitly `gas` (via `gas_cost_wei`) |
| `gas_cost_wei` | **`walletctl build`'s `estimatedCostWei` field — a wei quantity (gas units × gas price), never its `gas` field (gas *units* alone, ~1e5 on Base).** Confirmed defect, fixed 2026-08-12: `tick.py` used to pass the `gas` field straight through under a `gas_estimate_wei` name, so this gate compared gas *units* against wei-scale ceilings and could never fire. `gas_cost_wei` is the corrected name end-to-end (`guard.py`'s `_gate_gas` parameter, `evaluate_guardrails`'s keyword, `tick.py`'s local variable) specifically so this can't silently regress back to a unit mismatch. See `tests/test_tick.py::test_walletctl_build_cost_crosses_the_unit_boundary_into_the_gas_gate` for the boundary-crossing regression test. | `gas` |
| `eth_balance_wei` | `tick.py`'s `_walletctl_eth_balance_wei()` — best-effort-parses `walletctl status`'s plain-text `balance: X ETH` line | `eth_floor` |
| `outgoing_colonize_count` | `tick.py`'s `_outgoing_colonize_count()` (commit 4 of the launch-actions plan) — a live `GET /wallet/{addr}/fleet-visibility`, counting `outgoing` entries whose `missionType` is `"Colonize"` and `status` is still `"Outbound"`. Only fetched for an actual Colonize proposal; `None` on any fetch failure. | `mission_type` (via `_colony_cap_violation`) |
| `now` | defaults to `datetime.now(UTC)`; a test-supplied override for `index_lag` | `index_lag`, `gas` |

This mirrors the same posture `plan.py` takes with `killswitch_active` /
`pending_tx_unreconciled`: keep the decision function pure and fixture-testable, and let
the one module that's allowed to touch the network/subprocess (`tick.py`) gather the
facts.

## The 20 gates

| # | Gate | What it checks | Data it needs | On missing data |
| - | --- | --- | --- | --- |
| 1 | `killswitch` | `$VEYDRIFT_HOME/KILLSWITCH` absent | `killswitch_active` (bool, never missing) | n/a |
| 2 | `tier` | `action.function` ∈ the policy tier's allowed-to-*submit* set | `Action.function`, `Policy.tier` | function absent (off-chain action) → PASS trivially; function present but unknown to any tier → BLOCK |
| 3 | `mission_type` | **(Phase 5c, 2026-08-17)** for `launchFleetMission` only: `Action.mission_type` ∈ the allowed set {Transport(0), Deploy(1), Colonize(2), Harvest(4)} — default-deny, independent of `tier` | `Action.mission_type` (only checked when `Action.function == "launchFleetMission"`) | not `launchFleetMission` → PASS trivially; `mission_type is None` → BLOCK (never "nothing to check" — a malformed action) |
| 4 | `prerequisites` | the proposed entity's on-chain requirements (`techtree.py`, transcribed from `VeydriftDependencies.sol`/`VeydriftCatalog.sol`) are met on the target planet, plus shield-dome/missile-slot caps; for a `launchFleetMission` action, dispatches instead to a check that the origin planet actually owns every ship committed | target planet's building levels, account technology levels (or, for a fleet mission, the origin planet's ship counts) | any unmet requirement, or any level the snapshot didn't report → BLOCK; a shield-dome/missile-slot count the snapshot didn't report → BLOCK; a fleet mission's ship count the snapshot didn't report → BLOCK |
| 5 | `fleet_slots` | **(commit 2 of the launch-actions plan, 2026-08-28)** for `launchFleetMission` only: `fleet_slots_active < fleet_slots_limit` — the contract reverts `FleetSlotLimitReached(1 + ComputerTechnology)` otherwise | `Snapshot.fleet_slots_active`/`.fleet_slots_limit` (only checked for `ActionKind.FLEET_MISSION`) | not a fleet mission → PASS trivially; either field `None` → BLOCK (never "assume a slot is free") |
| 6 | `address` | on-chain destination ∈ the **live** `/runtime-config` address set | `live_addresses`, a built `unsigned_tx` | either missing → BLOCK, never PASS |
| 7 | `abi_hash` | live `deploymentAbiHash` == pinned | `Snapshot.deployment_abi_hash` | missing or mismatched → BLOCK **all** writes |
| 8 | `health` | `/health` reported `ok && readiness.ready`, **or** (2026-08-22) a positively confirmed combat-only degradation — `Snapshot.combat_only_degradation()` | `Snapshot.health_ok`, `.readiness_ready`, `.degradation_reasons`, `.game_maintenance`, `.randomness_readiness` | n/a — `combat_only_degradation()` is itself fail-closed (see below) |
| 9 | `game_paused` | **(added 2026-08-20)** `gameMaintenance.paused` is not true — a chain-side maintenance pause means any write would revert | `Snapshot.game_maintenance` | `None` (gameMaintenance missing from `/health`) → BLOCK — "cannot confirm not paused" is not "confirmed not paused"; see its own section below |
| 10 | `index_lag` | a prior receipt is indexed within `max_index_wait_s` | `AgentState.pending` | nothing pending → PASS (legitimately nothing to wait on, not missing data); pending but no receipt yet → WARN; past the deadline → BLOCK |
| 11 | `affordability` | `resourcesAsOfNow` ≥ live `Action.cost` | target planet in `Snapshot.planets` | planet not found → BLOCK |
| 12 | `energy` | post-action `produced ≥ required` | `PlanetSnapshot.energy` | `None` → BLOCK (**the flagship case** — see above) |
| 13 | `storage_overflow` | no resource hits cap before the next tick, unaddressed | `resources_as_of_now` / `production_per_hour` / `storage_caps` | see "Documented limitation" below — this one gate cannot fully honour the no-vacuous-pass rule given the frozen `models.py` |
| 14 | `fields` | `fields_used / fields_total` < 100%, warn at `field_warn_pct` | `PlanetSnapshot.fields_used`/`fields_total` | either `None`, or `fields_total == 0` → BLOCK |
| 15 | `reserve` | spend preserves `policy.reserves` floors | target planet's `resources_as_of_now` | planet not found → BLOCK |
| 16 | `gas` | `gas_cost_wei` ≤ `gas_per_tx_wei`, and today's cumulative + this tx ≤ `gas_per_day_wei` — **wei throughout, never gas units** | `gas_cost_wei`, `AgentState.cumulative_gas_wei_today` | no estimate → ESCALATE (this is normal and expected at tier 1 — see below) |
| 17 | `eth_floor` | wallet ETH ≥ `eth_gas_floor_wei` | `eth_balance_wei` (**never** `Snapshot.eth_balance_wei`) | `None` → ESCALATE (**the other flagship case**) |
| 18 | `value_ceiling` | `cost / holdings` > `escalate_above_pct_of_resources` → ESCALATE | target planet's `resources_as_of_now` | planet not found (with nonzero cost) → BLOCK; zero holdings with nonzero cost → ESCALATE (can't compute a %, not "0% so fine") |
| 19 | `idempotency` | no pending tx for the same idempotency key (`(planet, function, entity)`, extended with `mission_type`/target for a fleet mission, or `mission_id` for a resolve action — see `guard.idempotency_key`'s own docstring) | `AgentState.pending` | n/a — presence/absence is always knowable |
| 20 | `revert_streak` | same action reverted < `policy.escalation.on_revert_count` times | `AgentState.revert_counts` | n/a — a missing key means zero reverts, which is a real fact, not missing data |

**`health`'s exception for a combat-only degradation (2026-08-22).** Live, during this
fix's own planning: `/health` returned HTTP 503 (persistently, not a one-off), with a
body reporting `ok: false` while `readiness.ready: true`, `readiness.degradationReasons:
[]`, `gameMaintenance.paused: false`, and `randomnessReadiness.ready: false` — a
combat-only subsystem ("New attacks are temporarily paused"). `allow_combat` is
read-and-ignored everywhere in this codebase (`ActionsCfg.allow_combat`'s own docstring),
so combat is unconditionally unreachable regardless of policy, and this signal can never
affect a proposal this codebase would make. `Snapshot.combat_only_degradation()` is a
structural, fail-closed positive-confirmation check — `readiness_ready` True, no other
`degradation_reasons`, `game_maintenance` positively confirmed not paused, and
`randomness_readiness` positively confirmed not-ready (never `None`/unconfirmed) — never
an allowlist of known-safe reason strings, so it stays robust if
`randomness_readiness.reasons`' wording changes. Any other combination (a genuinely
unhealthy `readiness.ready`, a real pause, an unrelated degradation reason like
`health_unhealthy.json`'s RPC-unfinished-requests) still `BLOCK`s exactly as before. Both
`plan.py`'s rung 1 and this gate independently call the same `Snapshot` method — the
`game_maintenance`/`degradation_reasons` sharing precedent is not a parsing-drift risk:
the two-layer property this project maintains is about independent *decisions* (NO-OP vs
BLOCK), not independently re-parsing raw JSON. `read.py`'s `_fetch_or_exit` also
defensively recovers a parseable `/health` 5xx body (narrowly scoped to `/health`
specifically — every other route's 5xx still hard-fails unchanged), since this backend
signals the same condition via HTTP 503 as often as via a 200-with-`ok:false` body.

**`game_paused` is the second, independent line of defense behind `plan.py`'s rung `1b`**
(the first). Where `1b` is discretionary — `escalation.on_game_paused` lets an operator
choose ESCALATE vs. NO-OP — `game_paused` BLOCKs unconditionally: by the time a proposal
reaches `guard.py`, a confirmed pause is a hard safety fact, not a policy choice, and a
stale/racy proposal built just before a pause began must still be caught here even if
rung `1b` didn't fire on it. Fail-closed exactly like `energy`: `Snapshot.game_maintenance
is None` means "this check could not run" (the `/health` response didn't carry
`gameMaintenance` — genuinely possible on an older backend shape, confirmed against the
pre-2026-08-20 `health.json` fixture, which predates the field entirely), never "confirmed
not paused." `Snapshot.game_paused` (the flat bool) is a convenience flag for `plan.py`
and must never substitute for checking `game_maintenance` here — see `models.py`'s own
docstring on that field. Takes only `snapshot`, not `action`: unlike `energy` (scoped to
one planet) a pause blocks every write universally.

**`mission_type` mirrors `veydrift-wallet`'s `allowlist.ts` calldata-level mission-type check**
— `guard._ALLOWED_MISSION_TYPES` and `allowlist.ts`'s `OPERATOR_ALLOWED_MISSION_TYPES` are the
same set, verified identical by `test_tier_map_agrees_with_the_wallet_engines_allowlist`
(extended this phase). Combat mission types (3, 5, 6, 7, 8, 9) are never in that set — enabling
one requires an actual source change to both files, never a policy flag; this friction is
deliberate.

**`prerequisites` is new (this work package) and independently re-derives its inputs from
`Snapshot`, never trusts `plan.py`'s own filtering** — the same posture `_gate_energy`
already takes toward re-deriving the energy invariant rather than calling `plan.py`. It
closes a real, previously-live bug: nothing in `plan.py` or `guard.py` checked an on-chain
prerequisite of any kind before this change, so a fresh planet's rung-7 research pick
(Energy Technology, lowest level, tie-break by id) could be proposed and executed against
a planet with no Research Lab — a guaranteed on-chain revert the first time tier ≥ 2
actually tried it, since Energy Technology requires Research Lab ≥ 1
(`VeydriftDependencies.sol`'s `requireResearch`, composed from
`VeydriftCatalog.researchLabRequirement`). The same hole let rung 8 propose a Rocket
Launcher on a planet with no Shipyard (`requireDefense`'s unconditional
`shipyardLevel == 0` revert). `plan.py` is now filtered to never emit such a proposal in
the first place (see `references/strategy-playbook.md`); `prerequisites` is the
independent second check, exactly as `_gate_energy` is a second look at the energy
invariant `plan.py` already computed. **The tech-tree table itself is transcribed from
contract source and has never been validated against a live revert** — see
`techtree.py`'s own module docstring for the exact transcription methodology and
`tests/test_techtree.py` for the spot-checks pinned against the Solidity.

**`affordability`'s BLOCK detail includes a per-resource ETA.** For each resource short of
the proposed cost, the detail string now also states an estimated "affordable in ~Xh Ym"
(`calc.hours_to_afford`, from live `production_per_hour`), or an explicit "never
affordable" when that resource's cost exceeds its `storage_caps` value or its production
rate is zero. This is purely informational — the `BLOCK` decision itself is still decided
solely by `resources_as_of_now.covers(action.cost)`, unchanged.

**Why `gas`/`eth_floor` ESCALATE on missing data instead of BLOCK:** an ESCALATE (not
BLOCK) still lets a tier-1 dry run complete and print a full, honest report — the whole
point of `--dry-run` is that nothing is ever sent regardless of the guard decision, so
there's no safety reason to make the *decision itself* look more alarming than it is. At
tier ≥2, `tick.py`'s step 7 only proceeds to `walletctl send` when the decision is
`ALLOW`, so an ESCALATE from either gate already prevents a real send — it just does so
without dramatizing a routine "no gas estimate yet" state as a hard BLOCK. `address` and
`abi_hash`, by contrast, BLOCK on missing data: those failures mean "I cannot even tell
you this transaction is going to the right contract," which is categorically worse than
"I don't yet know exactly how much gas this will cost."

**`tier` allows `startShipProduction` from `economy` up, same as the other four economy
functions:** the project's tier table lists `startBuildingUpgrade`, `startResearch`,
`resolveFleetMission`, `startDefenseProduction`, and
**`startShipProduction`** as submittable from T2 (`economy`); T3 (`operator`) adds only
`launchFleetMission`. This corrects an earlier version of this document that
omitted `startShipProduction` from every tier's allowed set — a confirmed defect: `plan.py`
rung 8 proposes ships when `policy.actions.allow_ships` is set, but with no tier granting
the function, every such proposal was permanently `BLOCK`ed regardless of tier, making the
policy knob dead config (the same failure mode `allow_fleet_noncombat` is in today — see
§4 of the fix list this file was last revised against). `guard.py`'s
`_MIN_TIER_FOR_FUNCTION` now maps `startShipProduction` to `Tier.ECONOMY`, mirroring
`ECONOMY_SIGNATURES` in `veydrift-wallet/src/allowlist.ts`. Producing ships spends
resources on your own planet, the same risk profile already accepted for
`startDefenseProduction` at the same tier; combat remains gated separately, at the mission-
type level on `launchFleetMission`, regardless of this change.
`tests/test_guard.py::test_tier_allows_start_ship_production_from_economy_up` pins the
corrected behaviour down (BLOCK at `advisor`, PASS at `economy` and `operator`) so it
doesn't quietly regress.

### Documented limitation: `storage_overflow` cannot fully honour the no-vacuous-pass rule

`PlanetSnapshot.storage_caps` and `.production_per_hour` are plain `Resources` objects
that default to `0`/`0`/`0` — `models.py` (frozen) gives them no `None` state to
distinguish "the API omitted this" from "this planet genuinely has zero storage capacity
or production right now." The real planet-664 fixture *is* exactly that second case: a
freshly-settled, zero-state planet with every production rate at 0/hr. If this gate
treated an all-zero `storage_caps` as "missing, BLOCK," it would falsely block every
proposal on a brand-new planet forever.

So `storage_overflow` takes the documented, honest middle path: it only evaluates a
resource whose storage cap is **positive**, and says nothing about resources whose cap
is exactly zero — which means a genuinely-missing cap (as opposed to a genuinely-zero
one) silently isn't checked, rather than either false-blocking real zero-state planets or
false-passing missing data. `tests/test_guard.py::test_storage_overflow_ignores_a_zero_cap_resource_documented_limitation`
pins this behaviour down. If `models.py` ever grows a way to distinguish "field omitted"
from "field is zero" (e.g. making these `Resources | None`), this gate should be revisited
first.

## Configuring the gates

Every numeric ceiling a gate checks against comes from `policy.json`'s `limits`,
`reserves`, `storage`, and `escalation` blocks — nothing here is
hardcoded beyond the two contract-derived constants `guard.py` duplicates from
`skills/veydrift-wallet/abi/PINNED.json` (`PINNED_ABI_HASH`) and from
`veydrift-wallet/src/allowlist.ts` (`_MIN_TIER_FOR_FUNCTION`, the tier→function map).
Those two are duplicated rather than imported because this package must never import
from a TypeScript project — if the wallet
engine is ever re-pinned or its tier map changes, both copies need updating together;
`references/abi-pinning.md` (WP4a's) is the canonical description of the pin itself.

| Policy field | Gate(s) |
| --- | --- |
| `limits.gas_per_tx_wei`, `limits.gas_per_day_wei` | `gas` |
| `limits.eth_gas_floor_wei` | `eth_floor` |
| `limits.escalate_above_pct_of_resources` | `value_ceiling` |
| `limits.max_index_wait_s` | `index_lag` |
| `limits.field_warn_pct` | `fields` |
| `reserves.{metal,crystal,deuterium}` | `reserve` |
| `storage.hours_to_cap_trigger` | `storage_overflow` |
| `escalation.on_revert_count` | `revert_streak` |
| `tier` | `tier` |

`prerequisites` has no policy knob — it is derived entirely from `Snapshot` (building/
technology levels) against the fixed `techtree.py` table, the same posture `abi_hash`
takes toward its pinned constant.

`game_paused` also has no policy knob of its own — `escalation.on_game_paused` only
governs `plan.py`'s rung `1b` (ESCALATE vs. NO-OP, the discretionary call), not this gate,
which BLOCKs unconditionally on a confirmed pause regardless of that flag. See its row
above and the paragraph directly under the gate table.

## Offline testing: `vd guard run`

```
vd guard run --action a.json --snapshot s.json --policy p.json [--killswitch] [--json]
```

No network calls — `address`, `gas`, and `eth_floor` will honestly report their
missing-data verdict (`BLOCK`/`ESCALATE`) every time, since this entrypoint never supplies
`live_addresses`, `unsigned_tx`, `gas_cost_wei` or `eth_balance_wei`. That's not a bug
in the command; it's the same no-vacuous-pass behaviour exercised by hand. `tick.py` is
the fully-supplied, online caller — see `references/scheduling.md`.
