import { describe, expect, it } from "vitest";
import { encodeAbiParameters, encodeErrorResult, encodeFunctionData } from "viem";
import {
  classifyBackendHash,
  computeAbiHash,
  computePinnedAbiHash,
  decodeRevertData,
  decodeSimulateReturnData,
  describeRevert,
  extractRevertData,
  findFunctionsByName,
  functionsForSelector,
  getPinnedAbi,
  getResolvableAbi,
  getSelector,
  getSelectorForSignature,
  isNonpayableRead,
  loadPinnedMeta,
  NONPAYABLE_READ_FUNCTIONS,
  resolveFunctionAbi,
} from "../src/abi.js";

// The pin, re-derived 2026-09-28 after the game and alliance proxies' implementations changed
// on-chain (delegation + batch production). The deployed commit was CONFIRMED by matching the runtime
// code of every contract behind the proxies to a forge build of this commit (see PINNED.json's
// source.confirmation and references/abi-pinning.md). NOTE: this is deliberately NOT what the
// backend's /runtime-config reports -- that still says the previous pin below, recorded in
// PINNED.json as `backendReported` (known-stale). Previous pin: commit
// 202d1acd9e35d815bd66cb9bae744341b1b1cf9e, hash
// sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4.
const EXPECTED_HASH = "sha256:260b70d9a6d8051ef72c80bedc6b2453a75a98df539fd99abac6632f1bef30a9";
const EXPECTED_COMMIT = "2b329fb161b921a46966576be4eecd10573c7bef";
const STALE_BACKEND_HASH = "sha256:986ea81b6dbca8d86149cd3449849160d75d19ea692cd5c9d1900355ecf41ec4";

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
    // The alliance ABI is byte-identical to the previous pin (same hash); only the commit and the
    // implementation moved.
    expect(meta.commit).toBe(EXPECTED_COMMIT);
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

  it("deep-converts nested bigints in a struct (`Resources` tuple) output so the result is JSON-serializable", () => {
    // `previewResources(uint256) -> (uint128 metal, uint128 crystal, uint128 deuterium)`:
    // a single tuple output whose fields are bigints one level down. A flat top-level
    // pass leaves them as bigints and `JSON.stringify` then throws.
    const fn = resolveFunctionAbi("previewResources(uint256)");
    const selector = getSelector(fn);
    const returnData = encodeAbiParameters(fn.outputs, [
      { metal: 124861n, crystal: 15807n, deuterium: 26649n },
    ]);

    const decoded = decodeSimulateReturnData(selector, returnData);

    expect(decoded).toEqual({ _0: { metal: "124861", crystal: "15807", deuterium: "26649" } });
    expect(() => JSON.stringify(decoded)).not.toThrow();
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

// ---------------------------------------------------------------------------------------------
// The delegation entrypoints are served from the game proxy's fallback() and declared only in
// IVeydriftDelegation, so they live in a SUPPLEMENTAL pinned ABI merged for resolution -- never
// into the artifact the hash is computed over.
// ---------------------------------------------------------------------------------------------
describe("supplemental delegation ABI", () => {
  const DELEGATION: Array<[string, string]> = [
    ["setDelegate(address)", "0xca5eb5e1"],
    ["revokeDelegate()", "0x55d1ef38"],
    ["delegateOf(address)", "0x8d22ea2a"],
    ["delegatorOf(address)", "0x2222ef9f"],
    ["effectivePlayer(address)", "0x6d3498d8"],
  ];

  it.each(DELEGATION)("%s resolves for contract 'game' with selector %s", (sig, selector) => {
    const fn = resolveFunctionAbi(sig, "game");
    expect(getSelector(fn)).toBe(selector);
    expect(resolveFunctionAbi(sig)).toBe(fn); // default contract is game
  });

  it("is absent from the pinned game artifact but present in the resolvable ABI", () => {
    const has = (abi: readonly { type: string; name?: string }[]) =>
      abi.some((e) => e.type === "function" && e.name === "setDelegate");
    expect(has(getPinnedAbi("game"))).toBe(false);
    expect(has(getResolvableAbi("game"))).toBe(true);
  });

  it("merging never changes the game ABI hash", () => {
    expect(computePinnedAbiHash("game")).toBe(EXPECTED_HASH);
  });

  it("does not leak into the alliance contract's resolution", () => {
    expect(() => resolveFunctionAbi("setDelegate(address)", "alliance")).toThrow(/pinned "alliance" artifact/);
  });

  it("functionsForSelector finds a delegation function (describe/simulate/send rely on it)", () => {
    expect(functionsForSelector("0x55d1ef38").map((f) => f.name)).toEqual(["revokeDelegate"]);
  });

  it("PINNED.json's supplemental record matches the file on disk", () => {
    const [entry] = loadPinnedMeta("game").supplemental ?? [];
    expect(entry?.name).toBe("VeydriftDelegation");
    const delegationFns = getResolvableAbi("game").filter(
      (e) => e.type === "function" && DELEGATION.some(([sig]) => sig.startsWith(`${e.name}(`)),
    );
    expect(delegationFns).toHaveLength(DELEGATION.length);
    expect(entry?.abiHash).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it("deduplicates: an entry present in both the pinned artifact and the supplemental appears once", () => {
    const events = getResolvableAbi("game").filter((e) => e.type === "event" && e.name === "DelegateUpdated");
    expect(events).toHaveLength(1);
  });
});

describe("startProductionBatch (in the pinned game ABI)", () => {
  it("resolves by full signature with a tuple[] second input", () => {
    const fn = resolveFunctionAbi("startProductionBatch(uint256,(uint8,uint8,uint32)[])");
    expect(getSelector(fn)).toBe("0xa1de3f6a");
    expect(fn.inputs[1]?.type).toBe("tuple[]");
    expect(fn.stateMutability).toBe("nonpayable");
  });

  it("is part of the hashed artifact (unlike the fallback-routed delegation functions)", () => {
    expect(getPinnedAbi("game").some((e) => e.type === "function" && e.name === "startProductionBatch")).toBe(true);
  });
});

describe("backend hash classification (advisory, never the authority)", () => {
  const PINNED = EXPECTED_HASH;
  it("match: the backend reports the pinned hash", () => {
    expect(classifyBackendHash(PINNED, PINNED, STALE_BACKEND_HASH)).toBe("match");
  });
  it("known-stale: the backend reports exactly the value recorded at pin time", () => {
    expect(classifyBackendHash(PINNED, STALE_BACKEND_HASH, STALE_BACKEND_HASH)).toBe("known-stale");
  });
  it("other: a third value is flagged but is advisory", () => {
    expect(classifyBackendHash(PINNED, "sha256:something-else", STALE_BACKEND_HASH)).toBe("other");
  });
  it("unavailable: an absent field is not confused with a mismatch", () => {
    expect(classifyBackendHash(PINNED, "", STALE_BACKEND_HASH)).toBe("unavailable");
  });
  it("PINNED.json records the known-stale backend value", () => {
    expect(loadPinnedMeta("game").backendReported?.deploymentAbiHash).toBe(STALE_BACKEND_HASH);
  });
});

describe("custom-error decoding", () => {
  const gameAbi = getPinnedAbi("game");

  it("decodes InsufficientResources against the pinned ABI", () => {
    const data = encodeErrorResult({ abi: gameAbi, errorName: "InsufficientResources", args: [9035n, 44471n, 81685n] });
    const decoded = decodeRevertData(data);
    expect(decoded?.errorName).toBe("InsufficientResources");
    expect(decoded?.text).toBe("InsufficientResources(9035, 44471, 81685)");
  });

  it("decodes a delegation error added by the upgrade", () => {
    const a = "0x00000000000000000000000000000000000000a1";
    const b = "0x00000000000000000000000000000000000000b2";
    const data = encodeErrorResult({ abi: gameAbi, errorName: "DelegatedWalletCannotDelegate", args: [a, b] });
    expect(decodeRevertData(data)?.errorName).toBe("DelegatedWalletCannotDelegate");
  });

  it("decodes an error that only the alliance ABI declares", () => {
    const allianceOnly = getPinnedAbi("alliance").find(
      (e) => e.type === "error" && !gameAbi.some((g) => g.type === "error" && g.name === e.name),
    );
    expect(allianceOnly).toBeDefined();
    if (allianceOnly?.type !== "error") throw new Error("unreachable");
    const args = allianceOnly.inputs.map((input) => (input.type === "address" ? "0x00000000000000000000000000000000000000c3" : 1));
    const data = encodeErrorResult({ abi: getPinnedAbi("alliance"), errorName: allianceOnly.name, args } as never);
    expect(decodeRevertData(data)?.errorName).toBe(allianceOnly.name);
  });

  it("returns undefined for empty or unknown revert data rather than guessing", () => {
    expect(decodeRevertData(undefined)).toBeUndefined();
    expect(decodeRevertData("0xdeadbeef")).toBeUndefined();
  });

  it("extractRevertData finds the payload anywhere in a viem-style cause chain", () => {
    const data = "0x2ab0f96f0000000000000000000000000000000000000000000000000000000000000001";
    expect(extractRevertData({ cause: { cause: { data } } })).toBe(data);
    expect(extractRevertData({ cause: { data: { data } } })).toBe(data);
    expect(extractRevertData(new Error("no payload"))).toBeUndefined();
  });

  it("describeRevert names the decoded custom error, and falls back to the short message when it cannot", () => {
    const data = encodeErrorResult({ abi: gameAbi, errorName: "InsufficientResources", args: [1n, 2n, 3n] });
    const decoded = describeRevert({ shortMessage: "Execution reverted for an unknown reason.", cause: { data } });
    expect(decoded.message).toBe("InsufficientResources(1, 2, 3) (reverted)");
    expect(decoded.data).toBe(data);
    expect(describeRevert({ shortMessage: "rpc unreachable" }).message).toBe("rpc unreachable");
  });

  it("computeAbiHash is a pure function of the ABI it is given", () => {
    expect(computeAbiHash(getPinnedAbi("game"))).toBe(EXPECTED_HASH);
  });
});
