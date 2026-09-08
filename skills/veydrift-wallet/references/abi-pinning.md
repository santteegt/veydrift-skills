# ABI pinning

## Why this exists

`veydrift-wallet` never trusts a freshly-`forge build`-ed ABI at runtime. It trusts only the
committed `abi/VeydriftGame.202d1ac.json`, and every write path is gated on that pinned ABI's hash
matching the live backend's `deploymentAbiHash` (`walletctl verify-abi`, and `checkAllowlist` in
`src/allowlist.ts`). This document records how the pin was produced, how to reproduce it, and why
building from `main` gives you the wrong answer.

**The contract was upgraded on-chain on 2026-09-07.** The pin was moved from commit
`701bed3578cff4d134657c714c599dbdb55a4b6a`
(abiHash `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99`) to
`202d1acd9e35d815bd66cb9bae744341b1b1cf9e`
(abiHash `sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4`), the latter
reproduced locally and matched against live `/runtime-config` exactly. All allowlisted selectors
(the economy five, both `launchFleetMission` overloads, `launchInterplanetaryMissileAttack`, the
15 alliance-membership functions) and both silent-corruption traps (the 14-slot fleet tuple, the
`launchFleetMission` overload pair) are unchanged across the upgrade. See the changelog's `1.0.0`
entry for the full diff.

**`main` is not the deployed contract.** At re-pin time (2026-09-07) `main` HEAD was
`094f22776d0ef38dd025971bf0e27fc1e0ea28ab` — which is the backend's own reported `gitSha`, but
**not** its `deploymentCommit`; building the ABI from it produces a *different, wrong* hash. The
only correct source is the specific commit the live backend reports as `deploymentCommit`. (The
prior pin recorded the same lesson against `main` HEAD `84e468f6…` on 2026-08-11.)

## The pin, as shipped

| Field | Value |
| --- | --- |
| Deployment commit | `202d1acd9e35d815bd66cb9bae744341b1b1cf9e` |
| ABI hash | `sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4` |
| Verified against live `/runtime-config` | yes, `backend.build.deploymentAbiHash` matched exactly, 2026-09-07 |
| Prior pin (pre-2026-09-07 on-chain upgrade) | commit `701bed3578cff4d134657c714c599dbdb55a4b6a`, abiHash `sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99` |

`abi/PINNED.json` records this plus the foundry settings and the full provenance chain
(local clone path, commit, artifact path, build command, and the live-verification timestamp).
`abi/VeydriftGame.202d1ac.json` holds `{ abi, methodIdentifiers }` extracted from the forge
artifact — no bytecode, no metadata, since neither is needed (or wanted) here: this engine
encodes/decodes calldata and never deploys or verifies bytecode. (The basename tracks the
short deployment commit; it was `VeydriftGame.701bed3.json` before the 2026-09-07 re-pin.)

## Hash derivation

```
sha256( JSON.stringify( artifact.abi ) )
```

