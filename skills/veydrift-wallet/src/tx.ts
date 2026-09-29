/**
 * Transaction building, simulation, sending and receipts. This module owns the RPC client and is
 * the only place that turns an `Action` (a proposed call, e.g. from the veydrift-agent skill)
 * into calldata, and the only place a signed transaction is actually broadcast.
 *
 * `send` is intentionally the narrowest function here: it refuses to run without an explicit
 * `confirm: true`, refuses the six nonpayable-but-semantically-read functions outright (trap #3),
 * and always re-runs the allowlist (allowlist.ts) regardless of what already validated the tx --
 * defense in depth means this module does not trust its own callers.
 */

import {
  decodeFunctionData,
  encodeFunctionData,
  formatEther,
  getAddress,
  toFunctionSignature,
  type AbiParameter,
} from "viem";
import {
  describeRevert,
  fetchLiveRuntimeConfig,
  functionsForSelector,
  isNonpayableRead,
  resolveFunctionAbi,
  type Contract,
  type RuntimeConfig,
} from "./abi.js";
import { checkAllowlist, type Tier } from "./allowlist.js";
import { checkOnchainPin, needsDependencyPin, type OnchainPinResult } from "./onchain-pin.js";
import type { UnsignedTx, WalletProvider } from "./providers/types.js";
import { briefError, getPublicClient, type VeydriftPublicClient } from "./rpc.js";
import { checkEffectivePlayer, type EffectivePlayerCheck } from "./signer-binding.js";

export type { UnsignedTx, WalletProvider } from "./providers/types.js";
// The RPC helpers live in rpc.ts (so onchain-pin.ts can read the chain without a runtime import
// cycle); re-exported here so every existing `from "./tx.js"` import keeps working.
export { DEFAULT_RPC_URL, getPublicClient, getRpcUrl, type VeydriftPublicClient } from "./rpc.js";

// ---------------------------------------------------------------------------------------------
// Action -> calldata
// ---------------------------------------------------------------------------------------------

/** A proposed call. `function` may be a bare name (only valid when unambiguous on the pinned
 *  ABI) or a full canonical signature (required for overloaded functions such as
 *  launchFleetMission -- trap #2). `args` are positional, matching the ABI's declared input
 *  order; numbers/strings are coerced to the right JS type for encoding. */
export interface Action {
  function: string;
  args: unknown[];
  /** Which pinned contract `function` resolves against, and which live address `buildTx` sends
   *  the call to. Defaults to `"game"` -- every action JSON written before the alliance feature
   *  (including any hand-written manual-override file already in the wild) keeps building
   *  exactly the same transaction it always did, unchanged. The caller (`tick.py`'s
   *  `_action_to_walletctl_json`) always knows which contract it's targeting, so this is an
   *  explicit field here rather than something `buildTx` infers from the function name -- a
   *  same-named function on both ABIs (none exist today) would otherwise be ambiguous at the
   *  one point where ambiguity actually matters for tx safety. */
  contract?: Contract;
  /** wei, decimal string. Defaults to "0". Every reachable action here is non-payable. */
  value?: string;
  /** Human-readable rationale, carried through to the built tx and printed by `send`. */
  purpose?: string;
}

function coerceAbiValue(type: string, value: unknown, components?: readonly AbiParameter[]): unknown {
  const fixedArrayMatch = /^(.*)\[(\d*)\]$/.exec(type);
  if (fixedArrayMatch) {
    const innerType = fixedArrayMatch[1] as string;
    if (!Array.isArray(value)) {
      throw new Error(`expected an array for ABI type "${type}", got ${JSON.stringify(value)}`);
    }
    return value.map((v) => coerceAbiValue(innerType, v, components));
  }
  if (type === "tuple") {
    if (!components) throw new Error(`tuple type is missing "components" in the ABI entry`);
    if (Array.isArray(value)) {
      return components.map((c, i) =>
        coerceAbiValue(c.type, value[i], (c as AbiParameter & { components?: AbiParameter[] }).components),
      );
    }
    if (value && typeof value === "object") {
      const obj = value as Record<string, unknown>;
      return components.map((c) =>
        coerceAbiValue(c.type, obj[c.name ?? ""], (c as AbiParameter & { components?: AbiParameter[] }).components),
      );
    }
    throw new Error(`expected an array or object for tuple type, got ${JSON.stringify(value)}`);
  }
  if (/^u?int\d*$/.test(type)) {
    if (typeof value === "bigint") return value;
    if (typeof value === "number" || typeof value === "string") return BigInt(value);
    throw new Error(`cannot coerce ${JSON.stringify(value)} to ABI type "${type}"`);
  }
  // address, bool, string, bytes* -- pass through as-is.
  return value;
}

