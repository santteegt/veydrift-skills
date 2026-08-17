# Fork testing for tier≥2 Veydrift wallet actions — feasibility & execution plan

## Context

AGENTS.md §10 names this repo's single biggest untested surface: `tick.py`'s tier≥2 send
path (`_send_and_await`) — and, underneath it, `veydrift-wallet`'s `sendTx()` — has **never
executed against a real chain**. It's covered by monkeypatched unit tests on both sides, but
the actual `build → simulate → send → await receipt` sequence, for any of the 8 selectors
this engine can ever construct, has zero real-world executions. A second, related gap: the
one account this system has been built and verified against is at zero state — "every level
is 0, every queue is idle" — so cost scaling, queue behavior, and lazy settlement above level
0 are unverified by observation, only by formula derivation from contract source.

This plan closes both gaps using a local blockchain fork with impersonated accounts, forking
from an Alchemy Base RPC URL rather than a public endpoint.

**Scope, as agreed:** wallet-only fork testing (not the full `vd tick` agent loop — that
would require building a mock backend indexer, since `veydrift-agent`'s read/plan/guard
pipeline is hardcoded to `https://api.veydrift.com` with no fork override, and is already
covered by real monkeypatch-based unit tests). The new provider will be registered normally
in `providers/index.ts`, protected by a hard loopback-RPC guard, so fork tests run through
the *exact* production CLI/`sendTx()`/allowlist path rather than a parallel test-only
implementation.

## Feasibility verdict: highly feasible, small footprint

Key enablers found during research:

- **Foundry (anvil/forge/cast 1.5.1) is already installed locally** — confirmed via
  `anvil --version`. No new tooling dependency.
