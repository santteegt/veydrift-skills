import { describe, expect, it } from "vitest";
import {
  computePinnedAbiHash,
  findFunctionsByName,
  functionsForSelector,
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

// Alliance feature: VeydriftAllianceSystem.sol is a wholly separate deployed contract, pinned
// as a sibling artifact/meta file, resolved via the same functions above with an explicit
// `contract: "alliance"` argument. See references/abi-pinning.md's "Second contract" section.
describe("pinned alliance ABI (VeydriftAllianceSystem)", () => {
  // Ground truth: forge's own methodIdentifiers from the pinned commit's build, cross-checked
  // against `cast sig` independently (not derived from our own encoder) at pin time.
  const ALLIANCE_SIGNATURES: [string, `0x${string}`][] = [
    ["createAlliance(string,string,string)", "0x944cde0e"],
    ["updateAllianceProfile(uint256,string,string,string)", "0x3fd0e7a5"],
    ["inviteMember(uint256,address)", "0x9e6d6830"],
    ["cancelInvite(uint256,address)", "0x93a900f0"],
    ["acceptInvite(uint256)", "0xbf8e9176"],
    ["requestJoinAlliance(uint256)", "0xbc46277a"],
    ["cancelJoinRequest(uint256)", "0xc5c4bdcc"],
    ["dismissJoinRequest(uint256,address)", "0xcd844a18"],
    ["approveJoinRequest(uint256,address)", "0x8ff388c7"],
    ["kickMember(uint256,address)", "0xbd0e667c"],
    ["kickMembers(uint256,address[])", "0x7c581707"],
    ["leaveAlliance()", "0xdabd761d"],
    ["setMemberRole(uint256,address,uint8)", "0xbfbb73f1"],
    ["setMembersRole(uint256,address[],uint8)", "0xe0c22e19"],
    ["transferAllianceOwnership(uint256,address)", "0xb1d3b1e4"],
  ];

  it("EXPECTED_ALLIANCE_HASH: PINNED.alliance.json hashes to the recorded value", () => {
    const meta = loadPinnedMeta("alliance");
    expect(computePinnedAbiHash("alliance")).toBe(meta.abiHash);
    expect(meta.abiHash).toBe(
      "sha256:3992c8215c0f1f6bb01dd8afdbc39514a79a1f3fd9b2f7be07056b131cd4de8f",
    );
    expect(meta.commit).toBe("701bed3578cff4d134657c714c599dbdb55a4b6a");
  });

  it("getPinnedAbi('alliance') returns a non-empty ABI, distinct from the game ABI", () => {
    const allianceAbi = getPinnedAbi("alliance");
    expect(allianceAbi.length).toBeGreaterThan(0);
    expect(allianceAbi.length).not.toBe(getPinnedAbi("game").length);
  });

  it.each(ALLIANCE_SIGNATURES)("%s resolves via resolveFunctionAbi(sig, 'alliance') -> %s", (sig, expected) => {
    const fn = resolveFunctionAbi(sig, "alliance");
    expect(getSelectorForSignature(sig)).toBe(expected);
    expect(fn.name).toBe(sig.slice(0, sig.indexOf("(")));
  });

  it("createAlliance is not present on the game ABI", () => {
    expect(findFunctionsByName("createAlliance", "game")).toHaveLength(0);
    expect(findFunctionsByName("createAlliance", "alliance").length).toBeGreaterThan(0);
  });

  it("regression: resolveFunctionAbi with no contract argument still only resolves game functions -- an alliance signature passed bare must throw, not silently start scanning both ABIs", () => {
    expect(() => resolveFunctionAbi("createAlliance(string,string,string)")).toThrow(/pinned "game" artifact/);
  });

  it("functionsForSelector finds an alliance function via the merged cross-contract search", () => {
    const matches = functionsForSelector("0xdabd761d"); // leaveAlliance()
    expect(matches.some((fn) => fn.name === "leaveAlliance")).toBe(true);
  });

  it("functionsForSelector still finds game-contract functions (the merge is additive, not a regression)", () => {
    const matches = functionsForSelector("0x165715e3"); // startBuildingUpgrade(uint256,uint8)
    expect(matches.some((fn) => fn.name === "startBuildingUpgrade")).toBe(true);
  });

  it("no alliance function is a disguised nonpayable read -- NONPAYABLE_READ_FUNCTIONS stays game-only", () => {
    for (const [sig] of ALLIANCE_SIGNATURES) {
      const fn = resolveFunctionAbi(sig, "alliance");
      expect(isNonpayableRead(fn.name)).toBe(false);
    }
  });

  it("none of the 15 in-scope membership functions is payable -- the wallet's blanket value!=0 refusal excludes nothing here", () => {
    // The full alliance ABI does contain one payable function -- upgradeToAndCall(address,bytes),
    // the standard UUPS owner-only upgrade entrypoint (payable by OZ convention) -- but it is not
    // one of the 15 in-scope membership functions and is owner-only besides, so it's irrelevant
    // to this codebase's reachable surface. Scope the assertion to the 15, not the whole ABI.
    for (const [sig] of ALLIANCE_SIGNATURES) {
      const fn = resolveFunctionAbi(sig, "alliance");
      expect(fn.stateMutability).not.toBe("payable");
    }
  });
});
