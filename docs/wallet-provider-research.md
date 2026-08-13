# Veydrift Wallet Provider Research (WP4b)

**Status:** research only — no code, no provider changes. Informs `docs/SPEC.md` §6.5.
**Date:** 2026-08-12. **Author context:** wallet `0x224aba5d489675a7bd3ce07786fada466b46fa0f`, planet `664`
at `7:181:14`, Base mainnet (`chainId 8453`). Single-planet hobby account, one operator.

This document evaluates wallet-provider options for the `veydrift-wallet` skill's provider interface
(`docs/SPEC.md` §6.3). **No provider is implemented here.** The two providers already built —
`keystore` and `envkey` — are WP4a's output and are treated below only as the baseline to compare
against.

## 0. How to read this document — provenance key

Every claim below is tagged so a future reader (and this document will be read months from now) can
tell what load-bearing weight it can carry:

| Tag | Meaning |
| --- | --- |
| **[VERIFIED — source]** | I read the primary source myself in this pass (contract source, raw HTTP response, GitHub API) and can point at the exact line/byte |
| **[READ — vendor]** | From a vendor's own docs or marketing. Directionally useful, not neutral — vendors describe their own products favorably |
| **[READ — third party]** | From independent docs, blog posts, or search-engine synthesis. More neutral than vendor copy but not independently reproduced |
| **[UNCONFIRMED]** | I looked and could not pin it down, or found conflicting signals. Listed as an open question in §7, not asserted as fact |

Where prior notes (`SPEC.md`, `RESEARCH-ADDENDUM.md`, `NOTES.md` §13) already established something, I
re-verified it against the deployed contract source rather than citing the prior note as the source.

---

## 1. The constraint that governs every provider decision

**A Veydrift planet is permanently bound to the EOA that settled it.** This was re-verified directly
against the deployed contract source in this pass, not taken on faith from `NOTES.md` §13.

**[VERIFIED — contract source]** Method: cloned repo at `/Users/santteegt/GitRepositories/clones/veydrift`,
checked out the *deployed* commit `701bed3578cff4d134657c714c599dbdb55a4b6a` (main HEAD `84e468f` has
already drifted — confirmed by `RESEARCH-ADDENDUM.md` §1.1, and this pass did not re-verify the drift
list itself). The working tree was returned to `main` afterward; nothing in the clone was left modified.

