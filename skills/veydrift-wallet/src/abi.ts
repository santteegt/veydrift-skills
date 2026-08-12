/**
 * Pinned-ABI loading, hashing and live verification.
 *
 * The wallet engine never trusts a freshly-`forge build`-ed ABI at runtime; it trusts only the
 * committed `abi/VeydriftGame.701bed3.json`, and cross-checks its hash against the live
 * `/runtime-config` before any write path is used. See references/abi-pinning.md for the full
 * derivation and the main-vs-deployed divergence this guards against.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Abi, AbiFunction } from "viem";
import { toFunctionSelector, toFunctionSignature } from "viem";

// Resolve bundled paths relative to this file, never `cwd` -- this module may be invoked from
// anywhere once the skill is installed elsewhere (npx skills add copies the tree).
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ABI_DIR = join(__dirname, "..", "abi");

export const RUNTIME_CONFIG_URL = "https://api.veydrift.com/runtime-config";

export interface PinnedArtifact {
  abi: Abi;
  methodIdentifiers: Record<string, string>;
}

export interface PinnedMeta {
  commit: string;
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
  note?: string;
}

let _artifact: PinnedArtifact | undefined;
let _meta: PinnedMeta | undefined;

export function loadPinnedArtifact(): PinnedArtifact {
  if (!_artifact) {
    const raw = readFileSync(join(ABI_DIR, "VeydriftGame.701bed3.json"), "utf8");
    _artifact = JSON.parse(raw) as PinnedArtifact;
  }
  return _artifact;
}

export function loadPinnedMeta(): PinnedMeta {
  if (!_meta) {
    const raw = readFileSync(join(ABI_DIR, "PINNED.json"), "utf8");
    _meta = JSON.parse(raw) as PinnedMeta;
  }
  return _meta;
}

export function getPinnedAbi(): Abi {
  return loadPinnedArtifact().abi;
}

/** sha256(JSON.stringify(abi)) -- compact separators (JSON.stringify's default), key order as
 *  emitted by forge / preserved by JSON.parse. Matches
 *  scripts/veydrift-deployment-manifest.mjs:129-135 in the veydrift repo. */
export function computeAbiHash(abi: Abi): string {
  const json = JSON.stringify(abi);
  return "sha256:" + createHash("sha256").update(json).digest("hex");
}

/** Recompute the hash from the pinned file on disk (not from PINNED.json's cached value) so a
 *  hand-edited pin can't silently drift from what's actually in the ABI file. */
export function computePinnedAbiHash(): string {
  return computeAbiHash(getPinnedAbi());
}

export interface RuntimeConfig {
  chainId: number;
  /** Raw string as returned by the API -- not yet validated/checksummed. Callers must run it
   *  through viem's getAddress() before trusting it as an address. */
  contractAddress?: string;
  gameContractAddress?: string;
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

export interface AbiVerifyResult {
  match: boolean;
  pinnedHash: string;
  liveHash: string;
  pinnedCommit: string;
  liveDeploymentCommit: string;
  commitMatch: boolean;
}

/** The single source of truth for "is it safe to write". Recomputes the pinned hash from the
 *  on-disk ABI (not the cached value in PINNED.json) and compares to the live
 *  `deploymentAbiHash`. On any mismatch, callers must block every write -- see guard() usage in
 *  cli.ts and allowlist.ts. */
export async function verifyAbi(): Promise<AbiVerifyResult> {
  const meta = loadPinnedMeta();
  const pinnedHash = computePinnedAbiHash();
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
  };
}

// ---------------------------------------------------------------------------------------------
// Function resolution -- deliberately never "pick the first match by name". launchFleetMission
// is overloaded on the deployed ABI (trap #2); resolving by name alone throws instead of
// guessing.
// ---------------------------------------------------------------------------------------------

export function findFunctionsByName(name: string): AbiFunction[] {
  const abi = getPinnedAbi();
  return abi.filter((e): e is AbiFunction => e.type === "function" && e.name === name);
}

/** Resolve by exact full canonical signature, e.g.
 *  "startBuildingUpgrade(uint256,uint8)" or the 7-arg / 6-arg forms of launchFleetMission. */
export function findFunctionBySignature(signature: string): AbiFunction {
  const abi = getPinnedAbi();
  const match = abi.find(
    (e): e is AbiFunction => e.type === "function" && toFunctionSignature(e) === signature,
  );
  if (!match) {
    throw new Error(`No ABI function on the pinned artifact matches signature "${signature}"`);
  }
  return match;
}

/**
 * Resolve `nameOrSignature` to exactly one ABI function.
 *
 * If it contains "(" it is treated as a full signature and matched exactly (required for
 * overloaded functions). Otherwise it must resolve to exactly one function by name; if more than
 * one ABI entry shares that name (as `launchFleetMission` does), this throws rather than
 * silently picking one -- see references/abi-pinning.md and tests/abi.test.ts.
 */
export function resolveFunctionAbi(nameOrSignature: string): AbiFunction {
  if (nameOrSignature.includes("(")) {
    return findFunctionBySignature(nameOrSignature);
  }
  const candidates = findFunctionsByName(nameOrSignature);
  if (candidates.length === 0) {
    throw new Error(`No ABI function named "${nameOrSignature}" on the pinned artifact`);
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
// through `simulate` instead. RESEARCH-ADDENDUM.md §4.1.
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

/** Given a 4-byte selector, find which pinned-ABI function(s) it belongs to (0, 1, or 2 for the
 *  overloaded launchFleetMission). Used to decode calldata for printing / allowlist checks
 *  without assuming the caller told us the right function name. */
export function functionsForSelector(selector: `0x${string}`): AbiFunction[] {
  const abi = getPinnedAbi();
  const lower = selector.toLowerCase();
  const fns = abi.filter((e): e is AbiFunction => e.type === "function");
  return fns.filter((fn) => toFunctionSelector(fn).toLowerCase() === lower);
}