export interface BuildOptions {
  /** Sender address, used only for a best-effort gas estimate. Omit to skip estimation (build
   *  still succeeds -- `gas` is simply absent; `simulate`/`send` will estimate fresh). */
  from?: `0x${string}`;
  client?: VeydriftPublicClient;
  fetchConfig?: () => Promise<RuntimeConfig>;
}

export interface BuiltTx extends UnsignedTx {
  purpose?: string;
  functionName: string;
  signature: string;
  /** Set when a `from` was supplied but estimation still failed (e.g. the call would revert) --
   *  surfaced so the CLI can print a warning instead of silently guessing a gas limit. */
  gasEstimateError?: string;
  /** wei per gas unit, fetched live from the chain (EIP-1559 `maxFeePerGas`, falling back to a
   *  legacy `getGasPrice()` if the chain/RPC doesn't support fee-history estimation). `undefined`
   *  only when neither could be fetched -- see `feeEstimateError`. Never a guessed/defaulted
   *  value, and never zero unless the chain itself genuinely reported zero. */
  maxFeePerGas?: bigint;
  /** Set when neither estimateFeesPerGas nor getGasPrice could be fetched. */
  feeEstimateError?: string;
  /** gas * maxFeePerGas -- the field the wei-denominated gas ceilings (gas_per_tx_wei /
   *  gas_per_day_wei) actually compare against. `undefined` whenever either input is missing --
   *  this engine never guesses or defaults to zero for a value it did not measure. */
  estimatedCostWei?: bigint;
}

/** Live `maxFeePerGas`, wei per gas unit. Tries EIP-1559 fee-history estimation first (what Base
 *  actually uses), falls back to legacy `getGasPrice()` for a chain/RPC that doesn't support it.
 *  Never guesses, never defaults to zero -- returns `undefined` (with `.error` set) if both fail,
 *  so callers can surface `null` rather than a fabricated number. */
async function fetchMaxFeePerGas(
  client: VeydriftPublicClient,
): Promise<{ maxFeePerGas?: bigint; error?: string }> {
  try {
    const fees = await client.estimateFeesPerGas();
    if (fees.maxFeePerGas !== undefined) return { maxFeePerGas: fees.maxFeePerGas };
  } catch {
    // fall through to legacy gas price.
  }
  try {
    return { maxFeePerGas: await client.getGasPrice() };
  } catch (err) {
    return { error: briefError(err) };
  }
}

/**
 * Extra gas, in basis points of the node's estimate (10_000 = none), applied to `gas` for the
 * functions whose estimate has proven too tight to send at. An `OutOfGas` revert at exactly the
 * estimated limit is a known failure class here (see `references/tx-safety.md`); `simulate` now
 * catches it, but a batch is the function most exposed -- every order re-settles the planet, and
 * its cost grows with the order count -- so it gets margin up front. The unused part of the limit
 * is refunded; only the ceiling comparison (`gas * maxFeePerGas`) sees the larger figure, which
 * errs toward escalating.
 *
 * 15_000 for a batch: on a fork at a pinned state, a 15-order batch used 3,228,680 gas against an
 * estimate of 3,342,917 and succeeded at a limit of 1.00x the estimate but not at 0.99x
 * (`references/fork-testing.md` §14), so the estimate is tight but sufficient there. The 1.5x
 * covers state that moves between estimating and inclusion, which the fork cannot show. Change it
 * from measurement, not by feel.
 */
export const GAS_HEADROOM_BPS: Readonly<Record<string, number>> = {
  startProductionBatch: 15_000,
};

