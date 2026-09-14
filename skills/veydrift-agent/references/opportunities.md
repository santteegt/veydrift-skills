# Opportunities — every band's own candidate, independent of the ladder

`opportunities.py` is the module this document explains. Fully additive: zero changes to
`plan.py`/`candidates.py`/`guard.py`, no new policy field, no persisted state, no CLI
command of its own — this held for the original four families, still holds for
`transport` (added later by the ACS defense coordination plan's scope decision, see
below; its own write path, `generate_transport_candidates`, already allowlisted/guarded,
predates this module entirely and needed no new capability, only surfacing), and still
holds for the five ladder-band families added alongside `policy.strategy.planet_rotation`
(see "Bands 1-4: the opposite direction — diagnosing a band that's winning too often"
below).

## Why this exists: the ladder is a short-circuit, not "generate everything, then pick"

`plan_next_action` (`plan.py`) is a straight-line chain of early returns, one per band in
priority order (storage, building, research, unlock-chain, logistics, colonize, attack,
missile, noop). The moment an earlier band's candidate wins — a mine upgrade, say — the
function returns immediately. **Every later band's generator is never even called** that
tick, not called-and-discarded. Confirmed by direct read of `plan.py`'s control flow, not
inferred: each band is `if winner is not None: return _finalize(...)`, so Python simply
never reaches the code for a lower-priority band once an earlier one returns.

`attack_targets`/`missile_targets`/`foreign_debris_targets`/`colonize_targets` are already
fetched every tick, gated only by their own policy flag (`allow_combat`,
`allow_fleet_noncombat`, `strategy.colonize`) exactly like every other read in this
codebase — tier never gates a fetch anywhere (see the tier survey in `AGENTS.md`). But the
*data derived from them* — is there a raid target worth knowing about? an open colonize
slot? foreign debris to harvest? — was invisible on any tick where a higher-priority band
won, which in practice is most ticks. This module closes that gap: it calls the same
per-planet generators a second time, independent of the ladder, and reports every result
(plus, for Bands 1-4, the single winner each band's own `select_*` would pick right now —
see "Bands 1-4" below).

## The five per-planet families, and why exactly these five

| Family | Generator (`candidates.py`) | Gated internally on |
| --- | --- | --- |
| `attack` | `generate_attack_candidates` | `policy.actions.allow_combat` |
| `missile` | `generate_missile_candidates` | `policy.actions.allow_combat` |
| `colonize` | `generate_colonize_candidates` | `policy.strategy.colonize` |
| `foreign_harvest` | `generate_foreign_harvest_candidates` | `policy.actions.allow_fleet_noncombat` |
| `transport` | `generate_transport_candidates` | `policy.actions.allow_fleet_noncombat` |

`transport` needs its own dedicated call (`_scan_transport`, not `_scan_planet`'s
dispatch dict) — `generate_transport_candidates` takes `target_planets` as a required
**positional** 4th argument, unlike the other four generators' keyword-only
(`**target_kwarg`) shape. It reuses the same `target_planets` list `scan_opportunities`
already computed for the ladder's own ordering, so this adds no extra work.

