# Veydrift — Agent Resource Checklist

Everything an autonomous agent needs to operate a Veydrift planet on Base mainnet.
Scoped to wallet `0x224aba5d489675a7bd3ce07786fada466b46fa0f`, planet id `664` (7:181:14).

Verified live against `api.veydrift.com` on 2026-08-07.

---

## 1. Identity and signing

| Item | Value / requirement | Notes |
| --- | --- | --- |
| Signing key | Private key for `0x224a…fa0f` | **This wallet _is_ the planet.** There is no account recovery. If this key is on an agent host, that host is now a single point of failure for the whole game account. |
| Key storage | OS keychain, `.env` outside version control, or a KMS/HSM | Never in the prompt, never in a repo, never in chat history. Pass by env var reference only. |
| Recommended posture | Dedicated burner wallet holding **only** the planet + gas | Do not reuse a wallet that holds NFTs, tokens, or is an owner/admin anywhere. |
| Chain | Base mainnet, `chainId 8453` | Not Base Sepolia. The test deployment (84532) is a separate universe. |

**Hard rule to encode in the agent:** the key signs transactions to the Veydrift contracts and nothing else. Any request to sign a message, approve a token, or interact with a non-Veydrift address is a stop condition.

## 2. Funding

| Asset | Where | Purpose |
| --- | --- | --- |
| **ETH on Base** | `0x224a…fa0f` | Gas for every action. Base gas is cheap; ~0.01–0.02 ETH covers a lot of play. Set a low-balance alarm — an agent that runs dry stalls silently mid-queue. |
| Metal / Crystal / Deuterium | In-game, on the planet | Produced by mines. Also exist as ERC-20 proxies (see §4) — check before assuming resources are purely internal ledger entries. |
| ETH for settlement | Already spent | Start price was `12000000000000000` wei (0.012 ETH). Only relevant if you settle more planets. |

Give the agent a **gas budget ceiling** (e.g. "stop and report if cumulative gas this week exceeds X") so a retry loop can't drain the wallet.

## 3. Network access

| Resource | Endpoint | Auth |
| --- | --- | --- |
| Read API (indexed state) | `https://api.veydrift.com` | none |
| GraphQL (status only today) | `https://api.veydrift.com/graphql` | none |
| Base RPC (HTTP) | your own provider — Alchemy, QuickNode, or `https://mainnet.base.org` | key if paid |
| Base RPC (WebSocket) | same provider | for event subscriptions |
| Block explorer / ABI source | Basescan (Etherscan V2 API) | API key required for `getabi` |

Use a **private RPC** rather than the public endpoint if the agent polls frequently — public endpoints rate-limit and a throttled agent misreads state as "unchanged".

## 4. Contract addresses (Base mainnet)

Pulled live from `GET /runtime-config`:

| Contract | Address |
| --- | --- |
| Game (UUPS proxy) | `0xf397910F005151b09644228573a4353818D3755d` |
| Alliance system | `0x0E5a6210482B15780cf5Ec036107031dcA702001` |
| Moon system | `0x4935f1E0024F1Ea07877a583F89A51BF3d91Cf5C` |
| Randomness engine | `0xdc7d3388bfb07E2cC8DD3Be265d7C1182D34d069` |
| Referral system | `0x3246Df19Fa850E27eAC5292232aC2a51bbB7b835` |
| Migration | `0x33A56B6f6354D32Edeef43baECC3C94a316bf7d3` |
| Paid alliance invite | `0xD11be3728Ab28A1Ccd14C99cD4034115Fb22EF49` |
| Metal ERC-20 | `0x91A4f8A9D05F21E010dc1eE0B17Ab644D433cB41` |
| Crystal ERC-20 | `0xC6881a2C4C50E28AdCaC4D5577cD8e211E806B76` |
| Deuterium ERC-20 | `0x5A6027DE1C7E52B4b1AD0c13c3eC3Ad5FCb481e2` |
| Burning Chicken NFT / burn | `0x84EEA2bE67b17698B0E09B57eEEdA47aa921BbF0` |

Re-fetch `/runtime-config` at agent startup rather than hardcoding. It's a UUPS proxy — the implementation can be upgraded, and `deploymentAbiHash` tells you when the ABI changed underneath you.

## 5. ABI

> **Update (see `NOTES.md` §1):** the ABI is only needed for **writes**. Undocumented read endpoints
> (`/infrastructure`, `/research`, `/shipyard`, `/missions`) expose building/tech/ship levels *and
> live costs at your current level*, so an agent can plan the entire economy with zero RPC access.

