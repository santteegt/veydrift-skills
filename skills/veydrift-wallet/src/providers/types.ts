/**
 * The wallet provider interface. Two implementations exist: `keystore` (default, encrypted
 * EIP-2335/geth JSON keystore) and `envkey` (testing only, raw private key from env). Both are
 * genuinely functional so the interface is proven swappable, not just declared so.
 *
 * Spec: docs/SPEC.md §6.3.
 */

/** An unsigned transaction ready for a provider to sign and broadcast. */
export interface UnsignedTx {
  to: `0x${string}`;
  data: `0x${string}`;
  /** wei */
  value: bigint;
  chainId: number;
  /** gas limit, if pre-estimated. Providers that omit it must estimate before sending. */
  gas?: bigint;
}

export interface ProviderCapabilities {
  /** Can this provider produce a signature at all? */
  canSign: boolean;
  /** Can this provider run a local/remote simulation before signing? (Neither provider here
   *  does its own simulation -- that's `tx.ts`'s job via the public RPC -- so this is always
   *  false for both `keystore` and `envkey`. Reserved for a future remote-policy provider.) */
  canSimulate: boolean;
  /** Does signing consult a remote policy engine (e.g. a hosted MPC/HSM approval flow)?
   *  False for both local providers here. */
  remotePolicy: boolean;
}

export interface WalletProvider {
  readonly name: string;
  getAddress(): Promise<`0x${string}`>;
  signAndSend(tx: UnsignedTx): Promise<`0x${string}`>;
  capabilities(): ProviderCapabilities;
}
