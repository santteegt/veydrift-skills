import { describe, expect, it, vi } from "vitest";
import { getAddress, pad, type Hex } from "viem";
import { loadPinnedMeta, type RuntimeConfig } from "../src/abi.js";
import {
  checkOnchainPin,
  EIP1967_IMPLEMENTATION_SLOT,
  failedOnchainPin,
  needsDependencyPin,
  type PinClient,
} from "../src/onchain-pin.js";

const game = loadPinnedMeta("game");
const alliance = loadPinnedMeta("alliance");
const deps = game.dependencies!;

const PROXY = {
  game: game.implementation!.proxy,
  alliance: alliance.implementation!.proxy,
  randomnessEngine: deps.randomnessEngine.proxy,
  moonSystem: deps.moonSystem.proxy,
};
const IMPL: Record<string, string> = {
  [PROXY.game.toLowerCase()]: game.implementation!.address,
  [PROXY.alliance.toLowerCase()]: alliance.implementation!.address,
  [PROXY.randomnessEngine.toLowerCase()]: deps.randomnessEngine.implementation,
  [PROXY.moonSystem.toLowerCase()]: deps.moonSystem.implementation,
};

const asSlot = (address: string): Hex => pad(address.toLowerCase() as Hex, { size: 32 });
const OTHER = "0x00000000000000000000000000000000000000f1";

function config(overrides: Partial<RuntimeConfig> = {}): RuntimeConfig {
  return {
    chainId: 8453,
    contractAddress: PROXY.game,
    gameContractAddress: PROXY.game,
    allianceContractAddress: PROXY.alliance,
    moonContractAddress: PROXY.moonSystem,
    randomnessEngineAddress: PROXY.randomnessEngine,
    backend: { build: { deploymentAbiHash: "sha256:x", deploymentCommit: "x" } },
    ...overrides,
  } as RuntimeConfig;
}

function client(over: {
  impl?: Record<string, string | null>;
  randomness?: string;
  code?: Hex | undefined;
  storageThrows?: boolean;
} = {}): PinClient & { getCode: ReturnType<typeof vi.fn> } {
  const impls = { ...IMPL, ...(over.impl as Record<string, string>) };
  return {
    getStorageAt: vi.fn(async ({ address, slot }: { address: string; slot: string }) => {
      if (over.storageThrows) throw new Error("rpc down");
      expect(slot).toBe(EIP1967_IMPLEMENTATION_SLOT);
      const impl = over.impl && address.toLowerCase() in over.impl ? over.impl[address.toLowerCase()] : impls[address.toLowerCase()];
      return impl === null ? pad("0x00", { size: 32 }) : asSlot(impl as string);
    }),
    getCode: vi.fn(async () => over.code ?? "0x"),
    readContract: vi.fn(async () => over.randomness ?? PROXY.randomnessEngine),
    getBlockNumber: vi.fn(async () => 51_932_353n),
  } as unknown as PinClient & { getCode: ReturnType<typeof vi.fn> };
}

const run = (c: PinClient, extra: Parameters<typeof checkOnchainPin>[0] = {}) =>
  checkOnchainPin({ client: c, fetchConfig: async () => config(), verifyCode: false, ...extra });

