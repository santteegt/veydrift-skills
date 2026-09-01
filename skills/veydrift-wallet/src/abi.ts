/**
 * Pinned-ABI loading, hashing and live verification.
 *
 * The wallet engine never trusts a freshly-`forge build`-ed ABI at runtime; it trusts only the
 * committed `abi/VeydriftGame.701bed3.json` (and, since the alliance feature, the committed
 * `abi/VeydriftAllianceSystem.701bed3.json` sibling), and cross-checks the game contract's hash
 * against the live `/runtime-config` before any write path is used. See
 * references/abi-pinning.md for the full derivation, the main-vs-deployed divergence this
 * guards against, and -- new -- why the alliance contract's pin has no equivalent live-hash
 * re-check.
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

/** Which pinned contract to resolve against. Defaults to "game" everywhere below so every
 *  existing call site (predating the alliance feature) keeps its exact prior behavior with no
 *  argument change required. */
export type Contract = "game" | "alliance";

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

const ARTIFACT_FILENAMES: Record<Contract, string> = {
  game: "VeydriftGame.701bed3.json",
  alliance: "VeydriftAllianceSystem.701bed3.json",
};

const META_FILENAMES: Record<Contract, string> = {
  game: "PINNED.json",
  alliance: "PINNED.alliance.json",
};

const _artifacts: Partial<Record<Contract, PinnedArtifact>> = {};
const _metas: Partial<Record<Contract, PinnedMeta>> = {};

export function loadPinnedArtifact(contract: Contract = "game"): PinnedArtifact {
  if (!_artifacts[contract]) {
    const raw = readFileSync(join(ABI_DIR, ARTIFACT_FILENAMES[contract]), "utf8");
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

export function getPinnedAbi(contract: Contract = "game"): Abi {
  return loadPinnedArtifact(contract).abi;
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

export interface AbiVerifyResult {
  match: boolean;
  pinnedHash: string;
  liveHash: string;
  pinnedCommit: string;
  liveDeploymentCommit: string;
  commitMatch: boolean;
}

/** The single source of truth for "is it safe to write [to the game contract]". Recomputes the
 *  pinned hash from the on-disk ABI (not the cached value in PINNED.json) and compares to the
 *  live `deploymentAbiHash`. On any mismatch, callers must block every write -- see guard()
 *  usage in cli.ts and allowlist.ts.
 *
 *  **Game contract only, deliberately.** There is no `verifyAllianceAbi()` sibling: as of
 *  2026-09-01, `/runtime-config` exposes `allianceContractAddress` but no
 *  `allianceAbiHash`/`allianceDeploymentCommit` field anywhere -- only the single
 *  `backend.build.deploymentAbiHash`/`deploymentCommit` pair, which is for the game contract.
 *  The alliance ABI pin (`abi/PINNED.alliance.json`) was therefore verified exactly once, by
 *  construction (exact commit checkout + exact forge settings, matching the game contract's own
 *  pinned settings from the same build), and can never be automatically re-checked against a
 *  live hash the way this function re-checks the game contract's pin on every call. This is a
 *  real, permanent limit of the upstream API, not a gap this module can close -- see
 *  references/abi-pinning.md's "Second contract" section. */
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
  };
}

// ---------------------------------------------------------------------------------------------
// Function resolution -- deliberately never "pick the first match by name". launchFleetMission
// is overloaded on the deployed ABI (trap #2); resolving by name alone throws instead of
// guessing.
// ---------------------------------------------------------------------------------------------

export function findFunctionsByName(name: string, contract: Contract = "game"): AbiFunction[] {
  const abi = getPinnedAbi(contract);
  return abi.filter((e): e is AbiFunction => e.type === "function" && e.name === name);
}

/** Resolve by exact full canonical signature, e.g.
 *  "startBuildingUpgrade(uint256,uint8)" or the 7-arg / 6-arg forms of launchFleetMission. */
export function findFunctionBySignature(
  signature: string,
  contract: Contract = "game",
): AbiFunction {
  const abi = getPinnedAbi(contract);
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
    const abi = getPinnedAbi(contract);
    const fns = abi.filter((e): e is AbiFunction => e.type === "function");
    return fns.filter((fn) => toFunctionSelector(fn).toLowerCase() === lower);
  });
}
