# ABI pinning

`veydrift-wallet` never trusts an ABI it did not pin. It calldata-encodes, decodes and allowlists
against committed artifacts under `abi/`, and refuses every write when the deployed contracts stop
matching the pin. This reference covers what is pinned, how drift is detected, and how to re-pin.

## What is pinned

| Pin | File | What it describes |
| --- | --- | --- |
| Game ABI | `abi/VeydriftGame.<sha7>.json` (`PINNED.json`) | The `VeydriftGame` proxy's forge ABI |
| Alliance ABI | `abi/VeydriftAllianceSystem.<sha7>.json` (`PINNED.alliance.json`) | The `VeydriftAllianceSystem` proxy |
| Supplemental ABI | `abi/VeydriftDelegation.<sha7>.json` (`PINNED.json` → `supplemental`) | Entrypoints the game proxy serves from `fallback()` |
| On-chain pin | `PINNED*.json` → `implementation`, `dependencies` | Each proxy's implementation address + code hash |

`PINNED.json` also records the deployment commit, the foundry settings, a provenance block
(including how the commit was confirmed), and `backendReported` (below). `PINNED.json`'s `artifact`
field names the ABI file, so a re-pin changes data, not source.

Each artifact holds `{ abi, methodIdentifiers }` extracted from the forge build — no bytecode or
metadata, since this engine encodes and decodes calldata and never deploys.

**The game ABI hash is `sha256(JSON.stringify(artifact.abi))`** — compact JSON, forge's key order,
not canonicalized. `computePinnedAbiHash()` recomputes it from the file on disk every time and never
trusts the cached `abiHash`, so a hand-edited pin disagrees with itself.

**The supplemental ABI exists because the delegation functions are not in the game artifact.**
`setDelegate`, `revokeDelegate`, `delegateOf`, `delegatorOf` and `effectivePlayer` are routed through
the proxy's `fallback()` and declared only in an interface, so neither the forge `VeydriftGame`
artifact nor the backend's hash contains them. `getResolvableAbi("game")` merges the supplemental
ABI for function resolution (build, simulate, send, display) and never into `getPinnedAbi`, which is
what the hash is computed over. `launchTransportBatch` and a few preview selectors are also
fallback-routed and deliberately not pinned (not allowlisted).

## How drift is detected

**The authority is the chain, not the backend.** The backend's `deploymentAbiHash` /
`deploymentCommit` in `/runtime-config` are its own deployment metadata and can lag the chain
arbitrarily; comparing against them cannot detect an upgrade the backend has not recorded.
`src/onchain-pin.ts` instead reads each pinned proxy's EIP-1967 implementation slot from the chain
and compares it (and, on request, the implementation's runtime-code hash) to the pin.

- **Game and alliance proxies** — both must match before *any* write. The game is an OpenZeppelin
  TransparentUpgradeableProxy; the alliance is an ERC1967 forwarder with UUPS authorization.
  Either way behavior changes only by swapping the implementation. The game router reaches its
  modules and libraries through `immutable` addresses, and contract code cannot be replaced at an
  existing address, so the implementation address transitively pins every delegatecall target.
- **Dependencies** — the game also *calls* (not delegatecalls) the Randomness engine and the Moon
  system, whose addresses live in proxy storage and whose implementations are independently
  upgradeable. They are pinned under `dependencies` and required only for the functions in
  `dependencies.appliesTo` (fleet launches, missile, mission resolution). The Randomness address is
  cross-checked against the game's own `randomnessEngine()` getter; the game exposes no Moon getter,
  so the Moon proxy address is the backend-reported one (a documented residual). Other external
  contracts the game calls (migration, paid invites, space dock) are not pinned.
- **`tx.to`** must be one of the two pinned proxies.

Where it runs:

| Where | Mode | On failure |
| --- | --- | --- |
| `walletctl build` | slot reads only (fast); verdict stored in the tx file as `onchainPin` | The agent's `abi_hash` gate BLOCKs |
| `walletctl send` | slot reads + code hash, immediately before signing | Refuses with `SendRefusedError` |
| `walletctl verify-abi [--json]` | full | Exit 1 |
| `walletctl status` | full | Prints MISMATCH |

The check is fail-closed: an unreadable chain, an empty slot or a mismatch all yield `ok: false`,
and `send` throws `SendRefusedError` (never a generic error, which the agent would treat as a
possible broadcast). A `/runtime-config` that is unreachable is only a warning, because the verdict
never depends on the backend; a `/runtime-config` that names a different proxy address than the pin
is a failure.

**The backend hash is advisory.** `verifyAbi()` compares the pinned game ABI hash to
`deploymentAbiHash` and classifies it: `match`, `known-stale` (equals `backendReported`, the value
recorded at pin time), `other`, or `unavailable`. Only `other` warrants attention, and it never
overrides the on-chain result. The alliance and delegation entrypoints have no backend hash at all.

## Residual limits

- Moon proxy address is backend-reported (no on-chain getter).
- Migration, paid-invite and space-dock contracts the game calls are not pinned.
- `send` checks the chain at signing time; a block-level race between that check and inclusion
  (an upgrade landing in between) cannot be excluded.
- The pin proves the ABI and code match a build, not that the build is correct.

## Re-pinning

A mismatch means the contracts were upgraded (or an RPC is failing — `verify-abi` says which). To
move the pin, build the deployed commit and let the tool prove it:

```bash
# 1. Find the commit. /runtime-config may lag the chain; `main` is not the deployed contract
#    either. Candidates: the commit named by `deploymentCommit`, or the latest commit that changed
#    packages/contracts/src.
# 2. Build it with the pinned foundry settings (PINNED.json → foundry), into any scratch directory:
forge build --skip test --skip script          # in <contracts>/, submodules at the commit's SHAs

# 3. Prove it is what is deployed (exit 1 on any unmatched contract):
npm run repin -- confirm --out <contracts>/out

# 4. Regenerate the pins (runs `confirm` first and refuses to write on a mismatch):
npm run repin -- write --out <contracts>/out --commit <40-hex sha>
```

`confirm` walks every contract reachable from the pinned proxies — router, modules, nested modules,
libraries, and the Randomness/Moon implementations — and matches each one's runtime code to a forge
artifact byte-for-byte, masking `immutableReferences` (filled at construction) and `linkReferences`
(library placeholders). Matching the router alone proves little: the logic that changes lives in the
modules. Only a full match is "confirmed"; anything less must be recorded as compatible-only in
`PINNED.json` and stated as such.

`write` regenerates the three ABI files and both `PINNED*.json` (including `implementation`,
`dependencies` and `backendReported`) from the artifacts and the live chain, and removes superseded
ABI files. Three things stay manual on purpose, each a tripwire:

1. `tests/abi.test.ts` — expected hash, commit and known-stale backend hash.
2. The agent's `guard.py` — `PINNED_ABI_HASH` and `KNOWN_STALE_BACKEND_ABI_HASH`
   (`test_agent_hash_constants_agree_with_the_wallets_pin` fails until they match).
3. Docs and the CHANGELOG.

Finish with `VEYDRIFT_LIVE_TESTS=1 npm test` (reads the real chain) and `walletctl verify-abi`.

An ABI-neutral upgrade (no selector or signature changes) still requires a re-pin: the strict pin
blocks on the implementation address, not on the ABI, because behavior — limits, costs, caps — can
change without any signature changing.

`playerScore` / `firstPlanetOf` presence is a useful smoke check that a rebuild used the deployed
commit and not `main`: the pinned artifact's `methodIdentifiers` decide, and `tests/abi.test.ts`
asserts them.
