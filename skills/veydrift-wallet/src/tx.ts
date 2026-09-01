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
  createPublicClient,
  decodeFunctionData,
  encodeFunctionData,
  formatEther,
  getAddress,
  http,
  toFunctionSignature,
  type AbiParameter,
} from "viem";
import { base } from "viem/chains";
import {
  fetchLiveRuntimeConfig,
  functionsForSelector,
  isNonpayableRead,
  resolveFunctionAbi,
  type Contract,
  type RuntimeConfig,
} from "./abi.js";
import { checkAllowlist, type Tier } from "./allowlist.js";
import type { UnsignedTx, WalletProvider } from "./providers/types.js";

export type { UnsignedTx, WalletProvider } from "./providers/types.js";

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

/** Lazily-constructed singleton public client. Every function below also accepts an optional
 *  `client` override so tests can inject a mock instead of touching the real network. */
export function getPublicClient(): VeydriftPublicClient {
  if (!_publicClient) {
    _publicClient = createPublicClient({ chain: base, transport: http(getRpcUrl()) });
  }
  return _publicClient;
}

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
    return { error: (err as Error).message };
  }
}

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
      } catch (err) {
        gasEstimateError = (err as Error).message;
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
    const message = (err as { shortMessage?: string; message?: string }).shortMessage ?? (err as Error).message;
    return { ok: false, revertReason: message, functionName: fn?.name };
  }
}

// ---------------------------------------------------------------------------------------------
// Send -- the sole submission path. Refuses without confirm, refuses nonpayable-read functions,
// re-runs the allowlist unconditionally.
// ---------------------------------------------------------------------------------------------

export class SendRefusedError extends Error {}

export interface SendOptions {
  tier: Tier;
  confirm: boolean;
  provider: WalletProvider;
  fetchConfig?: () => Promise<RuntimeConfig>;
  /** Injectable for tests; forwarded to `checkAllowlist`'s own option of the same name. See
   *  `allowlist.ts`'s doc comment for why this is resolved lazily rather than eagerly. */
  resolveAllowCombat?: () => boolean;
  /** Injectable for tests; forwarded to `checkAllowlist`'s own option of the same name -- the
   *  alliance-feature counterpart to `resolveAllowCombat` above, same lazy-resolution rationale. */
  resolveAllowAlliance?: () => boolean;
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

  const allow = await checkAllowlist(tx, opts.tier, {
    fetchConfig: opts.fetchConfig,
    resolveAllowCombat: opts.resolveAllowCombat,
    resolveAllowAlliance: opts.resolveAllowAlliance,
  });
  if (!allow.ok) {
    throw new SendRefusedError(`allowlist rejected this transaction: ${allow.reason}`);
  }

  return opts.provider.signAndSend(tx);
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