- **`VEYDRIFT_RPC_URL` is already the single chokepoint for RPC target**
  ([tx.ts:37-39](../skills/veydrift-wallet/src/tx.ts#L37-L39)) — every read (`getPublicClient`) and
  every write (`keystore`/`envkey` providers' `signAndSend`) resolve through it. Pointing it
  at a local fork (`http://127.0.0.1:8545`) needs zero changes to existing code — it's just
  currently undocumented as a supported knob.
- **Contract authorization is plain `msg.sender`**, not signature-scheme-dependent
  (`_requirePlanetOwner`, confirmed in docs/wallet-provider-research.md against
  `VeydriftGame.sol:793-796`) — so Anvil's unsigned, node-trusted `eth_sendTransaction` for
  an impersonated account produces an *identical* authorization outcome to a real signed
  transaction from that address. This is the load-bearing fact that makes impersonation a
  faithful test, not an approximation.
- **Forking from "latest" (or any block after deployment) serves the actual deployed
  bytecode** at the pinned commit (`701bed35...`) — no `forge build` needed, and the pinned
  ABI / allowlist selectors stay valid completely unmodified against a fork.
- **`sendTx()`** ([tx.ts:339-362](../skills/veydrift-wallet/src/tx.ts#L339-L362)) **is already
  provider-injectable** — a new provider slots in with zero change to production send logic,
  `buildTx`/`simulateTx` already accept injectable clients/config for the same reason.
- This codebase's test culture already treats **Anvil's default account #0** as its
  established throwaway test key (`envkey.ts`'s leak-scanner explicitly excludes it;
  `tests/providers.test.ts` hardcodes it) — fork testing extends a pattern already present,
  not a foreign one.
- **`skills/veydrift-wallet/` is the tracked source**; `.claude/skills/veydrift-wallet/` is
  a gitignored local install copy (`.gitignore:45`). All changes below target the tracked
  source only — re-run the skill install afterward if you use the installed copy directly.

## What this closes, and what it deliberately doesn't

**Closes:**
- "Has never run against a real chain" for the entire reachable tier≥2 write surface: all 5
  `ECONOMY_SIGNATURES` (`startBuildingUpgrade`, `startResearch`, `resolveFleetMission`,
  `startDefenseProduction`, `startShipProduction` — `settlePlanet` was removed from this set in
  Phase 5, docs/SPEC.md §5.4/§9; it is no longer allowlisted at any tier) plus both non-combat
  `launchFleetMission` overloads (Transport/Deploy/Colonize/Harvest — the mission types
  `OPERATOR_ALLOWED_MISSION_TYPES` permits as of Phase 5b).
- "Cost scaling, queue behavior, lazy settlement above level 0 are unobserved" — by
  impersonating a real, *already-advanced* player address (not just the zero-state reference
  account) and using Anvil time-travel to force queue completions.

**Deliberately does not touch:**
- Combat. `_MIN_TIER_FOR_FUNCTION`, `allowlist.ts`'s `ECONOMY_SIGNATURES`/
  `LAUNCH_FLEET_MISSION_SIGNATURES`, and `OPERATOR_ALLOWED_MISSION_TYPES` are unchanged.
  This plan never exercises `Attack`/`AcsAttack`/`MissileAttack`/`Intercept` — per AGENTS.md
  §5, that friction is deliberate and this work doesn't lower it in passing.
- The full `vd tick` agent loop / `_await_indexed` (out of scope, deferred).
- `--confirm`'s unconditional requirement — the new provider changes *who* signs, never
  whether confirmation is required.
- Real Base mainnet — every impersonated send happens against a local, ephemeral fork.
  Nothing is ever signed with a real private key belonging to the impersonated address, and
  nothing leaves the fork. This is the same "whale impersonation" technique used ubiquitously
  for DeFi protocol testing — harmless to the real account being impersonated.

## Design

### 1. Fork startup

```bash
anvil --fork-url $VEYDRIFT_FORK_RPC_URL --chain-id 8453 [--fork-block-number N]
```

- Use an Alchemy Base RPC URL as `$VEYDRIFT_FORK_RPC_URL` — public endpoints
  (`mainnet.base.org`) rate-limit hard under Anvil's on-demand per-slot state
  fetching, which matters because reading one player's full state touches many storage
  slots.
- Default to forking "latest" for exploratory runs; pin an explicit `--fork-block-number`
  for any run whose results get written into a reference doc, mirroring the reproducibility
  principle already used for the ABI pin (AGENTS.md §6).
- Runs on `127.0.0.1:8545` (Anvil's default) — the new provider's safety gate depends on
  this being loopback.

### 2. New provider — `skills/veydrift-wallet/src/providers/fork-impersonate.ts`

Implements the existing `WalletProvider` interface
([types.ts](../skills/veydrift-wallet/src/providers/types.ts)):

- **Constructor** eagerly resolves `getRpcUrl()` and throws immediately unless
  `new URL(...).hostname` is `127.0.0.1`/`localhost`/`::1` — an unconditional refusal outside
  loopback, in the same defensive style as `envkey.ts`'s `refuseIfKeyLeakedInRepo`. This is
  what makes registering it in the normal provider list safe even if misselected in a real
  environment: production's `VEYDRIFT_RPC_URL` will never resolve to loopback.
- Reads the address to impersonate from a new env var,
  `VEYDRIFT_FORK_IMPERSONATE_ADDRESS` — fails fast if unset, same pattern as `envkey.ts`'s
  `VEYDRIFT_PRIVATE_KEY` check.
- `getAddress()` returns that address directly. No key material anywhere.
- `signAndSend(tx)`:
  1. `client.request({ method: "anvil_impersonateAccount", params: [address] })`
  2. `client.request({ method: "anvil_setBalance", params: [address, "0x..."] })` — gas
     top-up; removes flakiness even though an active real player likely already has ETH.
  3. `client.request({ method: "eth_sendTransaction", params: [{ from: address, to: tx.to,
     data: tx.data, value: ..., gas: tx.gas }] })` — no client-side signature; Anvil trusts
     the call because the account is impersonated, and auto-mines by default, so this
     returns a real, immediately-confirmable tx hash.
- `capabilities()` returns `{ canSign: false, canSimulate: false, remotePolicy: false }` —
  honest: this is a genuinely new provider category (node-trusted, no signature), distinct
  from both existing local-signing providers.
- Register in `providers/index.ts`: add `"fork-impersonate"` to the `ProviderName` union,
  `AVAILABLE_PROVIDERS`, and `getProvider()`'s switch.
- Document the new category in `references/providers.md` (currently frames `keystore`/
  `envkey` as "two genuinely working providers"), and add an explicit note to
  `references/tx-safety.md` that "never against mainnet" refers to mainnet specifically — a
  local fork is not mainnet, and this is the intended first real exercise of `sendTx`'s
  `provider.signAndSend()` line, not an exception to that standing rule.

### 3. Selecting target accounts

Two accounts, two distinct purposes:

- **The known reference address** (`0x7Cd117B9a5e8E5e9E11a5Db0C1e489dF899eda9A`, zero
  state) — for "first action from scratch" tests (level 0→1 building upgrade, first ship
  production). Results are directly comparable to whatever a real future tier-2 promotion
  would produce, since it's the same account.
- **A discovered, more advanced real player address** (non-zero building levels, non-empty
  queues) — needed to observe cost scaling and queue behavior above level 0. Find one via
  the backend's public `/highscores` route (already documented in
  docs/RESEARCH-ADDENDUM.md's route list) or by reading `PlanetSettled`/building-upgrade
  events directly off Base — read-only, no auth, doesn't touch the fork.

### 4. Time travel for lazy settlement / queue completion

After sending a build/research/ship/defense action, use `anvil_increaseTime` + `anvil_mine`
to jump the fork's clock past that action's queue-completion time (durations already
computable via `vd calc`), then call the lazy-settlement read-shaped functions
(`collectResources` etc. — via `simulate`, never `send`, per the existing and correct
`NONPAYABLE_READ_FUNCTIONS` restriction) to confirm levels/resources/queues update as the
formulas predict. (`settlePlanet` would have served the same purpose — its body is byte-
identical to `collectResources` — but it was removed from both enforcement layers in Phase 5,
docs/SPEC.md §5.4/§9, so it is no longer allowlisted or reachable at any tier; `collectResources`
alone already covers this observation.) This is the concrete mechanism for observing "queue
behavior and lazy settlement above level 0" for the first time ever in this project.

### 5. Execution runbook — new file `skills/veydrift-wallet/references/fork-testing.md`

Step-by-step: start Anvil, set `VEYDRIFT_RPC_URL` / a scratch `VEYDRIFT_HOME` /
`VEYDRIFT_FORK_IMPERSONATE_ADDRESS`, run
`walletctl build/simulate/send --tier operator --confirm --provider fork-impersonate`, then
`walletctl receipt`. Explicitly reuses the already-sanctioned
`VEYDRIFT_HOME=/tmp/empty --tier operator` pattern documented in `tx-safety.md` rather than
inventing a new bypass. One documented run per selector across the full 8-selector surface.

### 6. Tests — `skills/veydrift-wallet/tests/providers/fork-impersonate.test.ts`

- Loopback guard rejects a non-local `VEYDRIFT_RPC_URL` — pure unit test, no live fork
  needed.
- A real, ephemeral `anvil` child process (spawned in `beforeAll`, killed in `afterAll`)
  forking Base at a fixed block, impersonating Anvil's own default account #0 (this
  codebase's existing throwaway-key convention), proving the impersonate → setBalance →
  eth_sendTransaction → receipt plumbing works end-to-end without depending on real game
  state.
- The real-player-address runs stay a documented manual/scripted runbook (§5), not a CI
  assertion — they depend on live Base state at fork time and a specific account's queue
  status, which will drift.

## Verification

- `npm --prefix skills/veydrift-wallet test` and `npm --prefix skills/veydrift-wallet run
  typecheck` stay green with the new provider added (existing 104 TS tests + new
  fork-impersonate unit tests).
- Runbook executed at least once per selector; each receipt confirmed `status: "success"`
  via `walletctl receipt`, and resulting contract state independently spot-checked via
  `cast call` against the fork.
- After real runs complete, update AGENTS.md §10's "known gaps" bullets to reflect what's
  now actually verified vs. still unverified — a docs follow-up, not part of this coding
  phase.
