"""`vd` entrypoint.

Each feature module exposes a module-level ``app: typer.Typer`` which this file mounts.
Mounting is tolerant of missing modules on purpose: the work packages that provide them
are built in parallel, so a half-built tree should still run the parts that exist rather
than failing to import entirely.

Do not add command implementations here. This file only wires sub-apps together.
"""

from __future__ import annotations

import importlib

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Veydrift planet agent — reads game state, plans actions, builds onchain "
    "transactions. It never signs or submits: that is walletctl's job.",
)

#: (module, mount name, help). Order controls `vd --help` ordering.
_SUBAPPS: list[tuple[str, str, str]] = [
    ("veydrift_agent.read", "read", "Fetch and summarise game state from the read API"),
    ("veydrift_agent.calc", "calc", "Deterministic game calculators (no network)"),
    ("veydrift_agent.plan", "plan", "Decide the next action from a snapshot"),
    ("veydrift_agent.guard", "guard", "Evaluate guardrails against a proposed action"),
    ("veydrift_agent.tick", "tick", "Run one loop iteration"),
    ("veydrift_agent.log", "log", "Read and summarise the action and strategy logs"),
]

_MISSING: list[str] = []

for _module_path, _name, _help in _SUBAPPS:
    try:
        _module = importlib.import_module(_module_path)
        _sub = getattr(_module, "app")
    except (ImportError, AttributeError):
        _MISSING.append(_name)
        continue
    app.add_typer(_sub, name=_name, help=_help)


@app.command()
def doctor() -> None:
    """Report which sub-commands are wired up and where state lives."""
    from veydrift_agent.state import veydrift_home  # local import: state.py is WP3's

    typer.echo(f"VEYDRIFT_HOME: {veydrift_home()}")
    wired = [n for _, n, _ in _SUBAPPS if n not in _MISSING]
    typer.echo(f"wired:   {', '.join(wired) or '(none)'}")
    if _MISSING:
        typer.echo(f"missing: {', '.join(_MISSING)}")


if __name__ == "__main__":
    app()
