/**
 * The wallet-engine allowlist. Enforced HERE, independently of `veydrift-agent` -- a fully
 * compromised agent skill must still be unable to make `walletctl` sign anything outside
 * Veydrift. Spec: docs/SPEC.md §6.4.
 *
 * Five checks, all evaluated (never short-circuited in the report, though `ok` is AND of all):
 *   1. tx.to        in the live /runtime-config address set (never a hardcoded list -- now
 *      includes the alliance contract's address alongside the game contract's, see the
 *      alliance feature's notes below)
 *   2. tx.data[0:4] (selector) in the tier's allowed set, computed from the pinned ABI --
 *      unconditionally, OR conditionally on a policy flag for two disjoint selector sets:
 *      `policy.actions.allow_combat` at `operator` tier only (`launchInterplanetaryMissileAttack`,
 *      commit 7 -- see `COMBAT_SIGNATURES` below), or `policy.actions.allow_alliance` at
 *      `economy` tier **or above** (the 15 membership functions on `VeydriftAllianceSystem`,
 *      the alliance feature -- see `ALLIANCE_SIGNATURES` below). Note the "or above": unlike
 *      combat, alliance's tier requirement is a floor, not a ceiling -- an operator-tier wallet
 *      with `allow_alliance=true` must not be locked out just because `economy` is the minimum,
 *      not the maximum, tier alliance actions need.
 *   3. tx.value      == 0 (no payable action is whitelisted at any tier here)
 *   4. tx.chainId    == 8453 (Base)
 *   5. any failure -> caller must exit non-zero, log the rejection, sign nothing
 *
 * Plus one extra, spec-mandated restriction that lives at the calldata level rather than the
 * selector level: at `operator` tier, `launchFleetMission` is only allowed for mission types
 * Transport(0) / Deploy(1) / Colonize(2) / Harvest(4) unconditionally, plus Attack(3) when
 * `policy.actions.allow_combat` resolves true (launch-actions plan, commit 5 -- see
 * `COMBAT_ALLOWED_MISSION_TYPES` below). That restriction can't be expressed as a selector check
 * (the mission type is a regular argument, not part of the selector) so it's decoded from
 * calldata here. Missile (commit 7) and the alliance functions both needed no equivalent
 * calldata decode -- each is its own, brand-new selector on its own function, not a
 * `launchFleetMission` mission-type argument, so their policy-flag conditionality is folded
 * directly into check 2 instead.
 */

import { decodeFunctionData, getAddress } from "viem";
import { fetchLiveRuntimeConfig, getSelector, resolveFunctionAbi, type RuntimeConfig } from "./abi.js";
import {
  resolveAllowAlliance as resolveAllowAllianceFromPolicy,
  resolveAllowCombat as resolveAllowCombatFromPolicy,
} from "./policy.js";
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
 * FleetMissionType values `launchFleetMission` may submit at `operator` tier,
 * unconditionally -- no policy flag affects this set.
 * VeydriftGameStorage.sol:166-177 declares the full enum (10 members); this set is a
 * deliberate default-deny allowlist of it, not "everything the enum has," mirrored
 * exactly by `veydrift-agent`'s `guard.py` `_ALLOWED_MISSION_TYPES` (Phase 5c,
 * docs/SPEC.md §5.5) -- `test_tier_map_agrees_with_the_wallet_engines_allowlist`
 * (agent-side) parses both and fails naming the diff if they ever drift.
 *
 * - **0 Transport, 1 Deploy, 4 Harvest** — non-combat logistics between/around the
 *   player's own planets. Present since this constant was introduced.
 * - **2 Colonize** — added 2026-08-17 (Phase 5b, docs/SPEC.md §9). Confirmed as a
 *   genuine colonisation entrypoint, not combat-adjacent: `VeydriftGame.sol`'s
 *   `launchFleetMission` facade dispatches `missionType == Colonize` to
 *   `VeydriftColonizationModule`; `_launchColonizeFleetMission` ->
 *   `_validateColonyCreation` -> `_requireShips(originPlanetId, Ship.ColonyShip, 1)`
 *   (docs/RESEARCH-ADDENDUM.md §4, `veydrift-agent/references/contract-writes.md` §1).
 *   Widened here only in the same change that added `guard.py`'s `mission_type` gate --
 *   widening this set first, before that Python-side gate existed, would have reopened
 *   the single-layer-enforcement gap this allowlist alone used to cover (AGENTS.md §5).
 *
 * See `COMBAT_ALLOWED_MISSION_TYPES` below for Attack (3) -- gated on
 * `policy.actions.allow_combat`, checked separately, never merged into this set. The
 * remaining combat types (5 AcsDefend, 6 Intercept, 7 MissileAttack, 8 AcsAttack,
 * 9 DefenseHold) appear in neither set and stay refused unconditionally at every tier,
 * regardless of policy -- all five are alliance-coordination or Attack-adjacent
 * mission types this codebase has no other write path for (no
 * `joinAttackMission`/`launchInterplanetaryMissileAttack`/`launchDefenseHold`
 * allowlisting exists either); enabling any of them requires an actual source change to
 * both this file and `guard.py`'s, never a policy flag alone.
 */
