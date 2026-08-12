import { describe, expect, it, vi } from "vitest";
import { encodeFunctionData, getAddress } from "viem";
import { resolveFunctionAbi, type RuntimeConfig } from "../src/abi.js";
import type { UnsignedTx, WalletProvider } from "../src/providers/types.js";
import {
  buildTx,
  describeTx,
  getReceipt,
  sendTx,
  SendRefusedError,
  simulateTx,
  type VeydriftPublicClient,
} from "../src/tx.js";

const GAME_ADDRESS = getAddress("0xf397910F005151b09644228573a4353818D3755d");

function fixtureConfig(): RuntimeConfig {
  return {
    chainId: 8453,
    contractAddress: GAME_ADDRESS,
    gameContractAddress: GAME_ADDRESS,
    backend: { build: { deploymentAbiHash: "sha256:fixture", deploymentCommit: "fixture" } },
  };
}

function mockClient(overrides: {
  estimateGas?: (...args: unknown[]) => Promise<bigint>;
  getGasPrice?: () => Promise<bigint>;
  estimateFeesPerGas?: () => Promise<{ maxFeePerGas?: bigint; maxPriorityFeePerGas?: bigint }>;
  call?: (...args: unknown[]) => Promise<{ data?: `0x${string}` }>;
  getTransactionReceipt?: (...args: unknown[]) => Promise<unknown>;
} = {}): VeydriftPublicClient {
  return {
    estimateGas: overrides.estimateGas ?? vi.fn().mockResolvedValue(50_000n),
    getGasPrice: overrides.getGasPrice ?? vi.fn().mockResolvedValue(1_000_000_000n),
    estimateFeesPerGas: overrides.estimateFeesPerGas ?? vi.fn().mockResolvedValue({ maxFeePerGas: 2_000_000_000n }),
    call: overrides.call ?? vi.fn().mockResolvedValue({ data: "0x" }),
    getTransactionReceipt: overrides.getTransactionReceipt ?? vi.fn(),
  } as unknown as VeydriftPublicClient;
}

describe("buildTx", () => {
  it("encodes an Action against the live-config game address", async () => {
    const built = await buildTx(
      { function: "startBuildingUpgrade(uint256,uint8)", args: [664, 3], purpose: "energy-first opener" },
      { fetchConfig: async () => fixtureConfig() },
    );
    expect(built.to).toBe(GAME_ADDRESS);
    expect(built.value).toBe(0n);
    expect(built.chainId).toBe(8453);
    expect(built.functionName).toBe("startBuildingUpgrade");
    expect(built.purpose).toBe("energy-first opener");

    const fn = resolveFunctionAbi("startBuildingUpgrade(uint256,uint8)");
    const expectedData = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n, 3] });
    expect(built.data).toBe(expectedData);
  });

  it("coerces number/string args to bigint for uint types", async () => {
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: ["664"] },
      { fetchConfig: async () => fixtureConfig() },
    );
    const fn = resolveFunctionAbi("settlePlanet(uint256)");
    const expectedData = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    expect(built.data).toBe(expectedData);
  });

  it("estimates gas when a from-address and client are supplied", async () => {
    const client = mockClient();
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: [664] },
      {
        fetchConfig: async () => fixtureConfig(),
        from: "0x0000000000000000000000000000000000000d00",
        client,
      },
    );
    expect(built.gas).toBe(50_000n);
    expect(built.gasEstimateError).toBeUndefined();
  });

  it("omits gas (with an error note) rather than throwing when estimation fails", async () => {
    const client = mockClient({ estimateGas: vi.fn().mockRejectedValue(new Error("execution reverted")) });
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: [664] },
      {
        fetchConfig: async () => fixtureConfig(),
        from: "0x0000000000000000000000000000000000000d00",
        client,
      },
    );
    expect(built.gas).toBeUndefined();
    expect(built.gasEstimateError).toMatch(/execution reverted/);
  });

  it("requires a full signature for the overloaded launchFleetMission -- never picks one by name", async () => {
    await expect(
      buildTx({ function: "launchFleetMission", args: [] }, { fetchConfig: async () => fixtureConfig() }),
    ).rejects.toThrow(/overloaded/);
  });

  it("emits gas UNITS, maxFeePerGas (wei/unit) and estimatedCostWei = gas * maxFeePerGas -- FIX 1", async () => {
    const client = mockClient({
      estimateGas: vi.fn().mockResolvedValue(150_000n),
      estimateFeesPerGas: vi.fn().mockResolvedValue({ maxFeePerGas: 12_345_678n }),
    });
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: [664] },
      { fetchConfig: async () => fixtureConfig(), from: "0x0000000000000000000000000000000000000d00", client },
    );
    expect(built.gas).toBe(150_000n);
    expect(built.maxFeePerGas).toBe(12_345_678n);
    expect(built.estimatedCostWei).toBe(150_000n * 12_345_678n);
    expect(built.feeEstimateError).toBeUndefined();
  });

  it("falls back to getGasPrice() for maxFeePerGas when estimateFeesPerGas is unavailable", async () => {
    const client = mockClient({
      estimateFeesPerGas: vi.fn().mockRejectedValue(new Error("not supported")),
      getGasPrice: vi.fn().mockResolvedValue(9_000_000n),
    });
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: [664] },
      { fetchConfig: async () => fixtureConfig(), from: "0x0000000000000000000000000000000000000d00", client },
    );
    expect(built.maxFeePerGas).toBe(9_000_000n);
  });

  it("emits null (never zero, never a guess) for maxFeePerGas/estimatedCostWei when the live fee fetch fails", async () => {
    const client = mockClient({
      estimateFeesPerGas: vi.fn().mockRejectedValue(new Error("rpc down")),
      getGasPrice: vi.fn().mockRejectedValue(new Error("rpc down")),
    });
    const built = await buildTx(
      { function: "settlePlanet(uint256)", args: [664] },
      { fetchConfig: async () => fixtureConfig(), from: "0x0000000000000000000000000000000000000d00", client },
    );
    expect(built.maxFeePerGas).toBeUndefined();
    expect(built.estimatedCostWei).toBeUndefined();
    expect(built.feeEstimateError).toMatch(/rpc down/);
  });
});

