/**
 * `keystore` provider -- the default. An encrypted EIP-2335/geth-format JSON keystore, decrypted
 * via `ethers.Wallet.fromEncryptedJson` (scrypt+AES is not something to hand-roll -- SPEC.md §3).
 *
 * Path from VEYDRIFT_KEYSTORE, password from VEYDRIFT_KEYSTORE_PASSWORD or an interactive,
 * non-echoing prompt. The password is never accepted as a CLI flag (so it can never land in
 * argv, shell history, or `ps`), never logged, and the decrypted private key lives only in the
 * local scope of `signAndSend` -- it is never assigned to `this` or otherwise held past the
 * signing call.
 */

import { existsSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { Wallet } from "ethers";
import { createWalletClient, getAddress, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";
import { getRpcUrl } from "../tx.js";
import type { ProviderCapabilities, UnsignedTx, WalletProvider } from "./types.js";

function readKeystorePath(env: NodeJS.ProcessEnv): string {
  const path = env.VEYDRIFT_KEYSTORE;
  if (!path) {
    throw new Error(
      '"keystore" provider selected but VEYDRIFT_KEYSTORE is not set (path to an encrypted ' +
        "EIP-2335/geth JSON keystore file).",
    );
  }
  if (!existsSync(path)) {
    throw new Error(`VEYDRIFT_KEYSTORE points to "${path}", which does not exist.`);
  }
  return path;
}

/** Standard geth/EIP-2335 keystores carry the address in cleartext at the top level, so the
 *  address can be read without decrypting or prompting for a password at all. */
export function keystoreAddress(keystoreJson: string): `0x${string}` {
  const parsed = JSON.parse(keystoreJson) as { address?: string };
  if (!parsed.address) {
    throw new Error(
      'Keystore JSON has no top-level "address" field -- not a recognized geth/EIP-2335 keystore.',
    );
  }
  const withPrefix = parsed.address.startsWith("0x") ? parsed.address : `0x${parsed.address}`;
  return getAddress(withPrefix);
}

/** A masked (non-echoing) stdin prompt -- the standard Node recipe of intercepting the
 *  readline interface's internal output writer. Requires a TTY; if stdin isn't one (e.g. piped
 *  input in CI), fails fast with a message pointing at VEYDRIFT_KEYSTORE_PASSWORD instead of
 *  hanging forever waiting for input that will never come. */
export async function promptPassword(promptText: string): Promise<string> {
  if (!process.stdin.isTTY) {
    throw new Error(
      `${promptText.trim()} cannot be prompted interactively (stdin is not a TTY). ` +
        "Set VEYDRIFT_KEYSTORE_PASSWORD instead.",
    );
  }
  return new Promise((resolve, reject) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    const rlInternals = rl as unknown as { _writeToOutput: (str: string) => void };
    let masked = false;
    rlInternals._writeToOutput = (str: string) => {
      if (!masked) process.stdout.write(str);
    };
    process.stdout.write(promptText);
    masked = true;
    rl.question("", (answer) => {
      masked = false;
      rl.close();
      process.stdout.write("\n");
      resolve(answer);
    });
    rl.on("error", reject);
  });
}

export class KeystoreProvider implements WalletProvider {
  readonly name = "keystore";
  private readonly keystoreJson: string;
  private readonly address: `0x${string}`;
  private readonly env: NodeJS.ProcessEnv;

  constructor(env: NodeJS.ProcessEnv = process.env) {
    this.env = env;
    const path = readKeystorePath(env);
    this.keystoreJson = readFileSync(path, "utf8");
    this.address = keystoreAddress(this.keystoreJson);
  }

  async getAddress(): Promise<`0x${string}`> {
    return this.address;
  }

  async signAndSend(tx: UnsignedTx): Promise<`0x${string}`> {
    const password = this.env.VEYDRIFT_KEYSTORE_PASSWORD ?? (await promptPassword("Keystore password: "));
    // Decrypted key material is local to this call only -- never stored on `this`, never logged.
    const wallet = await Wallet.fromEncryptedJson(this.keystoreJson, password);
    const account = privateKeyToAccount(wallet.privateKey as `0x${string}`);
    const client = createWalletClient({ account, chain: base, transport: http(getRpcUrl()) });
    return client.sendTransaction({
      to: tx.to,
      data: tx.data,
      value: tx.value,
      chainId: tx.chainId,
      gas: tx.gas,
    });
  }

  capabilities(): ProviderCapabilities {
    return { canSign: true, canSimulate: false, remotePolicy: false };
  }
}