export const OPERATOR_ALLOWED_MISSION_TYPES: ReadonlySet<number> = new Set([0, 1, 2, 4]);

/**
 * FleetMissionType values that are permitted at `operator` tier only when
 * `policy.actions.allow_combat` resolves `true` (`resolveAllowCombat`, `policy.ts`) --
 * launch-actions plan, commit 5. Deliberately its own set, not merged into
 * `OPERATOR_ALLOWED_MISSION_TYPES`, so the unconditional-vs-conditional distinction stays
 * visible at a glance and the cross-layer test (agent-side
 * `test_tier_map_agrees_with_the_wallet_engines_allowlist`) can diff both halves against
 * `guard.py`'s matching two sets independently.
 *
 * Only **3 Attack**. The other combat mission types (5, 6, 7, 8, 9) are not added here --
 * they are alliance-coordination (AcsDefend, AcsAttack, DefenseHold) or otherwise require
 * their own separate contract entrypoints/preconditions this codebase does not implement;
 * `allow_combat` widens exactly the one mission type this codebase's own `launchFleetMission`
 * generator (once built) can actually produce, not "combat" as an undifferentiated whole.
 */
export const COMBAT_ALLOWED_MISSION_TYPES: ReadonlySet<number> = new Set([3]);

/**
 * Full canonical signature for `launchInterplanetaryMissileAttack` (commit 7 of the
 * launch-actions plan). Unlike Attack (mission type 3 on `launchFleetMission`), a missile
 * is its own, brand-new selector -- it shares nothing with the fleet path (no fleet
 * tuple, no mission type argument, no fleet slot, no travel time; fully synchronous,
 * confirmed by reading `VeydriftPlanetManagementModule.sol`'s
 * `launchInterplanetaryMissileAttack` directly). Permitted only at `operator` tier AND
 * only when `policy.actions.allow_combat` resolves `true` (`resolveAllowCombat`,
 * `policy.ts`) -- the same master combat flag Attack uses, checked the same lazy way (see
 * `checkAllowlist`'s selector check below), never merged into `tierSelectors`'s
 * unconditional set.
 */
const COMBAT_SIGNATURES = [
  "launchInterplanetaryMissileAttack(uint256,uint256,uint8,uint32)",
] as const;

function combatSelectorSet(): ReadonlySet<`0x${string}`> {
  const selectors = new Set<`0x${string}`>();
  for (const sig of COMBAT_SIGNATURES) {
    const fn = resolveFunctionAbi(sig);
    selectors.add(getSelector(fn));
  }
  return selectors;
}