describe("describeTx", () => {
  it("decodes function name/args and computes an ETH cost estimate", async () => {
    const fn = resolveFunctionAbi("startResearch(uint256,uint8)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n, 0] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };

    const client = mockClient();
    const display = await describeTx(tx, { purpose: "test purpose", client });

    expect(display.functionName).toBe("startResearch");
    expect(display.signature).toBe("startResearch(uint256,uint8)");
    expect(display.args).toEqual([664n, 0]);
    expect(display.estimatedGas).toBe(50_000n);
    expect(display.estimatedCostEth).toBeDefined();
    expect(display.purpose).toBe("test purpose");
    expect(display.to).toBe(GAME_ADDRESS);
  });

  it("uses tx.gas when already set, without calling estimateGas again", async () => {
    const fn = resolveFunctionAbi("settlePlanet(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453, gas: 99_999n };
    const estimateGas = vi.fn().mockResolvedValue(1n);
    const client = mockClient({ estimateGas });
    const display = await describeTx(tx, { client });
    expect(display.estimatedGas).toBe(99_999n);
    expect(estimateGas).not.toHaveBeenCalled();
  });
});

describe("simulateTx", () => {
  it("returns ok:true with return data on a successful call", async () => {
    const fn = resolveFunctionAbi("settlePlanet(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };

    const client = mockClient({ call: vi.fn().mockResolvedValue({ data: "0xdead" }) });
    const result = await simulateTx(tx, { client });

    expect(result.ok).toBe(true);
    expect(result.returnData).toBe("0xdead");
    expect(result.functionName).toBe("settlePlanet");
  });

  it("surfaces a revert reason instead of throwing -- this is how nonpayable-read functions are read", async () => {
    const fn = resolveFunctionAbi("protectedResources(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };

    const client = mockClient({ call: vi.fn().mockRejectedValue(new Error("execution reverted: not owner")) });
    const result = await simulateTx(tx, { client });

    expect(result.ok).toBe(false);
    expect(result.revertReason).toMatch(/reverted/);
    expect(result.functionName).toBe("protectedResources");
  });

  it("also reports maxFeePerGas/estimatedCostWei on a successful simulation -- FIX 1 applies to simulate too", async () => {
    const fn = resolveFunctionAbi("settlePlanet(uint256)");
    const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n] });
    const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };

    const client = mockClient({
      estimateGas: vi.fn().mockResolvedValue(80_000n),
      estimateFeesPerGas: vi.fn().mockResolvedValue({ maxFeePerGas: 5_000_000n }),
    });
    const result = await simulateTx(tx, { client });

    expect(result.ok).toBe(true);
    expect(result.gas).toBe(80_000n);
    expect(result.maxFeePerGas).toBe(5_000_000n);
    expect(result.estimatedCostWei).toBe(80_000n * 5_000_000n);
  });
});

