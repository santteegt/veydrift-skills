# Radar — the incoming-attack / resolved-battle / debris tracker

`radar.py` is the frozen-in-spirit contract this document explains. Fully self-contained
inside `veydrift-agent`: radar never constructs an on-chain `Action`, never calls
`guard.py`, and `veydrift-wallet` has no part in it at all.

## Why this exists: `incoming_fleets` alone cannot show a resolved attack

The design point for this whole module, not a footnote: `snapshot.incoming_fleets`
(`/wallet/{addr}/fleet-visibility`'s `incoming[]`) only ever lists **future arrivals**. An
Attack mission that has already resolved has already fallen out of that field entirely —
there is no route through the existing `Snapshot` composition that shows a battle that
already happened. A wallet that checks only `incoming_fleets` after the fact sees
`incoming: none` and reasonably-but-wrongly concludes nothing is wrong, even while sitting
on real combat losses and a freshly-created debris field a third party may already be
inbound to harvest.

Two design decisions follow directly from this, both load-bearing, not incidental:

1. **`incoming_fleet` (future arrivals) is not the only signal.** `resolved_attack` (past
   combat, sourced from `/wallet/{addr}/missions`) exists specifically because
   `incoming_fleet` structurally cannot see an attack that has already resolved.
2. **Every incoming mission is reported, not filtered to a `hostile` flag.**
   `IncomingFleet.hostile` is hardcoded `true` for every row (`read.py`'s
   `_incoming_fleet`, a known, deliberately-unfixed gap — `AcsDefend`/`DefenseHold`
   allied-reinforcement mission types could in principle appear in your own `incoming`
   array without being an attack, and this has never been observed live). Rather than
   filter on a flag that's never been validated, radar reports `mission_type_name`
   directly — a non-Attack mission (a Harvest, say) targeting your planet can itself be
   diagnostic (debris doesn't appear on an occupied slot for no reason), not noise to
   filter out.

## Three independent signals

| Signal | Source | What it catches | Notes |
| --- | --- | --- | --- |
| `incoming_fleet` | `/wallet/{addr}/fleet-visibility`'s `incoming[]` (`read.fetch_fleet_visibility`) | Future arrivals — any mission type targeting a tracked planet | Every row reported, not filtered to `hostile` (see above) |
| `resolved_attack` | `/wallet/{addr}/missions`'s `report`-bearing rows (`read.fetch_missions`) | Attacks that have already resolved | De-duplicated per wallet against `radar-state.json`; see "The real row shape" below |
| `debris` | `/universe/galaxies/{g}/systems/{s}`'s `debrisField` (`read.fetch_universe_system`) | A non-empty debris field on a tracked planet's own slot | Same route `tick._own_planet_debris` already reads (not reused directly — see radar.py's module docstring for why) |

## The real row shape (corrects the initially-assumed one)

`references/api-routes.md` §3.14 documents `/wallet/{addr}/missions`'s `rows[]` as a
tagged union: `{kind: "mission", mission, report?}` or `{kind: "battleReport", report}`.
**An early implementation of `_resolved_attack_findings` filtered on `kind ==
"battleReport"` — a live check against real account data showed that's wrong.** A resolved
Attack comes through as `kind: "mission"` with a top-level `report` object attached, not
as a separate `kind: "battleReport"` row — that half of the documented union has never
actually been observed. The bug meant that implementation would have silently found
nothing for a resolved attack it was specifically built to catch.

Fixed by keying off `report` truthiness instead of `kind`:

```python
for row in missions_data.get("rows") or []:
    report = row.get("report")
    if not report:
        continue
    ...
```

This covers both the confirmed real shape and the documented-but-unobserved
`kind: "battleReport"` one, should it ever actually appear. A real, unedited fixture (not
a synthetic guess) pins this shape at `tests/fixtures/wallet_missions_resolved_attack.json`,
and a dedicated regression test in `tests/test_radar.py` guards against re-introducing the
`kind` filter.

One more field-shape note from that same fixture: `report.blockNumber` arrives as a decimal
**string**, the same convention as every other numeric-looking field on this API —
`check_targets` sorts on it as `int`, never lexically (a lexical sort would misorder
differing-length block numbers).

## De-duplication: `radar-state.json`

Resolved-attack detection needs a "have I already alerted on this one" marker. Not folded
into `agent-state.json` (`AgentState`) — that model is implicitly single-wallet-shaped
(`policy.wallet`), but `vd radar check --alliance-id` can watch many wallets that are not
`policy.wallet` at all. `radar-state.json` is keyed by wallet address instead
(`state.RadarState`/`state.WalletRadarState`), and **both entry points share the same
file** — a resolved attack already surfaced by a scheduled `vd radar check` won't be
re-reported by the next `vd tick`, and vice versa.

Per wallet, `check_targets` sorts that page's qualifying rows newest-first by
`blockNumber`, walks from the newest until it hits the previously-seen cursor (or
exhausts the page), and reports everything strictly newer. A wallet with no cursor yet
(`last_seen_mission_id is None`) reports **every** qualifying row seen that run —
fail-closed toward reporting, not toward silence, on absent cursor data (the same posture
`AGENTS.md` §5 asks of every guard gate, applied here to a monitoring feature instead).

**Known scope limit, stated rather than papered over:** only page 1 (`pageSize=25`) of
`/wallet/{addr}/missions` is fetched. If more than a page's worth of new resolved attacks
landed on tracked planets since the last check, only the newest page is seen. Acceptable
for a defensive monitor checked regularly; not exhaustive across an arbitrarily long gap.

## Two entry points, one core

Both call the same `radar.check_targets(targets, state) -> RadarReport`:

1. **`vd tick`** (`policy.radar.enabled`, **default `true`** — a deliberate departure from
   this codebase's usual "empty/off == old behaviour" convention every other
   `ActionsCfg`/`StrategyCfg` flag follows, justified because radar is read-only/zero-risk
   and the whole point is not silently missing what a bare `incoming_fleets` check would
   miss). Scoped to `policy.planets` (empty == every owned planet) via
   `radar.targets_from_planet_snapshots` against the `Snapshot` the tick already fetched —
   no extra `/wallet/{addr}/planets` call. Findings fold into the tick report's new
   "Radar" line and, when non-empty, an unconditional `strategy.md` entry (never
   suppressed by the structural-tier-block/duplicate-tick suppression that routine guard
   narration gets — a radar finding is never routine).
2. **`vd radar check --wallet ADDR [--planets N,N] | --alliance-id N`** — standalone,
   scheduler-facing, no `policy.json` required at all. `--alliance-id` resolves via
   `GET /alliance/{id}` (see below) to every member's every planet — literally "monitoring
   all planet members," a design requirement this mode exists specifically to satisfy.

## `/alliance/{id}`: reliable for real, indexed alliances

`resolve_targets_for_alliance` uses `GET /alliance/{id}` (`read.fetch_alliance_by_id`) — a
previously-unwired route confirmed live for real, mainnet-indexed alliances, returning a
full `members[]` with addresses. An alliance id the indexer has never seen — for instance
one that only ever existed on a local test fork, never on real mainnet — 404s instead;
expected, not a reliability concern, since the route is reliable for anything the real
indexer actually knows about. This settles an earlier open design question in this
feature's plan: arbitrary alliance-by-id monitoring is supported, not scoped to only the
caller's own alliance (which `AllianceState.members` already covers, for the membership
feature — see `references/guardrails.md`'s alliance gate writeup — but only for the
*caller's own* alliance, which is why this feature needed the separate `/alliance/{id}`
route at all).

## Exit codes — the actual notification contract

No notification/webhook mechanism exists anywhere in this codebase. `vd radar check`'s
exit code (`radar.exit_code_for_report`, a pure function of a `RadarReport`) is what a
scheduler wrapper is expected to act on — see `references/scheduling.md`'s worked example:

- **`0`** — clean: no findings, no errors.
- **`1`** — one or more findings. Takes priority over errors: something concrete was
  found, report it, regardless of whether some other wallet/system also failed to fetch
  this run.
- **`2`** — no findings **and** at least one fetch failed — the check could not confirm
  "all clear," so a wrapper must not treat this the same as `0`.

`--json` prints a single-line, compact `RadarReport` (deliberately not pretty-printed —
meant for a wrapper to parse, alongside the human `rich` report on the same stdout).

## Verifying this end-to-end

Against a scratch `$VEYDRIFT_HOME`, never a real one:

```bash
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd radar check --wallet <addr>
VEYDRIFT_HOME=/tmp/scratch-veydrift uv run --directory skills/veydrift-agent vd radar check --alliance-id <id>
echo $?   # 0 clean / 1 findings / 2 error, per the contract above
```

A clean account should exit `0` with no findings printed. If the tracked wallet has a
recent resolved attack, `resolved_attack` should surface it even though `incoming: none`
would suggest otherwise — the direct way to confirm the resolved-attack signal is actually
reachable through this route, not just present in a fixture.
