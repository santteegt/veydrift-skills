import { describe, expect, it } from "vitest";
import { encodeAbiParameters, encodeFunctionData } from "viem";
import {
  computePinnedAbiHash,
  decodeSimulateReturnData,
  findFunctionsByName,
  functionsForSelector,
  getPinnedAbi,
  getSelector,
  getSelectorForSignature,
  isNonpayableRead,
  loadPinnedMeta,
  NONPAYABLE_READ_FUNCTIONS,
  resolveFunctionAbi,
} from "../src/abi.js";

// Live /runtime-config `backend.build.deploymentAbiHash` / `deploymentCommit`, re-probed
// 2026-09-07 after the on-chain contract upgrade. Reproduced locally by `forge build` at the
// reported deploymentCommit (see references/abi-pinning.md). Prior pin was
// sha256:62cdedb794d4aa11cce1e9ef61e26f12227ce40a3bf47dd6156db6dc5676bc99 at commit
// 701bed3578cff4d134657c714c599dbdb55a4b6a.
const EXPECTED_HASH = "sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4";
const EXPECTED_COMMIT = "202d1acd9e35d815bd66cb9bae744341b1b1cf9e";

describe("pinned ABI", () => {
  it("hashes to the spec-pinned value", () => {
    expect(computePinnedAbiHash()).toBe(EXPECTED_HASH);
  });

  it("PINNED.json records the same hash and the correct deployment commit", () => {
    const meta = loadPinnedMeta();
    expect(meta.abiHash).toBe(EXPECTED_HASH);
    expect(meta.commit).toBe(EXPECTED_COMMIT);
  });

  // The 2026-09-07 on-chain upgrade (commit 202d1ac) reversed the pre-upgrade main-vs-deployed
  // divergence for these two: `playerScore` is now ON the deployed contract, and
  // `firstPlanetOf`/`hasFirstPlanet`/`previewFirstPlanet` were removed from it. These
  // assertions pin that reversal so a careless rebuild against an older commit is caught.
  it("DOES contain playerScore -- added to the deployed contract in the 2026-09-07 upgrade", () => {
    expect(findFunctionsByName("playerScore").length).toBeGreaterThan(0);
  });

  it("does NOT contain firstPlanetOf -- removed from the deployed contract in the 2026-09-07 upgrade", () => {
    expect(findFunctionsByName("firstPlanetOf")).toHaveLength(0);
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
      "sha256:393335c106ecf203eb63d93d21c27b51e10fb4a217a5fd6de5feb63132999535",
    );
    expect(meta.commit).toBe("202d1acd9e35d815bd66cb9bae744341b1b1cf9e");
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

// ACS defense coordination feature: `walletctl simulate --json`'s decode step is what lets
// tick.py read `canCoordinate`/`netHoldingFuelCost` back from a view-function pre-check call
// without ever touching raw hex itself -- see references/coordination.md.
describe("decodeSimulateReturnData", () => {
  it("decodes a real view function's (canCoordinate, netHoldingFuelCost, depotSupport) tuple", () => {
    const fn = resolveFunctionAbi(
      "counterplayDefenseFuelContext(address,uint256,uint256,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),uint256)",
      "alliance",
    );
    const selector = getSelector(fn);
    const returnData = encodeAbiParameters(fn.outputs, [true, 12345n, 6789n]);

    const decoded = decodeSimulateReturnData(selector, returnData);

    expect(decoded).toEqual({ canCoordinate: true, netHoldingFuelCost: "12345", depotSupport: "6789" });
  });

  it("returns undefined for a function with no outputs", () => {
    const fn = resolveFunctionAbi("leaveAlliance()", "alliance");
    const selector = getSelector(fn);
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [] }).slice(0, 10) as `0x${string}`;

    expect(decodeSimulateReturnData(selector, data)).toBeUndefined();
  });

  it("returns undefined when returnData is undefined", () => {
    const fn = resolveFunctionAbi(
      "defenseHoldFuelContext(address,uint256,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),uint256)",
      "alliance",
    );
    expect(decodeSimulateReturnData(getSelector(fn), undefined)).toBeUndefined();
  });

  it("returns undefined (never throws) on malformed returnData rather than masking a successful simulation", () => {
    const fn = resolveFunctionAbi(
      "defenseHoldFuelContext(address,uint256,(uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32,uint32),uint256)",
      "alliance",
    );
    expect(decodeSimulateReturnData(getSelector(fn), "0xdead")).toBeUndefined();
  });

  it("returns undefined for an unresolvable selector", () => {
    expect(
      decodeSimulateReturnData("0xdeadbeef", "0x0000000000000000000000000000000000000000000000000000000000000001"),
    ).toBeUndefined();
  });
});
