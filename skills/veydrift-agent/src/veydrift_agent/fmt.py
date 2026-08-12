"""Rich-based summary rendering for `vd read`.

Every command's `--summary` (the default) goes through here. The one hard budget is
`snapshot`: SPEC.md §5.2 caps it at <=2 KB and names exactly what must be in it --
levels, the affordable-now set, energy balance + scale_bps, production/hr, hours-to-cap
per resource, queue ETAs, incoming fleets, fields used/total. `render_snapshot` below
builds to that list item-for-item and self-truncates if it ever overruns. Every other
target gets a best-effort compact rendering; there is no byte budget for those.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rich.console import Console

from veydrift_agent import models

#: SPEC.md §5.2: "--summary ... emits a <=2 KB digest". Enforced in `render_snapshot`.
_SNAPSHOT_BUDGET_BYTES = 2048

#: `soft_wrap=True` disables Rich's own line-wrapping (which inserts real newlines at
#: the console width) so the bytes we hand it are the bytes it prints -- important for
#: `render_snapshot`, which measures and truncates its own output to a byte budget.
_console = Console(soft_wrap=True)


# --------------------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------------------


def _fmt_resources(r: models.Resources) -> str:
    return f"M {r.metal:,} C {r.crystal:,} D {r.deuterium:,}"


def _hours_to_cap(current: int, per_hour: int, cap: int) -> str:
    """Hours until ``current`` (growing at ``per_hour``) reaches ``cap``.

    This is plain arithmetic over numbers the API already returns live (storage caps,
    production/hr) -- not a cost-scaling recomputation, which SPEC.md §5.3 reserves for
    `calc.py` (WP2) alone. `calc.py` will carry the canonical, tested version of this
    same formula; it is duplicated here in miniature so `read.py`'s summary has no hard
    import-time dependency on a sibling work package that may not exist yet in a
    partially-built tree (see the `cli.py` sub-app mounting comment for the same
    tolerance-of-missing-modules posture).
    """
    if per_hour <= 0:
        return "at cap" if current >= cap else "never (idle)"
    if current >= cap:
        return "at cap"
    hours = (cap - current) / per_hour
    return f"{hours:.1f}h" if hours < 1000 else ">1000h"


def _relative(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    delta = (dt - datetime.now(UTC)).total_seconds()
    if delta <= 0:
        return "due"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


# --------------------------------------------------------------------------------------
# snapshot -- the primary consumer of this whole module (SPEC.md §5.2)
# --------------------------------------------------------------------------------------


def render_snapshot(snap: models.Snapshot) -> str:
    lines: list[str] = [
        (
            f"snapshot {snap.wallet}  health={'ok' if snap.health_ok else 'DOWN'}  "
            f"indexed={snap.indexed_state or '?'}  block={snap.latest_indexed_block or '?'}"
        )
    ]

    if snap.technologies:
        nonzero = [t for t in snap.technologies if (t.level or 0) > 0]
        tech_str = ", ".join(f"{t.name} {t.level}" for t in nonzero) or "all level 0"
        lines.append(f"research: lab L{snap.research_lab_level}  {tech_str}")
        if snap.research_queue:
            q = snap.research_queue
            lines.append(f"  queue: {q.entity_name} -> L{q.target_level} ({_relative(q.ready_at)})")

    for planet in snap.planets:
        coord = planet.coordinates or f"planet {planet.planet_id}"
        fields = (
            f"{planet.fields_used}/{planet.fields_total}"
            if planet.fields_used is not None and planet.fields_total is not None
            else "?"
        )
        lines.append(f"-- {coord} (id {planet.planet_id})  fields {fields}")

        levels = [b for b in planet.buildings if (b.level or 0) > 0]
        levels_str = ", ".join(f"{b.name} {b.level}" for b in levels) or "all level 0"
        lines.append(f"   levels: {levels_str}")

        if planet.energy:
            e = planet.energy
            lines.append(f"   energy: {e.produced}/{e.required} (scale {e.scale_bps})")

        prod = planet.production_per_hour
        lines.append(f"   production/hr: {_fmt_resources(prod)}")

        caps = planet.storage_caps
        cur = planet.resources_as_of_now
        htc = (
            f"M {_hours_to_cap(cur.metal, prod.metal, caps.metal)}  "
            f"C {_hours_to_cap(cur.crystal, prod.crystal, caps.crystal)}  "
            f"D {_hours_to_cap(cur.deuterium, prod.deuterium, caps.deuterium)}"
        )
        lines.append(f"   hours-to-cap: {htc}")

        affordable = [
            e.name for e in (*planet.buildings, *planet.ships, *planet.defenses) if cur.covers(e.cost)
        ]
        shown = affordable[:8]
        extra = f" (+{len(affordable) - 8} more)" if len(affordable) > 8 else ""
        lines.append(f"   affordable now: {', '.join(shown) or 'none'}{extra}")

        queue_bits = []
        for kind in (models.QueueKind.BUILDING, models.QueueKind.SHIP, models.QueueKind.DEFENSE):
            q = planet.queues.get(kind)
            if q:
                queue_bits.append(f"{kind.value}: {q.entity_name} ({_relative(q.ready_at)})")
        lines.append(f"   queues: {'; '.join(queue_bits) or 'idle'}")

    if snap.incoming_fleets:
        inc = "; ".join(
            f"{f.mission_type_name or '?'} from {f.origin or '?'} -> planet {f.target_planet_id} "
            f"({_relative(f.arrives_at)})"
            for f in snap.incoming_fleets[:5]
        )
        more = len(snap.incoming_fleets) - 5
        lines.append(f"incoming: {inc}" + (f" (+{more} more)" if more > 0 else ""))
    else:
        lines.append("incoming: none")

    rendered = "\n".join(lines)
    encoded = rendered.encode("utf-8")
    if len(encoded) > _SNAPSHOT_BUDGET_BYTES:
        # The full picture is always available via --json / --out; the summary's whole
        # point is to fit in a budget, so truncate rather than blow it.
        truncated = encoded[: _SNAPSHOT_BUDGET_BYTES - 24].decode("utf-8", errors="ignore")
        rendered = truncated + "\n... (truncated, use --json)"
    return rendered


def print_snapshot(snap: models.Snapshot) -> None:
    _console.print(render_snapshot(snap), markup=False, highlight=False)


# --------------------------------------------------------------------------------------
# Bespoke renderers for a few high-value targets
# --------------------------------------------------------------------------------------


def render_health(data: dict[str, Any]) -> str:
    ok = data.get("ok")
    readiness = data.get("readiness") or {}
    worker = (data.get("backend") or {}).get("worker") or {}
    build = (data.get("backend") or {}).get("build") or {}
    lines = [
        f"health: ok={ok}  ready={readiness.get('ready')}  degraded={readiness.get('degraded')}",
        f"worker: role={worker.get('role')} index={worker.get('index')}/{worker.get('count')}",
    ]
    # null chainSync/indexer/rpc/most-of-readiness is a read-replica artifact when
    # worker.role == "reader", not an outage (RESEARCH-ADDENDUM.md / NOTES.md §9). Note
    # it rather than alarm on it.
    if worker.get("role") == "reader":
        lines.append("  (reader replica: chainSync/indexer/rpc nulls above are expected, not an outage)")
    reasons = readiness.get("degradationReasons") or []
    if reasons:
        lines.append(f"degradation reasons: {'; '.join(reasons)}")
    if build:
        commit = str(build.get("deploymentCommit", "?"))[:12]
        lines.append(f"deployed: {commit}  abi={build.get('deploymentAbiHash', '?')}")
    return "\n".join(lines)


def render_config(data: dict[str, Any]) -> str:
    build = (data.get("backend") or {}).get("build") or {}
    features = data.get("featureSupport") or {}
    on = sorted(k for k, v in features.items() if v)
    return "\n".join(
        [
            f"chain: {data.get('network', '?')} (chainId {data.get('chainId', '?')})",
            f"game contract: {data.get('gameContractAddress', data.get('contractAddress', '?'))}",
            f"deployed: {str(build.get('deploymentCommit', '?'))[:12]}  abi={build.get('deploymentAbiHash', '?')}",
            f"features on: {', '.join(on) or 'none'}",
        ]
    )


# --------------------------------------------------------------------------------------
# Generic fallback -- every target without a bespoke renderer above
# --------------------------------------------------------------------------------------


def render_generic(target: str, data: Any) -> str:
    """Scalars as ``key: value``; lists/dicts collapse to a count so a big nested payload
    (most wallet routes carry a ~30-field `indexer` bookkeeping block, for instance)
    never floods the terminal. Full detail is always one `--json` away."""
    if isinstance(data, list):
        return f"{target}: {len(data)} item(s) -- use --json for full detail"
    if not isinstance(data, dict):
        return str(data)

    lines = [f"{target}:"]
    for key, value in data.items():
        if key == "indexer":
            continue  # repeated bookkeeping block on every wallet route; not summary-worthy
        if isinstance(value, dict):
            lines.append(f"  {key}: {{{len(value)} field(s)}}")
        elif isinstance(value, list):
            lines.append(f"  {key}: [{len(value)} item(s)]")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


_BESPOKE = {
    "health": render_health,
    "config": render_config,
}


def render(target: str, data: Any) -> str:
    renderer = _BESPOKE.get(target)
    if renderer is not None:
        return renderer(data)
    return render_generic(target, data)


def print_summary(target: str, data: Any) -> None:
    _console.print(render(target, data), markup=False, highlight=False)
