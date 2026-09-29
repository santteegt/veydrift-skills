/**
 * On-chain pin verification: is the contract system behind the pinned proxies still the one this
 * skill's ABI/allowlist/calldata encoding was pinned against?
 *
 * Why this exists: `verifyAbi()` (abi.ts) trusts the backend's self-reported
 * `deploymentAbiHash`/`deploymentCommit`, and that metadata went stale -- the game and alliance
 * implementations were swapped on-chain several times after the backend's reported deployment
 * timestamp, and nothing noticed. The chain is the only source that cannot lag itself, so this module
 * reads each proxy's EIP-1967 implementation slot directly and compares it to the pin.
 *
 * What is pinned, and why an implementation address is enough for the game/alliance:
 * - The game proxy is an OZ v5 TransparentUpgradeableProxy and the alliance proxy an ERC1967
 *   forwarder with UUPS authorization; either way behavior changes only by swapping the implementation
 *   slot. The game router reaches its modules and libraries through `immutable` addresses baked into
 *   its bytecode, and contract code cannot be replaced at an existing address post-Cancun, so the
 *   implementation address transitively pins every delegatecall target (`codeHash` is a redundant
 *   defence-in-depth check, verified on request).
 * - The game also CALLs (not delegatecalls) other contracts whose addresses live in proxy storage and
 *   whose own implementations are upgradeable: the Randomness engine and the Moon system. Those are
 *   pinned as `dependencies` and enforced only for the functions that reach them (`appliesTo`).
 *
 * Fail-closed throughout: any RPC failure, empty slot, or mismatch yields `ok: false` with the reason
 * in `problems`. The runtime-config cross-checks are additive (a backend that disagrees about an
 * address is a problem; a backend that is unreachable is only a warning, because the verdict itself
 * never depends on the backend).
 */

import { getAddress, keccak256, parseAbi } from "viem";
import { fetchLiveRuntimeConfig, loadPinnedMeta, type RuntimeConfig } from "./abi.js";
import { getPublicClient, type VeydriftPublicClient } from "./rpc.js";

/** EIP-1967: bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1). */
export const EIP1967_IMPLEMENTATION_SLOT =
  "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc" as const;

export type PinClient = Pick<VeydriftPublicClient, "getStorageAt" | "getCode" | "readContract" | "getBlockNumber">;

export type ProxyName = "game" | "alliance" | "randomnessEngine" | "moonSystem";

export interface ProxyCheck {
  name: ProxyName;
  proxy: string;
  pinnedImplementation: string;
  liveImplementation: string | null;
  /** Whether the implementation's runtime code hash was also compared (skipped on the hot path). */
  codeVerified: boolean;
  ok: boolean;
  problems: string[];
}

export interface OnchainPinResult {
  /** The game AND alliance proxies (and `tx.to`, when supplied) match the pin -- required before ANY
   *  write. */
  ok: boolean;
  /** The Randomness and Moon dependencies match the pin -- additionally required for `appliesTo`. */
  dependenciesOk: boolean;
  game: ProxyCheck;
  alliance: ProxyCheck;
  randomnessEngine: ProxyCheck;
  moonSystem: ProxyCheck;
  /** Function names that also need `dependenciesOk` (from PINNED.json `dependencies.appliesTo`). */
  appliesTo: string[];
  problems: string[];
  warnings: string[];
  checkedAt: string;
  block: string | null;
}

export interface CheckOnchainPinOptions {
  client?: PinClient;
  fetchConfig?: () => Promise<RuntimeConfig>;
  /** Also compare each implementation's runtime code hash (2-4 extra `eth_getCode`). Default true;
   *  the per-tick `build` path passes false (a same-address code change is impossible post-Cancun, so
   *  the slot read is the check that matters). */
  verifyCode?: boolean;
  /** When given (a tx about to be built/sent), it must be one of the two pinned proxies. */
  to?: string;
}

const RANDOMNESS_GETTER = parseAbi(["function randomnessEngine() view returns (address)"]);

