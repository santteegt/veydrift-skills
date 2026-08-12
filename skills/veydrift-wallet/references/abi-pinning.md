# ABI pinning

## Why this exists

`veydrift-wallet` never trusts a freshly-`forge build`-ed ABI at runtime. It trusts only the
committed `abi/VeydriftGame.701bed3.json`, and every write path is gated on that pinned ABI's hash
matching the live backend's `deploymentAbiHash` (`walletctl verify-abi`, and `checkAllowlist` in
`src/allowlist.ts`). This document records how the pin was produced, how to reproduce it, and why
building from `main` gives you the wrong answer.

**`main` is not the deployed contract.** As of 2026-08-11, `main` HEAD
(`84e468f6371ef844b4aa8293921737d569d0486a` at that time) has already drifted from what's
actually running in production. Building the ABI
from whatever `main` happens to be at any given moment produces a *different, wrong* hash. The
only correct source is the specific commit the live backend reports as `deploymentCommit`.

## The pin, as shipped

| Field | Value |
| --- | --- |
| Deployment commit | `701bed3578cff4d134657c714c599dbdb55a4b6a` |
| ABI hash | `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99` |
| Verified against live `/runtime-config` | yes, `backend.build.deploymentAbiHash` matched exactly, same date |
| `main` HEAD's ABI hash (for contrast — DO NOT USE) | `sha256:361b1c94bf532b97b9971ad41c5be1b4d952710f7c56f046f3999b520179d2a8` |

`abi/PINNED.json` records this plus the foundry settings and the full provenance chain
(local clone path, commit, artifact path, build command, and the live-verification timestamp).
`abi/VeydriftGame.701bed3.json` holds `{ abi, methodIdentifiers }` extracted from the forge
artifact — no bytecode, no metadata, since neither is needed (or wanted) here: this engine
encodes/decodes calldata and never deploys or verifies bytecode.

## Hash derivation

```
sha256( JSON.stringify( artifact.abi ) )
```

