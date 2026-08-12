/**
 * `envkey` provider -- testing only. A raw private key from a plaintext environment variable.
 * ethskills ranks this as testing-grade storage (worse than an encrypted keystore, better than
 * committing it to Git). See references/providers.md.
 *
 * Two safety measures beyond the obvious "don't commit it":
 *  - a loud startup warning every time this provider is constructed
 *  - a refusal to start if the key's value is found anywhere in the repo this skill is running
 *    from (best-effort leak detection via `git grep`, not a substitute for not leaking it)
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createWalletClient, http, isHex, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";
import { getRpcUrl } from "../tx.js";
import type { ProviderCapabilities, UnsignedTx, WalletProvider } from "./types.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function findGitRoot(startDir: string): string | undefined {
  let dir = startDir;
  for (let i = 0; i < 10; i++) {
    if (existsSync(join(dir, ".git"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return undefined;
}

export function normalizePrivateKey(raw: string): Hex {
  const trimmed = raw.trim();
  const hex = trimmed.startsWith("0x") || trimmed.startsWith("0X") ? trimmed : `0x${trimmed}`;
  if (!isHex(hex) || hex.length !== 66) {
    throw new Error("VEYDRIFT_PRIVATE_KEY is not a 32-byte (64 hex char) private key");
  }
  return hex.toLowerCase() as Hex;
}

/** Paths excluded from the leak scan: this codebase's own test suites, which legitimately (and
 *  by instruction) hardcode well-known, clearly-marked throwaway test keys such as Anvil's
 *  default account #0. Excluding them means the check still catches a key accidentally pasted
 *  into application code, config, fixtures outside tests/, or a committed .env -- without
 *  permanently tripping over its own test fixtures. */
const LEAK_SCAN_EXCLUDE_PATHSPECS = [":(exclude,glob)**/tests/**", ":(exclude,glob)**/*.test.ts"];

/**
 * Best-effort leak check. If this skill happens to be running from inside a discoverable git
 * working tree, refuse to start when the raw key value (with/without "0x", either hex case)
 * appears in any tracked or untracked-but-not-gitignored file outside a test suite. Deliberately
 * uses `git grep` (not a hand-rolled recursive walk) so it respects .gitignore -- a key sourced
 * from a properly gitignored `.env` is exactly the sanctioned use case and must not trip this
 * check.
 *
 * This cannot catch every leak (the skill may run from outside any git repo once installed via
 * `npx skills add`, and it only scans one repo). It is a defence-in-depth safety net, not the
 * primary control -- the primary control is "never write a key to a tracked file" in the first
 * place.
 */
export function refuseIfKeyLeakedInRepo(key: Hex, startDir: string = __dirname): void {
  const repoRoot = findGitRoot(startDir);
  if (!repoRoot) return; // nothing to check.

  const body = key.slice(2);
  const needles = [key, body, key.toUpperCase(), body.toUpperCase()];

  for (const needle of needles) {
    try {
      const out = execFileSync(
        "git",
        [
          "-C",
          repoRoot,
          "grep",
          "--fixed-strings",
          "--files-with-matches",
          "--untracked",
          "-I",
          "-e",
          needle,
          "--",
          ".",
          ...LEAK_SCAN_EXCLUDE_PATHSPECS,
        ],
        { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
      );
      if (out.trim().length > 0) {
        const files = out.trim().split("\n");
        throw new Error(
          `refusing to start "envkey" provider: VEYDRIFT_PRIVATE_KEY's value was found in ` +
            `${files.length} file(s) under ${repoRoot} (${files.join(", ")}). A key readable ` +
            `from disk in cleartext outside an env var is not a secret -- rotate it and remove ` +
            `it from those files before using this provider again.`,
        );
      }
    } catch (err) {
      const status = (err as { status?: number }).status;
      if (status === 1) continue; // git grep: no matches -- fine, keep checking other needles.
      if (err instanceof Error && err.message.startsWith("refusing to start")) throw err;
      // git missing, or grep failed operationally (e.g. no commits yet): skip rather than block
      // a legitimate testing session on a missing/unusable git binary.
      return;
    }
  }
}

export class EnvKeyProvider implements WalletProvider {
  readonly name = "envkey";
  private readonly account: ReturnType<typeof privateKeyToAccount>;

  constructor(env: NodeJS.ProcessEnv = process.env) {
    const raw = env.VEYDRIFT_PRIVATE_KEY;
    if (!raw) {
      throw new Error('"envkey" provider selected but VEYDRIFT_PRIVATE_KEY is not set.');
    }
    const key = normalizePrivateKey(raw);
    refuseIfKeyLeakedInRepo(key);

    // eslint-disable-next-line no-console
    console.warn(
      '[veydrift-wallet] WARNING: using the "envkey" provider -- a raw private key from a ' +
        "plaintext environment variable. ethskills ranks this as testing-grade storage only. " +
        'Use "keystore" (the default) for anything beyond local testing.',
    );

    this.account = privateKeyToAccount(key);
  }

  async getAddress(): Promise<`0x${string}`> {
    return this.account.address;
  }

  async signAndSend(tx: UnsignedTx): Promise<`0x${string}`> {
    const client = createWalletClient({ account: this.account, chain: base, transport: http(getRpcUrl()) });
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