/** viem's `message` is a multi-line dump (URL, request body, docs link); its `shortMessage` is the
 *  one-line cause. Problems end up in tick reports and refusal text, so keep them to one line. */
function briefError(err: unknown): string {
  const e = err as { shortMessage?: string; message?: string };
  return (e.shortMessage ?? e.message ?? String(err)).split("\n")[0]!.trim();
}

function sameAddress(a: string | undefined | null, b: string | undefined | null): boolean {
  return !!a && !!b && a.toLowerCase() === b.toLowerCase();
}

function slotToAddress(raw: string | undefined): string | null {
  if (!raw || !/^0x[0-9a-fA-F]{64}$/.test(raw)) return null;
  const tail = raw.slice(-40);
  if (/^0+$/.test(tail)) return null;
  return getAddress(`0x${tail}`);
}

async function checkProxy(
  name: ProxyName,
  proxy: string,
  pinnedImplementation: string,
  pinnedCodeHash: string,
  client: PinClient,
  verifyCode: boolean,
): Promise<ProxyCheck> {
  const problems: string[] = [];
  let liveImplementation: string | null = null;
  let codeVerified = false;
  try {
    const raw = await client.getStorageAt({
      address: getAddress(proxy),
      slot: EIP1967_IMPLEMENTATION_SLOT,
    });
    liveImplementation = slotToAddress(raw);
    if (!liveImplementation) {
      problems.push(`${name}: EIP-1967 implementation slot of ${proxy} is empty or unreadable (not a proxy?)`);
    } else if (!sameAddress(liveImplementation, pinnedImplementation)) {
      problems.push(
        `${name}: implementation of ${proxy} is ${liveImplementation}, but the pin is ${pinnedImplementation} ` +
          `-- the contract was upgraded after this skill was pinned; re-pin before writing`,
      );
    } else if (verifyCode) {
      const code = await client.getCode({ address: getAddress(liveImplementation) });
      codeVerified = true;
      const liveHash = code && code !== "0x" ? keccak256(code) : null;
      if (!liveHash) {
        problems.push(`${name}: implementation ${liveImplementation} has no code`);
      } else if (liveHash.toLowerCase() !== pinnedCodeHash.toLowerCase()) {
        problems.push(`${name}: implementation ${liveImplementation} code hash ${liveHash} != pinned ${pinnedCodeHash}`);
      }
    }
  } catch (err) {
    problems.push(`${name}: could not read the chain (${briefError(err)})`);
  }
  return {
    name,
    proxy,
    pinnedImplementation,
    liveImplementation,
    codeVerified,
    ok: problems.length === 0,
    problems,
  };
}

