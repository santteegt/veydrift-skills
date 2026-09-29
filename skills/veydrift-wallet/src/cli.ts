#!/usr/bin/env node
/**
 * walletctl -- the veydrift-wallet CLI.
 *
 *   walletctl status
 *   walletctl verify-abi
 *   walletctl build   --action a.json
 *   walletctl simulate --tx tx.json
 *   walletctl send    --tx tx.json [--confirm]
 *   walletctl receipt --hash 0x...
 *   walletctl nonce   --address 0x...
 *
 * `send` without --confirm exits non-zero and prints the transaction it *would* have sent.
 * No env var or flag makes --confirm implicit.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { Command } from "commander";
import { formatEther, getAddress } from "viem";
import {
  classifyBackendHash,
  computePinnedAbiHash,
  decodeSimulateReturnData,
  fetchLiveRuntimeConfig,
  loadPinnedMeta,
  RUNTIME_CONFIG_URL,
  verifyAbi,
  type BackendHashStatus,
} from "./abi.js";
import { TIERS, type Tier } from "./allowlist.js";
import { checkOnchainPin, failedOnchainPin, type OnchainPinResult } from "./onchain-pin.js";
import { AVAILABLE_PROVIDERS, getProvider } from "./providers/index.js";
import {
  resolveExpectedWallet,
  resolveTier as resolvePolicyTier,
  TierResolutionError,
  WalletBindingResolutionError,
} from "./policy.js";
import {
  BroadcastUncertainError,
  buildTx,
  describeTx,
  getNonces,
  getPublicClient,
  getReceipt,
  getRpcUrl,
  sendTx,
  simulateTx,
  toStoredTx,
  SendRefusedError,
  type Action,
  type StoredTx,
} from "./tx.js";
import type { UnsignedTx } from "./providers/types.js";

const bigintReplacer = (_key: string, value: unknown): unknown =>
  typeof value === "bigint" ? value.toString() : value;

/** The on-chain pin verdict, never throwing: a check that could not run is a FAILED check (every
 *  consumer fails closed), not an absent one. */
async function onchainPinOrFailed(opts: Parameters<typeof checkOnchainPin>[0]): Promise<OnchainPinResult> {
  try {
    return await checkOnchainPin(opts);
  } catch (err) {
    return failedOnchainPin(`on-chain pin check could not run: ${(err as Error).message}`);
  }
}

function describeBackendStatus(status: BackendHashStatus): string {
  switch (status) {
    case "match":
      return "MATCH (the backend reports the pinned hash)";
    case "known-stale":
      return "KNOWN-STALE (equals the value recorded at pin time; the backend's deployment metadata lags the chain -- advisory only)";
    case "other":
      return "DIFFERENT (advisory only -- the on-chain pin is the authority)";
    default:
      return "(not reported)";
  }
}

/** Resolves the enforcing tier from `$VEYDRIFT_HOME/policy.json`, never from `flag`/`VEYDRIFT_TIER`
 *  alone -- see src/policy.ts. Exits non-zero (never falls back to a permissive default) on a
 *  malformed policy file or a policy/caller tier disagreement. */
function resolveTier(flag: string | undefined): Tier {
  try {
    return resolvePolicyTier({ cliFlag: flag });
  } catch (err) {
    if (err instanceof TierResolutionError) {
      console.error(err.message);
    } else {
      console.error(`tier resolution failed: ${(err as Error).message}`);
    }
    process.exit(4);
  }
}

/** Resolves the address `send` must sign as from `$VEYDRIFT_HOME/policy.json`'s `wallet` (`null`
 *  when no policy file exists). Exits 4, like `resolveTier`, on a malformed policy. */
function resolveWalletBinding(): `0x${string}` | null {
  try {
    return resolveExpectedWallet();
  } catch (err) {
    if (err instanceof WalletBindingResolutionError) {
      console.error(err.message);
    } else {
      console.error(`wallet binding resolution failed: ${(err as Error).message}`);
    }
    process.exit(4);
  }
}

