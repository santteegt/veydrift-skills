#!/usr/bin/env python3
"""Regenerate `schemas/policy.schema.json` and `schemas/action.schema.json` from the
frozen pydantic models in `models.py`, via `model_json_schema()` -- schemas are always
generated this way, never hand-written.

Run after any change to `Policy` or `Action` in `models.py`:

    uv run --directory skills/veydrift-agent python scripts/generate_schemas.py

or via the Makefile target:

    make -C skills/veydrift-agent schemas

Both schema files are committed -- this script is how they stay in sync, not a build
step CI needs to run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "src"))

from veydrift_agent.models import Action, Policy


def _write(model, filename: str) -> None:
    schema = model.model_json_schema()
    out = SKILL_ROOT / "schemas" / filename
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


def main() -> None:
    (SKILL_ROOT / "schemas").mkdir(parents=True, exist_ok=True)
    _write(Policy, "policy.schema.json")
    _write(Action, "action.schema.json")


if __name__ == "__main__":
    main()