**Not included: Deploy.** `select_logistics_candidate` dispatches across four families
(Transport, Deploy, local Harvest, foreign Harvest); Transport was excluded here on the
same reasoning as Deploy until the ACS defense coordination plan's explicit scope
decision to make every suggestion this codebase surfaces genuinely override-executable
(Transport's write path already existed and needed no new capability — only surfacing).
Deploy remains excluded: still the account's own fleet-logistics move, not something
external to be informed about, and this plan made no scope decision about it.

## Bands 1-4: the opposite direction — diagnosing a band that's winning too often

The five families above all diagnose a band the ladder never even reaches because
something *earlier* wins. Bands 1-4 have a mirror-image problem the same
`plan_next_action` early-return structure creates: **Band 2 (building) unconditionally
precedes Bands 3-4 (research, then shipyard/defense, then unlock-chain), with no
policy-configurable weight between them.** As long as any target planet has an empty
building queue and any mine/energy/infra candidate exists — which, on an established
economy, is essentially always true whenever the queue happens to be idle — Band 2 wins
and the tick ends there before research/shipyard/unlock-chain are even evaluated.
Declaring more `research_priority`/`ship_targets`/`defense_targets`/`building_priority`
names doesn't help: those fields only decide *which entity* wins within their own band,
never the relative order *between* bands. An account whose building queue empties often
relative to its tick cadence can see Band 2 win nearly every tick this way, leaving
research/ships/defense with zero automatic proposals for an extended stretch even though
nothing is misconfigured — reported directly from a real account's tick history, not yet
independently confirmed against its logs by this codebase's own tooling.

This is not fixed by changing the ladder — doing so would need real design work (a
band-level fairness scheme, analogous to `policy.strategy.planet_rotation`'s planet-level
one, with its own scoring/rotation questions across fundamentally different candidate
types) that hasn't been undertaken. What this module adds instead is visibility: five
more families (`storage`, `building`, `research`, `shipyard`, `unlock_chain`) report the
single winner Bands 1-4 would each independently pick *right now*, regardless of which
one the ladder itself picked this tick. A human or agent who notices the tick loop
repeatedly firing the same band can consult this report to see what's queued up behind
it, and, if that divergence from the planner's own judgement is genuinely warranted,
force one of the alternatives via `vd tick --action` + `allow_agent_action_override` (see
`references/manual-action-override.md`) — the codebase's existing, already-safe escape
hatch for exactly this class of situation. Nothing here changes automatic behavior; it is
a diagnostic surface, consulted manually, not a new automatic rotation.

| Family | Selector (`candidates.py`) | Real-time precondition replicated (verbatim from `plan.py`) |
| --- | --- | --- |
| `storage` | `select_storage_candidate` | None needed — self-gates on building-queue-empty internally |
| `building` | `select_building_candidate` (per planet) | Only planets whose `QueueKind.BUILDING` queue is currently empty |
| `research` | `select_research_candidate` | Only when `snapshot.research_queue is None` |
| `shipyard` | `select_shipyard_candidate` | None needed — self-gates on `economy_on_track` internally |
| `unlock_chain` | `select_unlock_chain_candidate` | None — reached unconditionally, same as the real ladder would in this situation |

Unlike the five per-planet families above (which call a bare `generate_*` and report
*every* viable candidate), these five call the same `select_*` functions `plan.py` itself
calls and report only the **winner** — `origin_planet_id` comes from `Action.planet_id`
directly here, not the launch-planet parameter `_to_finding` uses, since these are
ordinary planner-shaped actions where that field is always set. Reporting raw candidates
instead would have been wrong: `select_building_candidate` alone folds in storage-cap and
current-affordability preconditions that a bare `generate_mine_candidates` call knows
nothing about, so listing its raw output would surface picks the real ladder would never
actually make.

Expect the winning band's own finding to show up here too, most of the time — that's not
a bug, it's confirmation that this survey and the real ladder agree on what's
`plan_next_action`'s actual pick this tick. The new information is everything *else* in
the list: what's ready and waiting behind whatever band won.

## No new gating logic needed — the generators already self-gate

Every one of the five per-planet generators checks its own flag as its very first line
and returns `[]` immediately if it's off — confirmed by direct read of each function
body, not assumed. This means `opportunities.py` needs **zero** gating logic of its own
for these five: a policy with every relevant flag at its default (off) produces an empty
`OpportunityReport` automatically, because each generator call returns nothing. Turning
`opportunities.py` into something that respects a flag it doesn't otherwise know about
would have been a real duplication-of-truth risk (the exact kind of drift `AGENTS.md` §5
warns about elsewhere in this codebase for the two-enforcement-layer allowlists) —
reusing the generators' own internal checks avoids that entirely. The ladder-band
families (previous section) follow the identical rule where a flag exists
(`allow_building`, `allow_research`) — the two that instead need an explicit external
precondition (building, research) do so only for a real-time *queue state*, not a
`policy.*` flag, since neither `select_building_candidate` nor `select_research_candidate`
takes queue state as an input at all.

All five per-planet generators are also pure and side-effect-free: no `http`/`read`
calls, no mutation, nothing that could raise on bad network data. Every network-shaped
input arrives as an already-fetched parameter the caller supplies. Calling them a second
time, purely off data `tick.py` already fetched for the ladder, costs nothing extra — no
network call, no meaningful CPU. The same is true of the five `select_*` calls the
ladder-band families use.

## One finding per launch planet, not one global winner (the five per-planet families only)

Unlike the ladder (which calls `select_attack_candidate`/etc. — pick the single best
target across the whole account), `opportunities.py` calls each of the five per-planet
generators once per owned planet directly. Since fuel cost and reachability are
launch-planet-dependent, a multi-planet account can have a different best raid target
reachable from each of its planets — `scan_opportunities` surfaces all of them, not just
the account-wide best one. Each generator, called for one planet, still returns at most
one candidate for that planet (the single best reachable target from there) — so the
report's size scales with owned-planet count, never with the number of possible targets
in the universe. **The five ladder-band families are the opposite shape on purpose** —
they call `select_*`, not `generate_*`, and report the one account-wide (or, for
`building`, one per-planet) winner each band would actually pick, mirroring what the real
ladder computes rather than every raw candidate.

## No ranking score, only free text

`OpportunityFinding` carries no numeric score field at all, for any of the ten families.
For the five per-planet families, `Candidate.score` is hardcoded `None` in the first
place (only the mine/energy/building-style economic candidates carry a payback-hours
score) — the ranking rationale lives entirely in `Candidate.score_basis`/
`Action.rationale` as free text (e.g. "highest-raidable reachable target...", "most
heavily defended reachable target..."). For `building`/`shipyard` specifically, the
winner *can* carry a real internal payback-hours `Candidate.score` (these are the
economically-scored families) — that number is still never surfaced as its own field,
only folded into `detail` via the same `action.rationale or score_basis` text `_to_finding`
already uses, so the model's shape stays uniform across all ten families regardless of
which ones happen to have a real score internally.

## No persisted de-duplication, unlike radar's `resolved_attack` signal

An opportunity is a *current-state fact* — a raid target or colonize slot that's live
today may still be live tomorrow, and reporting it again is correct, not spam, the same
way the ladder's own `Action.alternatives` already re-reports non-winning candidates
every tick with no dedup. This is the opposite situation from radar's `resolved_attack`
signal (a one-time historical *event*, which must never re-alert forever) — see
`references/radar.md` for that contrast. No `radar-state.json`-equivalent file exists for
opportunities, and none is needed.

## Reporting: report line only, no unconditional `strategy.md` entry

The tick report gains an `opportunities:` line (silent when there are no findings) and
`proposals.jsonl` gains an `"opportunities"` key on every record. **Unlike radar,
opportunities do not force an unconditional `strategy.md` append.** Radar's findings are
naturally transient (an incoming fleet arrives once; a resolved attack is de-duplicated;
debris eventually gets harvested), so writing one line to `strategy.md` per finding is
rare in practice. An opportunity is standing state — the same reachable raid target can
stay true for many ticks in a row — so giving it radar's unconditional-per-tick treatment
would spam `strategy.md` every cadence interval for something that hasn't changed. The
information is still fully available in every tick's own printed report and
`proposals.jsonl`; only the persistent narration log is deliberately left alone.

## Verifying this end-to-end

Against a scratch `$VEYDRIFT_HOME`, never a real one, with `policy.actions.allow_combat:
true` (any tier, including `advisor`) and a planet holding real combat ships:

```bash
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd tick init
# edit the scratch policy: actions.allow_combat = true
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd tick --dry-run
```

An `opportunities:` line should appear whenever a live, reachable raid target exists —
even on a tick whose actual proposed action is something else entirely (a mine upgrade,
say). A clean account with no viable target, or with combat still disabled, produces no
line at all, same as before this feature existed.

For the five ladder-band families, no special setup is needed — any account with a
buildable mine/energy candidate and a real building queue state should show a `building`
finding (and, if the research/shipyard preconditions above happen to hold on that same
tick, `research`/`shipyard` findings alongside it) on essentially every dry-run tick,
proving the survey and the real ladder agree on the tick's actual pick.
