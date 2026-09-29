/**
 * Pinned-ABI loading, hashing and verification.
 *
 * The wallet engine never trusts a freshly-`forge build`-ed ABI at runtime; it trusts only the
 * committed `abi/VeydriftGame.<sha7>.json` (plus the alliance and delegation siblings). Drift is
 * detected two ways: `verifyAbi()` below compares the game ABI hash to the backend's
 * `/runtime-config` self-report (advisory -- the backend's deployment metadata can go stale, and did),
 * and `onchain-pin.ts` re-reads the proxies' EIP-1967 implementation slots from the chain itself
 * (the authority). See references/abi-pinning.md.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Abi, AbiFunction } from "viem";
import { decodeErrorResult, decodeFunctionResult, toFunctionSelector, toFunctionSignature } from "viem";

// Resolve bundled paths relative to this file, never `cwd` -- this module may be invoked from
// anywhere once the skill is installed elsewhere (npx skills add copies the tree).
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ABI_DIR = join(__dirname, "..", "abi");

export const RUNTIME_CONFIG_URL = "https://api.veydrift.com/runtime-config";

/** Which pinned contract to resolve against. Defaults to "game" everywhere below so every
 *  existing call site (predating the alliance feature) keeps its exact prior behavior with no
 *  argument change required. */
export type Contract = "game" | "alliance";

export interface PinnedArtifact {
  abi: Abi;
  methodIdentifiers: Record<string, string>;
}

/** A proxy pinned by its EIP-1967 implementation, read from the chain (never the backend). */
export interface PinnedProxy {
  proxy: string;
  proxyKind?: string;
  /** EIP-1967 implementation slot (same constant for every proxy pinned here). */
  slot?: string;
  address?: string;
  codeHash?: string;
  proxyAdmin?: string;
  observedAt?: string;
  observedAtBlock?: number;
}

/** An external contract the game CALLs (not delegatecalls) whose behavior can change without the
 *  game's own implementation changing -- pinned separately, enforced only for `appliesTo`. */
export interface PinnedDependency {
  proxy: string;
  proxyKind?: string;
  implementation: string;
  codeHash: string;
  addressSource?: string;
  matchesArtifact?: string;
}

export interface PinnedMeta {
  commit: string;
  /** Basename of the pinned ABI file under `abi/`. Read from here (not hard-coded in this module)
   *  so a re-pin is one artifact, not a meta file plus a source edit that can be forgotten. */
  artifact: string;
  abiHash: string;
  foundry: {
    solc: string;
    optimizer_runs: number;
    via_ir: boolean;
    cbor_metadata: boolean;
    bytecode_hash: string;
  };
  fetchedAt: string;
  source: Record<string, unknown>;
  /** On-chain pin: the proxy's implementation address + code hash at pin time. */
  implementation?: PinnedProxy & { address: string; codeHash: string };
  dependencies?: {
    randomnessEngine: PinnedDependency;
    moonSystem: PinnedDependency;
    /** Function names that reach the dependencies above, and therefore also need them pinned. */
    appliesTo: string[];
  };
  /** What `/runtime-config` reported when this was pinned, recorded as KNOWN-STALE: the backend's
   *  deployment metadata lagged the chain, so it is advisory and never the authority. */
  backendReported?: {
    deploymentAbiHash: string;
    deploymentCommit: string;
    deploymentTimestamp?: string;
    note?: string;
  };
  /** Extra ABIs merged into function resolution for the game address only -- entrypoints the
   *  proxy serves from `fallback()` (delegation) that the forge `VeydriftGame` artifact lacks. */
  supplemental?: Array<{ name: string; file: string; abiHash: string; artifactPath?: string; note?: string }>;
  note?: string;
}

const META_FILENAMES: Record<Contract, string> = {
  game: "PINNED.json",
  alliance: "PINNED.alliance.json",
};

const _artifacts: Partial<Record<Contract, PinnedArtifact>> = {};
const _metas: Partial<Record<Contract, PinnedMeta>> = {};
let _supplemental: PinnedArtifact[] | undefined;

export function loadPinnedArtifact(contract: Contract = "game"): PinnedArtifact {
  if (!_artifacts[contract]) {
    const raw = readFileSync(join(ABI_DIR, loadPinnedMeta(contract).artifact), "utf8");
    _artifacts[contract] = JSON.parse(raw) as PinnedArtifact;
  }
  return _artifacts[contract] as PinnedArtifact;
}

