import { describe, expect, it } from "vitest";
import { encodeFunctionData, getAddress } from "viem";
import type { RuntimeConfig } from "../src/abi.js";
import { resolveFunctionAbi } from "../src/abi.js";
import { checkAllowlist, tierSelectors } from "../src/allowlist.js";
import { ShipId, shipCountsToFleetTuple } from "../src/fleet.js";
import type { UnsignedTx } from "../src/providers/types.js";

// The live gameContractAddress from a real /runtime-config fetch (2026-08-12), used here only as
// a fixture value -- the allowlist itself always re-fetches live in production code.
const GAME_ADDRESS = getAddress("0xf397910F005151b09644228573a4353818D3755d");
const NON_VEYDRIFT_ADDRESS = getAddress("0x000000000000000000000000000000000000dead" as `0x${string}`);

function fixtureConfig(): RuntimeConfig {
  return {
    chainId: 8453,
    contractAddress: GAME_ADDRESS,
    gameContractAddress: GAME_ADDRESS,
    backend: { build: { deploymentAbiHash: "sha256:fixture", deploymentCommit: "fixture" } },
  };
}

function startBuildingUpgradeTx(overrides: Partial<UnsignedTx> = {}): UnsignedTx {
  const fn = resolveFunctionAbi("startBuildingUpgrade(uint256,uint8)");
  const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n, 3] });
  return { to: GAME_ADDRESS, data, value: 0n, chainId: 8453, ...overrides };
}

const SIX_ARG_LAUNCH_SIG =
  "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint256)";

function launchFleetMissionTx(missionType: number): UnsignedTx {
  const fn = resolveFunctionAbi(SIX_ARG_LAUNCH_SIG);
  const fleet = shipCountsToFleetTuple({ [ShipId.SmallCargo]: 1 });
  const data = encodeFunctionData({
    abi: [fn],
    functionName: fn.name,
    args: [664n, 665n, missionType, fleet, [0n, 0n, 0n], 100n],
  });
  return { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };
}

