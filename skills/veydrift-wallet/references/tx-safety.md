# Transaction safety: what ethskills says, and what this engine actually does

Built after reading `https://ethskills.com/SKILL.md` and `https://ethskills.com/wallets/SKILL.md`
(their convention: "onchain" is one word, used throughout this skill's docs). This records, rule
by rule, which of their guardrails this engine implements, which it consciously skips, and why.

## Implemented

| ethskills rule | Where |
| --- | --- |
| Checksum-validate addresses via `viem.getAddress()` | `describeTx`/`checkAllowlist` in `src/tx.ts`/`src/allowlist.ts`; `send` prints the checksummed destination before prompting |
| Never move funds silently — show amount, destination, gas cost; await confirmation | `walletctl send` prints destination, decoded function+args, value, estimated gas, total ETH cost and the action's `purpose` string *before* anything is signed, and refuses to proceed without `--confirm` |
| Log transactions, never keys | Password is read from `VEYDRIFT_KEYSTORE_PASSWORD` or a non-echoing stdin prompt, never a CLI flag (so never in argv/shell history/`ps`), and decrypted key material lives only in the local scope of `signAndSend` — never assigned to `this`, never logged. `console.warn` output never includes key material |
| Use a dedicated, limited-fund wallet for agent operations | Out of this engine's control (it's an operational choice by whoever funds the wallet), but the tier model (`docs/SPEC.md` §4) and this engine's allowlist are exactly the mechanism that makes "dedicated, limited-fund wallet" enforceable rather than aspirational — see the allowlist section below |
| Key storage hierarchy: prefer encrypted keystore over plaintext env var | `keystore` is the **default** provider; `envkey` exists, works, and prints a loud startup warning every time it's used, matching ethskills' "testing-grade storage only" ranking |
| Never commit secrets to Git | `envkey`'s `refuseIfKeyLeakedInRepo` (`src/providers/envkey.ts`) is a best-effort, defense-in-depth check: if the key's raw value is found anywhere in the containing git repo outside `tests/`, the provider refuses to start. This is a safety net, not the primary control — the primary control is simply never writing it there |
| Test on testnet first | Not this engine's decision to make — see "What this does not do" below |
| Implement spending thresholds requiring human approval | Partially this engine's job (`send` refuses without `--confirm`, categorically), partially the agent skill's (`gas_per_tx`/`gas_per_day`/`eth_gas_floor_wei`/`escalate_above_pct_of_resources` live in `policy.json`, owned by `veydrift-agent`) |

## Consciously skipped, and why

**The 2-of-3 Safe multisig recommendation.** ethskills' wallet doc recommends an audited Safe
multisig (or similar smart-account setup) as the most secure agent-wallet architecture, with a
2-of-3 threshold across an agent hot wallet, a human hot wallet, and a human cold wallet. This is
sound advice in general and **does not apply here**. See `providers.md` for the full reasoning —
in short, a Veydrift planet is bound to the specific EOA that settled it (`_planets[planetId].owner`,
no transfer function, and planets are not NFTs), so any provider that issues a *new* address —
which a Safe, by construction, does — cannot hold the planet the agent is meant to be operating.
Recording this disagreement with generally-good advice is deliberate, not an oversight.

