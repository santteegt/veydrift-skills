---
name: veydrift-wallet
description: Builds, allowlists, simulates and — only on an explicit human --confirm — submits Veydrift game transactions on Base (chainId 8453). Use this whenever the user wants to actually sign or send a Veydrift action, check wallet/provider status, verify the pinned contract ABI against live, encode a fleet-mission ship tuple, or work on the keystore/envkey wallet providers, the transaction allowlist, or EIP-7702/custody questions *for this Veydrift project specifically*. Trigger on "sign this", "send the tx", "walletctl", "check my wallet balance/address", "is the ABI still pinned correctly", "build a fleet mission", or any mention of the keystore/envkey providers. Do NOT use this for generic wallet/signing questions unrelated to Veydrift (e.g. general viem/ethers usage, an unrelated dApp's wallet integration, or a different chain/project's custody design) — this skill's allowlist, ABI pin and address-binding reasoning are specific to the deployed Veydrift contract on Base. For reading game state, planning the next build/research action, or gameplay strategy, hand off to `veydrift-agent` instead; this skill never decides *what* to do, only signs and sends what it's given.
---

# veydrift-wallet

The **only** thing in this project that ever builds real calldata bytes, signs, or submits
a transaction. `veydrift-agent` (a separate skill) proposes actions as plain JSON — it
never touches `viem`/`ethers`/`web3` and never signs anything. If a question is about *what*
to build next, planet strategy, or reading game state, route to `veydrift-agent` instead.

Single CLI: `walletctl`, run via `npx tsx src/cli.ts <command>` from this skill's directory
(or the built `dist/cli.js` after `npm run build` — see `AGENTS.md` at the repo root for
the exact commands verified to work in this environment).

```
walletctl status                      # provider, address, chainId, ETH balance, ABI pin state
walletctl verify-abi                  # live deploymentAbiHash vs pinned -- exit 1 on drift
walletctl build   --action a.json     # -> unsigned {to, data, value, chainId, gas}
walletctl simulate --tx tx.json       # eth_call + estimateGas; surfaces reverts
walletctl send    --tx tx.json --confirm
walletctl receipt --hash 0x...
```

## The constraint every design decision here answers to

**Planet 664 — or any Veydrift planet — is permanently bound to the EOA that settled it.**
Ownership (`_planets[planetId].owner`) is a plain struct field, not a token; there is no
`transferPlanet` function anywhere in the deployed contract; and `abandonPlanet` reverts
with `CannotAbandonHomePlanet` for a wallet's home planet
(`VeydriftPlanetManagementModule.sol:150`, verified directly against the deployed commit —
see `skills/veydrift-agent/references/contract-writes.md` §7 for the full citation).

**Consequence:** any provider that mints a *new* address — Safe multisig, ERC-4337 smart
accounts, most hosted MPC/TEE wallets — cannot ever hold this planet. That single fact
rules out the ethskills 2-of-3-Safe recommendation for this project specifically (sound
advice in general, wrong here) and is why this skill ships exactly two providers, both of
which sign with the *existing* key rather than minting a new one. Full reasoning:
`references/providers.md` §1; the deeper research pass across every hosted/self-hosted
alternative: `docs/wallet-provider-research.md`.

## Providers

Both implement the same interface and, proven by test, derive the **same address** from
the same key material:

```ts
interface WalletProvider {
  readonly name: string;
  getAddress(): Promise<`0x${string}`>;
  signAndSend(tx: UnsignedTx): Promise<`0x${string}`>;
  capabilities(): { canSign: boolean; canSimulate: boolean; remotePolicy: boolean };
}
```

| Provider | Status | Selected by |
| --- | --- | --- |
| `keystore` | default, implemented | `VEYDRIFT_KEYSTORE` (path) + `VEYDRIFT_KEYSTORE_PASSWORD` (env) or an interactive non-echoing prompt |
| `envkey` | testing only, implemented, loud startup warning | `WALLET_PROVIDER=envkey` + `VEYDRIFT_PRIVATE_KEY` |

`policy.wallet_engine.provider` (a `veydrift-agent`/`policy.json` concern) picks the
default; `WALLET_PROVIDER` overrides it. Neither provider is EIP-7702-based — both hold
the actual private key for the actual EOA. Full detail, including the leak-detection
refusal `envkey` runs on startup: `references/providers.md`.

## The `--confirm` invariant — the property the whole tier model rests on