> **Solved — see `NOTES.md` §13.1.** The source is publicly readable with no API key. Use either:
>
> 1. **Raw GitHub** (best): [`raw.githubusercontent.com/Borodutch/veydrift/main/packages/contracts/src/VeydriftGame.sol`](https://raw.githubusercontent.com/Borodutch/veydrift/main/packages/contracts/src/VeydriftGame.sol) — for the pinned deployment commit specifically, use [the `701bed35` blob instead](https://github.com/Borodutch/veydrift/blob/701bed3578cff4d134657c714c599dbdb55a4b6a/packages/contracts/src/VeydriftGame.sol)
>    — the facade enumerates every external entrypoint. Other modules sit alongside it in `src/`.
> 2. **Blockscout**: `base.blockscout.com/api/v2/addresses/0xf397910F005151b09644228573a4353818D3755d`
>    → `implementations[0]` = `0xf210b66b23731971ac606fC2C5c29a96eA19A99d`, then
>    `/api/v2/smart-contracts/0xf210b66b…` for the verified ABI.
>
> (The GitHub *contents* API returns empty anonymously, which is what made this look blocked at first.
> Raw file access is unaffected.)

Older fallbacks, no longer needed: Basescan `getabi` via the Etherscan V2 API (needs a key), the frontend bundle, or `forge build` on a clone.

Pin the ABI and compare `deploymentAbiHash` from `/runtime-config` each run; if it moved, refuse to write and alert.

## 6. Read endpoints the agent will actually use

| Endpoint | Returns |
| --- | --- |
| `GET /health` | `ok`, `latestIndexedBlock`, `indexedState`, `readiness`. **Gate every run on this.** |
| `GET /runtime-config` | addresses, chainId, `featureSupport`, ABI hash |
| `GET /wallet/{addr}/settlement` | home planet id, coords, fields, temperature, multipliers, current resources |
| `GET /wallet/{addr}/queues` | building / defense / ship / research queues (all `null` = idle) |
| `GET /planets/{id}` | planet snapshot |
| `GET /universe/galaxies/{g}/systems/{s}` | occupancy, debris fields, moons, archetypes — raid and colony targeting |
| `GET /universe/systems?galaxy=7&center=181&radius=2` | multi-system sweep |
| `GET /highscores` | rankings + attackability context (large payload, ~86 KB) |
| `GET /wallet/{addr}/infrastructure` | **undocumented** — building levels, live costs, durations, energy balance, production/hr, storage caps, raidable resources |
| `GET /wallet/{addr}/research` | **undocumented** — tech levels, live costs, durations, lab level |
| `GET /wallet/{addr}/shipyard` | **undocumented** — ship counts, live costs, fleet slots, shipyard/nanite levels |
| `GET /wallet/{addr}/missions` | **undocumented** — paginated mission archive |

See `NOTES.md` §1–§2 for the full endpoint map and the verified building / technology / ship ID tables. Still missing: a defense endpoint (exists under a name not yet guessed) and battle reports.

## 7. Runtime the agent needs

- **HTTP client + JSON** — for the read API.
- **EVM library** — viem or ethers (JS/TS), or web3.py. viem matches the repo's TS stack.
- **Persistent state file** — last action, pending tx hashes, gas spent, mission log. Required so a restart doesn't re-submit a queued build.
- **Scheduler** — the game runs on hourly production; polling every 5–15 minutes is plenty. Faster polling costs RPC quota and buys nothing.
- **Log + alert channel** — somewhere the agent reports what it did and where it stopped.

## 8. Guardrails to configure before it signs anything

| Guardrail | Suggested setting |
| --- | --- |
| Allowed contracts | Veydrift addresses in §4 only |
| Allowed actions | Start with build + research. Add fleet later. |
| Max gas per tx / per day | Set explicitly |
| Resource floor | Never spend below a reserve that keeps fuel available |
| Attack policy | Off by default. Score protection (<50k → 1.5× gap) and the 24h bashing window make bad targeting an active liability. |
| Index-lag policy | If `/health` is not `ok`, or the receipt isn't indexed within N seconds, **stop — do not act on the displayed balance** |
| Kill switch | A file or flag the agent checks each loop and halts on |

## 9. Optional

- **Etherscan V2 API key** — ABI fetch, tx history.
- **Alchemy webhook** — the backend exposes `POST /webhooks/alchemy`; you could mirror the pattern to get pushed events instead of polling.
- **Farcaster / Base app context** — Veydrift ships a Mini App manifest, so there may be a social surface worth watching for alliance chatter.

---

## Quick shakeout before going live

```
1. GET /health                              -> ok: true
2. GET /runtime-config                      -> record deploymentAbiHash
3. GET /wallet/0x224a…fa0f/settlement       -> planet 664, 7:181:14
4. GET /wallet/0x224a…fa0f/queues           -> all null (idle)
5. eth_call the game contract for building levels on planet 664
6. Check ETH balance on Base
7. Run the full agent loop in DRY-RUN for 24h — log intended txs, sign nothing
8. Compare dry-run decisions against your own judgement, then enable writes
```

Do not skip step 7.
