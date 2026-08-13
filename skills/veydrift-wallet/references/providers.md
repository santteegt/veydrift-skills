# Wallet providers

## The constraint that governs every decision in this document

**A Veydrift planet is permanently bound to the EOA that settled it.** Ownership is
`_planets[planetId].owner` — a plain struct field, not a token. There is no `transferPlanet`
function anywhere in the deployed contract, and planets are not NFTs, so there is no ERC-721
`transferFrom` escape hatch either.

Abandoning doesn't help either: `abandonPlanet` explicitly reverts for a wallet's home planet.
Verified directly against the pinned deployment commit (`701bed3578cff4d134657c714c599dbdb55a4b6a`,
not `main`) via `git show <commit>:<path>`, independent of any other claim:

```solidity
// packages/contracts/src/VeydriftPlanetManagementModule.sol:146-150 (at 701bed357...)
function abandonPlanet(uint256 planetId) external {
    ...
    if (homePlanetOf[msg.sender] == planetId) revert CannotAbandonHomePlanet();
```

```solidity
// packages/contracts/src/VeydriftGameStorage.sol:432 (at 701bed357...)
error CannotAbandonHomePlanet();
```

So a home planet can be neither transferred (no function exists) nor abandoned (the call reverts).
Planet 664, the example planet in `policy.json`, is unconditionally bound to whatever EOA settled
it.

**The consequence for wallet architecture is severe and easy to miss:** any provider that issues a
*new* address cannot hold the planet the agent is meant to be operating. Moving to one means
abandoning the planet and re-settling from scratch — except abandoning isn't even available for a
home planet, so in practice it means the account is simply stuck with whatever key settled it,
forever, or starting over on a different planet entirely.

That rules out **Safe multisig, ERC-4337 smart accounts and Cobo** as this engine's provider — all
of them mint a new address as part of onboarding. It's also why **ethskills' 2-of-3 Safe
recommendation for agent wallets — sound advice in general — does not apply here**, and
`references/tx-safety.md` records that disagreement explicitly rather than quietly deviating from it.

**Correction (2026-08-12):** Coinbase CDP Server Wallets and Turnkey are
*not* ruled out on address-binding grounds. Both support importing an existing private key, and
CDP's `importAccount` is framed by its own docs as being for preserving a wallet address when
migrating providers. They are ruled out here on **open-source and self-hosting** grounds instead —
the stated aim for this project. Getting the *reason* right matters: if that aim is ever relaxed,
these become viable again, whereas Safe and ERC-4337 never do.

The two shapes that remain viable are: providers that *adopt* the
existing key (encrypted keystore, HSM/KMS import, an MPC service that supports key import), or
EIP-7702 delegation, which lets the EOA gain smart-account behavior while **keeping its address**.
Neither of the two providers implemented in this pass is EIP-7702-based — both work by holding (in
one form or another) the actual private key for the actual EOA that owns the planet. This skill's
source repository ran a full evaluation of every remaining candidate against this constraint,
including whether EIP-7702 is actually usable — that is a research deliverable, not code, and no
provider beyond the two below is implemented here.

## The two providers

Both implement the same interface (`src/providers/types.ts`):

```ts
interface WalletProvider {
  readonly name: string;
  getAddress(): Promise<`0x${string}`>;
  signAndSend(tx: UnsignedTx): Promise<`0x${string}`>;
  capabilities(): { canSign: boolean; canSimulate: boolean; remotePolicy: boolean };
}
```

Both are genuinely functional, not one real implementation and one stub — that's what actually
demonstrates the interface is swappable rather than merely declared to be. `tests/providers.test.ts`
proves it directly: constructed from the *same* throwaway test key, both providers derive the
*same* address (acceptance criterion 11).

### `keystore` — the default

An encrypted EIP-2335/geth-format JSON keystore, decrypted via `ethers.Wallet.fromEncryptedJson`
(`src/providers/keystore.ts`). Scrypt+AES decryption is not something to hand-roll — this is
exactly the one place `ethers` earns its dependency slot in a codebase that otherwise uses `viem`
for everything chain-side.

- **Path**: `VEYDRIFT_KEYSTORE` env var, pointing at the keystore JSON file.
- **Password**: `VEYDRIFT_KEYSTORE_PASSWORD` env var, or an interactive, non-echoing stdin prompt
  if that's unset. Never a CLI flag (so it can never land in argv, shell history, or `ps`), never
  logged.
- **Address without decryption**: standard geth/EIP-2335 keystores carry the address in cleartext
  at the top level (`{ address, crypto, id, version }`), so `getAddress()` reads it directly —
  `walletctl status` and `walletctl build`'s best-effort `--from` gas estimate work without ever
  prompting for a password.
- **Key material lifetime**: decrypted only inside `signAndSend`'s local scope, for the duration of
  that one call. Never assigned to `this`, never cached across calls, never logged.

### `envkey` — testing only

