"""`vd read` -- fetch and summarise Veydrift game state.

Route table, payload-shape notes and health-gating rules: `references/api-routes.md`.
Live probes for every route were taken 2026-08-12 against wallet
``0x224aba5d489675a7bd3ce07786fada466b46fa0f`` / planet ``664``.

Entity ID -> name tables (buildings/technologies/ships/defenses) are imported directly
from `ids.py`, built by reading the deployed contract source directly.

Fleet-mission-type name<->id resolution is kept local regardless, deliberately NOT
sourced from `ids.py`: the live API's `missionType` field is a wire-format string like
``"AcsDefend"`` / ``"MissileAttack"`` (apps/backend/src/evm.ts's `FleetMissionSummary`,
confirmed live 2026-08-12), whereas `ids.py`'s `FLEET_MISSION_TYPE_NAMES` values are
human-display strings like ``"ACS Defend"`` / ``"Missile Attack"`` intended for CLI
input/output, not wire matching. Reversing that table would silently fail to resolve
exactly the four combat mission types (`Attack`, `AcsAttack`, `MissileAttack`,
`Intercept`) that matter most for hostile-fleet escalation, which is a correctness risk
worth a few duplicated lines instead.

``/chain/events`` is deliberately not exposed as a target: it is a Server-Sent Events
stream (``content-type: text/event-stream``), not a paginated JSON route -- confirmed by
probing it with a bounded timeout (see references/api-routes.md §"chain/events"). An
unparameterised GET keeps the connection open and never returns, which is exactly the
"did not return within 2 minutes" behaviour SPEC.md and RESEARCH-ADDENDUM.md both
describe, just with the mechanism identified.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich import print as rprint
from rich.console import Console

from veydrift_agent import fmt, http, models
from veydrift_agent.ids import BUILDING_NAMES, DEFENSE_NAMES, SHIP_NAMES, TECHNOLOGY_NAMES

app = typer.Typer(no_args_is_help=True, add_completion=False)

#: For diagnostics that must not land on stdout when a caller continues past them (e.g.
#: a recovered `/health` body) -- `--json`/`--out` output is a structured contract on
#: stdout, and a message interleaved there would corrupt it for any parser. Every other
#: `rprint(...)` in this module is immediately followed by `raise typer.Exit(...)`, so it
#: never reaches that risk -- this console exists only for the one path that continues.
_stderr_console = Console(stderr=True)

#: docs/RESEARCH-ADDENDUM.md §3 -- VeydriftGameStorage.sol:174. The API's fleet-visibility
#: rows carry `missionType` as a *string* already (evm.ts `FleetMissionSummary.missionType:
#: string`), so this table's main job is filling in `IncomingFleet.mission_type` (the int)
#: from that string, not the reverse.
FLEET_MISSION_TYPE_NAMES: dict[int, str] = {
    0: "Transport",
    1: "Deploy",
    2: "Colonize",
    3: "Attack",
    4: "Harvest",
    5: "AcsDefend",
    6: "Intercept",
    7: "MissileAttack",
    8: "AcsAttack",
    9: "DefenseHold",
}
FLEET_MISSION_TYPE_IDS: dict[str, int] = {v: k for k, v in FLEET_MISSION_TYPE_NAMES.items()}

#: Targets that return 60-2000+ KB payloads (see references/api-routes.md -- `/highscores`
#: alone measured ~2.2 MB on 2026-08-12, well past the ~86 KB NOTES.md/SPEC.md figure).
#: `--out` is mandatory for these; stdout is refused unconditionally.
_STDOUT_REFUSED = {"battle-reports", "highscores"}


# --------------------------------------------------------------------------------------
# Shared CLI option definitions (reused across every command)
# --------------------------------------------------------------------------------------

WalletOption = typer.Option(
    None, "--wallet", envvar="VEYDRIFT_WALLET", help="Wallet address, e.g. 0x224a...fa0f"
)
PlanetIdOption = typer.Option(None, "--planet-id", help="Planet id, e.g. 664")
JsonSummaryOption = typer.Option(
    False, "--json/--summary", help="Emit the raw API JSON instead of the rich summary (default: --summary)"
)
OutOption = typer.Option(
    None, "--out", help="Write the raw JSON response to this file instead of stdout", dir_okay=False
)
MaxAgeOption = typer.Option(
    None, "--max-age", help="Override the disk-cache TTL in seconds (default 60s; 15s for health)"
)


# --------------------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    """Bad CLI usage -- exit code 4 per SPEC.md §5.2's exit-code table."""
    rprint(f"[red]error:[/] {message}")
    raise typer.Exit(code=4)


def _need_wallet(wallet: str | None) -> str:
    if not wallet:
        _fail("`--wallet 0x...` is required (or set VEYDRIFT_WALLET).")
    return wallet


