/**
 * `fork-impersonate` provider -- fork testing only, never production. Runs the *exact* production
 * `sendTx` -> `provider.signAndSend` path against a local Anvil fork, using `anvil_impersonateAccount`
 * + node-trusted `eth_sendTransaction` instead of a locally-held private key.
 *
 * Why impersonation is authorization-identical here (not merely approximate): Veydrift's
 * `_requirePlanetOwner` checks plain `msg.sender`, not a signature scheme. An Anvil node that
 * trusts `eth_sendTransaction` from an impersonated account produces a transaction with the exact
 * same authorization outcome as one signed by that account's real private key -- there is no
 * signature-based check anywhere in the contract that impersonation would fail to satisfy.
 *
 * This is what makes it safe to register in the normal provider registry (`index.ts`) rather than
 * keeping it test-only: the loopback guard below refuses to construct this provider against
 * anything but a local node, and production's `VEYDRIFT_RPC_URL` never resolves to loopback, so
 * `getProvider({ provider: "fork-impersonate" })` is inert outside a local fork by construction.
 *
 * No key material anywhere in this file -- there is nothing to hold. Authorization comes from the
 * node trusting the impersonation call, not from a signature.
 */

import { createTestClient, getAddress, http, isAddress, publicActions, walletActions } from "viem";
import { base } from "viem/chains";
import { getRpcUrl } from "../tx.js";
import type { ProviderCapabilities, UnsignedTx, WalletProvider } from "./types.js";

/** Hostnames this provider will sign against. Deliberately a short allowlist, not a "looks
 *  private" heuristic -- `hostname` for a bracketed IPv6 literal (`http://[::1]:8545`) is
 *  `"[::1]"` in both Node's and browsers' WHATWG `URL` implementation, so both spellings of the
 *  IPv6 loopback address are listed to avoid a bracket-stripping assumption that doesn't hold. */
const LOOPBACK_HOSTNAMES = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

/**
 * Refuses unless `rpcUrl` resolves to a loopback host. This is the safety-critical check that
 * makes registering `fork-impersonate` in the normal provider registry safe: a real
 * `VEYDRIFT_RPC_URL` (Base mainnet, an Alchemy endpoint, anything reachable off-box) is rejected
 * outright, so this provider can never sign against a real chain no matter how it's selected.
 *
 * Deliberately takes the resolved URL as a plain argument rather than reading `process.env` or
 * calling `getRpcUrl()` itself -- callers (the constructor below, or a test) decide what URL is
 * being checked, which is what makes this unit-testable in isolation without env-var gymnastics.
 *
 * Limits, stated plainly (mirrors `envkey.ts`'s `refuseIfKeyLeakedInRepo` in spirit): this only
 * inspects the URL's hostname string. It cannot detect a loopback address reached indirectly (a
 * proxy, a port-forward, a DNS entry that happens to resolve to 127.0.0.1) and is not a network-
 * level sandbox -- it is a fail-closed guard against the ordinary, expected failure mode of
 * `VEYDRIFT_RPC_URL` pointing at a real network. Fails closed: an unparseable URL is refused, not
 * treated as "not obviously remote, so allow it".
 */
export function refuseIfNotLoopback(rpcUrl: string): void {
  let hostname: string;
  try {
    hostname = new URL(rpcUrl).hostname;
  } catch {
    throw new Error(
      `refusing to start "fork-impersonate" provider: RPC URL "${rpcUrl}" is not a parseable URL, ` +
        "so it cannot be confirmed to be a local loopback address.",
    );
  }
  if (LOOPBACK_HOSTNAMES.has(hostname)) return;
  throw new Error(
    `refusing to start "fork-impersonate" provider: RPC target "${rpcUrl}" (host "${hostname}") is ` +
      "not a loopback address. This provider issues node-trusted eth_sendTransaction calls from an " +
      "impersonated account -- that is only safe against a local Anvil fork " +
      "(127.0.0.1 / localhost / ::1), never a real network. Point VEYDRIFT_RPC_URL at a local fork " +
      "before selecting this provider.",
  );
}

function readImpersonateAddress(env: NodeJS.ProcessEnv): `0x${string}` {
  const raw = env.VEYDRIFT_FORK_IMPERSONATE_ADDRESS;
  if (!raw) {
    throw new Error(
      '"fork-impersonate" provider selected but VEYDRIFT_FORK_IMPERSONATE_ADDRESS is not set ' +
        "(the address to impersonate on the local Anvil fork).",
    );
  }
  if (!isAddress(raw)) {
    throw new Error(`VEYDRIFT_FORK_IMPERSONATE_ADDRESS ("${raw}") is not a valid Ethereum address.`);
  }
  return getAddress(raw);
}

/** Gas top-up applied to the impersonated account before sending. Removes a class of fork-testing
 *  flakiness (a fresh fork block, or an account that happens to be low on ETH) even though a real
 *  Veydrift player's account most likely already holds enough ETH to cover gas on its own. */
const GAS_TOP_UP_WEI = 10n ** 20n; // 100 ETH

export class ForkImpersonateProvider implements WalletProvider {
  readonly name = "fork-impersonate";
  private readonly address: `0x${string}`;

  constructor(env: NodeJS.ProcessEnv = process.env) {
    // Loopback guard first, before anything else -- including before the address is even read.
    refuseIfNotLoopback(getRpcUrl());
    this.address = readImpersonateAddress(env);
  }

  async getAddress(): Promise<`0x${string}`> {
    return this.address;
  }

  async signAndSend(tx: UnsignedTx): Promise<`0x${string}`> {
    // Constructed fresh per call (not the `tx.ts` singleton) so this always targets whatever
    // `VEYDRIFT_RPC_URL` currently resolves to -- matching `keystore.ts`/`envkey.ts`'s own
    // per-call `createWalletClient(..., http(getRpcUrl()))` pattern.
    const client = createTestClient({ chain: base, mode: "anvil", transport: http(getRpcUrl()) })
      .extend(publicActions)
      .extend(walletActions);

    await client.impersonateAccount({ address: this.address });
    await client.setBalance({ address: this.address, value: GAS_TOP_UP_WEI });

    // `account` is a plain address (not a viem LocalAccount), so viem treats it as a JSON-RPC
    // account and sends `eth_sendTransaction` for the node to sign -- never a local signature.
    // Anvil auto-mines, so the returned hash is immediately confirmable.
    return client.sendTransaction({
      account: this.address,
      to: tx.to,
      data: tx.data,
      value: tx.value,
      gas: tx.gas,
    });
  }

  /** Honest, not copied from `keystore`/`envkey`: this provider never produces a signature (the
   *  node signs on impersonation's behalf), so `canSign` is false even though it does broadcast a
   *  transaction. A genuinely new provider category -- node-trusted, unsigned -- not a third
   *  instance of the existing `{ canSign: true, canSimulate: false, remotePolicy: false }` triple. */
  capabilities(): ProviderCapabilities {
    return { canSign: false, canSimulate: false, remotePolicy: false };
  }
}