export async function buildTx(action: Action, opts: BuildOptions = {}): Promise<BuiltTx> {
  const contract: Contract = action.contract ?? "game";
  const fn = resolveFunctionAbi(action.function, contract);
  const coercedArgs = fn.inputs.map((input, i) =>
    coerceAbiValue(input.type, action.args[i], (input as AbiParameter & { components?: AbiParameter[] }).components),
  );
  const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: coercedArgs });

  const fetchConfig = opts.fetchConfig ?? fetchLiveRuntimeConfig;
  const config = await fetchConfig();
  const toRaw =
    contract === "alliance"
      ? config.allianceContractAddress
      : (config.gameContractAddress ?? config.contractAddress);
  if (!toRaw) {
    throw new Error(
      contract === "alliance"
        ? "live /runtime-config has no allianceContractAddress"
        : "live /runtime-config has no gameContractAddress/contractAddress",
    );
  }
  const to = getAddress(toRaw);
  const chainId = config.chainId ?? 8453;
  const value = action.value ? BigInt(action.value) : 0n;

  let gas: bigint | undefined;
  let gasEstimateError: string | undefined;
  let maxFeePerGas: bigint | undefined;
  let feeEstimateError: string | undefined;

  // Only touch the network (and thus only construct/use a client) when the caller gave us
  // something to query with -- matches the existing "no from -> build still succeeds, gas is
  // simply absent" contract, now extended to the fee fields.
  if (opts.from || opts.client) {
    const client = opts.client ?? getPublicClient();
    if (opts.from) {
      try {
        gas = await client.estimateGas({ account: opts.from, to, data, value });
        const headroomBps = GAS_HEADROOM_BPS[fn.name];
        if (headroomBps !== undefined) gas = (gas * BigInt(headroomBps)) / 10_000n;
      } catch (err) {
        // A revert during estimation carries the raw custom-error data; decode it against the
        // pinned errors so the reason reads `InsufficientResources(...)` rather than viem's
        // generic "unknown reason". Non-revert failures (RPC down) keep their own message.
        const described = describeRevert(err);
        gasEstimateError = described.decoded ? described.message : briefError(err);
      }
    }
    const fee = await fetchMaxFeePerGas(client);
    maxFeePerGas = fee.maxFeePerGas;
    feeEstimateError = fee.error;
  }

  const estimatedCostWei = gas !== undefined && maxFeePerGas !== undefined ? gas * maxFeePerGas : undefined;

  return {
    to,
    data,
    value,
    chainId,
    gas,
    gasEstimateError,
    maxFeePerGas,
    feeEstimateError,
    estimatedCostWei,
    purpose: action.purpose,
    functionName: fn.name,
    signature: toFunctionSignature(fn),
  };
}

/** The on-disk shape `build --out` writes and `send`/`simulate --tx` read back
 *  (`loadTxFile`, `cli.ts`). Kept here, next to `BuiltTx`, so the two stay in sync by
 *  construction rather than by two files agreeing to update together. */
export interface StoredTx {
  to: string;
  data: string;
  value: string;
  chainId: number;
  gas?: string;
  /** wei per gas unit, live from the chain when `build` fetched it. `null` (never a guessed
   *  number, never omitted) if the fetch failed. */
  maxFeePerGas?: string | null;
  /** gas * maxFeePerGas -- the field the Python guard's wei-denominated gas ceilings compare
   *  against. `null` whenever either input is missing. */
  estimatedCostWei?: string | null;
  /** The actual reason `gas`/`estimatedCostWei` are missing, when an estimate was
   *  genuinely attempted and failed (e.g. the call would revert) -- `null` for the benign
   *  case (no provider configured, so no estimate was ever attempted). Previously surfaced
   *  only as a `console.error` warning on `build`'s own stderr and never written here,
   *  which left `tick.py`'s consumer (and, downstream, `guard.py`'s `gas` gate) with no way
   *  to see *why* an estimate was missing -- see `veydrift-agent`'s `_walletctl_build`. */
  gasEstimateError?: string | null;
  /** Same as `gasEstimateError`, for a failed live `maxFeePerGas`/`getGasPrice` fetch
   *  (an RPC issue, not a revert) -- the other way `estimatedCostWei` ends up `null`. */
  feeEstimateError?: string | null;
  /** The on-chain pin verdict (`checkOnchainPin`) taken when this tx was built, so the agent's
   *  `abi_hash` gate judges the same moment the calldata was produced and needs no second
   *  `walletctl` call per tick. `null` only when the check could not run at all, which every
   *  consumer must treat as a failure. */
  onchainPin?: OnchainPinResult | null;
  purpose?: string;
  functionName?: string;
  signature?: string;
}

