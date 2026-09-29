/**
 * The one place that resolves the RPC URL and owns the shared public client.
 *
 * Lives in its own module (re-exported from `tx.ts`, so every existing import keeps working) so
 * that `onchain-pin.ts` can read chain state without importing `tx.ts` -- `tx.ts` imports
 * `onchain-pin.ts` for the pre-send check, and a runtime import in the other direction would be a
 * cycle.
 */

import { createPublicClient, http } from "viem";
import { base } from "viem/chains";

export const DEFAULT_RPC_URL = "https://mainnet.base.org";

export function getRpcUrl(): string {
  return process.env.VEYDRIFT_RPC_URL?.trim() || DEFAULT_RPC_URL;
}

/** The concrete client type our one createPublicClient call produces. Using this alias (rather
 *  than viem's generic, unparameterized `PublicClient` export) avoids a TS structural-typing trap
 *  where two differently-instantiated `PublicClient<Transport, Chain>` generics are reported as
 *  "unrelated" types even though they're the same shape. */
export type VeydriftPublicClient = ReturnType<typeof createPublicClient<ReturnType<typeof http>, typeof base>>;

let _publicClient: VeydriftPublicClient | undefined;

/** Lazily-constructed singleton public client. Every function that reads the chain also accepts an
 *  optional `client` override so tests can inject a mock instead of touching the real network. */
export function getPublicClient(): VeydriftPublicClient {
  if (!_publicClient) {
    _publicClient = createPublicClient({ chain: base, transport: http(getRpcUrl()) });
  }
  return _publicClient;
}

/** viem's `message` is a multi-line dump (request URL, request body, docs link); its `shortMessage`
 *  is the one-line cause. Errors end up in tick reports, refusal text and log files, and the RPC URL
 *  usually embeds an API key -- so anything that leaves this package goes through here, never through
 *  a raw `.message`. */
export function briefError(err: unknown): string {
  const e = err as { shortMessage?: string; message?: string };
  return (e.shortMessage ?? e.message ?? String(err)).split("\n")[0]!.trim();
}