Compact JSON (`JSON.stringify`'s default, no whitespace), key order exactly as forge emits it and
as `JSON.parse` preserves it — this is **not** a canonicalized/sorted-keys hash. It matches the
derivation the backend itself uses
(`scripts/veydrift-deployment-manifest.mjs:129-135` in the veydrift repo), which is why
comparing against `backend.build.deploymentAbiHash` is a
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
DEPLOY_COMMIT=202d1acd9e35d815bd66cb9bae744341b1b1cf9e   # from live /runtime-config, not memorized

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
Update the filename references in `src/abi.ts` (`ARTIFACT_FILENAMES`, both `game` and `alliance`
basenames) and `tests/abi.test.ts` (`EXPECTED_HASH`, `EXPECTED_COMMIT`, and the alliance
hash/commit) if the short-commit suffix changes.

## What the 2026-09-07 on-chain upgrade changed

Diff of the pinned game ABI, commit `701bed3` → `202d1ac` (289 → 303 ABI entries, 138 → 147
`methodIdentifiers`). **Nothing in the reachable write surface changed** — every allowlisted
selector and both silent-corruption traps are byte-identical. The changes are elsewhere:

| Added to the deployed contract | Removed from the deployed contract |
| --- | --- |
| `playerScore(address)` — see note below | `firstPlanetOf(address)` |
| `settleProductionUntil(uint256,uint64)` | `hasFirstPlanet(address)` |
| `settleAllianceMembershipBoundary(address)` | `previewFirstPlanet(address)` |
| `depositPaidAllianceInviteFee()`, `startPlanetWithAllianceInvite(...)` | `FLEET_RECALL_COST_BPS()` |
| moon-attack-parity surface (`launchBodyAttackMission`, `joinBodyAttackMission`, `resolveFleetMissionCombatRound`, `battleResolutionProgress`, `attackBodyProtectionStatus`, `moonAttackParityActivatedAt`, `initializeMoonAttackParity`) | `settleDuePlayerColonizeArrivals(address)`, `untrackResolvedFleetMission(uint256)` |
| temperature-migration surface (`migratePlanetTemperatures`, `planetTemperatureGenerationVersion`, `migratePlanetTemperatures`), `gamePaused()` | |

None of the added combat/moon functions are allowlisted — the allowlist is default-deny, so they
are unreachable through `walletctl` without an explicit source change to `src/allowlist.ts`, the
same as every other unlisted selector.

**`playerScore` reversed.** Before the upgrade it was a `main`-only function that reverted on the
deployed contract, and this doc warned against calling it. As of commit `202d1ac` it **is** on the
deployed contract. `tests/abi.test.ts` now asserts `playerScore` is present and `firstPlanetOf` is
absent — the exact opposite of the pre-upgrade assertions — so a careless rebuild against an older
commit is still caught. This codebase's own `src/` never called either function; the change is
docs-and-tests only here.

**`main` still diverges from the deployed contract.** The lesson is unchanged even though the
specific function list flipped: always build from the commit `/runtime-config` reports as
`deploymentCommit`, never from `main` (nor from its `gitSha`, which is a different thing again).

`src/abi.ts`'s `verifyAbi()` is the runtime guard: on any hash mismatch against live
`/runtime-config`, every write path must be treated as unsafe. `walletctl verify-abi` surfaces this
directly; `checkAllowlist` does not currently re-run the hash check per-transaction (it trusts the
pinned ABI file on disk for selector computation), so **run `walletctl verify-abi` before any
`send` session**, not just once at setup.

## Second contract: `VeydriftAllianceSystem`

The alliance feature added a second pinned contract — `abi/VeydriftAllianceSystem.202d1ac.json`
(artifact) + `abi/PINNED.alliance.json` (meta), same shape and same hash derivation
(`sha256(JSON.stringify(abi))`, compact separators) as the game contract's pair above. Both
built from the same local clone, same commit, same `forge build --skip test --skip script`
invocation — the alliance artifact is present in `out/VeydriftAllianceSystem.sol/` from
that same build, so no separate rebuild is needed. `src/abi.ts`'s loaders/resolvers all take an
optional `contract: "game" | "alliance" = "game"` parameter now; every pre-existing call site
(predating this feature) is unaffected by the default.

**This pin has no live-hash re-verification path, and never will.** `/runtime-config` exposes
`allianceContractAddress` directly (`0x0E5a6210482B15780cf5Ec036107031dcA702001`, unchanged across
the 2026-09-07 game-contract upgrade) but no `allianceAbiHash`/`allianceDeploymentCommit`
field anywhere — only the single `backend.build.deploymentAbiHash`/`deploymentCommit` pair,
which is for the game contract. `verifyAbi()` stays game-only, deliberately, with no
`verifyAllianceAbi()` sibling: there is nothing on the live API for it to compare against. The
alliance ABI pin is therefore verified exactly once, by construction — exact commit checkout +
exact forge settings, matching the game contract's own pinned settings from the same build — and
that is the permanent ceiling on this pin's guarantee. If the backend ever adds an equivalent
hash/commit field for the alliance contract, `verifyAbi()` should grow a real alliance-aware
counterpart at that point; until then, don't invent a substitute check that only looks like
verification.

**Re-pinned 2026-09-07 alongside the game contract.** When the game contract's `deploymentCommit`
moved to `202d1ac`, the alliance artifact was rebuilt from that same commit and re-pinned
(abiHash `sha256:3992c821…` → `sha256:393335c1…`; source added `joinFromPaidInvite` /
`redeemPaidInvite` / `paidInviteSystem` / war-protection functions, removed
`migrateLegacyWarMetadata`). This keeps both pinned artifacts from one coherent source tree. The
alliance contract's on-chain address is unchanged, so whether it was itself redeployed cannot be
confirmed from the API — but the 15 in-scope membership functions' selectors are byte-identical
between `701bed3` and `202d1ac`, so the allowlisted surface is unaffected either way.

## Provenance

- Game ABI hash and deployment commit re-verified live against
  `https://api.veydrift.com/runtime-config` on 2026-09-07 (see `abi/PINNED.json.source`).
  First pinned 2026-08-12 at commit `701bed3`; re-pinned 2026-09-07 at commit `202d1ac` after the
  on-chain contract upgrade.
- Foundry settings: `packages/contracts/foundry.toml` at commit `202d1acd9e35d815bd66cb9bae744341b1b1cf9e`
  (identical to the prior pin's: `solc 0.8.28`, `optimizer_runs 1`, `via_ir true`,
  `cbor_metadata false`, `bytecode_hash "none"`).

Verified against this skill's source repository as of 2026-09-07; that repository's own
docs carry the full derivation and the divergent-function-list detail behind the summary
above.
- `playerScore`/`firstPlanetOf` presence: independently confirmed against the pinned artifact's
  `methodIdentifiers` (147 entries at commit `202d1ac`; was 138 at `701bed3`). As of the
  2026-09-07 upgrade `playerScore` is present and `firstPlanetOf` is absent — the reverse of the
  pre-upgrade state.