describe("getReceipt", () => {
  function receiptClient(receipt: Record<string, unknown>): VeydriftPublicClient {
    return mockClient({ getTransactionReceipt: vi.fn().mockResolvedValue(receipt) });
  }

  it("reports status:'success' and computes actualCostWei = gasUsed * effectiveGasPrice -- FIX 2", async () => {
    const client = receiptClient({
      status: "success",
      blockNumber: 49_876_935n,
      gasUsed: 132_411n,
      effectiveGasPrice: 12_345_678n,
      transactionHash: "0xabc",
      to: GAME_ADDRESS,
      from: getAddress("0x0000000000000000000000000000000000000d00"),
    });
    const receipt = await getReceipt("0xabc", { client });
    expect(receipt.status).toBe("success");
    expect(receipt.actualCostWei).toBe(132_411n * 12_345_678n);
    expect(receipt.blockNumber).toBe(49_876_935n);
  });

  it("reports status:'reverted' for a reverted receipt -- this is what stops the Python side recording a revert as a success", async () => {
    const client = receiptClient({
      status: "reverted",
      blockNumber: 49_876_940n,
      gasUsed: 21_000n,
      effectiveGasPrice: 10_000_000n,
      transactionHash: "0xdead",
      to: GAME_ADDRESS,
      from: getAddress("0x0000000000000000000000000000000000000d00"),
    });
    const receipt = await getReceipt("0xdead", { client });
    expect(receipt.status).toBe("reverted");
    expect(receipt.actualCostWei).toBe(21_000n * 10_000_000n);
  });

  it("throws (never synthesizes success) when the receipt fetch itself fails", async () => {
    const client = mockClient({ getTransactionReceipt: vi.fn().mockRejectedValue(new Error("not found")) });
    await expect(getReceipt("0xnotfound", { client })).rejects.toThrow(/not found/);
  });

  it("throws when the receipt reports a status this engine doesn't recognize, rather than guessing", async () => {
    const client = receiptClient({
      status: "0x1", // unnormalized -- should never happen against a real viem client, but must not be trusted blindly
      blockNumber: 1n,
      gasUsed: 1n,
      effectiveGasPrice: 1n,
      transactionHash: "0xabc",
      to: GAME_ADDRESS,
      from: getAddress("0x0000000000000000000000000000000000000d00"),
    });
    await expect(getReceipt("0xabc", { client })).rejects.toThrow(/unrecognized receipt\.status/);
  });
});

function mockProvider(): WalletProvider & { signAndSend: ReturnType<typeof vi.fn> } {
  return {
    name: "mock",
    getAddress: vi.fn().mockResolvedValue(getAddress("0x0000000000000000000000000000000000000d00")),
    signAndSend: vi.fn().mockResolvedValue("0xabc123"),
    capabilities: () => ({ canSign: true, canSimulate: false, remotePolicy: false }),
  };
}

describe("sendTx", () => {
  const fn = resolveFunctionAbi("startBuildingUpgrade(uint256,uint8)");
  const data = encodeFunctionData({ abi: [fn], functionName: fn.name, args: [664n, 3] });
  const tx: UnsignedTx = { to: GAME_ADDRESS, data, value: 0n, chainId: 8453 };

  it("refuses without confirm:true -- no env var or flag makes it implicit", async () => {
    const provider = mockProvider();
    await expect(
      sendTx(tx, { tier: "economy", confirm: false, provider, fetchConfig: async () => fixtureConfig() }),
    ).rejects.toThrow(SendRefusedError);
    expect(provider.signAndSend).not.toHaveBeenCalled();
  });

  it("signs and sends when confirm:true and the allowlist passes", async () => {
    const provider = mockProvider();
    const hash = await sendTx(tx, {
      tier: "economy",
      confirm: true,
      provider,
      fetchConfig: async () => fixtureConfig(),
    });
    expect(hash).toBe("0xabc123");
    expect(provider.signAndSend).toHaveBeenCalledWith(tx);
  });

  it("refuses a nonpayable-but-semantically-read function outright, even at tier operator with confirm", async () => {
    const readFn = resolveFunctionAbi("collectResources(uint256)");
    const readData = encodeFunctionData({ abi: [readFn], functionName: readFn.name, args: [664n] });
    const readTx: UnsignedTx = { to: GAME_ADDRESS, data: readData, value: 0n, chainId: 8453 };
    const provider = mockProvider();

    await expect(
      sendTx(readTx, { tier: "operator", confirm: true, provider, fetchConfig: async () => fixtureConfig() }),
    ).rejects.toThrow(/semantically a read/);
    expect(provider.signAndSend).not.toHaveBeenCalled();
  });

  it("refuses when the allowlist rejects (e.g. tier too low)", async () => {
    const provider = mockProvider();
    await expect(
      sendTx(tx, { tier: "advisor", confirm: true, provider, fetchConfig: async () => fixtureConfig() }),
    ).rejects.toThrow(/allowlist rejected/);
    expect(provider.signAndSend).not.toHaveBeenCalled();
  });

  it("refuses when the destination is not the live Veydrift contract", async () => {
    const otherTx: UnsignedTx = { ...tx, to: getAddress("0x000000000000000000000000000000000000dead") };
    const provider = mockProvider();
    await expect(
      sendTx(otherTx, { tier: "economy", confirm: true, provider, fetchConfig: async () => fixtureConfig() }),
    ).rejects.toThrow(SendRefusedError);
    expect(provider.signAndSend).not.toHaveBeenCalled();
  });
});