/** `BuiltTx` -> `StoredTx`, the exact mapping `build --out` writes. A pure function so it
 *  can be unit-tested directly against `buildTx`'s documented `undefined`/`null`
 *  conventions, rather than only indirectly through a full CLI invocation. */
export function toStoredTx(built: BuiltTx, onchainPin?: OnchainPinResult | null): StoredTx {
  return {
    to: built.to,
    data: built.data,
    value: built.value.toString(),
    chainId: built.chainId,
    gas: built.gas?.toString(),
    maxFeePerGas: built.maxFeePerGas !== undefined ? built.maxFeePerGas.toString() : null,
    estimatedCostWei: built.estimatedCostWei !== undefined ? built.estimatedCostWei.toString() : null,
    gasEstimateError: built.gasEstimateError ?? null,
    feeEstimateError: built.feeEstimateError ?? null,
    onchainPin: onchainPin ?? null,
    purpose: built.purpose,
    functionName: built.functionName,
    signature: built.signature,
  };
}

// ---------------------------------------------------------------------------------------------
// Decoding / display -- shared by `build`, `simulate` and `send` printouts.
// ---------------------------------------------------------------------------------------------

export interface TxDisplay {
  to: `0x${string}`;
  functionName?: string;
  signature?: string;
  args?: unknown[];
  value: bigint;
  valueEth: string;
  estimatedGas?: bigint;
  gasPriceWei?: bigint;
  estimatedCostEth?: string;
  purpose?: string;
}

export async function describeTx(
  tx: UnsignedTx,
  opts: { purpose?: string; client?: VeydriftPublicClient } = {},
): Promise<TxDisplay> {
  const to = getAddress(tx.to);
  const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
  const fn = functionsForSelector(selector)[0];

  let functionName: string | undefined;
  let signature: string | undefined;
  let args: unknown[] | undefined;
  if (fn) {
    functionName = fn.name;
    signature = toFunctionSignature(fn);
    try {
      const decoded = decodeFunctionData({ abi: [fn], data: tx.data });
      args = decoded.args as unknown[] | undefined;
    } catch {
      // leave args undefined; the raw hex is still shown by the caller.
    }
  }

  const client = opts.client ?? getPublicClient();
  let estimatedGas = tx.gas;
  if (!estimatedGas) {
    estimatedGas = await client.estimateGas({ to, data: tx.data, value: tx.value }).catch(() => undefined);
  }
  let gasPriceWei: bigint | undefined;
  try {
    gasPriceWei = await client.getGasPrice();
  } catch {
    // best-effort; cost display simply omits it.
  }
  const estimatedCostEth =
    estimatedGas !== undefined && gasPriceWei !== undefined
      ? formatEther(estimatedGas * gasPriceWei)
      : undefined;

  return {
    to,
    functionName,
    signature,
    args,
    value: tx.value,
    valueEth: formatEther(tx.value),
    estimatedGas,
    gasPriceWei,
    estimatedCostEth,
    purpose: opts.purpose,
  };
}

// ---------------------------------------------------------------------------------------------
// Simulate -- eth_call + estimateGas. Surfaces reverts instead of throwing opaquely. This is the
// *only* sanctioned way to invoke the six nonpayable-but-semantically-read functions (trap #3).
// ---------------------------------------------------------------------------------------------

export interface SimulateResult {
  ok: boolean;
  gas?: bigint;
  /** wei per gas unit, live from the chain -- see `BuiltTx.maxFeePerGas`. Only fetched (and thus
   *  only ever set) on a successful simulation, matching `gas` above. */
  maxFeePerGas?: bigint;
  /** gas * maxFeePerGas. `undefined` whenever either input is missing -- never guessed. */
  estimatedCostWei?: bigint;
  returnData?: `0x${string}`;
  revertReason?: string;
  /** Set when the revert's raw data decoded against a pinned custom error (e.g.
   *  `InsufficientResources`); `revertReason` then reads `Name(args) (reverted)`. */
  errorName?: string;
  errorArgs?: string[];
  /** The raw revert payload, when the RPC returned one. */
  revertData?: `0x${string}`;
  functionName?: string;
}