describe("checkOnchainPin", () => {
  it("passes when every proxy still points at its pinned implementation", async () => {
    const result = await run(client());
    expect(result.ok).toBe(true);
    expect(result.dependenciesOk).toBe(true);
    expect(result.problems).toEqual([]);
    expect(result.block).toBe("51932353");
    expect(result.appliesTo).toEqual(deps.appliesTo);
  });

  it("fails when the game implementation was upgraded, naming both addresses", async () => {
    const result = await run(client({ impl: { [PROXY.game.toLowerCase()]: OTHER } }));
    expect(result.ok).toBe(false);
    expect(result.game.ok).toBe(false);
    expect(result.game.liveImplementation).toBe(getAddress(OTHER));
    expect(result.problems.join(" ")).toMatch(/game: implementation of .* is 0x[0-9a-fA-F]{40}, but the pin is/);
  });

  it("fails when the alliance implementation was upgraded, independently of the game's", async () => {
    const result = await run(client({ impl: { [PROXY.alliance.toLowerCase()]: OTHER } }));
    expect(result.ok).toBe(false);
    expect(result.game.ok).toBe(true);
    expect(result.alliance.ok).toBe(false);
  });

  it("a drifted dependency clears dependenciesOk but NOT ok -- economy writes keep working", async () => {
    const result = await run(client({ impl: { [PROXY.moonSystem.toLowerCase()]: OTHER } }));
    expect(result.ok).toBe(true);
    expect(result.dependenciesOk).toBe(false);
    expect(result.moonSystem.ok).toBe(false);
    expect(needsDependencyPin("launchFleetMission", result)).toBe(true);
    expect(needsDependencyPin("startBuildingUpgrade", result)).toBe(false);
    expect(needsDependencyPin(undefined, result)).toBe(false);
  });

  it("fails the randomness dependency when the game now points at a different engine", async () => {
    const result = await run(client({ randomness: OTHER }));
    expect(result.dependenciesOk).toBe(false);
    expect(result.randomnessEngine.problems.join(" ")).toMatch(/the game now points at/);
  });

  it("FAILS CLOSED when the chain cannot be read", async () => {
    const result = await run(client({ storageThrows: true }));
    expect(result.ok).toBe(false);
    expect(result.dependenciesOk).toBe(false);
    expect(result.game.liveImplementation).toBeNull();
    expect(result.problems.join(" ")).toMatch(/could not read the chain \(rpc down\)/);
  });

  it("fails when the implementation slot is empty (not a proxy)", async () => {
    const result = await run(client({ impl: { [PROXY.game.toLowerCase()]: null as unknown as string } }));
    expect(result.ok).toBe(false);
    expect(result.problems.join(" ")).toMatch(/slot .* is empty or unreadable/);
  });

  it("fails when /runtime-config claims a different proxy address than the pin", async () => {
    const result = await run(client(), { fetchConfig: async () => config({ gameContractAddress: OTHER }) });
    expect(result.ok).toBe(false);
    expect(result.problems.join(" ")).toMatch(/live \/runtime-config reports .*pinned proxy is/);
  });

  it("an unreachable /runtime-config is only a warning -- the verdict never depends on the backend", async () => {
    const result = await run(client(), {
      fetchConfig: async () => {
        throw new Error("HTTP 503");
      },
    });
    expect(result.ok).toBe(true);
    expect(result.warnings.join(" ")).toMatch(/could not cross-check .* \(HTTP 503\)/);
  });

  it("rejects a tx.to that is neither pinned proxy", async () => {
    const bad = await run(client(), { to: OTHER });
    expect(bad.ok).toBe(false);
    expect(bad.problems.join(" ")).toMatch(/tx\.to .* is neither pinned proxy/);
    expect((await run(client(), { to: PROXY.game.toLowerCase() })).ok).toBe(true);
    expect((await run(client(), { to: PROXY.alliance })).ok).toBe(true);
  });

  it("verifyCode compares the implementation's runtime code hash (and flags absent or wrong code)", async () => {
    const wrong = client({ code: "0x600160005260206000f3" });
    const mismatch = await run(wrong, { verifyCode: true });
    expect(mismatch.ok).toBe(false);
    expect(mismatch.game.codeVerified).toBe(true);
    expect(mismatch.problems.join(" ")).toMatch(/code hash 0x[0-9a-f]{64} != pinned/);

    const empty = await run(client({ code: "0x" }), { verifyCode: true });
    expect(empty.problems.join(" ")).toMatch(/has no code/);
  });

  it("does not fetch code when verifyCode is false (the per-tick hot path)", async () => {
    const c = client();
    await run(c, { verifyCode: false });
    expect(c.getCode).not.toHaveBeenCalled();
  });

  it("skips the code fetch when the implementation address itself already mismatches", async () => {
    const c = client({ impl: { [PROXY.game.toLowerCase()]: OTHER } });
    await run(c, { verifyCode: true });
    const fetchedFor = c.getCode.mock.calls.map((call) => String((call[0] as { address: string }).address).toLowerCase());
    expect(fetchedFor).not.toContain(OTHER);
  });
});

describe("failedOnchainPin", () => {
  it("is never ok, so every consumer fails closed", () => {
    const failed = failedOnchainPin("offline");
    expect(failed.ok).toBe(false);
    expect(failed.dependenciesOk).toBe(false);
    expect(failed.problems).toEqual(["offline"]);
  });
});

// Opt-in: reads the real chain. `VEYDRIFT_LIVE_TESTS=1 npm test` after any re-pin to prove the pin
// still describes what is deployed.
describe.skipIf(!process.env.VEYDRIFT_LIVE_TESTS)("live chain", () => {
  it("the deployed system matches the pin (implementations, code hashes, dependencies)", async () => {
    const result = await checkOnchainPin({ verifyCode: true });
    expect(result.problems).toEqual([]);
    expect(result.ok && result.dependenciesOk).toBe(true);
  }, 60_000);
});