def _need_planet_id(planet_id: int | None) -> int:
    if planet_id is None:
        _fail("`--planet-id N` is required for this target.")
    return planet_id


def _recover_health_body(path: str, exc: http.VeydriftServerError) -> dict[str, Any] | None:
    """Narrow, defensive recovery for `/health` specifically -- confirmed live
    (2026-08-22, twice): this backend signals `ok:false` via HTTP 503 on this route, not
    only via a 200-with-`ok:false` body -- a persistent, not one-off, condition. Every
    other route's 5xx behaviour through `_fetch_or_exit` is completely unaffected: this
    only ever returns non-`None` for `path == "/health"`, and only when the captured
    error body parses as a real health-response shape. Recovering the body never means
    "the game is fine" -- the recovered dict still goes through the exact same
    `_health_ok` / `Snapshot.combat_only_degradation` checks a normal 200 response
    would."""
    if path.rstrip("/") != "/health":
        return None
    try:
        recovered = json.loads(exc.body)
    except (json.JSONDecodeError, TypeError):
        return None
    return recovered if isinstance(recovered, dict) and "readiness" in recovered else None


def _fetch_or_exit(path: str, params: dict[str, Any] | None = None, *, max_age: float | None = None) -> dict[str, Any]:
    try:
        return http.fetch(path, params, max_age=max_age)
    except http.VeydriftHTTPError as exc:
        _fail(str(exc))
    except http.VeydriftServerError as exc:
        recovered = _recover_health_body(path, exc)
        if recovered is not None:
            _stderr_console.print(
                f"[yellow]/health returned HTTP {exc.status_code} but the body parsed -- "
                "evaluating it instead of aborting.[/yellow]"
            )
            return recovered
        rprint(f"[red]API unhealthy:[/] {exc}")
        raise typer.Exit(code=2)
    except http.VeydriftNetworkError as exc:
        rprint(f"[red]network error:[/] {exc}")
        raise typer.Exit(code=3)


def _emit(data: Any, *, target: str, json_output: bool, out: Path | None) -> None:
    if out is not None:
        out.write_text(json.dumps(data, indent=2, sort_keys=False))
        rprint(f"[green]wrote[/] {out} ({out.stat().st_size:,} bytes)")
        return
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    fmt.print_summary(target, data)


def _health_ok(data: dict[str, Any]) -> bool:
    """SPEC.md §5.2: gate on `ok === true` AND `readiness.ready === true` only. `null`
    for chainSync/indexer/rpc/most-of-readiness on a reader-worker response is a
    read-replica artifact, not an outage -- do not fold any of those into this check."""
    readiness = data.get("readiness") or {}
    return data.get("ok") is True and readiness.get("ready") is True


def _game_maintenance(data: dict) -> tuple[bool, models.GameMaintenance | None, list[str]]:
    """Parses gameMaintenance/pausedSince/readiness.degradationReasons from a raw
    /health payload. Fail-closed: only ever reports what it can positively confirm.
    Absent gameMaintenance -> (False, None, reasons), and callers must treat
    `None` as "cannot confirm not paused," never as confirmation of the opposite.

    Confirmed live (2026-08-20/21): `gameMaintenance` is always present on a real
    response, `{"paused": false, "observedAt": ..., "pausedSince": null,
    "pauseAgeSeconds": 0}` when not paused -- the `None` branch below is defensive
    handling for a malformed/future-changed response, not the shape a healthy backend
    normally sends. `readiness` also carries its own flattened `gamePaused`/
    `gamePauseAgeSeconds`, redundant with `gameMaintenance` -- deliberately not read
    here; `gameMaintenance` is the single source of truth (AGENTS.md §5)."""
    readiness = data.get("readiness") or {}
    reasons = list(readiness.get("degradationReasons") or [])
    maintenance_raw = data.get("gameMaintenance")
    if maintenance_raw is None:
        return False, None, reasons
    paused = maintenance_raw.get("paused") is True
    maintenance = models.GameMaintenance(
        paused=paused,
        paused_since=maintenance_raw.get("pausedSince"),
        pause_age_seconds=maintenance_raw.get("pauseAgeSeconds") or 0,
    )
    return paused, maintenance, reasons


def _randomness_readiness(data: dict) -> models.RandomnessReadiness | None:
    """Parses /health's randomnessReadiness block. `None` means unconfirmed (route
    absent/malformed) -- see `RandomnessReadiness`'s own docstring for why this must
    never be read as "combat readiness is fine." Distinct object from `readiness`
    (top-level, own key `randomnessReadiness`), with its own `reasons` list -- not the
    same as `readiness.degradationReasons`, confirmed live 2026-08-22: the latter was
    empty while randomnessReadiness.ready was false."""
    raw = data.get("randomnessReadiness")
    if raw is None:
        return None
    return models.RandomnessReadiness(ready=raw.get("ready") is True, reasons=list(raw.get("reasons") or []))