export async function simulateTx(
  tx: UnsignedTx,
  opts: { from?: `0x${string}`; client?: VeydriftPublicClient } = {},
): Promise<SimulateResult> {
  const client = opts.client ?? getPublicClient();
  const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
  const fn = functionsForSelector(selector)[0];

  try {
    // The `ok` verdict must reflect what `send` will actually submit, not an unlimited-gas
    // hypothetical. Every provider passes `tx.gas` to the chain verbatim (`providers/keystore.ts`,
    // `envkey.ts`, `fork-impersonate.ts` all set `gas: tx.gas`) -- so a call that only succeeds
    // with more gas than that is not a call that will succeed when actually sent. This was
    // confirmed live on an Anvil fork of Base: a `startResearch` call whose settlement sweep
    // was wider than `eth_estimateGas` accounted for simulated `ok: true` uncapped, was sent at
    // the estimated gas limit (465588), and reverted `OutOfGas` -- see references/tx-safety.md.
    //
    // When `tx.gas` isn't known yet (`build` ran without `--from`, or its own estimate failed),
    // fall back to a fresh estimate here and validate the call against *that* figure instead --
    // never leave the call uncapped (AGENTS.md §5: a guardrail must not pass vacuously on absent
    // data). If that fallback estimate itself fails, the failure propagates as `ok: false` below
    // rather than falling through to an uncapped call: a call `eth_estimateGas` can't even
    // estimate for is already evidence it wouldn't succeed, and "cannot verify" must never
    // become "assume it's fine."
    const gasLimit =
      tx.gas ?? (await client.estimateGas({ account: opts.from, to: tx.to, data: tx.data, value: tx.value }));

    const callResult = await client.call({
      account: opts.from,
      to: tx.to,
      data: tx.data,
      value: tx.value,
      gas: gasLimit,
    });
    // A separate, fresh estimate -- kept as the source for the `gas`/`estimatedCostWei`
    // reporting fields (consumed downstream by guard.py's `gas`/`eth_floor` gates), independent
    // of whatever gas figure the call above was capped at.
    const gas = await client
      .estimateGas({ account: opts.from, to: tx.to, data: tx.data, value: tx.value })
      .catch(() => undefined);
    const { maxFeePerGas } = await fetchMaxFeePerGas(client);
    const estimatedCostWei = gas !== undefined && maxFeePerGas !== undefined ? gas * maxFeePerGas : undefined;
    return { ok: true, gas, maxFeePerGas, estimatedCostWei, returnData: callResult.data, functionName: fn?.name };
  } catch (err) {
    const described = describeRevert(err);
    return {
      ok: false,
      revertReason: described.message,
      errorName: described.decoded?.errorName,
      errorArgs: described.decoded?.errorArgs,
      revertData: described.data,
      functionName: fn?.name,
    };
  }
}

// ---------------------------------------------------------------------------------------------
// Send -- the sole submission path. Refuses without confirm, refuses nonpayable-read functions,
// re-runs the allowlist unconditionally.
// ---------------------------------------------------------------------------------------------

/** Thrown for every refusal that happens before `provider.signAndSend` is invoked -- nothing was
 *  signed or broadcast. */
export class SendRefusedError extends Error {}

/** Thrown when `provider.signAndSend` itself fails. The failure may have happened before or after
 *  the transaction reached the network, so the caller must not assume it was never broadcast --
 *  check the sender's nonce (`getNonces`) before sending anything else. */
export class BroadcastUncertainError extends Error {}

