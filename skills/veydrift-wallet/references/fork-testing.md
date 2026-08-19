# Fork testing tier≥2 sends against a real chain

## Why this exists

AGENTS.md §10 names this repo's single biggest untested surface: `tick.py`'s tier≥2 send path
(`_send_and_await`) and, underneath it, `veydrift-wallet`'s `sendTx()` have never executed against
a real chain. `dac1050` closed the *tooling* half of that gap: `src/providers/fork-impersonate.ts`
runs the exact production `sendTx` → `provider.signAndSend` path against a local Anvil fork,
impersonating a real account instead of holding its key. This document is the execution runbook
for actually using it — exact commands, not prose, in the style of `references/abi-pinning.md`.

**Nothing here submits to mainnet.** Every send in this document targets a local, ephemeral Anvil
fork. The standing rule — "no transaction has ever been submitted to Veydrift from this codebase"
(`references/tx-safety.md`, `docs/SPEC.md` §11, `README.md`'s status section) — is about the real
chain. See `tx-safety.md`'s qualification for why a local fork is the intended first exercise of
`provider.signAndSend()` rather than a loophole in that rule.

## 1. Start Anvil

```bash
anvil --fork-url $VEYDRIFT_FORK_RPC_URL --chain-id 8453 [--fork-block-number N]
```

`$VEYDRIFT_FORK_RPC_URL` here is a plain shell variable for this runbook's own commands — it is
**not** read by any code in this package (contrast `VEYDRIFT_FORK_TEST_RPC_URL`, which
`tests/providers/fork-impersonate.test.ts` reads to gate its own optional e2e suite, §7 below).
Name it whatever you like; the value matters, the name doesn't.

- **`--chain-id 8453` is not optional.** `checkAllowlist` (`src/allowlist.ts`) rejects any
  `tx.chainId` other than `8453` (`references/tx-safety.md`'s allowlist check 4). Anvil defaults
  to chain id `31337` when forking without `--chain-id`, which would make every built transaction
  fail the allowlist for a reason that has nothing to do with what you're actually testing.
- **Use an Alchemy-class URL, not a public endpoint, for `$VEYDRIFT_FORK_RPC_URL`.** Anvil fetches
  state per-slot, on demand, as the fork needs it — reading one player's full state (planet,
  buildings, research, queues, resources) touches many storage slots in a short window. Base's
  public endpoint (`mainnet.base.org`, this package's own `DEFAULT_RPC_URL`) rate-limits hard under
  that access pattern; an Alchemy (or equivalent) app URL does not.
- **Reproducibility**: fork `latest` for exploration. For any run whose results get written into a
  doc (this one included), pin an explicit `--fork-block-number` — the same principle as the ABI
  pin (AGENTS.md §6): an unpinned fork means a future rebuild silently reproduces a different
  world, not the one the doc describes.
- Anvil listens on `127.0.0.1:8545` by default. `fork-impersonate`'s loopback guard
  (`refuseIfNotLoopback`, `src/providers/fork-impersonate.ts:49-67`) depends on the RPC host being
  one of `127.0.0.1` / `localhost` / `::1` / `[::1]` — Anvil's default satisfies this with no
  further flags.

## 2. Environment

```bash
export VEYDRIFT_RPC_URL=http://127.0.0.1:8545
export VEYDRIFT_FORK_IMPERSONATE_ADDRESS=0x224aba5d489675a7bd3ce07786fada466b46fa0f   # or whichever account, see §5
export VEYDRIFT_HOME=/tmp/empty
```

**Prefer a repo-local `.env` over your shell profile.** `.env` and `.env.*` are gitignored at
the repo root (`.gitignore:2-3`, with `!.env.example` so the template stays committable), so an
upstream RPC URL with an embedded API key never lands in a tracked file or in your machine-wide
profile. Copy the template and source it:

```bash
cp .env.example .env      # then fill in the values
set -a; . ./.env; set +a  # in the shell that runs the fork commands
```

Nothing auto-loads it — neither skill declares a dotenv dependency, so the `set -a` form above is
the load step. Being gitignored is also what keeps it clear of `envkey.ts`'s leak scanner: that
scan runs `git grep`, so a value in a properly gitignored file is the sanctioned case rather than
a violation (`src/providers/envkey.ts:52-58`).

`VEYDRIFT_HOME=/tmp/empty` reuses the pattern `references/tx-safety.md` already sanctions for
standalone `walletctl` use with an explicit `--tier`, rather than inventing a new bypass:
`resolveTier` (`src/policy.ts:63`) falls back to the caller-supplied `--tier`/`VEYDRIFT_TIER` only
when **no `policy.json` exists** at `$VEYDRIFT_HOME`. An empty scratch directory guarantees that.
If a `policy.json` *does* exist and its `tier` disagrees with `--tier`, `walletctl` refuses outright
(exit 4, naming both values) — it never silently prefers either, so this is not a way to talk your
way past a real policy file, only a way to run `walletctl` before one exists. Point
`VEYDRIFT_HOME` at a fresh empty directory each session; never your real `$VEYDRIFT_HOME`.

Then, per command:

```bash
walletctl build --action action.json --tier operator --out tx.json
walletctl simulate --tx tx.json --from "$VEYDRIFT_FORK_IMPERSONATE_ADDRESS"
walletctl send --tx tx.json --confirm --provider fork-impersonate --tier operator
walletctl receipt --hash 0x...
```

`--provider` exists on `build` (only to derive a best-effort `--from` for the gas estimate when
`--from` is omitted) and on `send` (who signs). **`simulate` and `receipt` have no `--provider`
flag at all** — `simulate` takes `--from` directly (`src/cli.ts:238`), and `receipt` takes only
`--hash` (`src/cli.ts:316-318`); neither needs a signer.

**`--from` on `simulate` is not optional in practice.** Omit it and the simulation runs against a
default zero-ish address instead of the impersonated one — every `simulate` reverted with
`NotPlanetOwner()` in testing until `--from` was added, even though the *built* transaction was
perfectly fine. `veydrift-agent`'s own tier≥2 path (`tick.py`'s `_walletctl_simulate`, added in
`1.1.1`) always passes `--from`; do the same by hand here.

Sanity-check the setup before building anything:

```bash
walletctl status --provider fork-impersonate
```

`capabilities: canSign=false canSimulate=false remotePolicy=false` should print — `getAddress()`
resolves to `VEYDRIFT_FORK_IMPERSONATE_ADDRESS`, and `rpcUrl:` should read back
`http://127.0.0.1:8545`, confirming the loopback guard passed.

## 3. The 7 selectors

`ECONOMY_SIGNATURES` (`src/allowlist.ts:38-60`) plus both `LAUNCH_FLEET_MISSION_SIGNATURES`
overloads (`src/allowlist.ts:65-68`) — 5 + 2 = 7 total selectors reachable at `operator` tier.
`settlePlanet` is gone (removed Phase 5, `CHANGELOG.md` `[0.2.0]`) — do not go looking for it.

| # | Function | Planner-reachable? |
| --- | --- | --- |
| 1 | `startBuildingUpgrade(uint256,uint8)` | Yes — `plan.py` bands 1-4/8b |
| 2 | `startResearch(uint256,uint8)` | Yes |
| 3 | `startShipProduction(uint256,uint8,uint32)` | Yes, gated on `policy.actions.allow_ships` |
| 4 | `startDefenseProduction(uint256,uint8,uint32)` | Yes, gated on `policy.actions.allow_defense` (see `guard.py`'s tier map) |
| 5 | `resolveFleetMission(uint256)` | Yes — permissionless, free, fires whenever a mission has been `Resolving` >60s (`plan.py:200`) |
| 6 | `launchFleetMission(...)` 6-arg (no `speed_pct`) | Yes, but **only** for Transport (0) and Harvest (4), and **only** behind `policy.actions.allow_fleet_noncombat` (default `False` — `models.py:257`). **Confirmed live 2026-08-19** — see §9. Deploy (1) and Colonize (2) are allowlisted at the wallet layer (`OPERATOR_ALLOWED_MISSION_TYPES = {0,1,2,4}`) but no `plan.py` rung emits either — encodable by hand, not planner-reachable |
| 7 | `launchFleetMission(...)` 7-arg (with `speed_pct`) | **Not planner-reachable at all.** `tick.py`'s encoder selects this overload only `if action.speed_pct is not None` (`tick.py:442-443`), and nothing in `models.py`/`plan.py`/`candidates.py` ever sets `Action.speed_pct` — every planner-produced `Action` has it `None`. Hand-write the action JSON to exercise this overload. **Confirmed live 2026-08-19** — see §9 |

**Correction to Transport's reachability claim (2026-08-19).** The line above used to read
"Transport additionally needs ≥2 owned planets" as if that were purely a planner-level heuristic
(`generate_transport_candidates`, `candidates.py:1913-1930`, requiring at least one other owned
planet with coordinates). It's more than a heuristic: the **contract itself** requires the
Transport/Deploy target to be a planet the sender owns — `VeydriftGameplayModule.sol`'s
`_launchFleetMission`, immediately after the `_missionMovement` call:

```solidity
if (missionType == FleetMissionType.Transport || missionType == FleetMissionType.Deploy) {
    _requirePlanetOwner(targetPlanetId);
}
```

Confirmed by reproduction, not just by reading: building a Transport from planet 664 (the
project's own, only planet) to a real third-party-owned planet (id 23) reverted `NotPlanetOwner()`
(selector `0xab2bcfd3`) at both `build`'s gas-estimation step and at `simulate`. Harvest and
Colonize carry **no** such check — the `if` above is scoped to exactly Transport and Deploy. See
`docs/RESEARCH-ADDENDUM.md` §4.3 for the full writeup and §9 below for what this means in
practice: the project's own account can never exercise Transport or Deploy, structurally, until
it owns a second planet — `generate_transport_candidates`'s ≥2-owned-planets precondition is the
contract's own rule, not an overcautious guess at one.

Combat mission types (3, 5-9) are refused unconditionally by `OPERATOR_ALLOWED_MISSION_TYPES` and
`guard.py`'s matching set — do not attempt to construct one; per AGENTS.md §5 that friction is
deliberate, not something this runbook works around.

For selectors 6/7 and for Colonize/Deploy on selector 6, you will need to hand-write
`action.json` — the planner never emits Deploy, Colonize, or a `speed_pct`-bearing action, so there
is no `vd tick` output to copy. Use `walletctl build --action`'s `Action` shape
(`src/tx.ts`'s `Action` interface: `{ function, args, value?, purpose? }`) directly; there is no
`veydrift-agent` shortcut for these two cases.

## 4. Two gotchas that will cost an afternoon otherwise

1. **`getPublicClient()` memoizes on first call.** `src/tx.ts:47-55`'s `_publicClient` singleton is
   constructed once, lazily, from whatever `VEYDRIFT_RPC_URL` resolves to *at that first call* —
   and every subsequent read in the same process reuses it. `export VEYDRIFT_RPC_URL=...` **before**
   the first `walletctl` invocation of a session, not between two invocations in the same shell
   that you expect to hit different endpoints — changing the env var mid-process does not
   re-target reads already bound to the old client. (Each `walletctl` CLI invocation is a fresh
   process, so this mostly bites you if you're driving `tx.ts` functions directly from a long-lived
   script or a test file, rather than from separate CLI calls — but get the export order right
   regardless, since it costs nothing to be safe.)

2. **`/runtime-config` is not governed by `VEYDRIFT_RPC_URL`.** `src/abi.ts:23` hardcodes
   `RUNTIME_CONFIG_URL = "https://api.veydrift.com/runtime-config"` — a real network call to the
   real backend, regardless of what your fork's RPC target is. Both `buildTx` (`src/tx.ts:163-164`,
   for the destination address) and `checkAllowlist` (`src/allowlist.ts:152/180`, re-fetched on
   every send) depend on it. **A fork run still needs the real API reachable — this is not
   hypothetical.** `https://api.veydrift.com/runtime-config` returned `503` at least once during
   the work that produced this provider; a fork run failing for that reason has nothing to do with
   the fork itself. Check reachability first:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://api.veydrift.com/runtime-config
   ```
   Both `buildTx` and `checkAllowlist` accept an injectable `fetchConfig` (`BuildOptions.fetchConfig`
   / `checkAllowlist`'s third argument) if you need to work around an outage in a test — not
   exposed as a CLI flag, so only reachable by calling these functions directly, not through
   `walletctl`.

## 5. Accounts

Both scenarios use the **same address**, `0x224aba5d489675a7bd3ce07786fada466b46fa0f` — the owner
of planet 664 (`homePlanetId: "664"`, per a live `GET /wallet/{addr}/planets` probe). Reusing one
address for both scenarios, rather than finding a second empty account, means both are exactly
reproducible and directly comparable: the only variable between them is the fork's block, not the
account.

- **Advanced/current state** — fork `latest` (or any recent block). Probed 2026-08-17 against
  the contract:

  ```bash
  GAME=0xf397910F005151b09644228573a4353818D3755d
  cast call $GAME "buildingLevel(uint256,uint8)(uint16)" 664 <buildingId> --rpc-url <base-rpc>
  cast call $GAME "technologyLevel(address,uint8)(uint16)" 0x224aba5d489675a7bd3ce07786fada466b46fa0f <techId> --rpc-url <base-rpc>
  ```

  | Building | Level | | Technology | Level |
  | --- | --- | --- | --- | --- |
  | Metal Mine | 10 | | Energy Technology (id 0) | 2 |
  | Crystal Mine | 9 | | | |
  | Deuterium Synthesizer | 5 | | | |
  | Solar Plant | 11 | | | |
  | Robotics Factory | 2 | | | |
  | Shipyard | 1 | | | |
  | Research Lab | 1 | | | |

  **Prefer the API's effective level over a raw `buildingLevel()` read when predicting what the
  chain will do next — this is a real finding from the first fork run, not a guess.** The raw
  contract read above showed Robotics Factory **2**; `GET /wallet/{addr}/planets` reported **3**.
  Neither was wrong: the contract had a completed-but-unsettled Robotics 2→3 upgrade sitting in
  its queue (lazy settlement — the level in storage doesn't advance until *something* touches the
  planet), while the API resolves and reports the *effective* level as if settled. Sending our own
  `startBuildingUpgrade(664, 0)` on the fork settled the pending Robotics upgrade first as a side
  effect, and the Metal Mine build duration the contract then computed used Robotics **3**,
  confirmed by `calc.build_seconds(robotics=3, ...)` matching the observed 1556s exactly (`calc.build_seconds(robotics=2, ...)` does not).
  So: a raw `buildingLevel()` call can under-report a level that is about to apply the moment you
  send anything, and an expectation set from that raw read will be wrong by exactly one completed
  upgrade. Use the API's `/planets`/`/research` routes to know what level a transaction will
  actually be costed/timed against; use a raw `cast call` only when you specifically want to see
  whether something is still sitting unsettled in the queue.

  This is the account this project is built around, played **by hand through the game UI** — not
  a stranger's wallet, and not one this codebase drives: nothing here has ever submitted a
  transaction (`docs/SPEC.md` §11, `README.md`'s status section). Because a human plays it,
  levels drift over real time regardless of anything in this repo, and a pending unsettled
  upgrade can exist at any moment as shown above. Re-probe before relying on a specific level as a
  test precondition, and treat the table as "known non-zero, known shape" rather than a frozen
  fixture. Pin `--fork-block-number` for any run whose numbers you intend to write down.

- **A second, temporarily-impersonated account, for Transport/Deploy only** — added 2026-08-19
  (round 2, §9). Planet 664 is the project's own account's *only* planet (`homePlanetId: "664"`),
  and §9's Transport/Deploy finding above means that account can never satisfy
  `_requirePlanetOwner(targetPlanetId)` for those two mission types — there is no second owned
  planet to name as a valid target. Exercising selectors 6/7 for Transport therefore needs a real
  account that owns ≥2 planets. `GET /highscores` was used to find one:
  `0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1`, 10 owned planets. This is the same sanctioned
  impersonation technique as the primary account above — no real key is ever touched
  (`VEYDRIFT_FORK_IMPERSONATE_ADDRESS` set to this address instead), and it's harmless to the real
  player since nothing leaves the local fork. Origin planet 23 (`2:477:7`) → target planet 184
  (`2:477:3`), both owned by this account, were the mission endpoints used — see §9.

- **Zero (or near-zero) state** — the *same* address, at a fork pinned to
  `--fork-block-number 50108632` or a small number of blocks after. That is the block of this
  planet's own `PlanetStarted` event (`transactionHash
  0x210493a5b19ae7e38badbcf13af9a7f97638c7028a0de5435a5cdcf46128bd8e`, confirmed via the same
  `/planets` probe) — the earliest block at which planet 664 exists at all under this address, and
  therefore the earliest point from which its building/research levels can be exercised from
  scratch without first needing the planet to be settled on the fork.

  **Creating a fresh address on the fork instead of using this one does not work.**
  `settleFirstPlanet`/`startPlanet` are `payable` on the deployed contract, and `checkAllowlist`
  refuses any transaction with `tx.value !== 0n` unconditionally (`references/tx-safety.md`'s
  allowlist check 3) — there is no tier or provider that can get a payable call past that check.
  A brand-new impersonated address has no planet and no way to acquire one through this engine.
  Pinning an *existing* settled address to a block at or shortly after its own settlement is the
  only route to a reproducible near-zero state.

## 6. Time travel for queue completion

After sending a build/research/ship/defense action:

```bash
# via cast, or client.request directly -- jump the fork's clock past queue completion
cast rpc anvil_increaseTime <seconds> --rpc-url http://127.0.0.1:8545
cast rpc anvil_mine --rpc-url http://127.0.0.1:8545
```

`<seconds>` is the queue's own `durationSeconds` — read it directly from the live API response for
the action you just sent (e.g. `GET /wallet/{addr}/research?planetId=664` reports
`"durationSeconds"` per technology alongside `"cost"`; `/infrastructure` and `/shipyard` do the
same for buildings and ships/defense), or independently recompute it via
`veydrift_agent.calc.build_seconds`/`ship_seconds`/`research_seconds` and confirm the two agree
(`vd calc verify` already does exactly this cross-check for three entities against live data,
AGENTS.md §8).

Then observe settlement through **`simulate`, never `send`**:

```bash
walletctl build --action collect.json --out collect-tx.json   # collectResources(planetId)
walletctl simulate --tx collect-tx.json --from "$VEYDRIFT_FORK_IMPERSONATE_ADDRESS"
```

`collectResources` is on `NONPAYABLE_READ_FUNCTIONS` (`src/abi.ts:204-211`) — it is `nonpayable` in
the ABI because it lazily settles state before returning, not because it is meant to be a
transaction. `sendTx` refuses it outright at any tier, correctly; `simulate` (`eth_call` +
`estimateGas`, never broadcast) is the only sanctioned way to invoke it.

## 7. The e2e test suite's own env var

`tests/providers/fork-impersonate.test.ts` has an unconditional suite (loopback guard, address
validation — no fork needed) and a skip-gated e2e suite that spawns a real, ephemeral `anvil`
process. That suite is gated on **both** `anvil` being installed **and**
`VEYDRIFT_FORK_TEST_RPC_URL` being set (the upstream RPC anvil forks from in the test) — absent
either, `npm test` stays green and fully offline, matching `tests/selectors.cast.test.ts`'s existing
optional-local-binary pattern. That test impersonates Anvil's default account #0
(`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`, a well-known public throwaway key/address, already
excluded from `envkey.ts`'s leak scanner and hardcoded in `tests/providers.test.ts`) and sends a
plain ETH transfer — it proves the impersonate → setBalance → eth_sendTransaction → receipt
plumbing works without depending on any live Veydrift game state. It is a separate, smaller check
from everything in §3-6 above, which exercises real game selectors against real game state.

## 8. Four verifications worth doing beyond the per-selector sweep

Each of these is a pass/fail finding in its own right — a formula or encoder that has only ever
been checked against itself, not against the contract's actual behavior. **Round 2 (2026-08-19,
§9) closed or extended three of the four** — 8.1, 8.2, and 8.3 below now carry a "Round 2" result
alongside the original plan; 8.4 was already closed in round 1 and is unchanged here.

### 8.1 Colony-target packing

`tick.py`'s `_encode_colony_target` (`tick.py:307-345`) has no public decoder anywhere on the ABI —
the only way to check it is end-to-end. Before sending a Colonize `launchFleetMission`:

```bash
cast call <gameContractAddress> 'isCoordinateAvailable(uint16,uint16,uint8)(bool)' <g> <s> <p> --rpc-url http://127.0.0.1:8545
cast call <gameContractAddress> 'coordinateKey(uint16,uint16,uint8)(bytes32)' <g> <s> <p> --rpc-url http://127.0.0.1:8545
cast call <gameContractAddress> 'occupiedCoordinates(bytes32)(bool)' <key-from-above> --rpc-url http://127.0.0.1:8545
```

(`<gameContractAddress>` is `config.gameContractAddress` from a live `/runtime-config` fetch, or
read straight off `walletctl status`'s `game contract:` line.) Send the Colonize mission, then
re-run all three. Confirm `isCoordinateAvailable` flips `true → false` and `occupiedCoordinates`
for *that exact key* flips `false → true` — not just that some slot changed, but that the specific
`(galaxy, system, position)` the action targeted is the one that settled.

**Round 2 result — the packing math is now closed, the slot-claim behavior is still open.** Read
the deployed encoder/decoder directly, `VeydriftColonizationModule.sol:472-490`:

```solidity
function _encodeColonyTarget(uint16 galaxy, uint16 system, uint8 position) ... {
    return COLONIZATION_COORDINATE_FLAG | (uint256(galaxy) << COLONIZATION_GALAXY_SHIFT)
        | (uint256(system) << COLONIZATION_SYSTEM_SHIFT) | uint256(position);
}
function _decodeColonyTarget(uint256 target) ... {
    galaxy = ((target >> COLONIZATION_GALAXY_SHIFT) & COLONIZATION_COORDINATE_MASK).toUint16();
    system = ((target >> COLONIZATION_SYSTEM_SHIFT) & COLONIZATION_COORDINATE_MASK).toUint16();
    position = (target & COLONIZATION_POSITION_MASK).toUint8();
}
```

`tick.py:292-304`'s `_COLONIZATION_COORDINATE_FLAG = 1 << 255`, `_COLONIZATION_GALAXY_SHIFT = 24`,
`_COLONIZATION_SYSTEM_SHIFT = 8` match this exactly. Round-trip tested in Python (reimplementing
`_decodeColonyTarget`'s exact shifts/masks against the source above, not just checked for
self-consistency with the encoder) for `7:181:14`, `2:477:3`, `1:1:1`, and the boundary case
`65535:65535:255` — all round-tripped exactly.

**Not done, and stated plainly: no real Colonize send was completed**, so the three `cast call`s
above were never actually re-run against a before/after state. Neither the project's own account
nor the round-2 impersonated multi-planet account (`0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1`,
§9) owns a Colony Ship — checked directly, `shipCount(planetId, 3)` returns 0 across every planet
checked for both accounts — and producing one needs its own multi-step unlock chain (Shipyard ≥4,
Impulse Drive ≥3) not pursued this round. So: the packing/unpacking math is verified against
source; whether `isCoordinateAvailable`/`occupiedCoordinates` actually flip for a well-formed
target on send remains unverified. This is the honest remaining gap in this section, not a claim
that Colonize is done.

### 8.2 The two fleet-tuple encoders

`tick.py`'s `_ship_counts_to_fleet_tuple` (`tick.py:347-370`) and `fleet.ts`'s
`shipCountsToFleetTuple` (`fleet.ts:101-129`) are currently cross-checked only against each other
(`test_tier_map_agrees_with_the_wallet_engines_allowlist` and friends compare source, not
on-chain behavior). Send a distinctive, asymmetric ship composition — e.g. `{ Destroyer: 3,
SmallCargo: 1 }`, nothing else — as a Transport, then read back what the contract actually recorded
for that mission (the fleet-mission indexer route, or `cast call` on the mission struct) and
confirm Destroyer landed at tuple index 9, not 10 (`fleet.ts:63`'s comment; `fleet.test.ts` pins
this in isolation, but never against a real contract response).

**Round 2 result — confirmed on two independent axes.**

1. **Real transactions**: decoded the raw calldata of both live Transport sends in §9 directly —
   not the built tx's `args` (which merely echo what was passed in), the actual on-chain `data`
   bytes, byte-offset-decoded. `smallCargo` landed at tuple index 0, `largeCargo` at index 4,
   matching the ABI's own named tuple component order exactly, with every other slot correctly
   zero. Neither real send touched a ship id above 8, so neither exercised the 9-13 shift itself.
2. **The Destroyer shift specifically**: built (not sent — no chain interaction is needed to prove
   an encoding claim, so this used `walletctl build` only) a synthetic action with `destroyer: 7`
   and every other ship 0, then decoded the raw calldata: **the value `7` landed at tuple index 9,
   exactly.** This is the direct, conclusive confirmation of the trap `AGENTS.md` §7 (trap #1) has
   documented and defended against since the tech-tree work.

### 8.3 The fuel formula

`guard.py`'s `_derive_fleet_mission_spend` (`guard.py:604-661`) has never been compared against
reality. Record the origin planet's deuterium balance immediately before sending a Transport, send
it, then compare the real post-send balance against the formula's predicted fuel cost. Note that
`_LOCAL_HARVEST_DISTANCE = 5` (`guard.py:584`) is a **fixed stand-in with no source citation** —
used only for same-planet Harvest missions, where `calc.distance` is undefined for two identical
coordinates. A mismatch there specifically is a finding about an unverified constant, not a test
bug — if it disagrees with the real fuel spend, that is exactly what this runbook exists to catch,
and it should be fixed at the source (`guard.py`), not patched around here.

**Round 2 result — the balance-delta method described above is unreliable; use the event
instead.** The balance-delta approach was tried first and produced a noisy, non-matching result
(~1 deuterium observed vs. calc predicting 8) — the delta is contaminated by production accruing
in the real-time gap between the before/after reads, so it is not a clean measurement and should
not be relied on for this check. The **corrected method**: `launchFleetMission` itself emits
`event FleetMissionCargo(uint256 indexed missionId, uint128 metal, uint128 crystal, uint128
deuterium, uint128 fuelCost)` (`VeydriftGameStorage.sol:602-608`) — the authoritative on-chain
figure, decoded directly from the transaction receipt's logs, not inferred from a balance read.

For the selector-6 Transport in §9 (origin `2:477:7` → target `2:477:3`, `{smallCargo: 2,
largeCargo: 1}`, distance 1020): **event `fuelCost = 10`**. Predicted via `calc.py`, using the
impersonated player's own real drive-tech levels (Combustion Drive 6, Impulse Drive 6, Hyperspace
Drive 7 — read live via `technologyLevel(address,uint8)`, not assumed):

```python
sc_cap, sc_fuel, sc_speed = calc.ship_movement_stats(Ship.SMALL_CARGO, combustion=6, impulse=6, hyperdrive=7)  # (5000, 20, 22000)
lc_cap, lc_fuel, lc_speed = calc.ship_movement_stats(Ship.LARGE_CARGO, combustion=6, impulse=6, hyperdrive=7)  # (25000, 50, 12000)
distance = calc.distance("2:477:7", "2:477:3")  # 1020
fuel = calc.mission_fuel([(sc_fuel, 2, sc_speed), (lc_fuel, 1, lc_speed)], distance, min(sc_speed, lc_speed), speed_percent=100)
# -> 10
```

**Exact match: predicted 10, chain-emitted 10.** `calc.distance`, `calc.ship_movement_stats`, and
`calc.mission_fuel` are now confirmed correct against a real chain observation — not merely
derived from contract source, for the first time. (The first mismatched attempt also had a real
input error on top of the method problem — impulse/hyperspace drive levels were queried correctly
but not actually passed into the Python call the first time — but the balance-delta method itself
was genuinely the wrong tool here regardless; use the event, not a balance delta, going forward.)

### 8.4 `simulateTx`'s `ok` verdict, capped at the gas that will actually be sent

`simulateTx` (`src/tx.ts`) used to run its `eth_call` uncapped — against the node's block gas
limit, not against `tx.gas` (the number `walletctl build` already estimated and the exact number
`send` submits verbatim). A separate, fresh `estimateGas()` was fetched right after, but only ever
returned as reporting metadata; nothing validated it against `tx.gas`, and nothing replayed the
call capped at it. `simulate` was answering "would this succeed given unlimited gas," not "will
the transaction that actually gets sent succeed" — and, like the defects this fork-testing effort
exists to surface, this is exactly the kind of gap that only shows up against real, accumulated
on-chain state, not a zero-state test account.

Confirmed on this fork, planet 664, real account `0x224aba5d489675a7bd3ce07786fada466b46fa0f`:

1. `walletctl build --action <startResearch action> --provider fork-impersonate --out tx.json`
   produced `tx.gas = 465588`.
2. `walletctl simulate --tx tx.json --from <address>` returned `ok: true` (pre-fix, uncapped call).
3. `walletctl send --tx tx.json --confirm --provider fork-impersonate --tier operator` submitted it
   at gas limit 465588 (`cast tx <hash>` confirmed `gasLimit 465588`, matching `tx.json` exactly).
   **Receipt: `status: "reverted"`.**
4. `cast run <hash>` traced the failure: the call genuinely executed `PlanetSettled`, a
   `completeAttackTargetSnapshotQueues` sweep across several nested delegatecalls, and emitted
   `ResearchQueued` — then hit `[OutOfGas] EvmError: OutOfGas`, reverting the whole transaction.
5. Resending the identical calldata at gas limit 931176 (2x) against the same fork state
   succeeded — `status: 1`, and `technologyLevel` confirmed the research level advanced — proving
   the failure was purely a gas shortfall, not a logic bug.

`startBuildingUpgrade`, `startShipProduction`, and `startDefenseProduction` all succeeded cleanly
earlier in this same fork session with no gas issue; this is not a general defect in every
selector, but a real one for any call whose settlement sweep is wider than `eth_estimateGas`'s
search happened to account for — `startResearch`'s `_settleResearchDue`
(`VeydriftPlanetManagementModule.sol:330`) pulls in a wide sweep when multiple things are due at
once. `simulateTx` now caps its `eth_call` at `tx.gas` (falling back to, and validating against, a
fresh estimate when `tx.gas` isn't yet known) — see `references/tx-safety.md`'s new section for the
mechanism, `tests/tx.test.ts`'s `simulateTx` block for the regression coverage, and
`CHANGELOG.md`'s `[Unreleased]` entry for the fix itself.

## 9. Round 2 (2026-08-19) — all 7 selectors, live on a pinned fork

Everything below ran against a local Anvil fork of Base at a pinned block, against the real
deployed contract. Every number is real, not illustrative — same standard as §5/§8 above.

### 9.1 The 5 selectors reachable from the project's own account

The project's own account (`0x224aba5d489675a7bd3ce07786fada466b46fa0f`, planet 664) was used for
these:

1. `startBuildingUpgrade` — already documented (round 1, §8.4 and `AGENTS.md` §10), `status:
   success`.
2. `startResearch` — already documented (round 1, the simulate-gas-cap finding, §8.4), `status:
   success` once the `simulateTx` gas cap fixed above was in place.
3. `startShipProduction` — Solar Satellite (Ship id 9) qty 1. `status: success`.
4. `startDefenseProduction` — Rocket Launcher (Defense id 0) qty 1. `status: success`.
5. `resolveFleetMission` — **not live-exercised.** This account has never flown a fleet mission
   (`GET /wallet/{addr}/fleet-visibility` returns empty `incoming`/`outgoing`/`returning`), so
   there is no real mission id to resolve on the fork. Confirmed **by source instead**:
   `VeydriftColonizationModule.sol:237-240` —
   ```solidity
   _requireGameNotPaused();
   FleetMission storage mission = _fleetMissions[missionId];
   if (mission.status != FleetMissionStatus.Outbound) return;
   ```
   an invalid/nonexistent mission id silently no-ops rather than reverting, by design
   (permissionless, safe against garbage input). This is verified by reading source, **not** by a
   real send — there was no real mission to resolve, so don't read this row as "live-executed."

### 9.2 Selectors 6/7 — a second, impersonated account

`startShipProduction`/`startDefenseProduction` above needed nothing beyond the project's own
account, but Transport (selector 6/7's live mission type) structurally cannot be exercised from
that account — see the Transport/Deploy ownership finding in §3 above: planet 664 is the account's
only planet, and the contract requires the Transport/Deploy *target* to also be an owned planet.
A different real, multi-planet player, `0x4e15e6643964f1a3d3a5af82d7683b9a30553aa1` (10 owned
planets, found via `GET /highscores`), was temporarily impersonated instead — the same sanctioned
technique this whole runbook is built on (§5's second account bullet); no real key is ever touched,
and it's harmless to the impersonated account since nothing leaves the local fork.

6. `launchFleetMission` 6-arg (Transport) — origin planet 23 (`2:477:7`) → target planet 184
   (`2:477:3`), ships `{smallCargo: 2, largeCargo: 1}`, cargo `{deuterium: 5000}`. `status:
   success`. **First real fleet mission ever launched by this codebase.**
7. `launchFleetMission` 7-arg (explicit `speedPercent`) — same origin/target, `{smallCargo: 1}`,
   `{deuterium: 1000}`, `speedPercent: 50`. `status: success`.

### 9.3 What this closes, and what it doesn't

All 7 allowlisted selectors (`ECONOMY_SIGNATURES` plus both `LAUNCH_FLEET_MISSION_SIGNATURES`
overloads, §3 above) have now been either live-sent on a fork or, for `resolveFleetMission`
specifically, confirmed correct by source where no real mission existed to exercise it live. What
remains untouched by this system, mainnet included: **mainnet itself** — nothing here has ever
submitted a transaction to the real chain (`docs/SPEC.md` §11, `README.md`'s status section) — and
Colonize's slot-claiming behavior specifically (§8.1's remaining gap: the mission-type encoding is
covered by this section's general Transport/7-arg confirmation, since Colonize shares the same two
overloads, but no Colonize mission was actually sent this round, so whether a well-formed target
actually flips `isCoordinateAvailable`/`occupiedCoordinates` on send is still open).
