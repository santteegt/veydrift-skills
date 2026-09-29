import { describe, expect, it } from "vitest";
import { getAddress, pad, type Hex } from "viem";
import { EIP1967_IMPLEMENTATION_SLOT } from "../src/onchain-pin.js";
import {
  confirmDeployment,
  embeddedAddresses,
  maskedEqual,
  parseArtifactCode,
  type ArtifactCode,
  type ConfirmClient,
} from "../src/repin.js";

const bytes = (...parts: Array<number[] | Uint8Array>) => Uint8Array.from(parts.flatMap((p) => [...p]));
const hex = (b: Uint8Array): Hex => `0x${Buffer.from(b).toString("hex")}`;
const addrBytes = (fill: string) => [...Buffer.from(fill.repeat(20), "hex")]; // fill = 2 hex chars, e.g. "11"
const PUSH20 = 0x73;

const PROXY = getAddress("0x00000000000000000000000000000000000000a1");
const IMPL = getAddress("0x00000000000000000000000000000000000000a2");
const MODULE = getAddress(`0x${"11".repeat(20)}`);

// A router whose code embeds MODULE as an immutable: `JUMPDEST PUSH20 <module> STOP STOP`.
const routerOnchain = bytes([0x5b, PUSH20], addrBytes("11"), [0x00, 0x00]);
const moduleCode = bytes([0x60, 0x80, 0x60, 0x40, 0x52, 0x00]);

const routerArtifact: ArtifactCode = {
  name: "Router:Router",
  code: bytes([0x5b, PUSH20], new Array(20).fill(0), [0x00, 0x00]),
  mask: bytes([1, 1], new Array(20).fill(0), [1, 1]), // the 20 address bytes are an immutable
};
const moduleArtifact: ArtifactCode = { name: "Module:Module", code: moduleCode, mask: new Uint8Array(moduleCode.length).fill(1) };

function client(codes: Record<string, Uint8Array>, impl: string | null = IMPL): ConfirmClient {
  return {
    getStorageAt: async ({ slot }) => {
      expect(slot).toBe(EIP1967_IMPLEMENTATION_SLOT);
      return impl ? pad(impl.toLowerCase() as Hex, { size: 32 }) : pad("0x00", { size: 32 });
    },
    getCode: async ({ address }) => {
      const code = codes[address.toLowerCase()];
      return code ? hex(code) : "0x";
    },
  };
}

describe("parseArtifactCode", () => {
  it("masks immutable positions and zero-fills unresolved library placeholders", () => {
    const placeholder = "__$" + "a".repeat(34) + "$__"; // 40 chars, not hex
    const parsed = parseArtifactCode("F:C", {
      deployedBytecode: {
        object: `0x5b${placeholder}00${"00".repeat(4)}`,
        immutableReferences: { "7": [{ start: 22, length: 4 }] },
        linkReferences: { "src/L.sol": { L: [{ start: 1, length: 20 }] } },
      },
    });
    expect(parsed).toBeDefined();
    expect(parsed!.code.length).toBe(26);
    expect([...parsed!.mask.subarray(1, 21)].every((m) => m === 0)).toBe(true); // link placeholder
    expect([...parsed!.mask.subarray(22, 26)].every((m) => m === 0)).toBe(true); // immutable
    expect(parsed!.mask[0]).toBe(1);
    expect(parsed!.mask[21]).toBe(1);
  });

  it("returns undefined for an artifact with no runtime code (interfaces, abstract contracts)", () => {
    expect(parseArtifactCode("I:I", { deployedBytecode: { object: "0x" } })).toBeUndefined();
    expect(parseArtifactCode("I:I", {})).toBeUndefined();
  });

  it("returns undefined rather than guessing when the bytecode is not valid hex", () => {
    expect(parseArtifactCode("X:X", { deployedBytecode: { object: "0xzz11223344" } })).toBeUndefined();
  });
});

