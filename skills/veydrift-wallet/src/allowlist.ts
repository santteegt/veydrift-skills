/**
 * The wallet-engine allowlist. Enforced HERE, independently of `veydrift-agent` -- a fully
 * compromised agent skill must still be unable to make `walletctl` sign anything outside
 * Veydrift. Spec: docs/SPEC.md §6.4.
 *
 * Five checks, all evaluated (never short-circuited in the report, though `ok` is AND of all):
 *   1. tx.to        in the live /runtime-config address set (never a hardcoded list)
 *   2. tx.data[0:4] (selector) in the tier's allowed set, computed from the pinned ABI
 *   3. tx.value      == 0 (no payable action is whitelisted at any tier here)
 *   4. tx.chainId    == 8453 (Base)
 *   5. any failure -> caller must exit non-zero, log the rejection, sign nothing
 *
 * Plus one extra, spec-mandated restriction that lives at the calldata level rather than the
 * selector level: at `operator` tier, `launchFleetMission` is only allowed for mission types
 * Transport(0) / Deploy(1) / Harvest(4). That can't be expressed as a selector check (the mission
 * type is a regular argument, not part of the selector) so it's decoded from calldata here.
 */

import { decodeFunctionData, getAddress } from "viem";
import { fetchLiveRuntimeConfig, getSelector, resolveFunctionAbi, type RuntimeConfig } from "./abi.js";
import type { UnsignedTx } from "./providers/types.js";

/** Injectable so tests can supply a fixture instead of hitting the real network. Defaults to the
 *  real live fetch -- production code paths always see the genuine /runtime-config response. */
export type RuntimeConfigFetcher = () => Promise<RuntimeConfig>;

export type Tier = "advisor" | "economy" | "operator";

export const TIERS: readonly Tier[] = ["advisor", "economy", "operator"];

export function isTier(x: string): x is Tier {
  return (TIERS as readonly string[]).includes(x);
}

/** Full canonical signatures (never bare names) for the economy tier's five actions. Using full
 *  signatures means resolveFunctionAbi never has to guess even for non-overloaded functions, and
 *  it fails loudly if the pinned ABI ever stops containing exactly this signature. */
const ECONOMY_SIGNATURES = [
  "startBuildingUpgrade(uint256,uint8)",
  "startResearch(uint256,uint8)",
  "resolveFleetMission(uint256)",
  // settlePlanet(uint256) was here through the prior phase. Removed 2026-08-17 (Phase
  // 5, docs/SPEC.md §5.4/§9 -- a breaking allowlist change): at the pinned commit its
  // body is `_touchPlayer(msg.sender); _collectPlanetResources(planetId);`, byte-
  // identical to `collectResources`, which `abi.ts`'s NONPAYABLE_READ_FUNCTIONS already
  // refuses in `sendTx` as a disguised read. It was allowlisted here and in
  // veydrift-agent's `guard.py` (`_MIN_TIER_FOR_FUNCTION`), with a live `tick.py`
  // encoder branch, but no planner rung ever produced this action -- allowlisted
  // capacity that could only ever burn gas for zero effect. Removed from all three
  // together, or the agent-side test_tier_map_agrees_with_the_wallet_engines_allowlist
  // fails.
  "startDefenseProduction(uint256,uint8,uint32)",
  // Added 2026-08-12. plan.py's rung 8 proposes ship production when policy.actions.allow_ships
  // is enabled, but no tier previously granted the selector, so such proposals could never be
  // submitted at any tier. Producing ships spends resources on your own planet -- the same risk
  // profile as startDefenseProduction above. Combat remains gated separately by mission type on
  // launchFleetMission (operator only, types 0 Transport / 1 Deploy / 2 Colonize / 4 Harvest --
  // see OPERATOR_ALLOWED_MISSION_TYPES below; Colonize added 2026-08-17, Phase 5b).
  "startShipProduction(uint256,uint8,uint32)",
] as const;

/** Both overloaded forms of launchFleetMission on the deployed ABI (trap #2). Both are allowed at
 *  the selector level for `operator`; the mission-type restriction is enforced separately below
 *  by decoding calldata, because it cannot be expressed as a selector-only check. */
