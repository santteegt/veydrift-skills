/**
 * Provider registry / factory. Selected by `policy.wallet_engine.provider` (an external caller's
 * concern -- see docs/SPEC.md §5.6), overridable by the `WALLET_PROVIDER` env var, defaulting to
 * `keystore`. Two genuinely working providers (not one real + one stub) is what proves this
 * interface is actually swappable.
 */

import { EnvKeyProvider } from "./envkey.js";
import { KeystoreProvider } from "./keystore.js";
import type { WalletProvider } from "./types.js";

export * from "./types.js";
export { EnvKeyProvider } from "./envkey.js";
export { KeystoreProvider } from "./keystore.js";

export type ProviderName = "keystore" | "envkey";
export const AVAILABLE_PROVIDERS: readonly ProviderName[] = ["keystore", "envkey"];
export const DEFAULT_PROVIDER: ProviderName = "keystore";

export interface GetProviderOptions {
  /** Explicit override, e.g. from policy.wallet_engine.provider or a CLI flag. Takes precedence
   *  over WALLET_PROVIDER, which takes precedence over the "keystore" default. */
  provider?: string;
  env?: NodeJS.ProcessEnv;
}

export function getProvider(opts: GetProviderOptions = {}): WalletProvider {
  const env = opts.env ?? process.env;
  const requested = (opts.provider ?? env.WALLET_PROVIDER ?? DEFAULT_PROVIDER).trim();
  switch (requested) {
    case "keystore":
      return new KeystoreProvider(env);
    case "envkey":
      return new EnvKeyProvider(env);
    default:
      throw new Error(
        `Unknown wallet provider "${requested}". Available: ${AVAILABLE_PROVIDERS.join(", ")}.`,
      );
  }
}