function loadTxFile(path: string): { tx: UnsignedTx; purpose?: string } {
  const raw = JSON.parse(readFileSync(path, "utf8")) as StoredTx;
  const tx: UnsignedTx = {
    to: getAddress(raw.to),
    data: raw.data as `0x${string}`,
    value: BigInt(raw.value ?? "0"),
    chainId: raw.chainId,
    gas: raw.gas ? BigInt(raw.gas) : undefined,
  };
  return { tx, purpose: raw.purpose };
}

const program = new Command();
program
  .name("walletctl")
  .description(
    "Veydrift wallet engine: builds, allowlists and simulates Veydrift game transactions, " +
      "and is the ONLY path in this codebase that ever submits one -- and only on explicit --confirm.",
  )
  .version("0.1.0");

// ---------------------------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------------------------
program
  .command("status")
  .description("Provider, address, chainId, ETH balance and ABI pin state.")
  .option("--provider <name>", `wallet provider (${AVAILABLE_PROVIDERS.join("|")})`)
  .action(async (opts: { provider?: string }) => {
    try {
      const provider = getProvider({ provider: opts.provider });
      const address = await provider.getAddress();
      const client = getPublicClient();
      const [balance, config, pin] = await Promise.all([
        client.getBalance({ address }),
        fetchLiveRuntimeConfig().catch((err: Error) => {
          console.error(`(warning) could not fetch live ${RUNTIME_CONFIG_URL}: ${err.message}`);
          return undefined;
        }),
        onchainPinOrFailed({ verifyCode: true }),
      ]);
      const meta = loadPinnedMeta();
      const pinnedHash = computePinnedAbiHash();

      console.log(`provider:        ${provider.name}`);
      console.log(`address:         ${address}`);
      let policyWallet: `0x${string}` | null | undefined;
      try {
        policyWallet = resolveExpectedWallet();
      } catch (err) {
        console.log(`policy wallet:   *** UNREADABLE -- ${(err as Error).message} ***`);
      }
      if (policyWallet === null) {
        console.log("policy wallet:   (no policy.json -- send will not check the signer address)");
      } else if (policyWallet !== undefined) {
        const match = policyWallet.toLowerCase() === address.toLowerCase();
        console.log(`policy wallet:   ${policyWallet} ${match ? "(MATCH)" : "*** MISMATCH -- send will refuse ***"}`);
      }
      console.log(`rpcUrl:          ${getRpcUrl()}`);
      console.log(`chainId:         8453 (Base)`);
      console.log(`balance:         ${formatEther(balance)} ETH`);
      console.log(`pinned ABI hash: ${pinnedHash}`);
      console.log(`pinned commit:   ${meta.commit}`);
      if (config) {
        const liveHash = config.backend?.build?.deploymentAbiHash ?? "";
        console.log(`backend ABI hash: ${liveHash || "(not reported)"}`);
        console.log(
          `backend vs pin:   ${describeBackendStatus(classifyBackendHash(pinnedHash, liveHash, meta.backendReported?.deploymentAbiHash))}`,
        );
        console.log(`game contract:   ${config.gameContractAddress ?? config.contractAddress}`);
      }
      console.log(
        `on-chain pin:    ${pin.ok ? "MATCH" : "*** MISMATCH -- game writes are unsafe until re-pinned (see verify-abi) ***"}`,
      );
      console.log(`  game impl:     ${pin.game.liveImplementation ?? "(unreadable)"}`);
      console.log(`  alliance impl: ${pin.alliance.liveImplementation ?? "(unreadable)"}`);
      console.log(
        `  dependencies:  ${pin.dependenciesOk ? "MATCH" : "*** MISMATCH -- fleet/missile/resolve are unsafe until re-pinned ***"}`,
      );
      const caps = provider.capabilities();
      console.log(
        `capabilities:    canSign=${caps.canSign} canSimulate=${caps.canSimulate} remotePolicy=${caps.remotePolicy}`,
      );
    } catch (err) {
      console.error(`status failed: ${(err as Error).message}`);
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// verify-abi
// ---------------------------------------------------------------------------------------------
program
  .command("verify-abi")
  .description(
    "Verify the deployed contracts against the pin. The AUTHORITY is the on-chain check (each proxy's " +
      `EIP-1967 implementation, read from the chain); the backend's ${RUNTIME_CONFIG_URL} hash is advisory. ` +
      "Exits 1 when the on-chain pin (or a pinned dependency) has drifted.",
  )
  .option("--json", "emit one JSON object ({ok, dependenciesOk, backend, onchain}) instead of text")
  .action(async (opts: { json?: boolean }) => {
    try {
      const [backend, onchain] = await Promise.all([
        verifyAbi().catch((err: Error) => ({ error: err.message })),
        onchainPinOrFailed({ verifyCode: true }),
      ]);
      const ok = onchain.ok && onchain.dependenciesOk;
      if (opts.json) {
        console.log(JSON.stringify({ ok, dependenciesOk: onchain.dependenciesOk, backend, onchain }, bigintReplacer));
        if (!ok) process.exitCode = 1;
        return;
      }
      const meta = loadPinnedMeta();
      console.log(`pinned commit:            ${meta.commit}`);
      console.log(`pinned ABI hash:          ${computePinnedAbiHash()}`);
      if ("error" in backend) {
        console.log(`backend runtime-config:   (unavailable: ${backend.error}) -- advisory only`);
      } else {
        console.log(`backend deploymentCommit: ${backend.liveDeploymentCommit}`);
        console.log(`backend deploymentAbiHash:${backend.liveHash}`);
        console.log(`backend vs pin:           ${describeBackendStatus(backend.backendStatus)}`);
      }
      const line = (label: string, c: OnchainPinResult["game"]) =>
        console.log(
          `${label} ${c.ok ? "OK      " : "MISMATCH"} proxy ${c.proxy || "?"}  impl ${c.liveImplementation ?? "(unreadable)"}` +
            `${c.ok ? "" : `  (pinned ${c.pinnedImplementation || "?"})`}`,
        );
      line("game proxy:        ", onchain.game);
      line("alliance proxy:    ", onchain.alliance);
      line("randomness engine: ", onchain.randomnessEngine);
      line("moon system:       ", onchain.moonSystem);
      for (const w of onchain.warnings) console.log(`(warning) ${w}`);
      if (!ok) {
        // A chain we could not read is a different problem from a chain that changed: only the latter
        // calls for a re-pin.
        const unreadable = onchain.problems.every((p) => /could not (read|verify|run)/.test(p));
        console.error(
          unreadable
            ? `\nON-CHAIN PIN COULD NOT BE VERIFIED (the chain was unreadable) -- treated as a failure, every write is blocked:`
            : `\nON-CHAIN PIN DRIFT. Treat every write path as unsafe until re-pinned:`,
        );
        for (const p of onchain.problems) console.error(`  - ${p}`);
        if (!unreadable) {
          console.error(
            "\nRe-pin checklist (references/abi-pinning.md): 1) identify the deployed commit and build it with the " +
              "pinned foundry settings; 2) match every implementation/module runtime code to the build; 3) replace " +
              "abi/*.json, PINNED*.json and the guard's PINNED_ABI_HASH together; 4) re-run the suites.",
          );
        }
        process.exitCode = 1;
      } else {
        console.log("\non-chain pin: MATCH");
      }
    } catch (err) {
      console.error(`verify-abi failed: ${(err as Error).message}`);
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// build
// ---------------------------------------------------------------------------------------------
program
  .command("build")
  .description("Build an unsigned transaction from an Action JSON file.")
  .requiredOption("--action <file>", "path to an Action JSON file: { function, args, value?, purpose? }")
  .option("--from <address>", "sender address, used only for a best-effort gas estimate")
  .option("--provider <name>", "derive --from from this provider's address if --from is omitted")
  .option("--out <file>", "write the unsigned tx JSON here instead of stdout")
  .action(async (opts: { action: string; from?: string; provider?: string; out?: string }) => {
    try {
      const action = JSON.parse(readFileSync(opts.action, "utf8")) as Action;

      let from: `0x${string}` | undefined = opts.from ? getAddress(opts.from) : undefined;
      if (!from) {
        try {
          const provider = getProvider({ provider: opts.provider });
          from = await provider.getAddress();
        } catch {
          // No provider configured/available -- build still succeeds, just without a gas estimate.
        }
      }

      const built = await buildTx(action, { from });
      if (built.gasEstimateError) {
        console.error(`(warning) gas estimation failed, "gas" omitted: ${built.gasEstimateError}`);
      }
      if (built.feeEstimateError) {
        console.error(
          `(warning) live fee fetch failed, "maxFeePerGas"/"estimatedCostWei" are null (not ` +
            `guessed, not zero): ${built.feeEstimateError}`,
        );
      }

      // The on-chain pin verdict is folded into the stored tx (fast mode: slot reads only -- the full
      // code-hash comparison runs in `send`/`verify-abi`), so the agent's `abi_hash` gate judges the
      // very moment this calldata was produced. A check that cannot run is recorded as a failure.
      const onchainPin = await onchainPinOrFailed({ verifyCode: false, to: built.to });
      if (!onchainPin.ok) {
        console.error(`(warning) on-chain pin check FAILED: ${onchainPin.problems.join("; ")}`);
      }
      const out: StoredTx = toStoredTx(built, onchainPin);
      const json = JSON.stringify(out, null, 2);
      if (opts.out) {
        writeFileSync(opts.out, json + "\n");
        console.log(`wrote ${opts.out}`);
      } else {
        console.log(json);
      }
    } catch (err) {
      console.error(`build failed: ${(err as Error).message}`);
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// simulate
// ---------------------------------------------------------------------------------------------
program
  .command("simulate")
  .description(
    "eth_call + estimateGas against a built tx.json. Surfaces reverts. The only sanctioned way " +
      "to invoke functions that are ABI-nonpayable but semantically reads.",
  )
  .requiredOption("--tx <file>", "path to an unsigned tx JSON (from `build`)")
  .option("--from <address>", "sender address for the call/estimate")
  .option("--json", "emit a single JSON line ({ok, revertReason, error, decoded}) instead of plain text")
  .action(async (opts: { tx: string; from?: string; json?: boolean }) => {
    try {
      const { tx } = loadTxFile(opts.tx);
      const from = opts.from ? getAddress(opts.from) : undefined;
      const result = await simulateTx(tx, { from });
      const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
      const decoded = result.ok ? decodeSimulateReturnData(selector, result.returnData) : undefined;

      if (opts.json) {
        console.log(
          JSON.stringify(
            {
              ok: result.ok,
              revertReason: result.ok ? null : result.revertReason,
              error: null,
              decoded: decoded ?? null,
            },
            bigintReplacer,
          ),
        );
        if (!result.ok) process.exitCode = 1;
        return;
      }

      console.log(`function:      ${result.functionName ?? `(unknown selector ${tx.data.slice(0, 10)})`}`);
      console.log(`ok:            ${result.ok}`);
      if (result.ok) {
        console.log(`estimated gas:    ${result.gas ?? "(unavailable)"}`);
        console.log(`maxFeePerGas:     ${result.maxFeePerGas ?? "(unavailable)"}`);
        console.log(`estimatedCostWei: ${result.estimatedCostWei ?? "(unavailable)"}`);
        console.log(`return data:      ${result.returnData ?? "0x"}`);
        if (decoded) console.log(`decoded:          ${JSON.stringify(decoded, bigintReplacer)}`);
      } else {
        console.log(`revert reason: ${result.revertReason}`);
        process.exitCode = 1;
      }
    } catch (err) {
      if (opts.json) {
        console.log(JSON.stringify({ ok: false, revertReason: null, error: (err as Error).message, decoded: null }));
      } else {
        console.error(`simulate failed: ${(err as Error).message}`);
      }
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// send
// ---------------------------------------------------------------------------------------------
program
  .command("send")
  .description(
    "Sign and submit a built tx.json. Without --confirm, exits non-zero and prints the " +
      "transaction it would have sent instead of sending it.",
  )
  .requiredOption("--tx <file>", "path to an unsigned tx JSON (from `build`)")
  .option("--confirm", "actually submit. Without this flag, nothing is signed or sent.", false)
  .option("--tier <tier>", `enforcing tier (${TIERS.join("|")}); defaults to $VEYDRIFT_TIER or "advisor"`)
  .option("--provider <name>", `wallet provider (${AVAILABLE_PROVIDERS.join("|")})`)
  .action(async (opts: { tx: string; confirm: boolean; tier?: string; provider?: string }) => {
    const { tx, purpose } = loadTxFile(opts.tx);
    const tier = resolveTier(opts.tier);
    const expectedAddress = resolveWalletBinding();

    const display = await describeTx(tx, { purpose });
    console.log("--- transaction ---");
    console.log(`to (checksummed): ${display.to}`);
    console.log(`function:         ${display.signature ?? `(unknown selector ${tx.data.slice(0, 10)})`}`);
    console.log(
      `args:             ${display.args ? JSON.stringify(display.args, bigintReplacer) : "(could not decode)"}`,
    );
    console.log(`value:            ${display.valueEth} ETH`);
    console.log(`estimated gas:    ${display.estimatedGas ?? "(unavailable)"}`);
    console.log(`estimated cost:   ${display.estimatedCostEth ? `${display.estimatedCostEth} ETH` : "(unavailable)"}`);
    console.log(`purpose:          ${display.purpose ?? "(none provided)"}`);
    console.log(`enforcing tier:   ${tier}`);
    console.log("-------------------");

    if (!opts.confirm) {
      console.error("\nNOT SENT -- pass --confirm to actually submit. No env var or flag makes --confirm implicit.");
      process.exitCode = 1;
      return;
    }

    let provider;
    try {
      provider = getProvider({ provider: opts.provider });
    } catch (err) {
      console.error(`\nREFUSED: could not load wallet provider: ${(err as Error).message}`);
      process.exitCode = 1;
      return;
    }
    try {
      const hash = await sendTx(tx, { tier, confirm: true, provider, expectedAddress });
      console.log(`\nSUBMITTED: ${hash}`);
    } catch (err) {
      // Output markers are part of the contract with veydrift-agent's tick: "REFUSED:" means
      // nothing was signed or broadcast; "BROADCAST UNCERTAIN:" means it may have been.
      if (err instanceof SendRefusedError) {
        console.error(`\nREFUSED: ${err.message}`);
      } else if (err instanceof BroadcastUncertainError) {
        console.error(`\nBROADCAST UNCERTAIN: ${err.message}`);
      } else {
        console.error(`\nsend failed: ${(err as Error).message}`);
      }
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// nonce
// ---------------------------------------------------------------------------------------------
program
  .command("nonce")
  .description("An address's mined (latest) and mempool-inclusive (pending) nonces, as JSON.")
  .requiredOption("--address <address>", "0x-prefixed sender address")
  .action(async (opts: { address: string }) => {
    try {
      const address = getAddress(opts.address);
      const nonces = await getNonces(address);
      console.log(JSON.stringify({ address, ...nonces }, bigintReplacer, 2));
    } catch (err) {
      console.error(`nonce failed: ${(err as Error).message}`);
      process.exitCode = 1;
    }
  });

// ---------------------------------------------------------------------------------------------
// receipt
// ---------------------------------------------------------------------------------------------
program
  .command("receipt")
  .description("Fetch a transaction receipt by hash.")
  .requiredOption("--hash <hash>", "0x-prefixed transaction hash")
  .action(async (opts: { hash: string }) => {
    try {
      const receipt = await getReceipt(opts.hash as `0x${string}`);
      console.log(JSON.stringify(receipt, bigintReplacer, 2));
    } catch (err) {
      console.error(`receipt failed: ${(err as Error).message}`);
      process.exitCode = 1;
    }
  });

await program.parseAsync(process.argv);
