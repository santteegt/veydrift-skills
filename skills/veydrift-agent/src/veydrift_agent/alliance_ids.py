"""Canonical enum <-> name map for `VeydriftAllianceSystem.sol`'s one on-chain enum.

**Deliberately a sibling module to `ids.py`, not an addition to it.** `ids.py`'s own module
docstring scopes itself explicitly to "Veydrift's six on-chain enums" from the *game* contract
(`VeydriftGame.sol` and its modules) at the pinned commit. `AllianceRole` is declared in a
genuinely different deployed contract, `VeydriftAllianceSystem.sol`, with its own separately
pinned ABI (`skills/veydrift-wallet/abi/VeydriftAllianceSystem.701bed3.json`) — folding it into
`ids.py` would widen that module's documented scope from "always the game contract" to
"sometimes not," which is a real regression, not a convenience. `FLEET_TUPLE_ORDER` living in
`ids.py` is not a counter-precedent: that's documentation of a *game*-contract calling
convention (the wallet's fleet-tuple encoding), still entirely about the one contract `ids.py`
already owns.

**Source of truth**: the deployed contract, not this docstring's memory of it. Repo
`/Users/santteegt/GitRepositories/clones/veydrift`, commit
`701bed3578cff4d134657c714c599dbdb55a4b6a` (the same pinned commit `ids.py` uses — both
contracts were read from the same commit). `AllianceRole` is declared at
`packages/contracts/src/VeydriftAllianceSystem.sol:37` (`enum AllianceRole { None, Member,
Officer, Owner }`).

No network calls, no cost math — this module is pure data, same posture as `ids.py`.
"""

from __future__ import annotations

from enum import IntEnum


class AllianceRole(IntEnum):
    """packages/contracts/src/VeydriftAllianceSystem.sol:37 (commit 701bed35).

    Member order is the contract's declaration order, which IS the on-chain role value —
    same convention `ids.py`'s six enums use. `NONE` is a real, meaningful value on-chain
    (the zero-value returned for an address with no membership row at all), not a filler.
    """

    NONE = 0
    MEMBER = 1
    OFFICER = 2
    OWNER = 3


ALLIANCE_ROLE_NAMES: dict[int, str] = {
    AllianceRole.NONE: "None",
    AllianceRole.MEMBER: "Member",
    AllianceRole.OFFICER: "Officer",
    AllianceRole.OWNER: "Owner",
}


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().replace("_", " ").replace("-", " ").split())


ALLIANCE_ROLE_IDS: dict[str, int] = {_normalize(name): id_ for id_, name in ALLIANCE_ROLE_NAMES.items()}


def role_name(id_: int) -> str:
    return ALLIANCE_ROLE_NAMES.get(id_, f"AllianceRole#{id_}")


def role_id(name: str) -> int:
    return ALLIANCE_ROLE_IDS[_normalize(name)]


def meets_min_role(role: int, minimum: int) -> bool:
    """`role >= minimum` — role values are already ordinal on-chain, so this is trivial
    arithmetic, but named for readability at call sites (`guard.py`'s `_gate_alliance_action`
    uses this for every "Officer or above" / "Owner only" precondition rather than repeating
    the raw comparison at each branch)."""
    return role >= minimum
