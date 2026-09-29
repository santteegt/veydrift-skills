"""Staleness guard for `docs/COVERAGE.md` — the write-entrypoint coverage ledger.

`docs/COVERAGE.md` Part 1 is meant to have one row for every write entrypoint in the pinned
ABI (`skills/veydrift-wallet/abi/VeydriftGame.202d1ac.json`), whether it's implemented,
planned, deferred, or explicitly out of scope. Nothing enforces that the document is kept in
sync with the ABI when a function is added, renamed, or removed on a re-pin — this test is
that enforcement, in the same spirit as
`test_guard.py::test_tier_map_agrees_with_the_wallet_engines_allowlist` (parses the real
files, `pytest.skip`s if a sibling path isn't present, and fails naming exactly what's wrong
rather than just asserting `False`).

**What this test guarantees**: every `nonpayable`/`payable` function name in the pinned ABI
appears somewhere in `docs/COVERAGE.md`'s text as a *whole identifier* — i.e. no entrypoint
was silently dropped from the ledger.

The whole-identifier match matters, and a plain substring check would pass vacuously without
it: four pinned-ABI names are proper substrings of another (`importMigratedState` /
`importMigratedStateWithReferral`, `setResourceToken` / `setResourceTokens`,
`settleFirstPlanet` / `settleFirstPlanetWithReferral`, `startPlanet` /
`startPlanetWithReferral`). Under `name in doc_text`, dropping the shorter row of any of
those pairs would still pass because the longer name spells it out — exactly the
"guardrail passes on absent data" failure this repo treats as its highest-value bug class
(AGENTS.md §5).

**What this test cannot check** — stated plainly, not implied: it says nothing about whether
a row's *content* is accurate. A function could be mentioned in COVERAGE.md with a completely
wrong Planner/Guard/Wallet/Status claim, or a stale citation, and this test would still pass.
Truthfulness of each row is a human-review problem (or a much more elaborate test that
cross-parses `guard.py`/`allowlist.ts`/`plan.py`, which this one deliberately does not
attempt — see `test_guard.py`'s own tier-map-agreement test for that narrower, harder
guarantee applied to a single column). This test only guarantees *completeness of mention*,
not correctness of description.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# tests/ -> veydrift-agent/ -> skills/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ABI_DIR = _REPO_ROOT / "skills" / "veydrift-wallet" / "abi"
_COVERAGE_DOC = _REPO_ROOT / "docs" / "COVERAGE.md"


def _pinned_abi_paths() -> list[Path]:
    """The pinned game ABI plus the supplemental delegation ABI, found by glob rather than by a
    hard-coded commit-suffixed filename. The filename changes on every re-pin; a hard-coded one
    turned this whole test into a silent skip the moment the pin moved (caught in review), which
    is exactly when it matters most. Returns `[]` only when the wallet skill is not alongside this
    checkout at all; a directory that exists but lacks exactly one game artifact is a failure."""
    if not _ABI_DIR.is_dir():
        return []
    game = sorted(_ABI_DIR.glob("VeydriftGame.*.json"))
    assert len(game) == 1, f"expected exactly one pinned VeydriftGame.<sha>.json in {_ABI_DIR}, found {[p.name for p in game]}"
    # The delegation entrypoints are served from the game proxy's fallback(), so they are absent
    # from the game artifact and live in a supplemental one (PINNED.json `supplemental`).
    return game + sorted(_ABI_DIR.glob("VeydriftDelegation.*.json"))


def _writable_function_names() -> set[str]:
    """Every ABI function name with `stateMutability` `nonpayable` or `payable` — the same
    filter docs/COVERAGE.md's own regeneration command uses (see its preamble). Deduplicated
    by name: an overloaded function (`launchFleetMission` has a 6-arg and a 7-arg form on the
    deployed ABI) only needs to be *mentioned* once for this test's purposes, even though
    COVERAGE.md documents both overloads as separate rows.
    """
    names: set[str] = set()
    for path in _pinned_abi_paths():
        raw = json.loads(path.read_text())
        entries = raw["abi"] if isinstance(raw, dict) else raw
        names |= {
            entry["name"]
            for entry in entries
            if entry.get("type") == "function" and entry.get("stateMutability") in ("nonpayable", "payable")
        }
    return names


def test_every_writable_abi_function_is_mentioned_in_coverage_doc():
    """Fails naming exactly which pinned-ABI function names are missing from
    docs/COVERAGE.md, so a re-pin (or a hand-edit that accidentally dropped a row) is caught
    immediately instead of silently going stale -- see this module's docstring for what a
    pass here does and does not guarantee.
    """
    if not _ABI_DIR.is_dir():
        pytest.skip(f"pinned ABI directory not found ({_ABI_DIR}) -- wallet skill not alongside this checkout")
    if not _COVERAGE_DOC.is_file():
        pytest.skip(f"docs/COVERAGE.md not found ({_COVERAGE_DOC})")

    names = _writable_function_names()
    assert names, f"no nonpayable/payable functions found under {_ABI_DIR} -- ABI parsing likely broke"

    doc_text = _COVERAGE_DOC.read_text()
    # Whole-identifier match, not `name in doc_text` -- see this module's docstring for the
    # four shadowed name pairs that would otherwise let a dropped row pass unnoticed.
    missing = sorted(
        name
        for name in names
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", doc_text)
    )

    assert not missing, (
        f"{len(missing)} pinned-ABI function(s) are not mentioned anywhere in docs/COVERAGE.md: "
        f"{missing}. Add a row for each (Part 1 of the doc) -- see its preamble for the exact "
        "jq command that regenerates the full function list, and the status vocabulary "
        "(implemented / planned P<n> / deferred / out of scope -- <reason> / correctly excluded)."
    )