describe("checkAllowlist", () => {
  it("allows an economy-tier action against the live game address", async () => {
    const result = await checkAllowlist(startBuildingUpgradeTx(), "economy", {
      fetchConfig: async () => fixtureConfig(),
    });
    expect(result.ok).toBe(true);
    expect(result.checks.every((c) => c.ok)).toBe(true);
  });

  it("rejects a tx to a non-Veydrift address", async () => {
    const tx = startBuildingUpgradeTx({ to: NON_VEYDRIFT_ADDRESS });
    const result = await checkAllowlist(tx, "economy", { fetchConfig: async () => fixtureConfig() });
    expect(result.ok).toBe(false);
    expect(result.checks.find((c) => c.name === "address")?.ok).toBe(false);
    expect(result.reason).toMatch(/not a live Veydrift contract address/);
  });

  it("rejects every selector at the advisor tier -- advisor may build/simulate but never send", async () => {
    const result = await checkAllowlist(startBuildingUpgradeTx(), "advisor", {
      fetchConfig: async () => fixtureConfig(),
    });
    expect(result.ok).toBe(false);
    expect(result.checks.find((c) => c.name === "selector")?.ok).toBe(false);
    expect(tierSelectors("advisor").size).toBe(0);
  });

  // This case used startShipProduction until 2026-08-12, when that became a legitimate
  // economy-tier action (docs/SPEC.md §4). abandonPlanet is a better example anyway: it is
  // irreversible and destroys the planet, so it must be outside EVERY tier's set, forever.
  it("rejects a selector outside the tier's allowed set (abandonPlanet is in no tier)", async () => {
    const fn = resolveFunctionAbi("abandonPlanet(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };
    for (const tier of ["advisor", "economy", "operator"] as const) {
      const result = await checkAllowlist(tx, tier, { fetchConfig: async () => fixtureConfig() });
      expect(result.ok).toBe(false);
      expect(result.checks.find((c) => c.name === "selector")?.ok).toBe(false);
    }
  });

  it("allows startShipProduction at economy (regression: it was previously in no tier)", async () => {
    const fn = resolveFunctionAbi("startShipProduction(uint256,uint8,uint32)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n, 0, 1] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };
    const result = await checkAllowlist(tx, "economy", { fetchConfig: async () => fixtureConfig() });
    expect(result.checks.find((c) => c.name === "selector")?.ok).toBe(true);
    const atAdvisor = await checkAllowlist(tx, "advisor", { fetchConfig: async () => fixtureConfig() });
    expect(atAdvisor.ok).toBe(false);
  });

  it("rejects nonzero value at every tier", async () => {
    const tx = startBuildingUpgradeTx({ value: 1n });
    const result = await checkAllowlist(tx, "economy", { fetchConfig: async () => fixtureConfig() });
    expect(result.ok).toBe(false);
    expect(result.checks.find((c) => c.name === "value")?.ok).toBe(false);
  });

  it("rejects the wrong chainId", async () => {
    const tx = startBuildingUpgradeTx({ chainId: 1 });
    const result = await checkAllowlist(tx, "economy", { fetchConfig: async () => fixtureConfig() });
    expect(result.ok).toBe(false);
    expect(result.checks.find((c) => c.name === "chainId")?.ok).toBe(false);
  });

  it("evaluates and reports every check, never short-circuiting the verdict list", async () => {
    const tx = startBuildingUpgradeTx({ to: NON_VEYDRIFT_ADDRESS, value: 1n, chainId: 1 });
    const result = await checkAllowlist(tx, "advisor", { fetchConfig: async () => fixtureConfig() });
    const names = result.checks.map((c) => c.name);
    expect(names).toEqual(expect.arrayContaining(["chainId", "value", "address", "selector"]));
    expect(result.checks.filter((c) => !c.ok).length).toBeGreaterThanOrEqual(4);
  });

  describe("operator tier's launchFleetMission mission-type restriction", () => {
    // Was `it.each([0, 1, 4])` until 2026-08-17 (Phase 5b, docs/SPEC.md §9): mission type 2
    // (Colonize) added -- the only widening OPERATOR_ALLOWED_MISSION_TYPES has had, added in the
    // same change as veydrift-agent's guard.py mission_type gate (see allowlist.ts's own comment
    // on the constant for the confirmation that Colonize is a genuine colonisation entrypoint,
    // not combat-adjacent).
    it.each([0, 1, 2, 4])("allows mission type %i (Transport/Deploy/Colonize/Harvest)", async (missionType) => {
      const result = await checkAllowlist(launchFleetMissionTx(missionType), "operator", {
        fetchConfig: async () => fixtureConfig(),
      });
      expect(result.ok).toBe(true);
    });

    // Was `it.each([2, 3, 5, 6, 7, 8, 9])` until 2026-08-17 (Phase 5b): 2 (Colonize) moved to the
    // "allows" list above. 3 (Attack) moved to its own describe block below on 2026-08-28
    // (launch-actions plan, commit 5) -- it is no longer unconditionally rejected, only rejected
    // when policy.actions.allow_combat resolves false. The remaining five are always refused,
    // regardless of allow_combat -- AGENTS.md §5's "combat stays unreachable by code, not by
    // config" still governs every mission type this flag does not name.
    it.each([5, 6, 7, 8, 9])(
      "rejects mission type %i even with allow_combat=true -- allow_combat widens only Attack",
      async (missionType) => {
        const result = await checkAllowlist(launchFleetMissionTx(missionType), "operator", {
          fetchConfig: async () => fixtureConfig(),
          resolveAllowCombat: () => true,
        });
        expect(result.ok).toBe(false);
        expect(result.checks.find((c) => c.name === "launchFleetMission.missionType")?.ok).toBe(false);
      },
    );

    describe("mission type 3 (Attack) -- conditional on policy.actions.allow_combat (launch-actions plan, commit 5)", () => {
      it("rejects Attack when allow_combat resolves false", async () => {
        const result = await checkAllowlist(launchFleetMissionTx(3), "operator", {
          fetchConfig: async () => fixtureConfig(),
          resolveAllowCombat: () => false,
        });
        expect(result.ok).toBe(false);
        expect(result.checks.find((c) => c.name === "launchFleetMission.missionType")?.ok).toBe(false);
        expect(result.reason).toMatch(/allow_combat=true/);
      });

      it("allows Attack when allow_combat resolves true", async () => {
        const result = await checkAllowlist(launchFleetMissionTx(3), "operator", {
          fetchConfig: async () => fixtureConfig(),
          resolveAllowCombat: () => true,
        });
        expect(result.ok).toBe(true);
        expect(result.checks.find((c) => c.name === "launchFleetMission.missionType")?.ok).toBe(true);
      });

      it("rejects (never passes vacuously) when resolveAllowCombat throws", async () => {
        const boom = () => {
          throw new Error("policy file is malformed");
        };
        const result = await checkAllowlist(launchFleetMissionTx(3), "operator", {
          fetchConfig: async () => fixtureConfig(),
          resolveAllowCombat: boom,
        });
        expect(result.ok).toBe(false);
        expect(result.checks.find((c) => c.name === "launchFleetMission.missionType")?.ok).toBe(false);
        expect(result.reason).toMatch(/could not be resolved/);
      });

      it("is not resolved at all for a non-Attack mission type -- lazy, never called unconditionally", async () => {
        let called = false;
        const result = await checkAllowlist(launchFleetMissionTx(0), "operator", {
          fetchConfig: async () => fixtureConfig(),
          resolveAllowCombat: () => {
            called = true;
            return true;
          },
        });
        expect(result.ok).toBe(true);
        expect(called).toBe(false);
      });
    });

    it("economy tier rejects launchFleetMission entirely, regardless of mission type", async () => {
      const result = await checkAllowlist(launchFleetMissionTx(0), "economy", {
        fetchConfig: async () => fixtureConfig(),
      });
      expect(result.ok).toBe(false);
      expect(result.checks.find((c) => c.name === "selector")?.ok).toBe(false);
    });
  });

  // Was 5 until 2026-08-12 (startShipProduction added: plan.py's rung 8 proposes ships when
  // policy.actions.allow_ships is set, but no tier granted the selector, making that knob dead
  // config). Was 6 until 2026-08-17 (Phase 5, docs/SPEC.md §5.4/§9): settlePlanet removed -- its
  // body at the pinned commit is byte-identical to collectResources, a disguised read this
  // engine already refuses to send, and no planner rung ever produced the action.
  it("tierSelectors('economy') contains exactly the five spec'd selectors", () => {
    const selectors = tierSelectors("economy");
    expect(selectors.size).toBe(5);
  });

  it("tierSelectors('operator') is economy's five plus both launchFleetMission overloads", () => {
    const economy = tierSelectors("economy");
    const operator = tierSelectors("operator");
    expect(operator.size).toBe(economy.size + 2);
    for (const s of economy) expect(operator.has(s)).toBe(true);
  });

  it("settlePlanet is no longer allowlisted at any tier (Phase 5 removal)", async () => {
    const fn = resolveFunctionAbi("settlePlanet(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };
    for (const tier of ["advisor", "economy", "operator"] as const) {
      const result = await checkAllowlist(tx, tier, { fetchConfig: async () => fixtureConfig() });
      expect(result.ok).toBe(false);
      expect(result.checks.find((c) => c.name === "selector")?.ok).toBe(false);
    }
  });
});