const LAUNCH_FLEET_MISSION_SIGNATURES = [
  "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint16,uint256)",
  "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint256)",
] as const;

/**
 * FleetMissionType values `launchFleetMission` may submit at `operator` tier.
 * VeydriftGameStorage.sol:166-177 declares the full enum (10 members); this set is a
 * deliberate default-deny allowlist of it, not "everything the enum has," mirrored
 * exactly by `veydrift-agent`'s `guard.py` `_ALLOWED_MISSION_TYPES` (Phase 5c,
 * docs/SPEC.md §5.5) -- `test_tier_map_agrees_with_the_wallet_engines_allowlist`
 * (agent-side) parses both and fails naming the diff if they ever drift.
 *
 * - **0 Transport, 1 Deploy, 4 Harvest** — non-combat logistics between/around the
 *   player's own planets. Present since this constant was introduced.
 * - **2 Colonize** — added 2026-08-17 (Phase 5b, docs/SPEC.md §9). The only widening
 *   this allowlist has ever had. Confirmed as a genuine colonisation entrypoint, not
 *   combat-adjacent: `VeydriftGame.sol`'s `launchFleetMission` facade dispatches
 *   `missionType == Colonize` to `VeydriftColonizationModule`;
 *   `_launchColonizeFleetMission` -> `_validateColonyCreation` ->
 *   `_requireShips(originPlanetId, Ship.ColonyShip, 1)` (docs/RESEARCH-ADDENDUM.md §4,
 *   `veydrift-agent/references/contract-writes.md` §1). Widened here only in the same
 *   change that adds `guard.py`'s `mission_type` gate -- widening this set first, before
 *   that Python-side gate existed, would have reopened the single-layer-enforcement gap
 *   this allowlist alone used to cover (AGENTS.md §5).
 * - **3 Attack, 5 AcsDefend, 6 Intercept, 7 MissileAttack, 8 AcsAttack, 9 DefenseHold**
 *   — combat, and stay refused unconditionally. `AGENTS.md` §5: "combat stays
 *   unreachable by code, not by config" -- `policy.json`'s `allow_combat` is read and
 *   ignored everywhere; enabling any of these requires an actual source change to both
 *   this set and `guard.py`'s, never a policy flag.
 */
export const OPERATOR_ALLOWED_MISSION_TYPES: ReadonlySet<number> = new Set([0, 1, 2, 4]);

/** Tier -> allowed 4-byte selectors, computed from the pinned ABI (never a hardcoded hex list).
 *  `advisor` is deliberately empty: it may build and simulate, but the empty set means the
 *  allowlist itself refuses every `send`, independent of any other guard. */
export function tierSelectors(tier: Tier): ReadonlySet<`0x${string}`> {
  const signatures: readonly string[] =
    tier === "advisor"
      ? []
      : tier === "economy"
        ? ECONOMY_SIGNATURES
        : [...ECONOMY_SIGNATURES, ...LAUNCH_FLEET_MISSION_SIGNATURES];

  const selectors = new Set<`0x${string}`>();
  for (const sig of signatures) {
    // Resolving through the pinned ABI (rather than hashing the string ourselves) means a drift
    // between this list and the actual deployed ABI throws here instead of allowlisting a
    // selector nobody verified exists.
    const fn = resolveFunctionAbi(sig);
    selectors.add(getSelector(fn));
  }
  return selectors;
}

function launchFleetMissionSelectorSet(): ReadonlyMap<`0x${string}`, string> {
  const map = new Map<`0x${string}`, string>();
  for (const sig of LAUNCH_FLEET_MISSION_SIGNATURES) {
    const fn = resolveFunctionAbi(sig);
    map.set(getSelector(fn), sig);
  }
  return map;
}

export interface AllowlistCheck {
  name: string;
  ok: boolean;
  detail?: string;
}

export interface AllowlistResult {
  ok: boolean;
  tier: Tier;
  reason?: string;
  checks: AllowlistCheck[];
}

/**
 * Run every check and report all of them (never short-circuit the report -- the full verdict
 * list is the audit artifact, matching the agent skill's `vd guard` convention). `ok` is the AND
 * of every check.
 */
