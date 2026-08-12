import { describe, expect, it } from "vitest";
import {
  computePinnedAbiHash,
  findFunctionsByName,
  getPinnedAbi,
  getSelectorForSignature,
  isNonpayableRead,
  loadPinnedMeta,
  NONPAYABLE_READ_FUNCTIONS,
  resolveFunctionAbi,
} from "../src/abi.js";

// From RESEARCH-ADDENDUM.md §1 and the live /runtime-config probe done for this work package
// (2026-08-12): backend.build.deploymentAbiHash at commit 701bed3578cff4d134657c714c599dbdb55a4b6a.
const EXPECTED_HASH = "sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99";
const EXPECTED_COMMIT = "701bed3578cff4d134657c714c599dbdb55a4b6a";

describe("pinned ABI", () => {
  it("hashes to the spec-pinned value", () => {
    expect(computePinnedAbiHash()).toBe(EXPECTED_HASH);
  });

  it("PINNED.json records the same hash and the correct deployment commit", () => {
    const meta = loadPinnedMeta();
    expect(meta.abiHash).toBe(EXPECTED_HASH);
    expect(meta.commit).toBe(EXPECTED_COMMIT);
  });

  it("does NOT contain playerScore -- main-only, reverts on the deployed contract (RESEARCH-ADDENDUM §1.1)", () => {
    expect(findFunctionsByName("playerScore")).toHaveLength(0);
  });

  it("DOES contain firstPlanetOf -- deployed-only, deleted on main (RESEARCH-ADDENDUM §1.1)", () => {
    expect(findFunctionsByName("firstPlanetOf").length).toBeGreaterThan(0);
  });

  it("getPinnedAbi returns a non-empty ABI", () => {
    expect(getPinnedAbi().length).toBeGreaterThan(0);
  });

  describe("launchFleetMission overload disambiguation (trap #2)", () => {
    it("resolving by bare name throws, listing both candidate signatures", () => {
      let error: Error | undefined;
      try {
        resolveFunctionAbi("launchFleetMission");
      } catch (err) {
        error = err as Error;
      }
      expect(error).toBeDefined();
      expect(error?.message).toMatch(/overloaded/);
      expect(error?.message).toContain("uint16,uint256");
      expect(error?.message).toContain("(uint128,uint128,uint128),uint256)");
    });

    it("resolves the 7-arg form by exact full signature", () => {
      const sig =
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint16,uint256)";
      const fn = resolveFunctionAbi(sig);
      expect(fn.inputs).toHaveLength(7);
    });

    it("resolves the 6-arg form by exact full signature, distinct from the 7-arg form", () => {
      const sig =
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint256)";
      const fn = resolveFunctionAbi(sig);
      expect(fn.inputs).toHaveLength(6);
    });

    it("the two overloads have different 4-byte selectors", () => {
      const sevenArg = getSelectorForSignature(
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint16,uint256)",
      );
      const sixArg = getSelectorForSignature(
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint256)",
      );
      expect(sevenArg).not.toBe(sixArg);
    });
  });

  // Ground truth for these came from `cast sig` (foundry), run independently against each
  // signature below and recorded here -- NOT derived from our own encoder. See also
  // tests/selectors.cast.test.ts, which re-runs `cast sig` live if foundry is available.
  describe("selectors cross-checked against `cast sig` (foundry), not our own encoder", () => {
    const cases: [string, `0x${string}`][] = [
      ["startBuildingUpgrade(uint256,uint8)", "0x165715e3"],
      ["startResearch(uint256,uint8)", "0x7f314b93"],
      ["resolveFleetMission(uint256)", "0xde09e7cf"],
      ["settlePlanet(uint256)", "0x921609d9"],
      ["startDefenseProduction(uint256,uint8,uint32)", "0xfec06283"],
      [
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint16,uint256)",
        "0x60eac16f",
      ],
      [
        "launchFleetMission(uint256,uint256,uint8,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),(uint128,uint128,uint128),uint256)",
        "0x28247df8",
      ],
      ["attackProtectionStatus(address,uint256)", "0x8a6b2246"],
    ];
    it.each(cases)("%s -> %s", (sig, expected) => {
      expect(getSelectorForSignature(sig)).toBe(expected);
    });
  });

  it("nonpayable-read trap list matches RESEARCH-ADDENDUM.md §4.1 exactly", () => {
    expect([...NONPAYABLE_READ_FUNCTIONS].sort()).toEqual(
      [
        "attackProtectionStatus",
        "collectResources",
        "debrisField",
        "maxRaidLoot",
        "protectedResources",
        "raidableResources",
      ].sort(),
    );
  });

  it("every nonpayable-read trap function is genuinely ABI-nonpayable (not view)", () => {
    for (const name of NONPAYABLE_READ_FUNCTIONS) {
      expect(isNonpayableRead(name)).toBe(true);
      const fns = findFunctionsByName(name);
      expect(fns.length).toBeGreaterThan(0);
      for (const fn of fns) {
        expect(fn.stateMutability).toBe("nonpayable");
      }
    }
  });

  it("isNonpayableRead is false for an ordinary write function", () => {
    expect(isNonpayableRead("startBuildingUpgrade")).toBe(false);
  });
});