describe("maskedEqual", () => {
  it("ignores masked positions but compares everything else", () => {
    expect(maskedEqual(routerOnchain, routerArtifact)).toBe(true);
    const tampered = Uint8Array.from(routerOnchain);
    tampered[0] = 0x5a; // an unmasked byte
    expect(maskedEqual(tampered, routerArtifact)).toBe(false);
  });

  it("requires equal length", () => {
    expect(maskedEqual(bytes([...routerOnchain], [0x00]), routerArtifact)).toBe(false);
  });
});

describe("embeddedAddresses", () => {
  it("finds PUSH20 and zero-padded PUSH32 address constants", () => {
    const push32 = bytes([0x7f], new Array(12).fill(0), addrBytes("22"));
    const found = embeddedAddresses(bytes([0x5b, PUSH20], addrBytes("11"), push32));
    expect(found).toEqual([getAddress(`0x${"11".repeat(20)}`), getAddress(`0x${"22".repeat(20)}`)]);
  });

  it("skips precompile-like and small constants", () => {
    const small = bytes([PUSH20], [0, 0, ...new Array(17).fill(0), 1]);
    const push32Small = bytes([0x7f], new Array(31).fill(0), [5]);
    expect(embeddedAddresses(bytes(small, push32Small))).toEqual([]);
  });

  it("never misreads a PUSH immediate as an opcode (a 0x73 inside a PUSH4 is data)", () => {
    const code = bytes([0x63, PUSH20, 0x11, 0x22, 0x33], [0x00]); // PUSH4 whose first data byte is 0x73
    expect(embeddedAddresses(code)).toEqual([]);
  });
});

describe("confirmDeployment", () => {
  const artifacts = [routerArtifact, moduleArtifact];
  const codes = { [IMPL.toLowerCase()]: routerOnchain, [MODULE.toLowerCase()]: moduleCode };

  it("confirms when the implementation AND every embedded module match a build", async () => {
    const result = await confirmDeployment({ client: client(codes), roots: { game: PROXY }, artifacts });
    expect(result.ok).toBe(true);
    expect(result.implementations.game).toBe(IMPL);
    expect(result.matched.map((m) => m.artifact)).toEqual(["Router:Router", "Module:Module"]);
    expect(result.unmatched).toEqual([]);
  });

  it("does NOT confirm on the router alone: a module that differs from the build is reported", async () => {
    const changedModule = Uint8Array.from(moduleCode);
    changedModule[1] = 0x81;
    const result = await confirmDeployment({
      client: client({ ...codes, [MODULE.toLowerCase()]: changedModule }),
      roots: { game: PROXY },
      artifacts,
    });
    expect(result.ok).toBe(false);
    expect(result.matched.map((m) => m.artifact)).toEqual(["Router:Router"]);
    expect(result.unmatched).toHaveLength(1);
    expect(result.unmatched[0]?.address).toBe(MODULE);
  });

  it("fails when a root proxy has an empty implementation slot", async () => {
    const result = await confirmDeployment({ client: client(codes, null), roots: { game: PROXY }, artifacts });
    expect(result.ok).toBe(false);
    expect(result.unmatched[0]?.via).toMatch(/slot empty/);
  });

  it("ignores embedded addresses with no code (EOAs, stray constants) instead of failing on them", async () => {
    const result = await confirmDeployment({
      client: client({ [IMPL.toLowerCase()]: routerOnchain }), // MODULE has no code
      roots: { game: PROXY },
      artifacts,
    });
    expect(result.ok).toBe(true);
    expect(result.matched).toHaveLength(1);
  });

  it("also checks extra implementations (external dependencies that are not embedded anywhere)", async () => {
    const dep = getAddress("0x00000000000000000000000000000000000000b1");
    const result = await confirmDeployment({
      client: client({ ...codes, [dep.toLowerCase()]: moduleCode }),
      roots: { game: PROXY },
      artifacts,
      extraImplementations: { randomnessEngine: dep },
    });
    expect(result.ok).toBe(true);
    expect(result.implementations.randomnessEngine).toBe(dep);
  });

  it("visits each contract once even when several contracts embed it", async () => {
    const result = await confirmDeployment({
      client: client(codes),
      roots: { game: PROXY, alliance: PROXY },
      artifacts,
    });
    expect(result.matched.filter((m) => m.artifact === "Module:Module")).toHaveLength(1);
  });
});