/**
 * The 15 in-scope alliance-membership functions on `VeydriftAllianceSystem` (a wholly separate
 * pinned contract -- `abi.ts`'s `resolveFunctionAbi(sig, "alliance")` resolves against it, never
 * the game ABI). Permitted at `economy` tier **or above**, and only when
 * `policy.actions.allow_alliance` resolves `true` (`resolveAllowAlliance`, `policy.ts`) -- see
 * `checkAllowlist`'s selector check below for exactly why this is an inclusive-tier check,
 * unlike combat's single-tier one. Diplomacy (`setDiplomacy`) and ACS coordination
 * (`openDefenseIntent`) are deliberately NOT here -- combat-adjacent, out of scope for this
 * phase.
 */
const ALLIANCE_SIGNATURES = [
  "createAlliance(string,string,string)",
  "updateAllianceProfile(uint256,string,string,string)",
  "inviteMember(uint256,address)",
  "cancelInvite(uint256,address)",
  "acceptInvite(uint256)",
  "requestJoinAlliance(uint256)",
  "cancelJoinRequest(uint256)",
  "dismissJoinRequest(uint256,address)",
  "approveJoinRequest(uint256,address)",
  "kickMember(uint256,address)",
  "kickMembers(uint256,address[])",
  "leaveAlliance()",
  "setMemberRole(uint256,address,uint8)",
  "setMembersRole(uint256,address[],uint8)",
  "transferAllianceOwnership(uint256,address)",
] as const;

function allianceSelectorSet(): ReadonlySet<`0x${string}`> {
  const selectors = new Set<`0x${string}`>();
  for (const sig of ALLIANCE_SIGNATURES) {
    const fn = resolveFunctionAbi(sig, "alliance");
    selectors.add(getSelector(fn));
  }
  return selectors;
}

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
 *
 * `opts.resolveAllowCombat` (launch-actions plan, commit 5) and `opts.resolveAllowAlliance`
 * (the alliance feature) each default to the real resolver from `policy.ts` -- injectable purely
 * so tests never touch the real filesystem, the same pattern `opts.fetchConfig` already uses
 * toward the live network. Both are called **lazily**: `resolveAllowCombat` only once a decoded
 * `launchFleetMission` mission type is actually Attack (see below), `resolveAllowAlliance` only
 * once the selector is actually one of the 15 alliance functions -- a malformed or absent policy
 * field must never block an unrelated transaction, which is exactly what calling either
 * unconditionally for every `send` would do.
 */
