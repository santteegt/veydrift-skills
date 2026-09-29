import { describe, expect, it, vi } from "vitest";
import { getAddress } from "viem";
import { getSelector, loadPinnedMeta, resolveFunctionAbi } from "../src/abi.js";
import { checkEffectivePlayer, type EffectivePlayerClient } from "../src/signer-binding.js";

const MAIN = getAddress("0x00000000000000000000000000000000000000a1");
const DELEGATE = getAddress("0x00000000000000000000000000000000000000d1");
const OTHER = getAddress("0x00000000000000000000000000000000000000f9");

const client = (effective: string | Error): EffectivePlayerClient & { readContract: ReturnType<typeof vi.fn> } =>
  ({
    readContract: vi.fn(async () => {
      if (effective instanceof Error) throw effective;
      return effective;
    }),
  }) as never;

describe("checkEffectivePlayer -- does the signer really act as the policy wallet, on-chain?", () => {
  it("passes when the signer is the wallet and acts as itself (no delegation involved)", async () => {
    const result = await checkEffectivePlayer({ signer: MAIN, player: MAIN, client: client(MAIN) });
    expect(result.ok).toBe(true);
    expect(result.effectivePlayer).toBe(MAIN);
  });

  it("passes when the signer is a delegate registered for the wallet", async () => {
    const result = await checkEffectivePlayer({ signer: DELEGATE, player: MAIN, client: client(MAIN) });
    expect(result.ok).toBe(true);
  });

  it("compares case-insensitively", async () => {
    const result = await checkEffectivePlayer({ signer: DELEGATE, player: MAIN.toLowerCase(), client: client(MAIN.toUpperCase().replace("0X", "0x")) });
    expect(result.ok).toBe(true);
  });

  it("refuses a delegate that was never registered or was revoked (it acts as itself)", async () => {
    const result = await checkEffectivePlayer({ signer: DELEGATE, player: MAIN, client: client(DELEGATE) });
    expect(result.ok).toBe(false);
    expect(result.problem).toMatch(/acts as .* on-chain, not as the policy wallet/);
  });

  it("refuses a signer registered as the delegate of a DIFFERENT main wallet", async () => {
    const result = await checkEffectivePlayer({ signer: DELEGATE, player: MAIN, client: client(OTHER) });
    expect(result.ok).toBe(false);
    expect(result.effectivePlayer).toBe(OTHER);
  });

  it("refuses when the policy wallet is itself someone's delegate (its own txs would act as that account)", async () => {
    const result = await checkEffectivePlayer({ signer: MAIN, player: MAIN, client: client(OTHER) });
    expect(result.ok).toBe(false);
  });

  it("FAILS CLOSED when the chain cannot be read", async () => {
    const result = await checkEffectivePlayer({
      signer: DELEGATE,
      player: MAIN,
      client: client(Object.assign(new Error("rpc down\nsecond line"), { shortMessage: "HTTP request failed." })),
    });
    expect(result.ok).toBe(false);
    expect(result.effectivePlayer).toBeNull();
    expect(result.problem).toBe(`could not read effectivePlayer(${DELEGATE}): HTTP request failed.`);
  });

  it("reads effectivePlayer(signer) from the pinned game proxy, through the supplemental ABI", async () => {
    const c = client(MAIN);
    await checkEffectivePlayer({ signer: DELEGATE, player: MAIN, client: c });
    const call = c.readContract.mock.calls[0]?.[0] as { address: string; abi: unknown[]; functionName: string; args: unknown[] };
    expect(call.address).toBe(getAddress(loadPinnedMeta("game").implementation!.proxy));
    expect(call.functionName).toBe("effectivePlayer");
    expect(call.args).toEqual([DELEGATE]);
    expect(getSelector(call.abi[0] as ReturnType<typeof resolveFunctionAbi>)).toBe("0x6d3498d8");
  });
});