export async function checkOnchainPin(opts: CheckOnchainPinOptions = {}): Promise<OnchainPinResult> {
  const client: PinClient = opts.client ?? getPublicClient();
  const verifyCode = opts.verifyCode ?? true;
  const gameMeta = loadPinnedMeta("game");
  const allianceMeta = loadPinnedMeta("alliance");
  const problems: string[] = [];
  const warnings: string[] = [];

  const gameImpl = gameMeta.implementation;
  const allianceImpl = allianceMeta.implementation;
  const deps = gameMeta.dependencies;
  if (!gameImpl || !allianceImpl || !deps) {
    throw new Error(
      "PINNED.json / PINNED.alliance.json carry no `implementation`/`dependencies` block -- the on-chain " +
        "pin cannot be verified; re-pin per references/abi-pinning.md",
    );
  }

  const [game, alliance, randomnessEngine, moonSystem] = await Promise.all([
    checkProxy("game", gameImpl.proxy, gameImpl.address, gameImpl.codeHash, client, verifyCode),
    checkProxy("alliance", allianceImpl.proxy, allianceImpl.address, allianceImpl.codeHash, client, verifyCode),
    checkProxy(
      "randomnessEngine",
      deps.randomnessEngine.proxy,
      deps.randomnessEngine.implementation,
      deps.randomnessEngine.codeHash,
      client,
      verifyCode,
    ),
    checkProxy(
      "moonSystem",
      deps.moonSystem.proxy,
      deps.moonSystem.implementation,
      deps.moonSystem.codeHash,
      client,
      verifyCode,
    ),
  ]);

  // The game's own getter is on-chain truth for which Randomness engine it calls.
  try {
    const live = await client.readContract({
      address: getAddress(gameImpl.proxy),
      abi: RANDOMNESS_GETTER,
      functionName: "randomnessEngine",
    });
    if (!sameAddress(live as string, deps.randomnessEngine.proxy)) {
      const msg = `randomnessEngine: the game now points at ${String(live)}, but the pin is ${deps.randomnessEngine.proxy}`;
      randomnessEngine.problems.push(msg);
      randomnessEngine.ok = false;
    }
  } catch (err) {
    const msg = `randomnessEngine: could not read the game's randomnessEngine() (${briefError(err)})`;
    randomnessEngine.problems.push(msg);
    randomnessEngine.ok = false;
  }

  // Cross-checks against the backend's runtime-config: additive only.
  try {
    const config = await (opts.fetchConfig ?? fetchLiveRuntimeConfig)();
    const claimed: Array<[ProxyCheck, string | undefined]> = [
      [game, config.gameContractAddress ?? config.contractAddress],
      [alliance, config.allianceContractAddress],
      [moonSystem, config.moonContractAddress as string | undefined],
      [randomnessEngine, config.randomnessEngineAddress as string | undefined],
    ];
    for (const [check, address] of claimed) {
      if (address && !sameAddress(address, check.proxy)) {
        check.problems.push(
          `${check.name}: live /runtime-config reports ${address}, but the pinned proxy is ${check.proxy}`,
        );
        check.ok = false;
      }
    }
  } catch (err) {
    warnings.push(`could not cross-check /runtime-config addresses (${briefError(err)})`);
  }

  if (opts.to !== undefined) {
    if (!sameAddress(opts.to, gameImpl.proxy) && !sameAddress(opts.to, allianceImpl.proxy)) {
      problems.push(`tx.to ${opts.to} is neither pinned proxy (game ${gameImpl.proxy}, alliance ${allianceImpl.proxy})`);
    }
  }

  let block: string | null = null;
  try {
    block = (await client.getBlockNumber()).toString();
  } catch {
    // informational only
  }

  for (const check of [game, alliance, randomnessEngine, moonSystem]) problems.push(...check.problems);
  const ok = game.ok && alliance.ok && !problems.some((p) => p.startsWith("tx.to"));
  return {
    ok,
    dependenciesOk: randomnessEngine.ok && moonSystem.ok,
    game,
    alliance,
    randomnessEngine,
    moonSystem,
    appliesTo: deps.appliesTo,
    problems,
    warnings,
    checkedAt: new Date().toISOString(),
    block,
  };
}

/** The result a caller should record when the check itself could not run (offline, RPC down): never
 *  `ok`, so every consumer fails closed. */
export function failedOnchainPin(reason: string): OnchainPinResult {
  const blank = (name: ProxyName): ProxyCheck => ({
    name,
    proxy: "",
    pinnedImplementation: "",
    liveImplementation: null,
    codeVerified: false,
    ok: false,
    problems: [reason],
  });
  return {
    ok: false,
    dependenciesOk: false,
    game: blank("game"),
    alliance: blank("alliance"),
    randomnessEngine: blank("randomnessEngine"),
    moonSystem: blank("moonSystem"),
    appliesTo: [],
    problems: [reason],
    warnings: [],
    checkedAt: new Date().toISOString(),
    block: null,
  };
}

/** True when `functionName` also needs the dependency pins (`dependencies.appliesTo`). */
export function needsDependencyPin(functionName: string | undefined, result: OnchainPinResult): boolean {
  return !!functionName && result.appliesTo.includes(functionName);
}