export function loadPinnedMeta(contract: Contract = "game"): PinnedMeta {
  if (!_metas[contract]) {
    const raw = readFileSync(join(ABI_DIR, META_FILENAMES[contract]), "utf8");
    _metas[contract] = JSON.parse(raw) as PinnedMeta;
  }
  return _metas[contract] as PinnedMeta;
}

/** The pinned artifact for `contract`, UNMERGED. This is what `computePinnedAbiHash` hashes and
 *  what the backend's `deploymentAbiHash` is comparable to -- merging supplemental entries in here
 *  would silently change the hash. Function/error resolution goes through `getResolvableAbi`. */
export function getPinnedAbi(contract: Contract = "game"): Abi {
  return loadPinnedArtifact(contract).abi;
}

function loadSupplementalArtifacts(): PinnedArtifact[] {
  if (!_supplemental) {
    const entries = loadPinnedMeta("game").supplemental ?? [];
    _supplemental = entries.map((entry) => {
      const artifact = JSON.parse(readFileSync(join(ABI_DIR, entry.file), "utf8")) as PinnedArtifact;
      const actual = computeAbiHash(artifact.abi);
      if (actual !== entry.abiHash) {
        throw new Error(
          `supplemental ABI "${entry.name}" (${entry.file}) hashes to ${actual}, but PINNED.json pins ` +
            `${entry.abiHash} -- refusing to resolve functions against a hand-edited pin`,
        );
      }
      return artifact;
    });
  }
  return _supplemental;
}

function abiEntryKey(entry: Abi[number]): string {
  if (entry.type === "function" || entry.type === "event" || entry.type === "error") {
    return `${entry.type}:${toFunctionSignature(entry as Parameters<typeof toFunctionSignature>[0])}`;
  }
  return `${entry.type}:${JSON.stringify(entry)}`;
}

/** The ABI used to RESOLVE functions (build/simulate/send/describe): the pinned artifact for
 *  `contract` plus, for `"game"` only, the supplemental interface ABIs (the delegation entrypoints
 *  the proxy serves from `fallback()`). Entries already present in the pinned artifact win, so a
 *  signature appearing in both is never double-counted. Never used for hashing. */
export function getResolvableAbi(contract: Contract = "game"): Abi {
  const pinned = getPinnedAbi(contract);
  if (contract !== "game") return pinned;
  const seen = new Set(pinned.map(abiEntryKey));
  const extra: Abi[number][] = [];
  for (const artifact of loadSupplementalArtifacts()) {
    for (const entry of artifact.abi) {
      const key = abiEntryKey(entry);
      if (seen.has(key)) continue;
      seen.add(key);
      extra.push(entry);
    }
  }
  return [...pinned, ...extra] as Abi;
}

/** Every custom error known to any pinned ABI (game, alliance, supplemental), for decoding a
 *  revert's raw data regardless of which contract raised it. */
function getAllErrorsAbi(): Abi {
  const seen = new Set<string>();
  const errors: Abi[number][] = [];
  for (const contract of ["game", "alliance"] as const) {
    for (const entry of getResolvableAbi(contract)) {
      if (entry.type !== "error") continue;
      const key = abiEntryKey(entry);
      if (seen.has(key)) continue;
      seen.add(key);
      errors.push(entry);
    }
  }
  return errors as Abi;
}

/** sha256(JSON.stringify(abi)) -- compact separators (JSON.stringify's default), key order as
 *  emitted by forge / preserved by JSON.parse. Matches
 *  scripts/veydrift-deployment-manifest.mjs:129-135 in the veydrift repo. Contract-agnostic --
 *  takes an already-loaded `Abi`, not a contract tag. */
export function computeAbiHash(abi: Abi): string {
  const json = JSON.stringify(abi);
  return "sha256:" + createHash("sha256").update(json).digest("hex");
}

/** Recompute the hash from the pinned file on disk (not from the meta file's cached value) so a
 *  hand-edited pin can't silently drift from what's actually in the ABI file. */
export function computePinnedAbiHash(contract: Contract = "game"): string {
  return computeAbiHash(getPinnedAbi(contract));
}