def _resources(raw: dict[str, Any] | None) -> models.Resources:
    raw = raw or {}
    return models.Resources(
        metal=raw.get("metal", 0), crystal=raw.get("crystal", 0), deuterium=raw.get("deuterium", 0)
    )


def _entities(
    raw: list[dict[str, Any]] | None,
    names: dict[int, str],
    *,
    level_key: str | None,
    count_key: str | None,
) -> list[models.Entity]:
    out: list[models.Entity] = []
    for item in raw or []:
        eid = item.get("id")
        if eid is None:
            continue
        out.append(
            models.Entity(
                id=eid,
                name=names.get(eid, f"unknown-{eid}"),
                level=item.get(level_key) if level_key else None,
                count=item.get(count_key) if count_key else None,
                cost=_resources(item.get("cost")),
                duration_seconds=item.get("durationSeconds"),
            )
        )
    return out


def _parse_datetime(raw: Any) -> datetime | None:
    """Accepts a raw int/float unix timestamp, a decimal-string unix timestamp (the
    format the live API actually uses for `arrivalAt`/`returnAt`/`readyAt`/`occurredAt`
    -- confirmed live 2026-08-17 against `/wallet/{addr}/fleet-visibility` and
    `/wallet/{addr}/activity`, e.g. `"arrivalAt": "1786947731"`; `wallet_activity.json`'s
    real, non-synthetic fixture already carried this shape in `transactionAt`/
    `occurredAt` and nobody had generalised the parser to match it), or an ISO 8601
    string (what the two *synthetic*, hand-built fixtures --
    `wallet_infrastructure_active_queue.json`, `wallet_overview_incoming.json` -- guessed
    before this was ever probed against a populated queue/fleet). Tries decimal-string-
    as-epoch before falling back to `fromisoformat` so both shapes parse correctly;
    previously a decimal-string epoch fell straight through to `fromisoformat`, raised
    `ValueError`, and silently became `None` -- looking like "not reported" when the API
    had reported it fine. `QueueEntry.ready_at`/`IncomingFleet.arrives_at` were the two
    fields this silently affected."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.lstrip("-").isdigit():
            try:
                return datetime.fromtimestamp(int(stripped), tz=UTC)
            except (ValueError, OverflowError, OSError):
                return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _queue_entry(
    raw: dict[str, Any] | None, *, fallback_kind: models.QueueKind, names: dict[int, str]
) -> models.QueueEntry | None:
    """Parse a `QueueState` object (apps/backend/src/evm.ts:170). The live account used
    for this work package is zero-state -- every queue is `null` -- so this branch is
    typed from the backend source, not exercised against a live populated response. See
    references/api-routes.md for that caveat and a worked example."""
    if not raw or raw.get("active") is False:
        return None
    kind_raw = raw.get("kind")
    try:
        kind = models.QueueKind(kind_raw) if kind_raw else fallback_kind
    except ValueError:
        kind = fallback_kind
    item_id = raw.get("itemId")
    as_of_now = raw.get("asOfNow") or {}
    return models.QueueEntry(
        kind=kind,
        entity_id=item_id if item_id is not None else -1,
        entity_name=names.get(item_id, f"unknown-{item_id}") if item_id is not None else "unknown",
        target_level=raw.get("targetLevel"),
        quantity=raw.get("quantity"),
        ready_at=_parse_datetime(raw.get("readyAt")),
        seconds_remaining=as_of_now.get("secondsRemaining"),
    )


def _incoming_fleet(raw: dict[str, Any]) -> models.IncomingFleet:
    """Parse a `FleetMissionSummary` row (apps/backend/src/evm.ts:640) from
    `fleetVisibility.incoming`. `missionType` arrives as a *string* on the wire; this
    resolves the paired int from FLEET_MISSION_TYPE_IDS. Like `_queue_entry`, this is
    typed from source and not exercised against a live populated row -- the probed
    account has no incoming fleets."""
    mission_type_name = raw.get("missionType")
    mission_type_id = FLEET_MISSION_TYPE_IDS.get(mission_type_name) if isinstance(mission_type_name, str) else None
    target_planet_id = raw.get("targetPlanetId")
    # TODO(before first tier-3 use): `hostile=True` is hardcoded for every row here.
    # `RESEARCH-ADDENDUM.md` §2 calls `fleet-visibility.incoming` "the hostile-fleet
    # detection surface", but `FleetMissionType` (§3) also includes `AcsDefend` (5) and
    # `DefenseHold` (9) -- allied-reinforcement mission types, not attacks -- and nothing
    # in the backend source rules out one of those appearing in *your own* `incoming`
    # array when an ally stations a fleet to defend your planet. Unverifiable against the
    # probed (zero-state, no incoming fleets) account, so this is left as `True` rather
    # than guessed at -- but if the live API also lists your own inbound allied/transport
    # traffic in this array, a tier-3 policy (the only tier that unlocks
    # `launchFleetMission`) would self-escalate on every single tick forever, since
    # `plan.py`'s escalation rung treats every `incoming` row as an attack. See
    # `references/api-routes.md` §5 for the full writeup; `mission_type_name` is already
    # populated on this model specifically so a future fix can disambiguate by name
    # instead of guessing.
    return models.IncomingFleet(
        mission_id=raw.get("missionId"),
        mission_type=mission_type_id,
        mission_type_name=mission_type_name,
        origin=raw.get("originPlanetId"),
        target_planet_id=int(target_planet_id) if target_planet_id is not None else None,
        arrives_at=_parse_datetime(raw.get("arrivalAt")),
        hostile=True,
    )


# --------------------------------------------------------------------------------------
# Single-route commands
# --------------------------------------------------------------------------------------


@app.command()
def health(
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /health -- gate on ok && readiness.ready only (see references/api-routes.md)."""
    data = _fetch_or_exit("/health", max_age=max_age)
    _emit(data, target="health", json_output=json_output, out=out)
    if not _health_ok(data):
        raise typer.Exit(code=2)


