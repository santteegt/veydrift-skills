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
 *
 * `send` without --confirm exits non-zero and prints the transaction it *would* have sent.
 * No env var or flag makes --confirm implicit.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { Command } from "commander";
import { formatEther, getAddress } from "viem";
import {
  computePinnedAbiHash,
  fetchLiveRuntimeConfig,
  loadPinnedMeta,
  RUNTIME_CONFIG_URL,
  verifyAbi,
} from "./abi.js";
import { TIERS, type Tier } from "./allowlist.js";
import { AVAILABLE_PROVIDERS, getProvider } from "./providers/index.js";
import { resolveTier as resolvePolicyTier, TierResolutionError } from "./policy.js";
import {
  buildTx,
  describeTx,
  getPublicClient,
  getReceipt,
  getRpcUrl,
  sendTx,
  simulateTx,
  SendRefusedError,
  type Action,
} from "./tx.js";
import type { UnsignedTx } from "./providers/types.js";

const bigintReplacer = (_key: string, value: unknown): unknown =>
  typeof value === "bigint" ? value.toString() : value;

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

interface StoredTx {
  to: string;
  data: string;
  value: string;
  chainId: number;
  gas?: string;
  /** wei per gas unit, live from the chain when `build` fetched it. `null` (never a guessed
   *  number, never omitted) if the fetch failed. See src/tx.ts's `fetchMaxFeePerGas`. */
  maxFeePerGas?: string | null;
  /** gas * maxFeePerGas -- the field the Python guard's wei-denominated gas ceilings compare
   *  against. `null` whenever either input is missing. */
  estimatedCostWei?: string | null;
  purpose?: string;
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
      const [balance, config] = await Promise.all([
        client.getBalance({ address }),
        fetchLiveRuntimeConfig().catch((err: Error) => {
          console.error(`(warning) could not fetch live ${RUNTIME_CONFIG_URL}: ${err.message}`);
          return undefined;
        }),
      ]);
      const meta = loadPinnedMeta();
      const pinnedHash = computePinnedAbiHash();

      console.log(`provider:        ${provider.name}`);
      console.log(`address:         ${address}`);
      console.log(`rpcUrl:          ${getRpcUrl()}`);
      console.log(`chainId:         8453 (Base)`);
      console.log(`balance:         ${formatEther(balance)} ETH`);
      console.log(`pinned ABI hash: ${pinnedHash}`);
      console.log(`pinned commit:   ${meta.commit}`);
      if (config) {
        const liveHash = config.backend?.build?.deploymentAbiHash;
        console.log(`live ABI hash:   ${liveHash}`);
        console.log(`ABI pin match:   ${liveHash === pinnedHash ? "MATCH" : "*** MISMATCH -- see verify-abi ***"}`);
        console.log(`game contract:   ${config.gameContractAddress ?? config.contractAddress}`);
      }
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
  .description(`Compare the pinned ABI hash to live ${RUNTIME_CONFIG_URL}. Exits 1 on drift.`)
  .action(async () => {
    try {
      const result = await verifyAbi();
      console.log(`pinned commit:          ${result.pinnedCommit}`);
      console.log(`pinned ABI hash:        ${result.pinnedHash}`);
      console.log(`live deploymentCommit:  ${result.liveDeploymentCommit}`);
      console.log(`live deploymentAbiHash: ${result.liveHash}`);
      console.log(`commit match:           ${result.commitMatch}`);
      console.log(`ABI hash match:         ${result.match}`);
      if (!result.match) {
        console.error(
          "\nABI HASH MISMATCH. Treat every write path as unsafe until re-pinned. " +
            "See references/abi-pinning.md for the rebuild recipe.",
        );
        process.exitCode = 1;
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

      const out: StoredTx = {
        to: built.to,
        data: built.data,
        value: built.value.toString(),
        chainId: built.chainId,
        gas: built.gas?.toString(),
        maxFeePerGas: built.maxFeePerGas !== undefined ? built.maxFeePerGas.toString() : null,
        estimatedCostWei: built.estimatedCostWei !== undefined ? built.estimatedCostWei.toString() : null,
        purpose: built.purpose,
      };
      const json = JSON.stringify({ ...out, functionName: built.functionName, signature: built.signature }, null, 2);
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
  .action(async (opts: { tx: string; from?: string }) => {
    try {
      const { tx } = loadTxFile(opts.tx);
      const from = opts.from ? getAddress(opts.from) : undefined;
      const result = await simulateTx(tx, { from });
      console.log(`function:      ${result.functionName ?? `(unknown selector ${tx.data.slice(0, 10)})`}`);
      console.log(`ok:            ${result.ok}`);
      if (result.ok) {
        console.log(`estimated gas:    ${result.gas ?? "(unavailable)"}`);
        console.log(`maxFeePerGas:     ${result.maxFeePerGas ?? "(unavailable)"}`);
        console.log(`estimatedCostWei: ${result.estimatedCostWei ?? "(unavailable)"}`);
        console.log(`return data:      ${result.returnData ?? "0x"}`);
      } else {
        console.log(`revert reason: ${result.revertReason}`);
        process.exitCode = 1;
      }
    } catch (err) {
      console.error(`simulate failed: ${(err as Error).message}`);
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

    try {
      const provider = getProvider({ provider: opts.provider });
      const hash = await sendTx(tx, { tier, confirm: true, provider });
      console.log(`\nSUBMITTED: ${hash}`);
    } catch (err) {
      if (err instanceof SendRefusedError) {
        console.error(`\nREFUSED: ${err.message}`);
      } else {
        console.error(`\nsend failed: ${(err as Error).message}`);
      }
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
