"""radar.py — attack/harvest mission radar.

Two entry points share one core (`check_targets` below):

1. `vd tick` (`tick.py`'s `_run_tick`, gated on `policy.radar.enabled`, default `True`)
   — scoped to `policy.planets` on `policy.wallet`, using `targets_from_planet_snapshots`
   against the Snapshot the tick already fetched (no extra `/wallet/{addr}/planets`
   call).
2. `vd radar check` (this module's own CLI, below) — scheduler-facing, no `policy.json`
   required: `--wallet [--planets ...]` (via `resolve_targets_for_wallet`) or
   `--alliance-id N` (via `resolve_targets_for_alliance`, expanding to every member's
   every planet).

Three independent signals, not one — see `references/radar.md` for the full writeup;
the short version a future reader needs before touching this file:

- **`incoming_fleet`** — from `/wallet/{addr}/fleet-visibility`'s `incoming[]`. Future
  arrivals only. Every row is reported, not filtered down to `IncomingFleet.hostile`
  (hardcoded `True` for every row today — `read.py`'s `_incoming_fleet`, a known,
  deliberately-unfixed gap): `mission_type_name` alone is more informative than a flag
  that has never been validated against a live non-Attack row, and even a non-hostile
  mission (a stranger's Harvest) targeting your planet is itself diagnostic.
- **`resolved_attack`** — from `/wallet/{addr}/missions`'s `kind == "battleReport"` rows
  (`read.fetch_missions`). This is the signal `incoming_fleet` structurally cannot
  provide: an attack that has already resolved has already fallen out of `incoming[]`.
  A live incident during this feature's planning missed exactly this — an Attack that
  resolved ~14.5h before a check-in was invisible to `incoming_fleets`, and the only
  visible clue was a stranger's Harvest mission inbound to the resulting debris.
  De-duplicated per wallet against `radar-state.json` (`state.load_radar_state`/
  `save_radar_state`) so a previously-surfaced battle report is not re-reported forever.
- **`debris`** — from `/universe/galaxies/{g}/systems/{s}`'s `debrisField` at a tracked
  planet's own slot. Reads the same route/shape `tick._own_planet_debris` already
  does, but is NOT that function reused directly: `_own_planet_debris` is
  `Snapshot`-shaped and wired into the harvest-candidate pipeline with 30+ existing
  test call sites, while a `WatchTarget` here can name a planet on a wallet this
  process has no `Snapshot` for at all (an alliance member's) — so this module reads
  the route independently instead of coupling to that function's signature.

Never constructs an on-chain `Action`, never touches `guard.py` — this whole module is
read-only.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from veydrift_agent import http, read
from veydrift_agent.models import PlanetSnapshot, RadarFinding, RadarReport, WatchTarget
from veydrift_agent.state import RadarState, WalletRadarState, load_radar_state, save_radar_state

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Monitor tracked planets for incoming attacks, resolved battles, and debris.",
)

_console = Console()


# --------------------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------------------


def _watch_targets_from_planets_payload(
    wallet: str, data: dict[str, Any], planet_ids: set[int] | None
) -> list[WatchTarget]:
    out: list[WatchTarget] = []
    for row in data.get("planets") or []:
        try:
            pid = int(row.get("planetId"))
        except (TypeError, ValueError):
            continue
        if planet_ids is not None and pid not in planet_ids:
            continue
        galaxy, system, position = row.get("galaxy"), row.get("system"), row.get("position")
        out.append(
            WatchTarget(
                wallet=wallet,
                planet_id=pid,
                galaxy=int(galaxy) if galaxy is not None else None,
                system=int(system) if system is not None else None,
                position=int(position) if position is not None else None,
            )
        )
    return out


def resolve_targets_for_wallet(wallet: str, planet_ids: list[int] | None = None) -> list[WatchTarget]:
    """`planet_ids` empty/`None` == every planet the wallet owns, discovered via
    `/wallet/{addr}/planets` — same "empty == discover all" convention `Policy.planets`
    already uses. Does not catch `http.VeydriftAPIError`; the caller decides how to
    degrade. A caller that already has a `Snapshot` (namely `tick.py`) should prefer
    `targets_from_planet_snapshots` instead of this function, to avoid a redundant
    fetch of data it already has."""
    data = http.fetch(f"/wallet/{wallet}/planets")
    wanted = set(planet_ids) if planet_ids else None
    return _watch_targets_from_planets_payload(wallet, data, wanted)


def targets_from_planet_snapshots(
    wallet: str, planets: list[PlanetSnapshot], planet_ids: list[int] | None = None
) -> list[WatchTarget]:
    """Builds `WatchTarget`s directly from an already-fetched `Snapshot`'s planets —
    `tick.py`'s own entry point, avoiding a redundant `/wallet/{addr}/planets` fetch
    when a full `Snapshot` already exists. `planet_ids` empty/`None` == every planet in
    `planets` (mirrors `resolve_targets_for_wallet`'s convention; in practice this is
    `policy.planets`, itself already "empty == every owned planet" by the time a
    `Snapshot` is built)."""
    wanted = set(planet_ids) if planet_ids else None
    out: list[WatchTarget] = []
    for planet in planets:
        if wanted is not None and planet.planet_id not in wanted:
            continue
        galaxy = system = position = None
        if planet.coordinates:
            parts = planet.coordinates.split(":")
            if len(parts) == 3:
                try:
                    galaxy, system, position = (int(p) for p in parts)
                except ValueError:
                    galaxy = system = position = None
        out.append(
            WatchTarget(wallet=wallet, planet_id=planet.planet_id, galaxy=galaxy, system=system, position=position)
        )
    return out


def resolve_targets_for_alliance(alliance_id: int | str) -> tuple[list[WatchTarget], list[str]]:
    """Resolves every member's every planet — literally "monitoring all planet
    members." Raises `http.VeydriftAPIError` if the top-level `/alliance/{id}` fetch
    itself fails (an unknown/unreal id, or a network/server error) — the caller cannot
    proceed at all without a member list. A single member's own
    `/wallet/{addr}/planets` fetch failing, in contrast, is best-effort: one member's
    data being briefly unavailable must not blank out every other member's. Returns
    `(targets, errors)` — `errors` collects one string per member whose planets could
    not be resolved, folded into the final `RadarReport.errors` by the caller rather
    than silently dropped."""
    data = read.fetch_alliance_by_id(alliance_id)
    alliance = data.get("alliance") or {}
    members = alliance.get("members") or []
    targets: list[WatchTarget] = []
    errors: list[str] = []
    for member in members:
        address = member.get("address")
        if not address:
            continue
        try:
            planets_data = http.fetch(f"/wallet/{address}/planets")
        except http.VeydriftAPIError as exc:
            errors.append(f"could not fetch planets for alliance member {address}: {exc}")
            continue
        targets.extend(_watch_targets_from_planets_payload(address, planets_data, None))
    return targets, errors


# --------------------------------------------------------------------------------------
# check_targets — the shared core
# --------------------------------------------------------------------------------------


def _incoming_fleet_findings(wallet: str, planet_ids: set[int]) -> tuple[list[RadarFinding], str | None]:
    try:
        visibility = read.fetch_fleet_visibility(wallet)
    except http.VeydriftAPIError as exc:
        return [], f"{wallet}: fleet-visibility fetch failed: {exc}"

    findings: list[RadarFinding] = []
    for row in visibility.get("incoming") or []:
        try:
            target_planet_id = int(row.get("targetPlanetId"))
        except (TypeError, ValueError):
            continue
        if target_planet_id not in planet_ids:
            continue
        mission_type_name = row.get("missionType") or "unknown"
        origin = row.get("originPlanetId")
        arrives_at = read._parse_datetime(row.get("arrivalAt"))
        detail = f"{mission_type_name} incoming from planet {origin}" if origin else f"{mission_type_name} incoming"
        if arrives_at is not None:
            detail += f", arriving {arrives_at.isoformat()}"
        findings.append(
            RadarFinding(kind="incoming_fleet", wallet=wallet, planet_id=target_planet_id, detail=detail, occurred_at=arrives_at)
        )
    return findings, None


def _resolved_attack_findings(
    wallet: str, planet_ids: set[int], wallet_state: WalletRadarState
) -> tuple[list[RadarFinding], str | None]:
    try:
        # Page 1 only (default pageSize=25) -- if more than a page's worth of new
        # resolved attacks landed on tracked planets since the last check, only the
        # newest page is seen. Acceptable for a defensive monitor checked regularly;
        # documented rather than silently assumed exhaustive.
        missions_data = read.fetch_missions(wallet)
    except http.VeydriftAPIError as exc:
        return [], f"{wallet}: missions fetch failed: {exc}"

    # Confirmed live: a resolved Attack comes through with `kind: "mission"` and an
    # attached top-level `report` object -- NOT as a separate `kind: "battleReport"`
    # row. references/api-routes.md §3.14's documented tagged union
    # (`{kind: "mission", mission, report?}` or `{kind: "battleReport", report}`) is
    # real, but the `kind: "battleReport"` half has never actually been observed --
    # only the `report?` optional field on a `kind: "mission"` row. Keying off `report`
    # truthiness rather than `kind` covers both the confirmed shape and the
    # documented-but-unobserved one, so a hypothetical `kind: "battleReport"` row would
    # still be caught if one is ever seen. See references/radar.md for the full writeup.
    qualifying: list[dict[str, Any]] = []
    for row in missions_data.get("rows") or []:
        report = row.get("report")
        if not report:
            continue
        mission_id = report.get("missionId")
        if mission_id is None:
            continue
        try:
            target_planet_id = int(report.get("targetPlanetId"))
        except (TypeError, ValueError):
            continue
        if target_planet_id not in planet_ids:
            continue
        qualifying.append(report)

    # Newest-first by blockNumber, not by list order -- list order is unconfirmed for
    # this route, blockNumber is a reliable monotonic ordering when present. Confirmed
    # live: `blockNumber` arrives as a decimal STRING ("50794981"), same convention as
    # every other numeric-looking field on this API -- sorted as int, never lexically
    # (a lexical sort would misorder differing-length block numbers).
    def _block_number(report: dict[str, Any]) -> int:
        try:
            return int(report.get("blockNumber") or 0)
        except (TypeError, ValueError):
            return 0

    qualifying.sort(key=_block_number, reverse=True)

    findings: list[RadarFinding] = []
    newest_seen: str | None = wallet_state.last_seen_mission_id
    cursor = wallet_state.last_seen_mission_id
    for i, report in enumerate(qualifying):
        mission_id = str(report["missionId"])
        if i == 0:
            newest_seen = mission_id
        if cursor is not None and mission_id == cursor:
            break  # everything from here on (older, by our sort) was already reported
        outcome = report.get("outcome", "unknown outcome")
        loot = report.get("loot")
        detail = f"battleReport {mission_id}: {outcome}"
        if loot:
            detail += f" (loot: {loot})"
        findings.append(
            RadarFinding(kind="resolved_attack", wallet=wallet, planet_id=int(report["targetPlanetId"]), detail=detail)
        )
    wallet_state.last_seen_mission_id = newest_seen
    return findings, None


def _debris_findings(targets: list[WatchTarget]) -> tuple[list[RadarFinding], list[str]]:
    by_system: dict[tuple[int, int], list[WatchTarget]] = {}
    for t in targets:
        if t.galaxy is None or t.system is None or t.position is None:
            continue
        by_system.setdefault((t.galaxy, t.system), []).append(t)

    findings: list[RadarFinding] = []
    errors: list[str] = []
    for (galaxy, system), group in by_system.items():
        try:
            data = read.fetch_universe_system(galaxy, system)
        except http.VeydriftAPIError as exc:
            errors.append(f"{galaxy}:{system}: universe fetch failed: {exc}")
            continue
        slots_by_position: dict[int, dict[str, Any]] = {}
        for slot in data.get("planets") or []:
            try:
                slots_by_position[int(slot.get("position"))] = slot
            except (TypeError, ValueError):
                continue
        for target in group:
            slot = slots_by_position.get(target.position)
            if slot is None:
                continue
            debris = slot.get("debrisField")
            if not isinstance(debris, dict):
                continue
            try:
                metal = int(debris.get("metal", 0))
                crystal = int(debris.get("crystal", 0))
            except (TypeError, ValueError):
                continue
            if metal <= 0 and crystal <= 0:
                continue
            findings.append(
                RadarFinding(
                    kind="debris",
                    wallet=target.wallet,
                    planet_id=target.planet_id,
                    detail=f"debris field on this planet's own slot: {metal} metal / {crystal} crystal",
                )
            )
    return findings, errors


def check_targets(targets: list[WatchTarget], state: RadarState) -> RadarReport:
    """The shared core both entry points call. Best-effort per wallet/system — one
    fetch failing never aborts the rest; failures are collected into
    `RadarReport.errors`, never silently swallowed (AGENTS.md §5's fail-closed posture,
    applied to a monitoring feature: a degraded check must never look identical to a
    confirmed-clean one to a caller only checking `findings`).

    Mutates `state` in place (advances each checked wallet's `last_seen_mission_id`) —
    callers are responsible for persisting it via `state.save_radar_state` after this
    returns; this function does not save it itself, so a caller can inspect the report
    before deciding to persist (or, for a dry inspection, choose not to)."""
    findings: list[RadarFinding] = []
    errors: list[str] = []

    by_wallet: dict[str, list[WatchTarget]] = {}
    for t in targets:
        by_wallet.setdefault(t.wallet, []).append(t)

    for wallet, wallet_targets in by_wallet.items():
        planet_ids = {t.planet_id for t in wallet_targets}

        incoming_findings, incoming_error = _incoming_fleet_findings(wallet, planet_ids)
        findings.extend(incoming_findings)
        if incoming_error:
            errors.append(incoming_error)

        wallet_state = state.wallets.setdefault(wallet, WalletRadarState())
        resolved_findings, resolved_error = _resolved_attack_findings(wallet, planet_ids, wallet_state)
        findings.extend(resolved_findings)
        if resolved_error:
            errors.append(resolved_error)

    debris_findings, debris_errors = _debris_findings(targets)
    findings.extend(debris_findings)
    errors.extend(debris_errors)

    return RadarReport(findings=findings, errors=errors)


def exit_code_for_report(report: RadarReport) -> int:
    """Pure function of a `RadarReport` — the actual notification contract for
    `vd radar check` (no notification/webhook mechanism exists anywhere in this
    codebase; a scheduler wrapper is expected to act on this exit code, per
    `references/scheduling.md`):

    - `0` — clean: no findings, no errors.
    - `1` — one or more findings. Takes priority over errors: something concrete was
      found, report it, regardless of whether some other wallet/system also failed to
      fetch this run.
    - `2` — no findings AND at least one fetch failed — the check could not confirm
      "all clear," so a wrapper must not treat this the same as exit `0`.
    """
    if report.findings:
        return 1
    if report.errors:
        return 2
    return 0


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def print_radar_report(report: RadarReport) -> None:
    if not report.findings and not report.errors:
        _console.print(Panel("No findings.", title="Radar", border_style="green", expand=False))
        return

    if report.findings:
        table = Table(title="Radar findings", expand=False)
        table.add_column("kind")
        table.add_column("wallet")
        table.add_column("planet")
        table.add_column("detail")
        for f in report.findings:
            table.add_row(f.kind, f.wallet, str(f.planet_id), f.detail)
        _console.print(table)

    if report.errors:
        _console.print(
            Panel("\n".join(report.errors), title="Radar check errors", border_style="red", expand=False)
        )


# --------------------------------------------------------------------------------------
# vd radar check — scheduler-facing standalone entry point
# --------------------------------------------------------------------------------------


def _fail(message: str) -> None:
    _console.print(f"[red]error:[/] {message}")
    raise typer.Exit(code=4)


def _parse_planet_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            _fail(f"invalid --planets entry {part!r}; expected a comma-separated list of integers")
    return out


@app.command()
def check(
    wallet: str | None = typer.Option(None, "--wallet", envvar="VEYDRIFT_WALLET", help="Wallet address to watch"),
    planets: str | None = typer.Option(
        None, "--planets", help="Comma-separated planet ids to watch (default: every planet the wallet owns)"
    ),
    alliance_id: str | None = typer.Option(
        None, "--alliance-id", help="Watch every planet of every member of this alliance"
    ),
    json_output: bool = typer.Option(False, "--json", help="Also print the RadarReport as JSON"),
) -> None:
    """Standalone, scheduler-facing radar check — no `policy.json` required. Exactly one
    of `--wallet` or `--alliance-id` is required. Exit code is the notification contract
    (see `exit_code_for_report`'s docstring): `0` clean, `1` findings, `2` could not
    complete the check."""
    if bool(wallet) == bool(alliance_id):
        _fail("pass exactly one of `--wallet` or `--alliance-id`.")

    planet_ids = _parse_planet_ids(planets)
    if planet_ids is not None and alliance_id:
        _fail("`--planets` only applies with `--wallet`, not `--alliance-id`.")

    pre_errors: list[str] = []
    try:
        if wallet:
            targets = resolve_targets_for_wallet(wallet, planet_ids)
        else:
            targets, pre_errors = resolve_targets_for_alliance(alliance_id)  # type: ignore[arg-type]
    except http.VeydriftHTTPError as exc:
        _console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(code=2)
    except http.VeydriftServerError as exc:
        _console.print(f"[red]API unhealthy:[/] {exc}")
        raise typer.Exit(code=2)
    except http.VeydriftNetworkError as exc:
        _console.print(f"[red]network error:[/] {exc}")
        raise typer.Exit(code=3)

    if not targets:
        _console.print("[yellow]no planets resolved to watch -- nothing to check.[/yellow]")
        raise typer.Exit(code=0 if not pre_errors else 2)

    state = load_radar_state()
    report = check_targets(targets, state)
    report.errors = pre_errors + report.errors
    save_radar_state(state)

    print_radar_report(report)
    if json_output:
        # Compact, single-line -- deliberately not `indent=2`: this is meant for a
        # wrapper script to parse (the whole point of `--json`), and a single line is
        # trivial to locate/grep even when printed alongside the human `rich` report
        # above, unlike a pretty-printed multi-line block interleaved with table output.
        print(report.model_dump_json())

    raise typer.Exit(code=exit_code_for_report(report))