export async function checkAllowlist(
  tx: UnsignedTx,
  tier: Tier,
  opts: { fetchConfig?: RuntimeConfigFetcher } = {},
): Promise<AllowlistResult> {
  const fetchConfig = opts.fetchConfig ?? fetchLiveRuntimeConfig;
  const checks: AllowlistCheck[] = [];
  const fail = (name: string, detail: string) => checks.push({ name, ok: false, detail });
  const pass = (name: string, detail?: string) => checks.push({ name, ok: true, detail });

  // 4. chainId == 8453
  if (tx.chainId !== 8453) {
    fail("chainId", `expected 8453 (Base), got ${tx.chainId}`);
  } else {
    pass("chainId", "8453");
  }

  // 3. value == 0 -- no payable action is whitelisted at any tier reachable from this engine.
  if (tx.value !== 0n) {
    fail("value", `expected 0 wei, got ${tx.value.toString()} wei`);
  } else {
    pass("value", "0");
  }

  // 1. tx.to in the LIVE runtime-config address set.
  let toChecksum: `0x${string}` | undefined;
  try {
    toChecksum = getAddress(tx.to);
  } catch {
    fail("address", `"${tx.to}" is not a validly-formatted address`);
  }
  if (toChecksum) {
    try {
      const config = await fetchConfig();
      const candidates = [config.gameContractAddress, config.contractAddress].filter(
        (a: string | undefined): a is string => typeof a === "string" && a.length > 0,
      );
      const liveAddresses = new Set(candidates.map((a) => getAddress(a)));
      if (liveAddresses.size === 0) {
        fail("address", "live /runtime-config returned no contract address to check against");
      } else if (!liveAddresses.has(toChecksum)) {
        fail(
          "address",
          `${toChecksum} is not a live Veydrift contract address (live set: ${[...liveAddresses].join(", ")})`,
        );
      } else {
        pass("address", toChecksum);
      }
    } catch (err) {
      fail("address", `could not fetch live /runtime-config: ${(err as Error).message}`);
    }
  }

  // 2. selector in the tier's allowed set (computed from the pinned ABI).
  const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
  let allowedSelectors: ReadonlySet<`0x${string}`>;
  try {
    allowedSelectors = tierSelectors(tier);
  } catch (err) {
    fail("selector", `could not compute "${tier}" tier's selector set: ${(err as Error).message}`);
    allowedSelectors = new Set();
  }
  if (!allowedSelectors.has(selector)) {
    fail("selector", `${selector} is not in the "${tier}" tier's allowed set`);
  } else {
    pass("selector", `${selector} allowed at tier "${tier}"`);
  }

  // Extra: operator's launchFleetMission is restricted to mission types 0 Transport / 1 Deploy /
  // 2 Colonize / 4 Harvest (OPERATOR_ALLOWED_MISSION_TYPES). This is a calldata-level check -- the
  // mission type is an ordinary argument, not part of the selector -- so it only runs once we know
  // the selector is one of the two launchFleetMission overloads.
  const launchFleetSelectors = launchFleetMissionSelectorSet();
  if (launchFleetSelectors.has(selector)) {
    if (tier !== "operator") {
      fail("launchFleetMission", `launchFleetMission is only reachable at tier "operator", not "${tier}"`);
    } else {
      const sig = launchFleetSelectors.get(selector) as string;
      try {
        const fn = resolveFunctionAbi(sig);
        const decoded = decodeFunctionData({ abi: [fn], data: tx.data });
        const missionType = Number(decoded.args?.[2]);
        if (!OPERATOR_ALLOWED_MISSION_TYPES.has(missionType)) {
          fail(
            "launchFleetMission.missionType",
            `missionType=${missionType} is not in the operator-allowed set {0 Transport, 1 Deploy, 2 Colonize, 4 Harvest}`,
          );
        } else {
          pass("launchFleetMission.missionType", String(missionType));
        }
      } catch (err) {
        fail("launchFleetMission.missionType", `could not decode calldata: ${(err as Error).message}`);
      }
    }
  }

  const ok = checks.every((c) => c.ok);
  return {
    ok,
    tier,
    checks,
    reason: ok ? undefined : checks.filter((c) => !c.ok).map((c) => `${c.name}: ${c.detail}`).join("; "),
  };
}