export interface RuntimeConfig {
  chainId: number;
  /** Raw string as returned by the API -- not yet validated/checksummed. Callers must run it
   *  through viem's getAddress() before trusting it as an address. */
  contractAddress?: string;
  gameContractAddress?: string;
  /** The alliance contract's live address (confirmed present in /runtime-config as of
   *  2026-09-01). Unlike gameContractAddress, there is no matching live ABI-hash field anywhere
   *  in this response -- see verifyAbi()'s doc comment below. */
  allianceContractAddress?: string;
  backend: {
    build: {
      deploymentAbiHash: string;
      deploymentCommit: string;
      [k: string]: unknown;
    };
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export async function fetchLiveRuntimeConfig(): Promise<RuntimeConfig> {
  const res = await fetch(RUNTIME_CONFIG_URL);
  if (!res.ok) {
    throw new Error(`GET ${RUNTIME_CONFIG_URL} -> HTTP ${res.status}`);
  }
  return (await res.json()) as RuntimeConfig;
}

/** How the backend's self-reported deployment hash relates to the pin:
 *  - `match`: it reports the pinned hash (the backend caught up);
 *  - `known-stale`: it reports exactly the value recorded in PINNED.json's `backendReported` (the
 *    backend's deployment metadata lags the chain -- expected, not drift);
 *  - `other`: some third value (advisory only -- the chain is the authority, see onchain-pin.ts);
 *  - `unavailable`: the field was absent. */
export type BackendHashStatus = "match" | "known-stale" | "other" | "unavailable";

export interface AbiVerifyResult {
  /** `pinnedHash === liveHash` -- the literal comparison, kept for callers that want it. */
  match: boolean;
  pinnedHash: string;
  liveHash: string;
  pinnedCommit: string;
  liveDeploymentCommit: string;
  commitMatch: boolean;
  backendStatus: BackendHashStatus;
}

/** Pure classifier for `AbiVerifyResult.backendStatus` (exported for tests). */
export function classifyBackendHash(
  pinnedHash: string,
  liveHash: string,
  knownStaleHash: string | undefined,
): BackendHashStatus {
  if (!liveHash) return "unavailable";
  if (liveHash === pinnedHash) return "match";
  if (knownStaleHash !== undefined && liveHash === knownStaleHash) return "known-stale";
  return "other";
}

/** The backend-reported half of drift detection -- ADVISORY. Recomputes the pinned game ABI hash
 *  from the on-disk ABI (not the cached value in PINNED.json) and compares it to the live
 *  `deploymentAbiHash`, classifying the result (see `BackendHashStatus`).
 *
 *  This is no longer "the single source of truth for is it safe to write": the backend's
 *  `deploymentAbiHash`/`deploymentCommit`/timestamp are set by its own deploy metadata and stayed at
 *  the 2026-09-07 values while the game and alliance implementations were swapped on-chain several
 *  times after. The authority is `checkOnchainPin()` (onchain-pin.ts), which reads the proxies'
 *  EIP-1967 implementation slots directly from the chain. Neither the alliance nor the delegation
 *  entrypoints are covered by the backend's hash at all (no `allianceAbiHash` field exists, and the
 *  delegation functions are not in the game artifact) -- see references/abi-pinning.md. */
export async function verifyAbi(): Promise<AbiVerifyResult> {
  const meta = loadPinnedMeta("game");
  const pinnedHash = computePinnedAbiHash("game");
  const config = await fetchLiveRuntimeConfig();
  const liveHash = config.backend?.build?.deploymentAbiHash ?? "";
  const liveDeploymentCommit = config.backend?.build?.deploymentCommit ?? "";
  return {
    match: pinnedHash === liveHash,
    pinnedHash,
    liveHash,
    pinnedCommit: meta.commit,
    liveDeploymentCommit,
    commitMatch: meta.commit === liveDeploymentCommit,
    backendStatus: classifyBackendHash(pinnedHash, liveHash, meta.backendReported?.deploymentAbiHash),
  };
}

// ---------------------------------------------------------------------------------------------
// Function resolution -- deliberately never "pick the first match by name". launchFleetMission
// is overloaded on the deployed ABI (trap #2); resolving by name alone throws instead of
// guessing.
// ---------------------------------------------------------------------------------------------

export function findFunctionsByName(name: string, contract: Contract = "game"): AbiFunction[] {
  const abi = getResolvableAbi(contract);
  return abi.filter((e): e is AbiFunction => e.type === "function" && e.name === name);
}

/** Resolve by exact full canonical signature, e.g.
 *  "startBuildingUpgrade(uint256,uint8)" or the 7-arg / 6-arg forms of launchFleetMission. */
export function findFunctionBySignature(
  signature: string,
  contract: Contract = "game",
): AbiFunction {
  const abi = getResolvableAbi(contract);
  const match = abi.find(
    (e): e is AbiFunction => e.type === "function" && toFunctionSignature(e) === signature,
  );
  if (!match) {
    throw new Error(
      `No ABI function on the pinned "${contract}" artifact matches signature "${signature}"`,
    );
  }
  return match;
}

/**
 * Resolve `nameOrSignature` to exactly one ABI function on the given pinned contract (defaults
 * to "game" -- every call site written before the alliance feature keeps its exact prior
 * behavior unchanged).
 *
 * If it contains "(" it is treated as a full signature and matched exactly (required for
 * overloaded functions). Otherwise it must resolve to exactly one function by name; if more than
 * one ABI entry shares that name (as `launchFleetMission` does), this throws rather than
 * silently picking one -- see references/abi-pinning.md and tests/abi.test.ts.
 *
 * This never searches across both contracts at once -- a caller who doesn't know which contract
 * a function lives on should not be building a transaction against it. (Contrast
 * `functionsForSelector` below, which does search both, but only for display/decode purposes.)
 */
export function resolveFunctionAbi(
  nameOrSignature: string,
  contract: Contract = "game",
): AbiFunction {
  if (nameOrSignature.includes("(")) {
    return findFunctionBySignature(nameOrSignature, contract);
  }
  const candidates = findFunctionsByName(nameOrSignature, contract);
  if (candidates.length === 0) {
    throw new Error(`No ABI function named "${nameOrSignature}" on the pinned "${contract}" artifact`);
  }
  if (candidates.length > 1) {
    const sigs = candidates.map((c) => toFunctionSignature(c));
    throw new Error(
      `"${nameOrSignature}" is overloaded on the deployed ABI (${candidates.length} forms). ` +
        `Select by full signature, never by name. Candidates:\n  ${sigs.join("\n  ")}`,
    );
  }
  return candidates[0] as AbiFunction;
}

export function getSelector(fn: AbiFunction): `0x${string}` {
  return toFunctionSelector(fn);
}

export function getSelectorForSignature(signature: string): `0x${string}` {
  return toFunctionSelector(signature);
}

export function getSignature(fn: AbiFunction): string {
  return toFunctionSignature(fn);
}

// ---------------------------------------------------------------------------------------------
// Trap #3: functions that are ABI `nonpayable` (not `view`) because they lazily settle state
// before returning, but are semantically reads. `send` must refuse these outright; route them
// through `simulate` instead. RESEARCH-ADDENDUM.md §4.1. All six are game-contract functions;
// no alliance-contract function is a disguised read (none lazily settle anything -- see
// references/abi-pinning.md's "Second contract" section).
// ---------------------------------------------------------------------------------------------

export const NONPAYABLE_READ_FUNCTIONS = [
  "attackProtectionStatus",
  "collectResources",
  "debrisField",
  "maxRaidLoot",
  "protectedResources",
  "raidableResources",
] as const;

export function isNonpayableRead(functionName: string): boolean {
  return (NONPAYABLE_READ_FUNCTIONS as readonly string[]).includes(functionName);
}

/** Given a 4-byte selector, find which pinned-ABI function(s) it belongs to, searching BOTH
 *  pinned contracts (0, 1, or 2+ matches -- 2 for the overloaded launchFleetMission within the
 *  game ABI; a cross-contract collision between the game and alliance ABIs is a theoretical,
 *  vanishingly unlikely residual risk this function does not attempt to disambiguate, since it
 *  is used only for display/decode, never to decide which contract a transaction targets --
 *  that decision is made explicitly via `Action.contract` in tx.ts, not inferred from a
 *  selector). Used to decode calldata for printing / allowlist checks without assuming the
 *  caller told us the right function name. */
export function functionsForSelector(selector: `0x${string}`): AbiFunction[] {
  const lower = selector.toLowerCase();
  const contracts: Contract[] = ["game", "alliance"];
  return contracts.flatMap((contract) => {
    const abi = getResolvableAbi(contract);
    const fns = abi.filter((e): e is AbiFunction => e.type === "function");
    return fns.filter((fn) => toFunctionSelector(fn).toLowerCase() === lower);
  });
}

/**
 * Decode `returnData` against the resolved function's own ABI `outputs`, whenever
 * they're non-empty -- a generalizable capability (any future read-shaped call
 * benefits, not just the two ACS-coordination view functions this was built for:
 * `counterplayDefenseFuelContext`/`defenseHoldFuelContext`, called via the same
 * `buildTx`/`simulateTx` pipeline as any other Action, `contract: "alliance"`). Lives
 * here rather than in `tx.ts`/`simulateTx` itself: decoding is a display/consumption
 * concern (this is what `walletctl simulate --json` calls, `cli.ts`), not part of
 * building or executing the call -- `simulateTx` stays free of it. Returns `undefined`
 * (not an error) when the function has no outputs, isn't resolvable from the pinned
 * ABIs, or decoding otherwise fails -- a decode failure must never mask a successful
 * simulation as `ok: false`.
 */
/** Recursively replace every `bigint` with its decimal string so the result is
 *  `JSON.stringify`-able. A flat top-level pass isn't enough: a struct/`tuple`
 *  output (e.g. a `Resources` return) decodes to an object whose `uint128` fields
 *  are nested bigints, and `tuple[]` nests them one level deeper again. */
function deepStringifyBigints(value: unknown): unknown {
  if (typeof value === "bigint") return value.toString();
  if (Array.isArray(value)) return value.map(deepStringifyBigints);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, deepStringifyBigints(v)]));
  }
  return value;
}