export interface SendOptions {
  tier: Tier;
  confirm: boolean;
  provider: WalletProvider;
  /** The address the provider must sign as (`policy.json`'s `signer`, else its `wallet`, via
   *  `resolveExpectedSigner`), or `null` when there is no policy to bind against. Required, not
   *  optional, so no caller can skip the check by omission. */
  expectedAddress: `0x${string}` | null;
  /** The game player the signer must act as on-chain (`policy.json`'s `wallet`). Omitted means "the
   *  same address as `expectedAddress`" -- the non-delegated case -- so a caller cannot weaken the
   *  binding by leaving it out. Ignored when `expectedAddress` is `null`. */
  expectedPlayer?: `0x${string}` | null;
  fetchConfig?: () => Promise<RuntimeConfig>;
  /** Injectable for tests; forwarded to `checkAllowlist`'s own option of the same name. See
   *  `allowlist.ts`'s doc comment for why this is resolved lazily rather than eagerly. */
  resolveAllowCombat?: () => boolean;
  /** Injectable for tests; forwarded to `checkAllowlist`'s own option of the same name -- the
   *  alliance-feature counterpart to `resolveAllowCombat` above, same lazy-resolution rationale. */
  resolveAllowAlliance?: () => boolean;
  /** Injectable for tests; forwarded to `checkAllowlist` (`allow_acs_defense`). */
  resolveAllowAcsDefense?: () => boolean;
  /** Injectable for tests; forwarded to `checkAllowlist` (`allow_delegation`, for `revokeDelegate`). */
  resolveAllowDelegation?: () => boolean;
  /** Injectable for tests; defaults to the real `checkEffectivePlayer` (an on-chain `eth_call`). */
  checkEffectivePlayer?: (o: { signer: string; player: string }) => Promise<EffectivePlayerCheck>;
  /** Injectable for tests; defaults to the real `checkOnchainPin`. Runs LAST, right before signing
   *  (it is the only network-dependent refusal, so every cheap local refusal happens first). */
  checkOnchainPin?: (o: { to: string; verifyCode: boolean }) => Promise<OnchainPinResult>;
}

export async function sendTx(tx: UnsignedTx, opts: SendOptions): Promise<`0x${string}`> {
  if (!opts.confirm) {
    throw new SendRefusedError(
      "refusing to send without --confirm. No env var or flag makes --confirm implicit.",
    );
  }

  const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
  const fn = functionsForSelector(selector)[0];
  if (fn && isNonpayableRead(fn.name)) {
    throw new SendRefusedError(
      `refusing to send "${fn.name}": it is ABI-nonpayable but semantically a read (it lazily ` +
        `settles state before returning). Use "walletctl simulate" instead -- sending it would ` +
        `pay gas for a read.`,
    );
  }

  let signerAddress: `0x${string}` | undefined;
  if (opts.expectedAddress !== null) {
    try {
      signerAddress = await opts.provider.getAddress();
    } catch (err) {
      throw new SendRefusedError(`could not derive the provider's signer address: ${briefError(err)}`);
    }
    if (signerAddress.toLowerCase() !== opts.expectedAddress.toLowerCase()) {
      throw new SendRefusedError(
        `signer address mismatch: provider "${opts.provider.name}" signs as ${signerAddress}, but policy.json's ` +
          `${opts.expectedPlayer && opts.expectedPlayer.toLowerCase() !== opts.expectedAddress.toLowerCase() ? "signer" : "wallet"} ` +
          `is ${opts.expectedAddress}. The transaction was planned and simulated for the policy ` +
          `wallet; fix the provider key or the policy before sending.`,
      );
    }
  }

  const allow = await checkAllowlist(tx, opts.tier, {
    fetchConfig: opts.fetchConfig,
    resolveAllowCombat: opts.resolveAllowCombat,
    resolveAllowAlliance: opts.resolveAllowAlliance,
    resolveAllowAcsDefense: opts.resolveAllowAcsDefense,
    resolveAllowDelegation: opts.resolveAllowDelegation,
  });
  if (!allow.ok) {
    throw new SendRefusedError(`allowlist rejected this transaction: ${allow.reason}`);
  }

  // The signer must act as the policy wallet ON-CHAIN, not merely share its address: the game's
  // delegation lets a separate key act as an owner, and a wallet that is itself someone's delegate
  // would act as that other account. Network-dependent, so it runs after every local refusal, and
  // fail-closed like the pin check below.
  if (opts.expectedAddress !== null && signerAddress !== undefined) {
    const player = opts.expectedPlayer ?? opts.expectedAddress;
    let acting: EffectivePlayerCheck;
    try {
      acting = await (opts.checkEffectivePlayer ?? ((o) => checkEffectivePlayer(o)))({ signer: signerAddress, player });
    } catch (err) {
      throw new SendRefusedError(`could not verify which player the signer acts as: ${briefError(err)}`);
    }
    if (!acting.ok) {
      throw new SendRefusedError(`signer binding failed: ${acting.problem ?? "the signer does not act as the policy wallet"}`);
    }
  }

  // The on-chain pin: the contract system behind the pinned proxies must still be the one this
  // skill was pinned against. Read from the chain (never the backend), fail-closed, and every
  // failure path -- including the RPC being down -- is a refusal: an ordinary throw here would be
  // reported as "send failed", which the agent must treat as a possible broadcast.
  let pin: OnchainPinResult;
  try {
    pin = await (opts.checkOnchainPin ?? ((o) => checkOnchainPin(o)))({ to: tx.to, verifyCode: true });
  } catch (err) {
    throw new SendRefusedError(`could not verify the on-chain pin before signing: ${briefError(err)}`);
  }
  if (!pin.ok) {
    throw new SendRefusedError(
      `on-chain pin check failed -- the deployed contracts differ from what this skill is pinned to ` +
        `(re-pin per references/abi-pinning.md): ${pin.problems.join("; ")}`,
    );
  }
  if (needsDependencyPin(fn?.name, pin) && !pin.dependenciesOk) {
    throw new SendRefusedError(
      `refusing "${fn?.name}": a contract it calls (Randomness engine / Moon system) differs from the pin ` +
        `-- ${pin.problems.join("; ")}`,
    );
  }

  try {
    return await opts.provider.signAndSend(tx);
  } catch (err) {
    throw new BroadcastUncertainError(
      `${briefError(err)} -- the transaction may or may not have been broadcast; check the ` +
        `sender's nonce before sending again.`,
    );
  }
}