@app.command()
def config(
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /runtime-config -- chain, contract addresses, deploymentAbiHash, feature flags."""
    data = _fetch_or_exit("/runtime-config", max_age=max_age)
    _emit(data, target="config", json_output=json_output, out=out)


@app.command()
def settlement(
    wallet: str | None = WalletOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/settlement -- player + home planet identity."""
    w = _need_wallet(wallet)
    data = _fetch_or_exit(f"/wallet/{w}/settlement", max_age=max_age)
    _emit(data, target="settlement", json_output=json_output, out=out)


@app.command()
def planets(
    wallet: str | None = WalletOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/planets -- every planet the wallet owns, with coordinates,
    fields and key building levels. Use this to discover planet ids for --planet-id."""
    w = _need_wallet(wallet)
    data = _fetch_or_exit(f"/wallet/{w}/planets", max_age=max_age)
    _emit(data, target="planets", json_output=json_output, out=out)


@app.command()
def queues(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/queues -- building/defense/ship (planet-scoped, needs
    --planet-id to filter) and research (player-scoped, always returned)."""
    w = _need_wallet(wallet)
    params = {"planetId": planet_id} if planet_id is not None else None
    data = _fetch_or_exit(f"/wallet/{w}/queues", params, max_age=max_age)
    _emit(data, target="queues", json_output=json_output, out=out)


@app.command()
def highscore(
    wallet: str | None = WalletOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/highscore -- this wallet's own score breakdown (singular; not
    to be confused with `highscores`, the ~2 MB global leaderboard)."""
    w = _need_wallet(wallet)
    data = _fetch_or_exit(f"/wallet/{w}/highscore", max_age=max_age)
    _emit(data, target="highscore", json_output=json_output, out=out)


@app.command()
def infrastructure(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/infrastructure?planetId= -- building levels + live costs,
    energyBalance, productionPerHour, storageCaps, current building queue."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/infrastructure", {"planetId": pid}, max_age=max_age)
    _emit(data, target="infrastructure", json_output=json_output, out=out)


@app.command()
def research(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/research?planetId= -- technology levels + live costs
    (player-scoped; the route still requires a planetId to resolve resources-on-hand)."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/research", {"planetId": pid}, max_age=max_age)
    _emit(data, target="research", json_output=json_output, out=out)


@app.command()
def shipyard(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/shipyard?planetId= -- ship counts + live costs, fleetSlots,
    current ship-production queue."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/shipyard", {"planetId": pid}, max_age=max_age)
    _emit(data, target="shipyard", json_output=json_output, out=out)


@app.command()
def defenses(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/defenses?planetId= -- defense counts + live costs, current
    defense-production queue. (RESEARCH-ADDENDUM.md §2's "missing" route -- confirmed
    live at this exact name.)"""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/defenses", {"planetId": pid}, max_age=max_age)
    _emit(data, target="defenses", json_output=json_output, out=out)


@app.command()
def moon(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/moon?planetId= -- moon state for the given planet, or
    `moonAvailable: false` with `unavailableReason` if none exists yet."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/moon", {"planetId": pid}, max_age=max_age)
    _emit(data, target="moon", json_output=json_output, out=out)


@app.command()
def overview(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/overview?planetId= -- settlement + planets + queues +
    fleetVisibility in one call. Does NOT include infrastructure/research/shipyard/
    defenses (RESEARCH-ADDENDUM.md §2) -- use `snapshot` for the full picture."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    data = _fetch_or_exit(f"/wallet/{w}/overview", {"planetId": pid}, max_age=max_age)
    _emit(data, target="overview", json_output=json_output, out=out)


@app.command(name="fleet-visibility")
def fleet_visibility(
    wallet: str | None = WalletOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/fleet-visibility -- incoming/outgoing/returning/joinableAttacks/
    completedMissions/battleReports. `incoming` is the hostile-fleet escalation surface
    (RESEARCH-ADDENDUM.md §2). Wallet-scoped, not planet-scoped: the backend ignores a
    `planetId` on this route (apps/backend/src/server.ts:139-141), so --planet-id is not
    offered here."""
    w = _need_wallet(wallet)
    data = _fetch_or_exit(f"/wallet/{w}/fleet-visibility", max_age=max_age)
    _emit(data, target="fleet-visibility", json_output=json_output, out=out)


@app.command()
def missions(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/missions -- paginated mission archive (default page 1, 25/page).
    Optional --planet-id filters to one planet's missions."""
    w = _need_wallet(wallet)
    params = {"planetId": planet_id} if planet_id is not None else None
    data = _fetch_or_exit(f"/wallet/{w}/missions", params, max_age=max_age)
    _emit(data, target="missions", json_output=json_output, out=out)


def fetch_activity(wallet: str, *, since: str | None = None, max_age: float | None = None) -> dict[str, Any]:
    """GET /wallet/{addr}/activity, bypassing the CLI/`_emit` layer -- called directly by
    `tick.py`'s human-activity reconciliation check (tick.py never goes through the CLI
    command). Unlike `_fetch_or_exit`, this does NOT catch `http.VeydriftAPIError`; a
    caller needing best-effort behaviour (tick.py) catches it itself, the same posture
    `tick._live_addresses` already takes for `/runtime-config`.

    `since`'s exact wire format is unverified -- no fixture or probe in this repo
    demonstrates a request that actually sets it (`references/api-routes.md` §3.15 lists
    it as a real query param but `vd read activity` has never exercised it). Assumed
    unix-epoch-seconds-as-string, matching the fixture's `transactionAt`/`occurredAt`
    shape. If that assumption is wrong, this degrades to an ignored filter or a caught
    4xx -- never a crash -- so a caller must not treat an empty `items` list as proof
    nothing happened."""
    params: dict[str, Any] | None = {"since": since} if since is not None else None
    return http.fetch(f"/wallet/{wallet}/activity", params, max_age=max_age)


@app.command()
def activity(
    wallet: str | None = WalletOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /wallet/{addr}/activity -- chronological event feed (settlements, queue
    completions, transfers, ...) across the whole wallet."""
    w = _need_wallet(wallet)
    data = _fetch_or_exit(f"/wallet/{w}/activity", max_age=max_age)
    _emit(data, target="activity", json_output=json_output, out=out)


def _maybe_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _universe_archetype_for_planet(coordinates: str | None, *, max_age: float | None) -> str | None:
    """Best-effort: this planet's own `archetype`, sourced from
    `/universe/galaxies/{g}/systems/{s}` -- the ONLY family of routes that ever reports it
    (references/api-routes.md §3.16; confirmed live 2026-08-17 against galaxy 7 system
    181 -- planet 664's own slot there carries `archetype: "frozen-ice"`, matching this
    module's existing `universe_galaxy_system.json` fixture). No wallet route
    (`overview`/`infrastructure`/etc.) ever carries `archetype`, which is why
    `PlanetSnapshot.archetype` was hardwired `None` before this (Phase 5 of the general-
    strategy-engine program, docs/SPEC.md §5.4).

    `max_age` is the caller's cadence gate, in seconds (`tick.py` passes
    `policy.cadence.universe_hours * 3600`) -- this function has no cadence opinion of its
    own, it just forwards `max_age` to `http.fetch`'s existing disk cache. That cache,
    not a new timer, is what keeps a 10-minute tick cadence from re-hitting this route
    every tick: the same galaxy:system response is served from disk until it's
    `max_age` seconds old.

    Returns `None` on anything that isn't a clean hit -- unparseable coordinates, an
    unreachable API, no matching `position` in the response. `archetype` is an
    enrichment field, never load-bearing for a guard/plan decision, so a failure here
    must never abort the rest of the snapshot."""
    if not coordinates:
        return None
    parts = coordinates.split(":")
    if len(parts) != 3:
        return None
    try:
        galaxy, system, position = (int(p) for p in parts)
    except ValueError:
        return None
    try:
        data = http.fetch(f"/universe/galaxies/{galaxy}/systems/{system}", max_age=max_age)
    except http.VeydriftAPIError:
        return None
    for slot in data.get("planets") or []:
        if _maybe_int(slot.get("position")) == position:
            archetype = slot.get("archetype")
            return archetype if isinstance(archetype, str) else None
    return None


def fetch_fleet_visibility(wallet: str, *, max_age: float | None = None) -> dict[str, Any]:
    """GET /wallet/{addr}/fleet-visibility, bypassing the CLI/`_emit` layer -- the same
    "raw dict, not a `models.py` type" posture `fetch_activity` already takes, and for the
    same reason: `tick.py`'s revived rung-3 wiring (`_resolvable_mission_ids`, Phase 5)
    needs the player's own `outgoing` missions, and `Snapshot` (models.py, frozen for this
    phase -- see the WP report) has no field to carry a mission list. Does NOT catch
    `http.VeydriftAPIError` -- same contract as `fetch_activity`; the caller decides how
    to degrade."""
    return http.fetch(f"/wallet/{wallet}/fleet-visibility", max_age=max_age)


@app.command()
def universe(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /universe/galaxies/{g}/systems/{s} -- the real indexed neighbourhood (other
    players, debris, moons, migration reservations) around a planet.

    SPEC.md §5.2 lists no galaxy/system flags for `universe`, so this command derives
    them: --wallet + --planet-id resolve to a galaxy:system via `/wallet/{addr}/planets`,
    then that system is fetched. This is a deliberate reading of an underspecified part
    of SPEC.md -- see references/api-routes.md for the three different "universe" routes
    this project could have used and why this one (not the procedurally-generated
    `/universe/system`, not the radius-scan `/universe/systems`) was picked."""
    w = _need_wallet(wallet)
    pid = _need_planet_id(planet_id)
    planets_data = _fetch_or_exit(f"/wallet/{w}/planets", max_age=max_age)
    match = next(
        (p for p in planets_data.get("planets", []) if str(p.get("planetId")) == str(pid)), None
    )
    if match is None:
        _fail(f"planet {pid} not found for wallet {w}; run `vd read planets` to list yours.")
    galaxy, system = match["galaxy"], match["system"]
    data = _fetch_or_exit(f"/universe/galaxies/{galaxy}/systems/{system}", max_age=max_age)
    _emit(data, target="universe", json_output=json_output, out=out)


@app.command(name="battle-reports")
def battle_reports(
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /battle-reports -- ~60 KB for the default page (25 rows). --out is
    MANDATORY: this command refuses stdout unconditionally, --json/--summary included."""
    if out is None:
        _fail(
            "`--out FILE` is mandatory for `battle-reports` -- the payload is tens of KB "
            "and would blow the context window. Refusing to print to stdout."
        )
    data = _fetch_or_exit("/battle-reports", max_age=max_age)
    out.write_text(json.dumps(data, indent=2))
    count = len(data) if isinstance(data, list) else "?"
    rprint(f"[green]wrote[/] {out} ({out.stat().st_size:,} bytes, {count} report(s))")


@app.command()
def highscores(
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
) -> None:
    """GET /highscores -- the global leaderboard. --out is MANDATORY: this command
    refuses stdout unconditionally. Measured ~2.2 MB on 2026-08-12 for the default page
    (50 rows x 8 ranking categories, each row carrying a full planet+tactical payload) --
    well past the ~86 KB NOTES.md/SPEC.md figure; see references/api-routes.md."""
    if out is None:
        _fail(
            "`--out FILE` is mandatory for `highscores` -- the payload is 1-2+ MB and "
            "would blow the context window. Refusing to print to stdout."
        )
    data = _fetch_or_exit("/highscores", max_age=max_age)
    out.write_text(json.dumps(data, indent=2))
    rprint(f"[green]wrote[/] {out} ({out.stat().st_size:,} bytes)")


# --------------------------------------------------------------------------------------
# snapshot -- composed from health + overview + infrastructure + research + shipyard +
# defenses. The primary consumer of this whole work package (SPEC.md §5.2).
# --------------------------------------------------------------------------------------


def _planet_snapshot(
    planet_id: int,
    *,
    infrastructure_raw: dict[str, Any],
    shipyard_raw: dict[str, Any],
    defenses_raw: dict[str, Any],
    overview_planet: dict[str, Any] | None,
    archetype: str | None = None,
) -> models.PlanetSnapshot:
    energy_raw = infrastructure_raw.get("energyBalance") or {}
    sources = energy_raw.get("sources") or {}
    energy = (
        models.EnergyBalance(
            produced=energy_raw.get("produced", 0),
            required=energy_raw.get("required", 0),
            scale_bps=energy_raw.get("scaleBps", 10_000),
            solar_satellite_energy=sources.get("solarSatelliteEnergy"),
        )
        if energy_raw
        else None
    )

    queues: dict[models.QueueKind, models.QueueEntry | None] = {
        models.QueueKind.BUILDING: _queue_entry(
            infrastructure_raw.get("queue"), fallback_kind=models.QueueKind.BUILDING, names=BUILDING_NAMES
        ),
        models.QueueKind.SHIP: _queue_entry(
            shipyard_raw.get("queue"), fallback_kind=models.QueueKind.SHIP, names=SHIP_NAMES
        ),
        models.QueueKind.DEFENSE: _queue_entry(
            defenses_raw.get("queue"), fallback_kind=models.QueueKind.DEFENSE, names=DEFENSE_NAMES
        ),
    }

    coordinates = name = None
    fields_used = fields_total = temperature = None
    metal_mult = crystal_mult = deut_mult = 10_000
    if overview_planet:
        coordinates = overview_planet.get("coordinates")
        name = overview_planet.get("name")
        fields_used = overview_planet.get("fieldsUsed")
        fields_total = overview_planet.get("fieldsCapacity")
        temperature = overview_planet.get("temperature")
        metal_mult = overview_planet.get("metalMultiplierBps", 10_000)
        crystal_mult = overview_planet.get("crystalMultiplierBps", 10_000)
        deut_mult = overview_planet.get("deuteriumMultiplierBps", 10_000)

    raidable = infrastructure_raw.get("raidableResources")
    protected = infrastructure_raw.get("protectedResources")

    crawler_raw = infrastructure_raw.get("crawlerProduction")
    crawler_production = (
        models.CrawlerProduction(
            total=crawler_raw.get("total"),
            effective=crawler_raw.get("effective"),
            max_effective=crawler_raw.get("maxEffective"),
            boost_bps=crawler_raw.get("boostBps"),
            capped=crawler_raw.get("capped"),
        )
        if crawler_raw
        else None
    )

    return models.PlanetSnapshot(
        planet_id=planet_id,
        coordinates=coordinates,
        name=name,
        #: Phase 5: populated from `/universe/galaxies/{g}/systems/{s}` when the caller
        #: opts in via `snapshot(..., universe_cadence_hours=...)` -- `None` otherwise
        #: (no wallet route this function otherwise composes from ever reports it; see
        #: api-routes.md §3.16).
        archetype=archetype,
        temperature=temperature,
        fields_used=fields_used,
        fields_total=fields_total,
        metal_multiplier_bps=metal_mult,
        crystal_multiplier_bps=crystal_mult,
        deuterium_multiplier_bps=deut_mult,
        resources=_resources(infrastructure_raw.get("resources")),
        resources_as_of_now=_resources(infrastructure_raw.get("resourcesAsOfNow")),
        storage_caps=_resources(infrastructure_raw.get("storageCaps")),
        production_per_hour=_resources(infrastructure_raw.get("productionPerHour")),
        raidable_resources=_resources(raidable) if raidable else None,
        protected_resources=_resources(protected) if protected else None,
        energy=energy,
        buildings=_entities(infrastructure_raw.get("buildings"), BUILDING_NAMES, level_key="level", count_key=None),
        ships=_entities(shipyard_raw.get("ships"), SHIP_NAMES, level_key=None, count_key="count"),
        defenses=_entities(defenses_raw.get("defenses"), DEFENSE_NAMES, level_key=None, count_key="count"),
        queues=queues,
        missile_silo_level=defenses_raw.get("missileSiloLevel"),
        crawler_production=crawler_production,
    )


@app.command()
def snapshot(
    wallet: str | None = WalletOption,
    planet_id: int | None = PlanetIdOption,
    json_output: bool = JsonSummaryOption,
    out: Path | None = OutOption,
    max_age: float | None = MaxAgeOption,
    universe_cadence_hours: float | None = typer.Option(
        None,
        "--universe-cadence-hours",
        help=(
            "If set, also fetch each planet's archetype from "
            "/universe/galaxies/{g}/systems/{s} (Phase 5), cached for this many hours "
            "(policy.cadence.universe_hours from tick.py). Omit to skip the fetch "
            "entirely, unchanged from pre-Phase-5 behaviour -- this is opt-in so a bare "
            "`vd read snapshot` never gains a new network call by surprise."
        ),
    ),
) -> None:
    """Composed snapshot: health + overview + infrastructure + research + shipyard +
    defenses, one PlanetSnapshot per planet (all owned planets if --planet-id is
    omitted). The primary consumer of this work package.

    Deviates from SPEC.md §5.2's literal "health + infrastructure + research + shipyard
    + defenses + fleet-visibility": this fetches `overview` in place of a bare
    `fleet-visibility` call. `overview` embeds a byte-identical `fleetVisibility` block
    (RESEARCH-ADDENDUM.md §2: "overview... bundles settlement + planets + queues +
    fleetVisibility") at the same call cost, and additionally carries
    coordinates/fields/temperature -- which SPEC.md's own required digest content
    ("fields used/total") needs and a bare fleet-visibility call cannot supply. Reported
    as a spec inconsistency; see the WP1 report and references/api-routes.md.
    """
    w = _need_wallet(wallet)

    health_raw = _fetch_or_exit("/health", max_age=max_age)
    health_ok = _health_ok(health_raw)
    game_paused, game_maintenance, degradation_reasons = _game_maintenance(health_raw)
    readiness_ready = (health_raw.get("readiness") or {}).get("ready") is True
    randomness_readiness = _randomness_readiness(health_raw)

    # Always fetched, even in single-planet (`--planet-id`) mode: `owned_planet_count`
    # below needs the account's *true* total, which the single-planet fast path would
    # otherwise never learn (see models.Snapshot.owned_planet_count's docstring).
    planets_raw = _fetch_or_exit(f"/wallet/{w}/planets", max_age=max_age)
    all_planet_ids = [int(p["planetId"]) for p in planets_raw.get("planets", [])]
    owned_planet_count = len(all_planet_ids)

    if planet_id is not None:
        planet_ids = [planet_id]
    else:
        planet_ids = all_planet_ids
        if not planet_ids:
            _fail(f"wallet {w} has no settled planets.")

    # research + technologies are per-player (models.py docstring); one call suffices
    # even for a multi-planet wallet. Any owned planet id works as the query anchor.
    research_raw = _fetch_or_exit(f"/wallet/{w}/research", {"planetId": planet_ids[0]}, max_age=max_age)

    planet_snapshots: list[models.PlanetSnapshot] = []
    incoming_fleets: list[models.IncomingFleet] = []
    indexer_block: dict[str, Any] = {}
    fleet_slots_active: int | None = None
    fleet_slots_limit: int | None = None

    for pid in planet_ids:
        overview_raw = _fetch_or_exit(f"/wallet/{w}/overview", {"planetId": pid}, max_age=max_age)
        infrastructure_raw = _fetch_or_exit(f"/wallet/{w}/infrastructure", {"planetId": pid}, max_age=max_age)
        shipyard_raw = _fetch_or_exit(f"/wallet/{w}/shipyard", {"planetId": pid}, max_age=max_age)
        defenses_raw = _fetch_or_exit(f"/wallet/{w}/defenses", {"planetId": pid}, max_age=max_age)

        overview_planet = None
        for p in (overview_raw.get("planetsResponse") or {}).get("planets", []):
            if _maybe_int(p.get("planetId")) == pid:
                overview_planet = p
                break

        archetype: str | None = None
        if universe_cadence_hours is not None and overview_planet is not None:
            archetype = _universe_archetype_for_planet(
                overview_planet.get("coordinates"), max_age=universe_cadence_hours * 3600
            )

        planet_snapshots.append(
            _planet_snapshot(
                pid,
                infrastructure_raw=infrastructure_raw,
                shipyard_raw=shipyard_raw,
                defenses_raw=defenses_raw,
                overview_planet=overview_planet,
                archetype=archetype,
            )
        )

        for raw_fleet in (overview_raw.get("fleetVisibility") or {}).get("incoming", []):
            incoming_fleets.append(_incoming_fleet(raw_fleet))

        if not indexer_block:
            indexer_block = infrastructure_raw.get("indexer") or {}
        if fleet_slots_active is None:
            slots = shipyard_raw.get("fleetSlots") or {}
            fleet_slots_active = slots.get("active")
            fleet_slots_limit = slots.get("limit")

    technologies = _entities(research_raw.get("technologies"), TECHNOLOGY_NAMES, level_key="level", count_key=None)
    research_queue = _queue_entry(
        research_raw.get("queue"), fallback_kind=models.QueueKind.RESEARCH, names=TECHNOLOGY_NAMES
    )
    build = (health_raw.get("backend") or {}).get("build") or {}

    snap = models.Snapshot(
        taken_at=datetime.now(UTC),
        wallet=w,
        health_ok=health_ok,
        game_paused=game_paused,
        game_maintenance=game_maintenance,
        degradation_reasons=degradation_reasons,
        readiness_ready=readiness_ready,
        randomness_readiness=randomness_readiness,
        indexed_state=indexer_block.get("indexedState"),
        safe_to_serve_indexed_state=indexer_block.get("safeToServeIndexedState"),
        latest_indexed_block=_maybe_int(indexer_block.get("latestIndexedBlock")),
        deployment_abi_hash=build.get("deploymentAbiHash"),
        eth_balance_wei=None,  # no read route reports wallet ETH balance; walletctl's job
        technologies=technologies,
        research_lab_level=research_raw.get("researchLabLevel", 0),
        research_queue=research_queue,
        fleet_slots_active=fleet_slots_active,
        fleet_slots_limit=fleet_slots_limit,
        planets=planet_snapshots,
        owned_planet_count=owned_planet_count,
        incoming_fleets=incoming_fleets,
    )

    if out is not None:
        out.write_text(snap.model_dump_json(indent=2))
        rprint(f"[green]wrote[/] {out} ({out.stat().st_size:,} bytes)")
    elif json_output:
        typer.echo(snap.model_dump_json(indent=2))
    else:
        fmt.print_snapshot(snap)

    if not health_ok:
        raise typer.Exit(code=2)