- `struct Planet` ([VeydriftGameStorage.sol:68-80](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGameStorage.sol#L68-L80)) carries `address owner` as a plain field — no
  ERC-721, no token registry. `grep -rn "IERC721\|ERC721\|_mint(" packages/contracts/src/*.sol` finds
  ERC-20 mints only ([VeydriftToken.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftToken.sol), [VeydriftResourceToken.sol](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftResourceToken.sol)); nothing mints a planet.
- `grep -lE "transferPlanet|sellPlanet|giftPlanet|setPlanetOwner"` across every `.sol` file returns
  nothing. There is no transfer function.
- `transferOwnership(address)` exists ([VeydriftGame.sol:55](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol#L55)) but is `onlyOwner` — the **game contract
  admin**, unrelated to any specific planet. An ordinary player cannot call it.
- `abandonPlanet(uint256)` ([VeydriftPlanetManagementModule.sol:146-206](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftPlanetManagementModule.sol#L146-L206)) ends in
  `delete _planets[planetId]` — destruction, not transfer. It also **reverts with
  `CannotAbandonHomePlanet`** if `homePlanetOf[msg.sender] == planetId` (line 150).

That last point is a detail beyond what `NOTES.md` §13 recorded, and it matters here: `NOTES.md` §12.6
confirms planet 664 is this wallet's `homePlanetId` — its only planet. **For a single-planet account,
the planet cannot even be abandoned.** The only two things that can happen to it are: it stays bound to
this EOA forever, or the private key controlling this EOA changes custody (still the same address,
still the same planet).

- `_requirePlanetOwner` ([VeydriftGame.sol:793-796](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol#L793-L796)) is a plain `planetRef.owner != msg.sender` check —
  ordinary `msg.sender` authorization, not `tx.origin`. This is relevant to §2: it means any code path
  that ends in *this exact address* calling `VeydriftGame` — whether a raw signed transaction or an
  EIP-7702-delegated account's own internal `call()` — presents the same `msg.sender` and passes this
  check. Nothing here special-cases delegated-code accounts one way or the other; ownership is address
  identity, full stop.

**Consequence, stated plainly:**

> **Any provider that issues a NEW address cannot hold planet 664.** Safe multisig, ERC-4337 smart
> accounts, Cobo, Coinbase CDP Server Wallets and Turnkey-generated wallets all mint a new address by
> default. Adopting one as a wallet *replacement* means abandoning the planet and re-settling from
> scratch — except abandonment itself is blocked while it's the home planet, so in practice it means
> **losing this planet outright**, not even trading it away cleanly.

That collapses the option space to two shapes, evaluated below:

1. **Providers that adopt the existing key** — encrypted keystore, HSM/KMS import, or an MPC service
   that supports genuine key import (§3.1, §3.5, §3.6, and the import-capable hosted services in §3.7–§3.9).
2. **EIP-7702 delegation on the existing EOA** — the address never changes; only the code that runs
   when something calls it changes (§2, §3.2–§3.4).

**A nuance this pass surfaced that the prior framing didn't separate out:** these two shapes are not
mutually exclusive alternatives to pick between — they *compose*. EIP-7702 delegation doesn't replace
key custody; it adds capabilities (session keys, spending caps, batching) on top of whatever already
holds the signing key. A keystore-held key can sign a 7702 authorization exactly as well as it signs a
plain transaction today. This reframes §3.2–§3.4 below: they are not competitors to the keystore
baseline, they are optional additions to it.

---

## 2. EIP-7702 on Base — confirmed live, not just inferred

`docs/SPEC.md` §6.1 states Base's post-Pectra status is "evidence, not proof" from a block header field
(`requestsHash`, client `reth/base v1.1.1`) and says a landed 7702 transaction is what would actually
confirm it. **This pass obtained that confirmation directly.**

**[VERIFIED — direct HTTP fetch]** Method: `curl` (not the LLM-summarizing WebFetch tool, to avoid
trusting a model's paraphrase of a load-bearing claim) against `https://basescan.org/txnAuthList` on
2026-08-12. The raw HTML contains:

- The literal string `10,000,000 EIP-7702 authorizations` (page caps display at the most recent 10,000
  of that total).
- Real transaction hashes (e.g. `/tx/0xba45e2808d60302f4dbc7f63ab5d4e8cf914789eab289c358788c194d8c1d4db`)
  and real delegate/delegator address pairs, timestamped `2026-08-12`.
- Recognizable delegate contracts in the listing, including labels for **"MetaMask: EIP-7702
  Delegator"** and **"Bitget Wallet: 7702 Logic v0.0.3"** — i.e. mainstream wallet software is actively
  using this on Base, not just test transactions.

I then fetched that specific transaction's detail page directly and confirmed in the raw HTML:

```
EIP-7702: 0x79709e7D...35Fa131E1 Delegate to 0xA09dE8ad...0d1E67Ab1 | Success | Aug-12-2026
Txn Type: EIP-7702
```

**This is a landed, successful, type-0x04 set-code transaction on Base mainnet, independently verified
today.** It upgrades the prior "evidence, not proof" framing to "confirmed live" for EIP-7702 as a Base
mainnet mechanism in general.

**What this does *not* confirm**, and remains an open question (§7):

- That any specific delegate contract this document discusses (CoinbaseSmartWallet, Safe's SafeLite,
  or a hand-rolled minimal delegate) works correctly when exercised — third-party transactions
  succeeding proves the *mechanism* works on Base, not that a *specific implementation* is safe to
  point planet 664's key at.
- That this project's own tooling (viem, in the `veydrift-wallet` skill) can construct and submit a
  type-0x04 transaction correctly. No transaction of any type has been submitted from this codebase yet
  (`SPEC.md` §11).
- Recommendation for whoever implements this: before relying on 7702 for planet 664, do one throwaway-EOA
  dry run end to end (build → sign → send → confirm the delegation on Base) using the exact library this
  project will use, and treat that as the actual proof-of-capability, not this document's confirmation
  that *Base as a chain* supports it.

---

## 3. Candidate evaluation

Each candidate is scored against: preserves the planet-owning address (gating), open source, free tier,
self-hostable, key-import support, policy enforcement, Base support, operational burden for a
single-planet hobby account run by one person.

### 3.1 Encrypted keystore — implemented baseline

**[VERIFIED — SPEC.md §6.3, cross-checked against WP4a's stated design]**

EIP-2335/geth-style encrypted JSON, decrypted via `ethers.Wallet.fromEncryptedJson`, held in memory
only for the signing call. This is what's actually running today.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Yes** — trivially; it *is* the existing key, unmodified |
| Open source | Yes (`ethers` v6, MIT) |
| Free tier | N/A — no service, no tier |
| Self-hostable | Yes — it's a local file, nothing to host |
| Key import | N/A — never left local custody |
| Policy enforcement | None built into the provider itself; enforced one layer up, in the
  `veydrift-wallet` allowlist (`SPEC.md` §6.4) and `veydrift-agent`'s guard gates (§5.5). This is a
  reasonable division of labor, not a gap — the provider's job is signing, not policy |
| Base support | N/A — chain-agnostic; any EVM chain viem supports |
| Operational burden | Lowest of anything evaluated: one encrypted file, one password, no network
  dependency, no account, no vendor relationship |

**Verdict: still the correct default for this account's scale.** Nothing evaluated below beats it on
cost, dependency count, or address-preservation certainty. Its weaknesses (a single machine holding a
decrypted key in memory during signing, no remote-attestation, no multi-party approval) are real but
matter far more at higher value or multi-operator scale than a single hobby planet.

### 3.2 EIP-7702 delegation — bare / self-authored minimal delegate

The floor case: the existing EOA signs a 7702 authorization pointing at *some* delegate contract, with
no specific vendor involved.

**[VERIFIED — mechanism, via §2]** the chain-level mechanism works. **[UNCONFIRMED]** what delegate
contract to point at, since writing one is itself a security-sensitive smart-contract task this
document is explicitly not scoped to do (`SPEC.md` §6.5: "No provider is implemented in this pass").

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Yes, by construction** — this is the entire mechanism (§1's `_requirePlanetOwner`
  check confirms VeydriftGame doesn't care how the calling address arrived at its code, only that
  `msg.sender` matches) |
| Open source | Depends entirely on which delegate contract is chosen |
| Free tier | N/A — only gas cost, no vendor |
| Self-hostable | Yes — no third party required to *use* 7702, only to have *written* the delegate |
| Key import | N/A — same key, same custody, unchanged |
| Policy enforcement | Only what the delegate contract implements. A minimal/no-op delegate implements
  none |
| Base support | **Confirmed live** (§2) |
| Operational burden | Low to use once deployed; **non-trivial to build safely** — a naively written
  delegate is a well-documented footgun (front-running during initialization, storage collisions with
  the EOA's own state before delegation). This is exactly why §3.3 and §3.4 exist: don't write this from
  scratch |

**Verdict: don't do this without an audited delegate.** Use §3.3 or §3.4's existing contracts instead.

### 3.3 EIP-7702 + Base's `EIP7702Proxy` → CoinbaseSmartWallet

**[VERIFIED — GitHub API + raw README, `base/eip-7702-proxy`, fetched 2026-08-12]**

Base's own open-source pattern for exactly this transition. An `EIP7702Proxy` (ERC-1967-compliant) is
what the EOA delegates to; the proxy then points at `CoinbaseSmartWallet`'s implementation. The design
explicitly targets the two known 7702 delegation footguns: atomic implementation-setting +
initialization (prevents front-running the setup step) and an external `NonceTracker` (prevents replay).

- **License:** MIT. Repo: `github.com/base/eip-7702-proxy`, `pushed_at` confirms active maintenance.
- **Audited:** three rounds by Cantina/Spearbit — first private audit 2025-02-03, second 2025-03-05, a
  **public competition** 2025-04-13 (reports committed in-repo under `audits/`). `coinbase/smart-wallet`
  itself (the implementation contract) carries a separate Cantina audit dated April 2024, plus a
  Code4rena contest in March 2024. This is materially more scrutiny than any other delegate contract
  found in this pass.
- **Deployed on Base mainnet at a fixed address** via deterministic CREATE2:
  `EIP7702Proxy: 0x7702cb554e6bFb442cb743A7dF23154544a7176C` (verified on Basescan per the repo's own
  release notes, v1.0.0, published 2025-05-08).
- **Does not require Coinbase's hosted CDP service to use.** The proxy and implementation are ordinary
  deployed contracts; a signed 7702 authorization pointing at them is a normal transaction. CDP's
  hosted infrastructure (bundlers, paymasters) is what you'd need for gas *sponsorship* or third-party
  relaying — not for basic self-submitted signed transactions.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Yes** — same mechanism as §3.2, but pointed at an audited implementation |
| Open source | Yes, MIT, both the proxy and the wallet implementation |
| Free tier | N/A — self-operated, gas cost only |
| Self-hostable | Yes in the sense that matters here — you don't depend on a vendor's servers to
  transact. (You could optionally use CDP's bundler for sponsorship; not required) |
| Key import | N/A — the underlying EOA key custody is whatever you already chose (§3.1) |
| Policy enforcement | `CoinbaseSmartWallet` supports multi-owner and session-key-style patterns; the
  degree of spend-cap/allowlist policy available depends on which owner/session-key module is
  configured — **not independently verified in this pass exactly which policy primitives ship by
  default vs require custom module code** (§7) |
| Base support | **Confirmed** — deployed on Base mainnet today, addresses given above |
| Operational burden | Moderate: one extra signed transaction (the delegation itself) plus whatever
  ongoing key management the smart-account layer requires (adding/removing owners, session keys). Not
  zero, but bounded and one-time-ish for the delegation step |

**Verdict: the most credible "keep the address, gain smart-account features" path found in this
research pass.** It satisfies open-source and preserves-address simultaneously, which nothing hosted
does. It does **not** satisfy free/self-hosted-*infrastructure* in the sense of "nothing to trust" — you
are trusting Base's and Coinbase's audited-but-not-infallible contract code, same as any smart-contract
integration. That is a materially smaller trust surface than a hosted custodian, but it is not zero.

### 3.4 EIP-7702 + Safe `SafeLite`

**[VERIFIED — Safe docs, fetched 2026-08-12, cross-checked against `5afe/safe-eip7702` GitHub metadata]**

Safe's own answer to the same problem: a lite version of the Safe contract compatible with 7702
delegation (no proxy, no initialization step, unlike a traditional Safe deployment). This is the
technical mechanism that could reconcile the ethskills 2-of-3-Safe recommendation (`SPEC.md` §6.1) with
address preservation — if it were production-ready.

**It is explicitly not.** Safe's own docs state: *"All the above approaches are experimental and the
contracts are not yet audited. Use them at your own risk."* The docs also state existing (regular) Safe
contracts **cannot** be used as a 7702 delegate target at all — delegating to the standard Safe
Singleton or Proxy exposes the EOA to front-running risk during setup, which is exactly the class of bug
§3.3's `EIP7702Proxy` was built to close. `SafeLite` is also explicitly incompatible with `Safe{Wallet}`
(the standard Safe UI/tooling) due to a different storage layout, and drops standard Safe features
(Modules, Fallback Handler, Guards).

Repo `5afe/safe-eip7702`: LGPL-3.0, last pushed 2025-01-06 — over a year stale as of this writing, which
is consistent with "experimental, not actively hardened" rather than "shipped and maintained."

| Criterion | Verdict |
| --- | --- |
| Preserves address | Yes, by the same 7702 mechanism |
| Open source | Yes, LGPL-3.0 |
| Free tier | N/A |
| Self-hostable | Architecturally yes; practically, nothing to self-host since it's a contract, not a
  service |
| Key import | N/A |
| Policy enforcement | SafeLite explicitly **drops** Modules and Guards — the parts of Safe that would
  carry policy logic — to fit the 7702 constraints. What remains is closer to bare multisig than the
  full Safe policy surface |
| Base support | **Unconfirmed** — no chain list found in the fetched docs; not stated to include Base |
| Operational burden | High relative to its payoff right now: it's an unaudited, feature-reduced,
  apparently-stalled variant of a product whose flagship version explicitly can't be used here |

**Verdict: not usable today.** Recorded because the ethskills 2-of-3-Safe pattern is a named point of
comparison in `SPEC.md` §6.1, and this is the closest thing to "Safe, but 7702-compatible" that exists —
worth revisiting if it graduates out of experimental status, not worth building on now.

### 3.5 Web3Signer

**[VERIFIED/READ — mixed: license and activity via GitHub API, capabilities via ConsenSys docs, fetched
2026-08-12]**

Open-source (Apache 2.0) remote signing daemon from ConsenSys, written in Java. Actively maintained
(`pushed_at: 2026-08-11` at the time of this check — the day before this research). Originally built for
consensus-layer (BLS) validator signing; **also supports execution-layer secp256k1 signing** for
ordinary Ethereum accounts, which is the relevant mode here. `EthSigner` (the older, execution-layer-only
predecessor) was archived in 2024 and folded into Web3Signer as a single product.

- Loads keys from **encrypted V3 keystores on disk** — the same on-disk keystore format family the
  `keystore` provider already uses (`ethers.Wallet.fromEncryptedJson` reads/writes the same EIP-2335-ish
  V3 JSON format). Migrating from the current `keystore` provider to Web3Signer would not require
  re-keying — the existing keystore file is plausibly loadable as-is, though this specific
  file-compatibility claim was not tested end-to-end against this project's actual keystore file
  **[UNCONFIRMED]**.
- Also supports key material in Azure Key Vault, HashiCorp Vault, and AWS KMS/Secrets Manager — i.e. it
  is both a standalone self-hosted option and a front-end for the HSM/KMS-import shape named in
  `SPEC.md` §6.5.
- **No built-in transaction-level policy engine was found in the fetched docs** — no destination
  allowlist, no function-selector restriction, no spend cap. It's a signing boundary (network-isolate the
  key, expose only a signing API), not a policy engine. That's not a defect relative to its actual
  purpose, but it means adopting it would not remove the need for the `veydrift-wallet` allowlist
  (`SPEC.md` §6.4) — Web3Signer would sit *behind* that allowlist, not replace it.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Yes** — it signs with an existing key; the address is whatever the imported key
  material produces |
| Open source | Yes, Apache 2.0, actively maintained |
| Free tier | N/A — self-run software, no vendor tier |
| Self-hostable | Yes — that's its entire purpose |
| Key import | Yes — file-based (V3 keystore), or via a supported vault/HSM backend |
| Policy enforcement | None built in beyond key isolation; would need the existing allowlist layered on
  top regardless |
| Base support | Chain-agnostic — signs secp256k1, same as any EOA; Base support is really "does the
  caller's RPC/broadcast layer support Base," which it already does via this project's own viem code |
| Operational burden | **Meaningfully higher than the keystore baseline for no proportionate gain at
  this scale**: an always-on Java process, TLS mutual-auth setup between the agent and the signer, and
  an extra network hop for every signature — for a single key, single operator, single planet. This
  pays for itself when you have many keys/many callers/need an audit trail of *who* requested a
  signature across a team. None of that applies here yet |

**Verdict: a real, credible, fully-open-source-and-self-hosted option that satisfies the address
constraint — but disproportionate to this account's current scale.** Worth reconsidering only if the
account grows into multi-signer or multi-service territory.

### 3.6 HashiCorp Vault + `kaleido-io/vault-plugin-secrets-ethsign`

**[VERIFIED — GitHub API for license/activity, `pkg.go.dev` + vendor docs for capability, fetched
2026-08-12]**

A Vault secrets-engine plugin (from Kaleido) that turns Vault into a software HSM for secp256k1 signing.
Apache 2.0, open source. Supports importing an existing private key ("passed in as a hexadecimal string
without the `0x` prefix") — a direct, address-preserving import path, no different in principle from the
keystore baseline except the key lives behind Vault's ACL/audit-log system instead of a flat file.

**Maintenance flag:** `pushed_at: 2023-04-03` — **over three years stale** as of this research date
(2026-08-12), not archived but showing no recent activity. This is a real risk for anything holding key
material: an unmaintained signing plugin is a liability, not a convenience, however sound the code was
when written.

| Criterion | Verdict |
| --- | --- |
| Preserves address | Yes — direct raw-key import |
| Open source | Yes, Apache 2.0 |
| Free tier | N/A — Vault itself has a free/OSS edition; this plugin runs inside it |
| Self-hostable | Yes |
| Key import | Yes, explicitly documented |
| Policy enforcement | Vault's own ACL/policy system governs *who can ask Vault to sign*, but nothing
  here inspects transaction contents (destination, selector, value) the way the existing allowlist does |
| Base support | Chain-agnostic, same reasoning as §3.5 |
| Operational burden | Higher than the keystore baseline (a Vault server to run and secure) for a
  benefit — Vault's broader secrets-management ecosystem — that mostly matters if this account is one
  secret among many Vault already manages. As a dedicated deployment just for this one key, it's
  disproportionate; as an addition to an *already-running* Vault instance, cheap |

**Verdict: technically sound and address-preserving, but stale enough to require re-auditing the plugin
source before trusting it with a real key, and disproportionate infrastructure for one key unless Vault
is already part of this operator's stack for other reasons.**

### 3.7 Cobo CAW (Cobo Agentic Wallet)

**[READ — mixed vendor/third-party, fetched 2026-08-12; genuinely could not fully resolve the
address-preservation question — see below]**

Cobo's MPC-based wallet product aimed at AI agents. **[READ — vendor]** Cobo's own description: "wallets
secured by MPC threshold signatures: the user's key share is held by them, Cobo's share is used only for
liveness and policy enforcement, and neither party can sign alone." This is genuine MPC — a real
threshold scheme, not TEE-based single-key custody (see §3.8, §3.9 for the distinction, which matters
because the prior framing in `SPEC.md`/`RESEARCH-ADDENDUM.md` groups Cobo, CDP, and Turnkey together
loosely as "MPC/AA" — only Cobo, of the three, is actually MPC by this research).

**On key import specifically:** general industry material on MPC key import (**[READ — third
party]**, a Sodot engineering blog post surfaced independently, not Cobo's own) explains the structural
tension: *"Importing a full private key breaks the 'no single point of failure' promise of MPC... if the
private key existed as a whole at any given point in time, 'splitting' it into several key shares within
the MPC wallet does not provide the same security guarantee."* Whether Cobo's specific CAW product
exposes a key-import path that preserves the original address was **not confirmed** in this pass — Cobo's
documentation describes MPC key generation and disaster recovery (reconstructing a key *out of* Cobo's
MPC scheme) but no clearly documented *import-in* flow was found. **[UNCONFIRMED]**.

That uncertainty is secondary to a harder blocker, already established and reconfirmed here:

**[READ — vendor, but the negative claim ("not open source, not self-hostable") is the kind of claim
vendor silence corroborates rather than vendor marketing asserts]** Cobo CAW is a hosted product — "MPC
mode is now live... custodial mode... set to launch in the future," invite-code gated. Its **SDKs** are
open source (LangChain/OpenAI Agents/Claude MCP/CrewAI integrations), but the **wallet infrastructure
itself** — the MPC nodes, the policy engine, the liveness service — is Cobo's hosted service. There is no
self-hosted deployment path in anything found.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Unconfirmed** for imported keys; **no** for freshly generated CAW wallets |
| Open source | SDKs only, not the wallet infrastructure |
| Free tier | Invite-code gated at time of research; pricing not found |
| Self-hostable | **No** |
| Key import | Structurally possible for MPC in general, but not confirmed as a CAW feature |
| Policy enforcement | Yes — this is CAW's actual strength, described as a first-class feature |
| Base support | Claimed "80+ blockchains" **[READ — vendor]**; Base specifically not confirmed by name |
| Operational burden | Low *if* it fit the constraints — hosted services usually are lower-burden
  day-to-day. Moot given the disqualifiers above |

**Verdict: disqualified on open-source/self-hosted grounds regardless of how the address-preservation
question resolves.** Directly conflicts with the user's stated aim of an open-source, self-hosted, free
option, exactly as `SPEC.md` §6.5 anticipated.

### 3.8 Coinbase CDP Server Wallets v2

**[VERIFIED — CDP docs, fetched 2026-08-12]**

**A correction to the prior framing worth stating precisely:** CDP Server Wallets are **not MPC**.
They're single-key custody inside an **AWS Nitro Enclave (TEE)** — "private keys are generated,
encrypted, and used for signing [inside the enclave], and the unencrypted key is never exposed — not
even to Coinbase." That's architecturally the same custody shape as Turnkey (§3.9) and OKX (§3.10):
TEE-held single key, not threshold MPC. Grouping all three with Cobo as "MPC" (as the pre-existing
framing in `SPEC.md`/`RESEARCH-ADDENDUM.md` loosely does) is not accurate; only Cobo is genuinely MPC
among the four hosted candidates evaluated here.

**A second correction:** CDP Server Wallets v2 **does support key import that preserves the original
address.** `importAccount` accepts an existing private key, and CDP's own docs frame this explicitly as
for "migrating users from another wallet provider and you want to preserve their wallet addresses." Only
individual private-key import is supported (not raw HD seeds — each key must be derived and imported
one at a time), and import/export happens end-to-end encrypted directly into/out of the TEE.

So, contrary to the blanket "mints a new address" framing in `SPEC.md` §6.1: **if the operator were
willing to import planet 664's raw private key into CDP, the resulting CDP-managed account would be the
same address, and would still own the planet.** This does not change the recommendation (see below) —
it changes *why* CDP is disqualified.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **Yes, via import** — the blanket "mints a new address" claim is only true for
  freshly *generated* CDP accounts, not imported ones |
| Open source | No — CDP Server Wallets is Coinbase's hosted product; SDKs are open source, the wallet
  service is not |
| Free tier | Has a free tier for development; production pricing not evaluated in depth here |
| Self-hostable | **No** — inherently tied to Coinbase's AWS Nitro Enclave fleet |
| Key import | **Yes, confirmed, address-preserving** |
| Policy enforcement | Yes — CDP advertises "advanced policy controls" as a core feature |
| Base support | Yes — Base is a first-class CDP/Coinbase chain |
| Operational burden | Low day-to-day (hosted), but requires trusting Coinbase's enclave attestation
  and continued product availability indefinitely — a different kind of burden (counterparty risk, not
  operational effort) |

**Verdict: disqualified on open-source/self-hosted grounds, not on address-preservation grounds.**
Importing the actual planet-owning key into a third party's enclave is also its own trust decision this
document is not making on the operator's behalf — it trades local custody for Coinbase's attestation and
policy engine.

### 3.9 Turnkey

**[READ — mixed vendor/third-party, fetched 2026-08-12]**

Same TEE shape as CDP: **[READ — vendor]** "private keys are generated and stored inside AWS Nitro
Enclaves," not MPC. **[READ — third party, independent of Turnkey's own site]** explicitly states
"Turnkey is not self-hostable as it uses proprietary TEE infrastructure on AWS" — the same third-party
source names Openfort's "Opensigner" as an open-source, self-hostable alternative for anyone who needs
that property, which is a useful pointer but out of scope to evaluate further here.

Turnkey **does document key/wallet import** ("import existing wallets using a 12-24 word seed phrase or
a private key in hexadecimal... Ethereum-compatible"). Whether the imported material lands as a
plain single-key TEE-held account (address-preserving, like CDP's) or gets re-derived through Turnkey's
own HD scheme (which could change the address) **was not confirmed at the implementation-detail level**
in this pass — the fetched import docs describe the UI flow (`handleImportWallet`, a secure iframe) but
not the underlying key-material handling in enough depth to state a verdict with confidence.
**[UNCONFIRMED]**.

| Criterion | Verdict |
| --- | --- |
| Preserves address | Plausible (raw private-key import is explicitly supported) but **not confirmed**
  at the mechanism level |
| Open source | SDKs only ("open-source crypto wallet SDKs, REST APIs... in TypeScript, Swift, Kotlin,
  Python, Go, Ruby"); the signing infrastructure is not |
| Free tier | Has one; not evaluated in depth |
| Self-hostable | **No**, confirmed by a third-party source independent of Turnkey's own marketing |
| Key import | Documented, mechanism details unconfirmed |
| Policy enforcement | Yes — "advanced policy engine," a stated core feature |
| Base support | Turnkey is chain-agnostic for secp256k1 signing; Base specifically not confirmed |
| Operational burden | Low day-to-day if usable; moot given the self-hosting disqualifier |

**Verdict: disqualified on open-source/self-hosted grounds, same as CDP.** This was already the
project's stated position (`SPEC.md` v2 dropped Turnkey from implementation entirely) and this pass
found nothing to reverse that; it did find that the "mints a new address" claim needs the same caveat
as CDP's — it may not universally hold for imported keys, but that's moot given the disqualifier.

### 3.10 OKX OnchainOS Agentic Wallet

**[VERIFIED — the actual skill source from `okx/onchainos-skills`, fetched 2026-08-12 — the strongest
evidence in this section, since it's the shipped product description rather than marketing copy]**

Read the real `SKILL.md` and reference docs from `github.com/okx/onchainos-skills`, not just search
summaries. Two lines settle this candidate:

```
TEE signing: the private key is generated and stored inside a server-side secure enclave
and never leaves the TEE — the Agent cannot export or locally sign with it.
```

and, from the account-FAQ routing row, the only import/export-adjacent flow mentioned is triggered by
*"export mnemonic, migrate, import to hardware wallet"* — i.e. **exporting out** of OKX to other
custody, with **no import-in path** documented anywhere in the skill's own routing table or reference
files. Login is via social/email OAuth, which mints a wallet per account, not per imported key.

| Criterion | Verdict |
| --- | --- |
| Preserves address | **No** — no import path found; new account, new address |
| Open source | The `onchainos-skills` CLI wrapper is open source (MIT-style skill repo); the wallet/TEE
  backend is not |
| Free tier | Not evaluated in depth; product is broadly available |
| Self-hostable | **No** |
| Key import | **Not found** in the shipped skill's own documentation |
| Policy enforcement | Yes — spending limits, whitelists mentioned in the skill's routing table |
| Base support | **Not confirmed** — the fetched chain-support reference returned 404; OKX's marketing
  cites "nearly 20 chains" without naming Base explicitly in anything fetched here |
| Operational burden | Low day-to-day if usable; moot |

**Verdict: disqualified on both address-preservation and open-source/self-hosted grounds** — the
clearest double-disqualification of anything evaluated.

---

## 4. Skills.sh survey — prior art

Ran `npx skills search` for `wallet`, `7702`, `signer`, `MPC custody`, `keystore private key`, and
`ethereum-wingman`, then read the actual `SKILL.md`/reference source (not just search snippets) for the
candidates named in `SPEC.md` §6.5 plus what turned up alongside them.

**[VERIFIED — read shipped source directly]**

| Skill | What it actually is | Relevant to this decision? |
| --- | --- | --- |
| `coinbase/agentic-wallet-skills@agentic-wallet` | A thin CLI router over the `awal` npm package. Auth is **email OTP sign-in**, which provisions a Coinbase-hosted wallet — no key-import flow anywhere in the skill. Confirms §3.8's product is what backs this, in its "mint a new hosted wallet" mode, not its import mode | Confirms: agentic-wallet skills of this shape are thin wrappers over a hosted, address-minting custodial service |
| `okx/onchainos-skills@okx-agentic-wallet` | Same shape: a router CLI (`onchainos`) over OKX's hosted TEE wallet, social/email login, explicit "agent cannot locally sign" statement (quoted in §3.10) | Same conclusion, independently confirmed for a second vendor |
| `starchild-ai-agent/official-skills@wallet-policy` | Not a wallet at all — it's a **policy-JSON generator** for **Privy** (a third hosted wallet-as-a-service provider not otherwise named in `SPEC.md`'s candidate list). Converts natural language into Privy policy rules and calls `wallet_propose_policy` | A third hosted-provider dependency surfaced by the survey, not evaluated in depth in §3 since it wasn't a named candidate, but it fits the same pattern: hosted backend, not self-hosted |
| `austintgriffith/ethereum-wingman@ethereum-wingman` | A Scaffold-ETH-based **local development framework** (React hooks, contract config, `useTransactor`) — general dApp-building guidance, not a wallet-custody product at all | Not actually comparable to the others; included here only because `SPEC.md` §6.5 named it as "already found" |
| `paulrberg/agent-skills@cli-cast` | A **local, Foundry-`cast`-based** CLI pattern: explicitly separates read / prepare / simulate / sign / broadcast so "no state-changing action is hidden inside command construction," resolves RPC endpoints itself, no hosted wallet backend | The one candidate in this survey whose architecture resembles what `veydrift-wallet` already does (`walletctl build/simulate/send`, `SPEC.md` §6.2) — a validating precedent, not a new dependency |

**The pattern across the three "agentic wallet" skills that are actual wallet products (Coinbase, OKX,
and Privy-via-Starchild) is consistent and, given the stated open-source aim, disqualifying by
construction**: each is a CLI shim whose entire value is calling a hosted, account-per-signup, address-
minting backend. None of the three skills themselves do local signing, and none document a key-import
path. This is a structural mismatch with "open source, self-hosted," independent of and in addition to
the planet-binding problem in §1 — even setting aside whether they can hold planet 664 at all, adopting
one of these skills means adopting the hosted backend behind it, which was already ruled out in §3.7–3.10
on other grounds.

---

## 5. Comparison table

| Provider | Preserves address | Open source | Self-hostable | Free tier | Key import | Policy engine | Base support | Burden (this account) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Encrypted keystore (baseline) | Yes | Yes | Yes | N/A | N/A | External (allowlist) | Chain-agnostic | Lowest |
| EIP-7702, bare/no delegate | Yes | Depends | Yes | N/A | N/A | None by default | Confirmed live | Low to use, risky to build |
| EIP-7702 + Base `EIP7702Proxy`→CoinbaseSmartWallet | Yes | Yes (MIT, audited×3) | Yes* | N/A | N/A | Partial, unconfirmed extent | Confirmed live, deployed | Moderate |
| EIP-7702 + Safe `SafeLite` | Yes | Yes (LGPL-3.0) | Yes* | N/A | N/A | Reduced vs. full Safe | Unconfirmed | High (unaudited, stalled) |
| Web3Signer | Yes | Yes (Apache-2.0) | Yes | N/A | Yes (V3 keystore, HSM/vault) | None built-in | Chain-agnostic | Disproportionate at this scale |
| Vault + `ethsign` plugin | Yes | Yes (Apache-2.0) | Yes | N/A | Yes (raw key) | Vault ACL only | Chain-agnostic | Disproportionate; plugin stale since 2023 |
| Cobo CAW | Unconfirmed | SDKs only | **No** | Invite-gated | Structurally possible, unconfirmed for CAW | Yes | Unconfirmed | Moot — disqualified |
| Coinbase CDP Server Wallets v2 | **Yes, via import** | No | **No** | Yes (dev) | **Yes, confirmed** | Yes | Yes | Moot — disqualified |
| Turnkey | Plausible, unconfirmed | SDKs only | **No** | Yes | Documented, mechanism unconfirmed | Yes | Unconfirmed | Moot — disqualified |
| OKX OnchainOS Agentic Wallet | **No** | Partial (CLI only) | **No** | Unclear | **Not found** | Yes | Unconfirmed | Moot — double-disqualified |

\* "Self-hostable" for the 7702-delegate-contract rows means *no vendor server is required to transact*
— the delegate is a deployed, permissionless contract, not a hosted API. It does not mean there is
nothing to self-host, since there's nothing running that you *would* host; the trust surface is the
contract's own code plus its audit history, not a service uptime commitment.

---

## 6. Recommendation

**No candidate satisfies "open source + free + self-hosted + policy-enforced + preserves the planet's
address" all at once, better than what's already running.** Saying that plainly is more useful than
forcing a pick that doesn't actually improve on the baseline.

**Keep the encrypted keystore as the sole provider for now.** It is the only option in this survey that
is simultaneously free, fully open source, fully self-hosted, and preserves the address with zero
ambiguity — every other candidate that also preserves the address (§3.2–§3.6) adds real operational
weight (an always-on signing daemon, a Vault deployment, a smart-contract delegation with its own trust
surface) for policy or remote-signing benefits this single-planet, single-operator account doesn't need
yet. The policy gap is already covered by the allowlist and guard layers built in WP4a/WP3, which is the
right place for policy to live regardless of which provider eventually signs.

**The one direction worth prototyping, not adopting yet:** EIP-7702 delegation to Base's audited,
open-source `EIP7702Proxy` → `CoinbaseSmartWallet` path (§3.3). It's the only mechanism found in this
research that adds real smart-account capability (session keys, spending caps, batched calls — the exact
things `SPEC.md` §6.1 wanted from a Safe-like setup) **without** requiring a new address, a hosted
custodian, or an unaudited contract. Because it composes with the existing keystore (§1's compositional
point), adopting it later would not mean replacing the current provider — it would mean the keystore
provider signs one additional transaction type (a 7702 authorization) and gains an optional execution
path through the delegated code. That's a much smaller step than switching providers outright, and it's
the one path in this entire survey that plausibly gets closer to "agentic, policy-enforced, still
self-hosted" over time.

**Explicitly not recommended, regardless of future re-evaluation of their address-preservation
mechanics:** Cobo, Coinbase CDP, Turnkey, OKX OnchainOS. All four are hosted services whose core
infrastructure the operator cannot run themselves. That conflicts with the stated aim directly, and no
amount of policy-engine sophistication changes that.

---

## 7. Open questions blocking a firmer recommendation

1. **Whether `CoinbaseSmartWallet.execute()` accepts a plain self-submitted transaction (the delegated
   EOA calling its own address) without requiring the ERC-4337 `EntryPoint`/bundler path.** This
   document reasons from `_requirePlanetOwner`'s plain `msg.sender` check (§1) that a 7702-delegated
   EOA calling `VeydriftGame` through its own code should present the same `msg.sender` as a raw
   transaction — that much follows from how EIP-7702 is specified. What's *not* verified is whether
   `CoinbaseSmartWallet`'s own authorization logic for its `execute()` entrypoint permits a bare
   self-call path at all, or whether it's designed assuming ERC-4337 UserOperations. Needs a direct
   read of `coinbase/smart-wallet`'s source, not inferred from this pass's docs research.
2. **No transaction of any type — let alone a 7702 authorization — has been submitted from this
   project's own tooling.** §2 confirms Base supports 7702 in general; it does not confirm this
   project's viem-based code can construct one correctly. A throwaway-EOA dry run is the actual gate
   before trusting planet 664's key to any 7702 flow.
3. **Web3Signer's V3-keystore file compatibility with the existing `keystore` provider's files was
   asserted from format family, not tested.** Before treating "just point Web3Signer at the existing
   file" as true, load the actual keystore file into a Web3Signer test instance and confirm it decrypts
   to the same address.
4. **Turnkey's and Cobo's exact import mechanics remain unconfirmed at the level needed to state a
   verdict with confidence** (§3.7, §3.9) — moot for the current recommendation since both are
   disqualified on self-hosting grounds regardless, but worth resolving if either vendor's policy
   engine becomes attractive enough to revisit the self-hosting requirement itself as a hard constraint.
5. **Audit reports are point-in-time.** `EIP7702Proxy`'s three Cantina/Spearbit rounds (Feb–Apr 2025)
   predate this document by over a year; re-check for any post-audit changes or disclosed
   vulnerabilities before relying on the deployed addresses cited in §3.3.
6. **Base chain support was not independently confirmed for Cobo, Turnkey, or OKX** — each was checked
   against whatever chain lists their own docs surfaced, and none explicitly named Base in what was
   fetched. Given all three are disqualified on other grounds, this wasn't chased further, but it
   means "Base support: unconfirmed" in §5 should not be read as "Base support: absent" — only as
   "not found in this pass."
7. **`protectedResources` and other unresolved gameplay questions** (`RESEARCH-ADDENDUM.md` §6, `NOTES.md`
   §6) are unrelated to wallet custody and out of scope here, noted only so this document isn't mistaken
   for having touched them.

---

*Research conducted 2026-08-12. Contract claims in §1 verified directly against
`/Users/santteegt/GitRepositories/clones/veydrift` at commit `701bed3578cff4d134657c714c599dbdb55a4b6a`
(the deployed commit per `RESEARCH-ADDENDUM.md` §1 — `main` has independently drifted and was not relied
on for any claim here). The clone was returned to its original `main` branch state after inspection; no
files in it were modified. Vendor claims are dated to their fetch date and should be re-verified before
any implementation decision, especially audit status (§7.5) and any pricing/tier details, which vendors
change without notice.*