**No environment variable, no config field, and no other flag can make `--confirm`
implicit.** `walletctl send --tx tx.json` without `--confirm` always exits non-zero and
always prints the exact transaction it would have sent — same code path as a successful
build, short-circuited right before `provider.signAndSend`, so what's printed is what
would actually be signed, not a separate preview that could drift from reality. A fully
compromised `veydrift-agent` can construct any `Action` JSON and call `build`/`simulate`
freely; it cannot make this engine submit anything without a human (or a deliberately
scripted, explicitly flagged invocation) putting `--confirm` on that exact command line.

**No transaction has ever been submitted to Veydrift from this codebase.** The write path
is built, allowlisted, and fixture-tested — never executed against mainnet
(`docs/SPEC.md` §11). Do not describe this system as having "sent" anything; it hasn't.

## Defense in depth: the allowlist, enforced here independently of the agent skill

`checkAllowlist` re-runs unconditionally inside `send`, regardless of what already
validated the transaction upstream. Five checks, all evaluated and reported:

1. `tx.to` ∈ addresses from a **live** `/runtime-config` fetch — never hardcoded
2. `tx.data`'s 4-byte selector ∈ the tier's allowed set, **computed from the pinned ABI**
   (never a hand-typed hex constant)
3. `tx.value == 0` — no payable action is whitelisted at any tier reachable here
4. `tx.chainId == 8453` (Base)
5. `operator`-only: `launchFleetMission`'s mission-type argument (decoded from calldata,
   since it isn't part of the selector) must be Transport(0)/Deploy(1)/Harvest(4) —
   combat mission types are unreachable through this engine no matter what tier is set

Any failure: non-zero exit, the rejection reason printed, nothing signed. Full mechanics
and rationale: `references/tx-safety.md`.

## Two traps the encoder has to get right, both already built and tested

1. **The 14-slot fleet tuple.** The contract's fleet entrypoints take a fixed
   `(uint32 × 14)` tuple, but there are 16 ships — `SolarSatellite` (9) and `Crawler` (15)
   can't fly and have no slot. Every flyable ship id above 9 is shifted down one tuple
   index (a Destroyer, Ship id 10, lands at tuple index **9**, not 10).
   `shipCountsToFleetTuple()` (`src/fleet.ts`) is the one function that must do this
   conversion — never hand-index a tuple at a call site. Throws on `SolarSatellite`/
   `Crawler` input, even at count zero.
2. **`launchFleetMission` is overloaded** — both a 7-arg and a 6-arg form live on the
   deployed ABI simultaneously. Selecting by bare name is ambiguous; every resolution in
   this codebase goes through the full canonical signature.

Full detail on both, plus the six ABI-`nonpayable`-but-semantically-read functions
(`protectedResources`, `raidableResources`, `maxRaidLoot`, `debrisField`,
`collectResources`, `attackProtectionStatus` — route these through `simulate`, never
`send`): `skills/veydrift-agent/references/contract-writes.md` (the write-entrypoint
reference lives in the agent skill since it documents the contract, not the encoder;
this skill's own `references/tx-safety.md` documents what `send` refuses and why).

## ABI pinning

Every write is gated on the pinned ABI's hash matching the live backend's
`deploymentAbiHash` — **`main` is not the deployed contract** and building from it
produces a different, wrong hash. `walletctl verify-abi` is the check; run it before any
`send` session, not just once at setup (`checkAllowlist` trusts the on-disk pin per
transaction, it does not re-fetch and re-hash every time). Full rebuild recipe, the exact
pinned hash, and the `main`-vs-deployed function-list divergence (most notably
`playerScore`, which reverts on the deployed contract despite appearing in older docs as
a recommended read): `references/abi-pinning.md`.

## Routing table

| Question | Read |
| --- | --- |
| Provider selection, swap procedure, the address-binding constraint in full | `references/providers.md` |
| Exact allowlist checks, the `--confirm` invariant, what ethskills recommends that this engine consciously skips (and why) | `references/tx-safety.md` |
| ABI pin provenance, rebuild recipe, `main`-vs-deployed divergence | `references/abi-pinning.md` |
| Contract write entrypoints and every calldata-level trap (fleet tuple, overloads, fake-read functions, revert conditions) | `skills/veydrift-agent/references/contract-writes.md` |
| Every hosted/self-hosted provider evaluated beyond the two implemented here (EIP-7702, Web3Signer, Vault, Cobo, CDP, Turnkey, OKX) and why none of them is adopted yet | `docs/wallet-provider-research.md` |
| Repo map, install, key custody, what's never been verified | `AGENTS.md` (repo root) |