A raw private key from `VEYDRIFT_PRIVATE_KEY`, signed via `viem`'s `privateKeyToAccount`
(`src/providers/envkey.ts`). ethskills ranks a plaintext env-var key as testing-grade storage —
better than committing it, worse than an encrypted keystore. Two things beyond "don't commit it":

- **A loud startup warning** every time this provider is constructed, so it's never silently in
  use.
- **A leak-detection refusal**: if the skill is running from inside a discoverable git working
  tree, the provider refuses to start when the key's raw value (with/without `0x`, either hex case)
  is found in any tracked or untracked-but-not-gitignored file *outside* a `tests/` directory or
  `*.test.ts` file. The exclusion exists because this codebase's own test suite legitimately (and
  by instruction) hardcodes a well-known, clearly-marked throwaway key — Anvil/Foundry's default
  account #0 — to prove the two providers agree; without the exclusion, the safety net would
  permanently trip on its own tests. This is a best-effort defense-in-depth check, not the primary
  control (the primary control is simply never writing a key to a tracked file), and it can't catch
  every leak — once the skill is installed elsewhere via `npx skills add`, it may not be running
  inside any git repo at all, in which case the check silently no-ops.

## RPC endpoint

Every read (`getPublicClient()`) and every write (both providers' `signAndSend`) resolve
their RPC target through one chokepoint, `getRpcUrl()` (`src/tx.ts`):

```ts
export const DEFAULT_RPC_URL = "https://mainnet.base.org";
export function getRpcUrl(): string {
  return process.env.VEYDRIFT_RPC_URL?.trim() || DEFAULT_RPC_URL;
}
```

- **Default**: `https://mainnet.base.org`, Base's public RPC endpoint. Works, but is
  rate-limited and shared with everyone else hitting it — a sequence of calls in a short
  window (e.g. `status` immediately followed by `build`/`simulate`) can get throttled.
- **Override**: set `VEYDRIFT_RPC_URL` to any Base-mainnet-compatible JSON-RPC endpoint,
  for example an Alchemy app URL (`https://base-mainnet.g.alchemy.com/v2/<your-key>`).
  This applies regardless of which provider (`keystore`/`envkey`) is selected — it's
  orthogonal to signing. No code change is required; `walletctl status`'s `rpcUrl:` line
  reflects whichever endpoint is currently configured, so switching is verifiable before
  it's ever used for a `send`.
- **Not the same knob as `/runtime-config`**: `verify-abi` and `buildTx`'s destination
  address both come from a live fetch of `https://api.veydrift.com/runtime-config`
  (`abi.ts`'s `RUNTIME_CONFIG_URL`), which is a separate HTTP endpoint unrelated to
  `VEYDRIFT_RPC_URL` — changing the RPC endpoint does not change where the ABI/address
  data comes from, and vice versa.

## Selection and swap procedure

Selected by `policy.wallet_engine.provider` (a `veydrift-agent` concern),
overridable by the `WALLET_PROVIDER` env var, defaulting to `keystore`
(`src/providers/index.ts`'s `getProvider()`). To swap providers:

```bash
# keystore (default) -- no action needed beyond setting these two:
export VEYDRIFT_KEYSTORE=/path/to/keystore.json
export VEYDRIFT_KEYSTORE_PASSWORD=...   # or omit and answer the interactive prompt

# envkey (testing only)
export WALLET_PROVIDER=envkey
export VEYDRIFT_PRIVATE_KEY=0x...
```

No code change is required to swap — `walletctl status` immediately reflects whichever provider is
configured, including its resolved address, so switching providers is verifiable before it's ever
used for a `send`. Because of the address-binding constraint above, swapping providers is **only**
safe when both providers are backed by the *same* private key (e.g. moving a key from a plaintext
env var into a proper keystore file is a legitimate swap; pointing `envkey` at a freshly-generated
key that has never settled the target planet is not — it produces a wallet that can build and sign
transactions, but not for the planet you meant).

## Where the harder providers are actually evaluated

This skill's source repository ran a research pass (not code shipped in this package)
evaluating the remaining candidates against the address-binding constraint above: EIP-7702
delegation on the existing EOA, Web3Signer / HashiCorp Vault, Cobo CAW, Coinbase CDP Server
Wallets, Turnkey, and OKX OnchainOS — including which of those are open-source/self-hostable versus
hosted.

**EIP-7702 on Base is confirmed live** (verified 2026-08-12): transaction
`0xba45e2808d60302f4dbc7f63ab5d4e8cf914789eab289c358788c194d8c1d4db` in block
`49860849` on Base mainnet has `type: 0x4` with a one-entry `authorizationList`, checked directly
via `eth_getTransactionByHash` against `https://mainnet.base.org`. An earlier draft of this
project described this as inferred from the Pectra `requestsHash` block header field and flagged it
as unproven; that caveat is retired. **This package still does not rely on 7702 anywhere** — both
implemented providers hold the actual key for the actual EOA — but the delegation path is now a real
option for a future provider rather than a speculative one.