export function decodeSimulateReturnData(
  selector: `0x${string}`,
  returnData: `0x${string}` | undefined,
): Record<string, unknown> | undefined {
  if (!returnData) return undefined;
  const fn = functionsForSelector(selector)[0];
  if (!fn || !fn.outputs || fn.outputs.length === 0) return undefined;
  try {
    const decoded = decodeFunctionResult({ abi: [fn], data: returnData });
    // viem returns a bare value for a single output (even when that value is itself
    // an array, e.g. a `tuple[]`) and an array of values for multiple outputs --
    // key off `outputs.length`, not `Array.isArray`, or a single array-typed output
    // gets truncated to its first element.
    const values = fn.outputs.length === 1 ? [decoded] : (decoded as readonly unknown[]);
    const named: Record<string, unknown> = {};
    fn.outputs.forEach((output, i) => {
      const key = output.name && output.name.length > 0 ? output.name : `_${i}`;
      named[key] = deepStringifyBigints(values[i]);
    });
    return named;
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------------------------
// Custom-error decoding. A reverting `eth_call`/`estimateGas` reaches us as a viem error whose
// `shortMessage` is "Execution reverted for an unknown reason." for every custom error -- e.g. an
// unaffordable call reverts with `InsufficientResources(metal, crystal, deuterium)`, which the pinned
// ABI knows perfectly well. Decode the raw revert data against every pinned error instead.
// ---------------------------------------------------------------------------------------------

/** Walks a viem error's `cause` chain for the raw revert payload (hex `data`). */
export function extractRevertData(err: unknown): `0x${string}` | undefined {
  let node: unknown = err;
  for (let depth = 0; node && typeof node === "object" && depth < 8; depth++) {
    const data = (node as { data?: unknown }).data;
    if (typeof data === "string" && /^0x[0-9a-fA-F]{8,}$/.test(data)) return data as `0x${string}`;
    if (data && typeof data === "object") {
      const inner = (data as { data?: unknown }).data;
      if (typeof inner === "string" && /^0x[0-9a-fA-F]{8,}$/.test(inner)) return inner as `0x${string}`;
    }
    node = (node as { cause?: unknown }).cause;
  }
  return undefined;
}

export interface DecodedRevert {
  errorName: string;
  errorArgs: string[];
  /** e.g. `InsufficientResources(9035, 44471, 81685)` */
  text: string;
}

/** Decode raw revert data against the pinned custom errors (game + alliance + supplemental), plus
 *  the two Solidity built-ins. `undefined` when the data is empty or matches nothing pinned. */
export function decodeRevertData(data: `0x${string}` | undefined): DecodedRevert | undefined {
  if (!data) return undefined;
  try {
    const decoded = decodeErrorResult({ abi: getAllErrorsAbi(), data });
    const args = ((decoded.args ?? []) as readonly unknown[]).map((a) =>
      typeof a === "bigint" ? a.toString() : String(a),
    );
    return { errorName: decoded.errorName, errorArgs: args, text: `${decoded.errorName}(${args.join(", ")})` };
  } catch {
    return undefined;
  }
}

/** One-liner used wherever a viem error's message is surfaced: the decoded custom error when the raw
 *  revert data is recoverable and known, otherwise the error's own short message. */
export function describeRevert(err: unknown): { message: string; decoded?: DecodedRevert; data?: `0x${string}` } {
  const data = extractRevertData(err);
  const decoded = decodeRevertData(data);
  const raw = (err as { shortMessage?: string; message?: string }).shortMessage ?? (err as Error).message;
  return { message: decoded ? `${decoded.text} (reverted)` : raw, decoded, data };
}