export async function checkAllowlist(
  tx: UnsignedTx,
  tier: Tier,
  opts: {
    fetchConfig?: RuntimeConfigFetcher;
    resolveAllowCombat?: () => boolean;
    resolveAllowAlliance?: () => boolean;
  } = {},
): Promise<AllowlistResult> {
  const fetchConfig = opts.fetchConfig ?? fetchLiveRuntimeConfig;
  const resolveAllowCombat = opts.resolveAllowCombat ?? resolveAllowCombatFromPolicy;
  const resolveAllowAlliance = opts.resolveAllowAlliance ?? resolveAllowAllianceFromPolicy;
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
      const candidates = [
        config.gameContractAddress,
        config.contractAddress,
        config.allianceContractAddress,
      ].filter((a: string | undefined): a is string => typeof a === "string" && a.length > 0);
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

  // 2. selector in the tier's allowed set (computed from the pinned ABI). A combat
  // selector (currently only launchInterplanetaryMissileAttack, commit 7) is never in
  // this unconditional set -- it's checked separately below, lazily, the same posture
  // Attack's mission-type argument already takes (checkAllowlist's own doc comment).
  // Alliance selectors (the 15 VeydriftAllianceSystem membership functions) follow the
  // same lazy-conditional shape, but at an inclusive tier check (economy OR operator),
  // not a single tier -- see the branch below.
  const selector = tx.data.slice(0, 10).toLowerCase() as `0x${string}`;
  let allowedSelectors: ReadonlySet<`0x${string}`>;
  let combatSelectors: ReadonlySet<`0x${string}`>;
  let allianceSelectors: ReadonlySet<`0x${string}`>;
  try {
    allowedSelectors = tierSelectors(tier);
    combatSelectors = combatSelectorSet();
    allianceSelectors = allianceSelectorSet();
  } catch (err) {
    fail("selector", `could not compute "${tier}" tier's selector set: ${(err as Error).message}`);
    allowedSelectors = new Set();
    combatSelectors = new Set();
    allianceSelectors = new Set();
  }
  if (allowedSelectors.has(selector)) {
    pass("selector", `${selector} allowed at tier "${tier}"`);
  } else if (combatSelectors.has(selector) && tier === "operator") {
    // Lazy on purpose -- see checkAllowlist's own doc comment above. A malformed or
    // absent actions.allow_combat must never block an unrelated, non-combat
    // transaction, which is exactly what calling it unconditionally for every send
    // would do -- this selector IS the combat action, so it's safe to resolve here.
    try {
      if (resolveAllowCombat()) {
        pass("selector", `${selector} allowed at tier "${tier}" (combat, policy.actions.allow_combat=true)`);
      } else {
        fail("selector", `${selector} requires policy.actions.allow_combat=true; it is not`);
      }
    } catch (err) {
      fail(
        "selector",
        `${selector} requires policy.actions.allow_combat, but it could not be resolved: ${(err as Error).message}`,
      );
    }
  } else if (allianceSelectors.has(selector) && (tier === "economy" || tier === "operator")) {
    // Inclusive tier check, deliberately -- economy is alliance's FLOOR, not its ceiling (unlike
    // combat, which is only ever checked at the single top tier, operator). An operator-tier
    // wallet with allow_alliance=true must not be locked out of alliance actions just because
    // economy happens to be the minimum tier they need, not the maximum tier they're allowed at.
    try {
      if (resolveAllowAlliance()) {
        pass("selector", `${selector} allowed at tier "${tier}" (alliance, policy.actions.allow_alliance=true)`);
      } else {
        fail("selector", `${selector} requires policy.actions.allow_alliance=true; it is not`);
      }
    } catch (err) {
      fail(
        "selector",
        `${selector} requires policy.actions.allow_alliance, but it could not be resolved: ${(err as Error).message}`,
      );
    }
  } else {
    fail("selector", `${selector} is not in the "${tier}" tier's allowed set`);
  }

  // Extra: operator's launchFleetMission is restricted to mission types 0 Transport / 1 Deploy /
  // 2 Colonize / 4 Harvest unconditionally, plus 3 Attack when policy.actions.allow_combat
  // resolves true (launch-actions plan, commit 5). This is a calldata-level check -- the mission
  // type is an ordinary argument, not part of the selector -- so it only runs once we know the
  // selector is one of the two launchFleetMission overloads.
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
        if (OPERATOR_ALLOWED_MISSION_TYPES.has(missionType)) {
          pass("launchFleetMission.missionType", String(missionType));
        } else if (COMBAT_ALLOWED_MISSION_TYPES.has(missionType)) {
          // Lazy on purpose -- see checkAllowlist's own doc comment above.
          try {
            if (resolveAllowCombat()) {
              pass("launchFleetMission.missionType", `${missionType} (combat, policy.actions.allow_combat=true)`);
            } else {
              fail(
                "launchFleetMission.missionType",
                `missionType=${missionType} (Attack) requires policy.actions.allow_combat=true; it is not`,
              );
            }
          } catch (err) {
            fail(
              "launchFleetMission.missionType",
              `missionType=${missionType} (Attack) requires policy.actions.allow_combat, but it could not ` +
                `be resolved: ${(err as Error).message}`,
            );
          }
        } else {
          fail(
            "launchFleetMission.missionType",
            `missionType=${missionType} is not in the allowed set {0 Transport, 1 Deploy, 2 Colonize, ` +
              `4 Harvest, 3 Attack (only with policy.actions.allow_combat=true)}`,
          );
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
