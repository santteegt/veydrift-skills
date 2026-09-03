# Opportunities — attack/missile/colonize/foreign-harvest candidates independent of the ladder

`opportunities.py` is the module this document explains. Fully additive: zero changes to
`plan.py`/`candidates.py`/`guard.py`, no new policy field, no persisted state, no CLI
command of its own.

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
won, which in practice is most ticks. This module closes that gap: it calls the same four
generators a second time, independent of the ladder, and reports every result.

## The four families, and why exactly these four

| Family | Generator (`candidates.py`) | Gated internally on |
| --- | --- | --- |
| `attack` | `generate_attack_candidates` | `policy.actions.allow_combat` |
| `missile` | `generate_missile_candidates` | `policy.actions.allow_combat` |
| `colonize` | `generate_colonize_candidates` | `policy.strategy.colonize` |
| `foreign_harvest` | `generate_foreign_harvest_candidates` | `policy.actions.allow_fleet_noncombat` |

**Not included: Transport/Deploy.** `select_logistics_candidate` dispatches across four
families (Transport, Deploy, local Harvest, foreign Harvest); only the *external
opportunity* half of that set — foreign Harvest — belongs here. Transport/Deploy are the
account's own fleet-logistics moves, not something external to be informed about.

## No new gating logic needed — the generators already self-gate

Every one of the four generators checks its own flag as its very first line and returns
`[]` immediately if it's off — confirmed by direct read of each function body, not
assumed. This means `opportunities.py` needs **zero** gating logic of its own: a policy
with every relevant flag at its default (off) produces an empty `OpportunityReport`
automatically, because each generator call returns nothing. Turning `opportunities.py`
into something that respects a flag it doesn't otherwise know about would have been a
real duplication-of-truth risk (the exact kind of drift `AGENTS.md` §5 warns about
elsewhere in this codebase for the two-enforcement-layer allowlists) — reusing the
generators' own internal checks avoids that entirely.

All four generators are also pure and side-effect-free: no `http`/`read` calls, no
mutation, nothing that could raise on bad network data. Every network-shaped input
arrives as an already-fetched parameter the caller supplies. Calling them a second time,
purely off data `tick.py` already fetched for the ladder, costs nothing extra — no
network call, no meaningful CPU.

## One finding per launch planet, not one global winner

Unlike the ladder (which calls `select_attack_candidate`/etc. — pick the single best
target across the whole account), `opportunities.py` calls each generator once per owned
planet directly. Since fuel cost and reachability are launch-planet-dependent, a
multi-planet account can have a different best raid target reachable from each of its
planets — `scan_opportunities` surfaces all of them, not just the account-wide best one.
Each generator, called for one planet, still returns at most one candidate for that
planet (the single best reachable target from there) — so the report's size scales with
owned-planet count, never with the number of possible targets in the universe.

## No ranking score, only free text

`Candidate.score` is hardcoded `None` for all four of these families (only the
mine/energy/building-style economic candidates carry a payback-hours score) — the
ranking rationale lives entirely in `Candidate.score_basis`/`Action.rationale` as free
text (e.g. "highest-raidable reachable target...", "most heavily defended reachable
target..."). `OpportunityFinding.detail` carries that string directly rather than
inventing a new numeric score with its own units to get wrong.

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
