"""veydrift-agent: reads Veydrift game state, plans actions, builds onchain calldata.

This package never signs or submits a transaction -- that is `veydrift-wallet`'s job.
See `skills/veydrift-agent/SKILL.md` and `docs/SPEC.md` for the full contract.
"""

from __future__ import annotations

__version__ = "0.1.0"