**Account abstraction / ERC-4337.** Same root cause: a smart-account wallet has its own address,
distinct from the EOA that owns the planet. Not evaluated further here; EIP-7702 (which keeps the
EOA's address while adding smart-account behavior) is the shape that *could* apply, and is
explicitly deferred to `docs/wallet-provider-research.md` (WP4b) rather than implemented in this
pass. `docs/RESEARCH-ADDENDUM.md` §6.1 notes Base blocks already carry Pectra's `requestsHash`
header field as circumstantial evidence the chain supports type-0x04 transactions, but is explicit
that this is "evidence, not proof" pending an actual landed 7702 transaction — this engine does not
build on that assumption anywhere.

**Hardware wallet / cloud KMS as the top storage tier.** ethskills ranks these above an encrypted
keystore. Not implemented in this pass (`SPEC.md` §0 explicitly drops Turnkey from this pass and
defers all hosted/HSM/MPC providers to WP4b's research document); `keystore` is this engine's
strongest *implemented* tier. Two working providers — not one working + one aspirational stub — is
what SPEC.md asks this package to prove, and a KMS integration would be exactly that kind of stub
without a real account to test against.

**Interactive y/n confirmation prompt for `send`.** ethskills' pattern describes "request approval"
as a step distinct from printing the transaction details. This engine folds that into a single
explicit `--confirm` flag rather than an interactive prompt: the flag itself *is* the approval
mechanism, and — critically — nothing else can set it. See "The `--confirm` invariant" below for
why a flag was chosen over an interactive prompt (scriptability without weakening the guarantee).

**Gas-price / gas-cost ceilings inside this engine.** `send` computes and prints the estimated ETH
cost, but does not itself refuse a transaction for being "too expensive" — `gas_per_tx_wei` /
`gas_per_day_wei` / `escalate_above_pct_of_resources` ceilings live in `policy.json`, enforced by
`veydrift-agent`'s `vd guard` before `walletctl build` is ever invoked (`docs/SPEC.md` §5.5). This
engine's allowlist is a second, independent gate on *what* can be signed (destination, selector,
value, chainId, mission type) — not a policy engine for *how much*. Duplicating cost ceilings here
would mean two places to keep in sync with no safety benefit, since the agent skill's gate runs
first in the intended flow.

## The `--confirm` invariant

**No environment variable and no other flag can make `--confirm` implicit.** This is the one
property the whole tier model rests on: `veydrift-agent` (even fully compromised) can construct any
`Action` JSON it likes and call `walletctl build`/`simulate` freely, but it cannot make `walletctl
send` actually submit anything without a human (or a deliberately-scripted, explicitly-flagged
invocation) passing `--confirm` on that exact command line. There is no `VEYDRIFT_AUTO_CONFIRM`,
no `--yes`, no config field that flips this. `walletctl send --tx tx.json` (no `--confirm`) always
exits non-zero and always prints the transaction it *would* have sent — this is deliberately the
same code path as a successful build, just short-circuited before `provider.signAndSend` is ever
called, so what you see printed is exactly what would be signed, not a separate "preview" that
could drift from reality.

## The tier is read from `policy.json`, not asserted by the caller

**Fixed 2026-08-12 (judge finding).** `checkAllowlist`'s selector check (below) enforces "selector
∈ the tier's allowed set" -- but *which tier* used to come straight from `walletctl`'s own `--tier`
flag / `VEYDRIFT_TIER` env var, which `veydrift-agent`'s `tick.py` populates from its own read of
`policy.json`. A compromised `veydrift-agent` process under a tier-1 (`advisor`) policy could just
invoke `walletctl send --tier operator` directly and the allowlist would enforce `operator`'s rules
-- defeating the entire point of an *independent* second check.

`walletctl` now resolves the enforcing tier itself, from `$VEYDRIFT_HOME/policy.json`
(`VEYDRIFT_HOME` env, default `~/.veydrift` -- the same file and the same default `veydrift-agent`
uses), never from its own `--tier`/`VEYDRIFT_TIER` alone. `src/policy.ts`'s `resolveTier`:

1. **Policy file exists and parses with a valid `tier`** -> that tier is authoritative. If a
   caller-supplied `--tier`/`VEYDRIFT_TIER` is *also* present and disagrees, `walletctl` refuses
   outright (exit 4) and names both values -- it never silently prefers either one.
2. **No policy file at all** (`ENOENT`) -> falls back to `--tier`/`VEYDRIFT_TIER`, defaulting to
   `advisor` -- this engine's original, sole behavior, kept for standalone use without
   `veydrift-agent` installed alongside it.
3. **Policy file exists but is unreadable for another reason, unparseable, or has no valid `tier`
   field** -> refuses outright. A malformed security policy is never treated as "absent" and never
   falls through to a permissive default.

### The honest residual limit

This closes the specific gap the judge identified -- a compromised agent can no longer *talk its
way* into a higher tier by lying about which one it's operating at. It does **not** make this
engine tamper-proof against a fully compromised host: `walletctl` still reads signing credentials
straight from the environment (`VEYDRIFT_PRIVATE_KEY` for `envkey`, `VEYDRIFT_KEYSTORE_PASSWORD` for
`keystore`, when set). A fully compromised agent process runs in that same environment, so it can
read those same variables and sign directly with `viem`/`ethers` -- bypassing `walletctl`, the
allowlist, and this tier check entirely, without ever invoking this CLI at all. The two-layer
defence (agent-side `vd guard` + this engine's independent allowlist) is only airtight when signing
requires something the compromised process cannot read out of its own environment -- i.e. the
`keystore` provider's **interactive** password prompt (no `VEYDRIFT_KEYSTORE_PASSWORD` set), which
demands a human at a TTY for every single send. Setting `VEYDRIFT_KEYSTORE_PASSWORD` (or using
`envkey` at all) trades that guarantee for convenience; say so plainly rather than overclaiming a
"defense in depth" that a leaked env var defeats in one step.

**A second, cheaper bypass of the tier check specifically** (found by the second judge pass,
2026-08-12, confirmed by execution). The paragraph above is about *credential* reachability. The
tier check has its own, weaker hole that needs no credentials at all: `resolveTier` falls back to
the caller-supplied `--tier` / `VEYDRIFT_TIER` whenever **no policy file exists** (rule 2). A
compromised agent controls its own environment, so it can point `VEYDRIFT_HOME` at an empty
directory and pass `--tier operator`:

```
VEYDRIFT_HOME=/tmp/empty  walletctl … --tier operator   ->  resolved tier: operator
```

The no-policy fallback is deliberate — `walletctl` has to be usable standalone, before `vd tick
init` has ever run — so this is not a bug to remove. It does mean **the tier check defends against
an honest-but-misconfigured caller, not against a hostile one.** Against a hostile caller it adds
nothing that the credential bypass above hasn't already conceded. What still holds unconditionally,
against any caller: Veydrift-address-only from a live `/runtime-config` fetch, chainId 8453,
`value == 0`, game-selectors-only, the operator mission-type restriction, `--confirm` mandatory,
and refusal to `send` a nonpayable-read function. Those are properties of the transaction, not
claims by the caller, which is why they survive a compromise that the tier check does not.

## Defense in depth: the allowlist doesn't trust the agent skill

`src/allowlist.ts`'s `checkAllowlist` is re-run unconditionally inside `sendTx` regardless of what
already validated the transaction upstream (`docs/SPEC.md` §6.4). Five checks, every one evaluated
and reported (never short-circuited in the report, only in the final `ok`):

1. `tx.to` must be in the address set from a **live** `/runtime-config` fetch — never a hardcoded
   address, so a contract migration is reflected automatically and a stale hardcoded address can't
   silently become wrong.
2. `tx.data`'s 4-byte selector must be in the tier's allowed set, and that set is **computed from
   the pinned ABI** (`resolveFunctionAbi` + `toFunctionSelector`), not from a hand-typed hex
   constant — if the pinned ABI ever stops containing one of the tier's signatures, computing the
   selector throws loudly instead of silently allowlisting nothing (or worse, misresolving).
3. `tx.value` must be `0` — no payable action is whitelisted at any tier reachable from this
   engine, so this check can never legitimately fail for a real proposed action; if it does,
   something upstream is already wrong.
4. `tx.chainId` must be `8453` (Base).
5. `operator`'s `launchFleetMission` gets one more check that can't be expressed as a selector
   check at all: the mission type is an ordinary calldata *argument*, not part of the selector, so
   `checkAllowlist` decodes the calldata (`decodeFunctionData`) and rejects anything other than
   Transport(0)/Deploy(1)/Harvest(4) even though the selector itself is allowed. This is the one
   place the allowlist has to understand *what a transaction does*, not just *where it goes and
   what function it calls* — combat mission types (Attack, AcsAttack, MissileAttack, Intercept) are
   unreachable through this engine no matter what tier is configured.

Any failure anywhere in the five checks: `sendTx` throws `SendRefusedError`, the CLI exits non-zero,
nothing is signed, and the rejection reason is printed (not silently swallowed).

## The two other traps `send` refuses outright, independent of the allowlist

Beyond the allowlist, `sendTx` (`src/tx.ts`) has two more categorical refusals that run *before*
the allowlist check, because they're not really about policy — they're about the ABI lying about
what a function is:

- **The six nonpayable-but-semantically-read functions** (`attackProtectionStatus`,
  `collectResources`, `debrisField`, `maxRaidLoot`, `protectedResources`, `raidableResources`) are
  `nonpayable` in the ABI because they lazily settle state before returning, not because they're
  meant to be transactions. `sendTx` refuses all six unconditionally — even at `operator` tier with
  `--confirm` — with a message pointing at `walletctl simulate` instead. Sending one of these would
  mean paying real gas to perform what is, semantically, a read.
- **`launchFleetMission`'s overload ambiguity** never reaches `send` as ambiguity in the first
  place: `resolveFunctionAbi` requires the full canonical signature for any overloaded function
  (trap #2, `references/abi-pinning.md`), so `build` fails loudly at construction time if a caller
  tries to select it by bare name. By the time a tx reaches `send`, the selector already
  unambiguously identifies one of the two real overloads.

## What this engine deliberately does not do

- It never decides *when* to send — that's a human, or `veydrift-agent`'s tier-gated proposal flow,
  never this engine acting on its own initiative.
- It never submits a transaction from a test, from CI, or during development. **No transaction has
  ever been submitted to Veydrift from this codebase** (`docs/SPEC.md` §11) — the write path is
  built, allowlisted, and fixture-simulated, never executed against mainnet, by design and by
  standing project rule.
- It does not attempt to recover a lost password or lost keystore. There is no recovery path — see
  `README.md`'s key-custody section for the operational consequence of that.