Compact JSON (`JSON.stringify`'s default, no whitespace), key order exactly as forge emits it and
as `JSON.parse` preserves it — this is **not** a canonicalized/sorted-keys hash. It matches the
derivation the backend itself uses
(`scripts/veydrift-deployment-manifest.mjs:129-135` in the veydrift repo, per
`RESEARCH-ADDENDUM.md` §1), which is why comparing against `backend.build.deploymentAbiHash` is a
valid check and not just an internal consistency check against our own artifact.

`src/abi.ts`'s `computePinnedAbiHash()` recomputes this from the on-disk pinned ABI file every
time — it never trusts the cached `abiHash` field in `PINNED.json`. A hand-edited pin (or a
corrupted file) is caught by `verify-abi` disagreeing with itself, not just with the live API.

## Rebuild recipe

Foundry settings that matter for reproducibility, from `packages/contracts/foundry.toml` at the
deployment commit: `solc 0.8.28`, `optimizer_runs 1`, `via_ir true`, `cbor_metadata false`,
`bytecode_hash "none"`. None of these affect the ABI's *shape*, but they're recorded in
`PINNED.json.foundry` because a solc version bump or optimizer setting change is exactly the kind
of thing that silently produces a different artifact layout on a future rebuild, and because
`cbor_metadata`/`bytecode_hash` are the settings that would otherwise embed a metadata hash — moot
for us since we only ever hash the `abi` field, never bytecode, but worth pinning anyway so a
rebuild is a real reproduction, not a coincidence.

**The clone at `/Users/santteegt/GitRepositories/clones/veydrift` may be sitting on any branch or
commit when you go to rebuild — check it out fresh every time, do not assume it's still on the
deployment commit from a previous session.** It's a shared, mutable working tree, not a
purpose-built pin. In particular, do not trust the working tree's current `HEAD` as evidence of
anything; always `git checkout` the exact commit below before building.

```bash
REPO=/Users/santteegt/GitRepositories/clones/veydrift
DEPLOY_COMMIT=701bed3578cff4d134657c714c599dbdb55a4b6a   # from live /runtime-config, not memorized

git -C "$REPO" status --short                 # confirm clean before touching it
git -C "$REPO" checkout "$DEPLOY_COMMIT"
git -C "$REPO" submodule update --init --recursive --depth 1

cd "$REPO/packages/contracts"
rm -rf out                                     # do not trust a stale `out/` from a prior checkout
forge build --skip test --skip script

node -e '
  const fs = require("fs");
  const crypto = require("crypto");
  const artifact = JSON.parse(fs.readFileSync("out/VeydriftGame.sol/VeydriftGame.json", "utf8"));
  const hash = "sha256:" + crypto.createHash("sha256").update(JSON.stringify(artifact.abi)).digest("hex");
  console.log(hash);
'
```

Then compare that hash to `curl -s https://api.veydrift.com/runtime-config | jq -r .backend.build.deploymentAbiHash`
**before** copying anything into `abi/`. If they don't match: stop, do not proceed with a
mismatched ABI, and re-check which commit `/runtime-config` actually reports as
`deploymentCommit` — it may have moved since this document was written.

To actually re-pin (only after the hash matches live):

```bash
node -e '
  const fs = require("fs");
  const artifact = JSON.parse(fs.readFileSync("'"$REPO"'/packages/contracts/out/VeydriftGame.sol/VeydriftGame.json", "utf8"));
  const pinned = { abi: artifact.abi, methodIdentifiers: artifact.methodIdentifiers };
  fs.writeFileSync("abi/VeydriftGame.<short-commit>.json", JSON.stringify(pinned, null, 2) + "\n");
'
```

...and update `abi/PINNED.json`'s `commit`, `abiHash`, `fetchedAt`, and `source` fields to match.
Update the filename references in `src/abi.ts` (`ABI_DIR`/pinned-file basename) if the short-commit
suffix changes.

## The main-vs-deployed divergence (RESEARCH-ADDENDUM.md §1.1)

| Only on `main` (does **not** exist on the deployed contract) | Only on deployed (deleted on `main`) |
| --- | --- |
| `playerScore(address)` | `firstPlanetOf(address)` |
| `settleProductionUntil(uint256,uint64)` | `hasFirstPlanet(address)` |
| `settleAllianceMembershipBoundary(address)` | `previewFirstPlanet(address)` |
| `depositPaidAllianceInviteFee()` | `FLEET_RECALL_COST_BPS()` |
| `startPlanetWithAllianceInvite(bytes32,uint64,uint8,bytes32,bytes32)` | 3 × `SafeCast*` errors |
| event `AllianceBonusCreditedToPlanet(...)` | |

**`playerScore` foremost.** Prior project docs (`NOTES.md` §13.5) list `playerScore` among "useful
read functions for an agent (all public views on the game proxy)". It is **not on the deployed
implementation** — a call to it reverts. This is exactly the kind of mistake pinning the ABI to
`main` would reproduce silently: the encoder would happily build a call to a function that doesn't
exist on-chain, and you'd only find out at `eth_call` time. Use `GET /wallet/{addr}/highscore`
instead (`tests/abi.test.ts` asserts `playerScore` is absent from the pinned ABI and `firstPlanetOf`
is present, so this stays caught if the pin is ever rebuilt carelessly).

`src/abi.ts`'s `verifyAbi()` is the runtime guard: on any hash mismatch against live
`/runtime-config`, every write path must be treated as unsafe. `walletctl verify-abi` surfaces this
directly; `checkAllowlist` does not currently re-run the hash check per-transaction (it trusts the
pinned ABI file on disk for selector computation), so **run `walletctl verify-abi` before any
`send` session**, not just once at setup.

## Provenance

- ABI hash and deployment commit re-verified live against
  `https://api.veydrift.com/runtime-config` on 2026-08-12 (see `abi/PINNED.json.source`).
- Foundry settings: `packages/contracts/foundry.toml` at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`.

Verified against this skill's source repository as of 2026-08-12; that repository's own
docs carry the full derivation and the divergent-function-list detail behind the summary
above.
- `playerScore`/`firstPlanetOf` presence: independently confirmed against the pinned artifact's
  `methodIdentifiers` (138 entries) during this work package, matching the addendum exactly.