export interface Nonces {
  /** Transactions from this address mined as of the latest block. */
  latest: number;
  /** `latest` plus this address's transactions the node holds in its mempool. */
  pending: number;
  /** The latest block number these were read at. */
  blockNumber: bigint;
}

/** The sender's mined and pending transaction counts. A nonce `n` is consumed on-chain once
 *  `latest > n`; it is in flight while `pending > n >= latest`. */
export async function getNonces(
  address: `0x${string}`,
  opts: { client?: VeydriftPublicClient } = {},
): Promise<Nonces> {
  const client = opts.client ?? getPublicClient();
  const blockNumber = await client.getBlockNumber();
  const [latest, pending] = await Promise.all([
    client.getTransactionCount({ address, blockNumber }),
    client.getTransactionCount({ address, blockTag: "pending" }),
  ]);
  return { latest, pending, blockNumber };
}

// ---------------------------------------------------------------------------------------------
// Receipt -- the only place `receipt.status` is read and turned into a verdict the Python guard
// can trust. Never synthesize "success": if the receipt can't be fetched, or reports a status
// this engine doesn't recognize, this throws rather than returning something guessed.
// ---------------------------------------------------------------------------------------------

export interface TxReceipt {
  status: "success" | "reverted";
  blockNumber: bigint;
  gasUsed: bigint;
  effectiveGasPrice: bigint;
  /** gasUsed * effectiveGasPrice -- the actual wei cost paid, as opposed to `estimatedCostWei`
   *  (build/simulate's pre-flight guess). Never omitted, never guessed. */
  actualCostWei: bigint;
  transactionHash: `0x${string}`;
  to: `0x${string}` | null;
  from: `0x${string}`;
  [key: string]: unknown;
}

export async function getReceipt(
  hash: `0x${string}`,
  opts: { client?: VeydriftPublicClient } = {},
): Promise<TxReceipt> {
  const client = opts.client ?? getPublicClient();
  const receipt = await client.getTransactionReceipt({ hash });
  if (receipt.status !== "success" && receipt.status !== "reverted") {
    // Should be unreachable against a real EIP-658+ chain (Base included) -- viem already
    // normalizes the on-chain 0x0/0x1 status byte to these two strings. Refuse rather than pass
    // through an unrecognized value the Python guard might otherwise treat as truthy/successful.
    throw new Error(
      `unrecognized receipt.status "${String((receipt as { status?: unknown }).status)}" for ${hash} -- ` +
        "refusing to report a transaction outcome this engine cannot classify as success or reverted.",
    );
  }
  return { ...receipt, actualCostWei: receipt.gasUsed * receipt.effectiveGasPrice };
}
