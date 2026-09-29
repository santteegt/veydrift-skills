/**
 * Proof that the key about to sign really acts as the wallet the transaction was planned for.
 *
 * `sendTx` used to require signer == `policy.wallet`. The game contract's single-wallet delegation
 * lets a separate (delegate) key act as an owner, so the honest invariant is no longer "same
 * address" but "the signer's *effective player* is `policy.wallet`". `effectivePlayer(signer)` is a
 * view on the game proxy: the delegate's main wallet if the signer is a registered delegate, else
 * the signer itself. Comparing it to the policy wallet covers every case in one call:
 *
 *   - signer == wallet, no delegation involved      -> effectivePlayer(wallet) == wallet   (ok)
 *   - signer is a delegate registered for `wallet`  -> effectivePlayer(signer) == wallet   (ok)
 *   - signer is a delegate of a DIFFERENT main       -> effectivePlayer(signer) == other   (refuse)
 *   - a delegate that was never registered / revoked -> effectivePlayer(signer) == signer  (refuse)
 *   - `wallet` is itself someone's delegate          -> effectivePlayer(wallet) == other   (refuse):
 *     its own transactions would act as that other account.
 *
 * Fail-closed: an unreadable chain is a refusal, never a pass. This is a point-in-time read at send
 * time; a revoke or re-point landing between this check and inclusion is not excluded. The effect is
 * bounded -- the delegate then acts as itself, so planet-scoped calls revert `NotPlanetOwner` and
 * cost only gas -- except calls with no planet argument (alliance membership), which would execute as
 * the delegate address's own (empty) account.
 */

import { getAddress } from "viem";
import { loadPinnedMeta, resolveFunctionAbi } from "./abi.js";
import { getPublicClient, type VeydriftPublicClient } from "./rpc.js";

export type EffectivePlayerClient = Pick<VeydriftPublicClient, "readContract">;

export interface EffectivePlayerCheck {
  ok: boolean;
  signer: string;
  /** The player the signer must act as (`policy.wallet`). */
  expectedPlayer: string;
  /** What the chain says the signer acts as; `null` when it could not be read. */
  effectivePlayer: string | null;
  /** Why it is not ok. */
  problem?: string;
}

export async function checkEffectivePlayer(opts: {
  signer: string;
  player: string;
  client?: EffectivePlayerClient;
}): Promise<EffectivePlayerCheck> {
  const client = opts.client ?? getPublicClient();
  const base = { signer: opts.signer, expectedPlayer: opts.player };
  const gameProxy = loadPinnedMeta("game").implementation?.proxy;
  if (!gameProxy) {
    return { ...base, ok: false, effectivePlayer: null, problem: "the pin records no game proxy address to read effectivePlayer from" };
  }
  let effective: string;
  try {
    effective = (await client.readContract({
      address: getAddress(gameProxy),
      abi: [resolveFunctionAbi("effectivePlayer(address)")],
      functionName: "effectivePlayer",
      args: [getAddress(opts.signer)],
    })) as string;
  } catch (err) {
    const e = err as { shortMessage?: string; message?: string };
    const reason = (e.shortMessage ?? e.message ?? String(err)).split("\n")[0]!.trim();
    return { ...base, ok: false, effectivePlayer: null, problem: `could not read effectivePlayer(${opts.signer}): ${reason}` };
  }
  if (effective.toLowerCase() !== opts.player.toLowerCase()) {
    return {
      ...base,
      ok: false,
      effectivePlayer: effective,
      problem:
        `signer ${opts.signer} acts as ${effective} on-chain, not as the policy wallet ${opts.player} ` +
        `(delegate not registered for this wallet, revoked, or registered for another account)`,
    };
  }
  return { ...base, ok: true, effectivePlayer: effective };
}
