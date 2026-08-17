/**
 * `fork-impersonate` provider tests.
 *
 * The loopback-guard and address-validation tests below are unconditional -- they need no fork,
 * no anvil binary, no network. The e2e suite at the bottom is the one exception: it spawns a real
 * `anvil` process forking a live RPC and actually exercises `signAndSend`. It is skip-gated on
 * both anvil being installed *and* an explicit `VEYDRIFT_FORK_TEST_RPC_URL` env var (the upstream
 * RPC anvil forks from) being set, mirroring `tests/selectors.cast.test.ts`'s existing optional-
 * local-binary pattern: absent either precondition, `npm test` stays green and fully offline.
 *
 * WELL-KNOWN PUBLIC THROWAWAY TEST KEY/ADDRESS -- Anvil/Foundry/Hardhat's default account #0
 * (`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`). Printed to stdout by every `anvil` invocation,
 * never used to hold real funds, and already hardcoded in `tests/providers.test.ts` and excluded
 * by `envkey.ts`'s leak scanner. Only the *address* is used here -- this provider never touches a
 * private key.
 */
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { type ChildProcess, spawn } from "node:child_process";
import { createPublicClient, http } from "viem";
import { base } from "viem/chains";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ForkImpersonateProvider, refuseIfNotLoopback } from "../../src/providers/fork-impersonate.js";
import type { UnsignedTx } from "../../src/providers/types.js";

const ANVIL_BIN = join(homedir(), ".foundry", "bin", "anvil");
const anvilAvailable = existsSync(ANVIL_BIN) || (() => {
  try {
    execFileSync("anvil", ["--version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
})();

const FORK_TEST_RPC_URL = process.env.VEYDRIFT_FORK_TEST_RPC_URL;

const ANVIL_ACCOUNT_0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" as const;
const ANVIL_ACCOUNT_1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8" as const;

// ---------------------------------------------------------------------------------------------
// Loopback guard -- unconditional, no fork needed. This is the safety property; test it hardest.
// ---------------------------------------------------------------------------------------------

describe("refuseIfNotLoopback", () => {
  it("rejects a real remote RPC endpoint", () => {
    expect(() => refuseIfNotLoopback("https://mainnet.base.org")).toThrow(/refusing to start/);
  });

  it("rejects an Alchemy-style remote endpoint", () => {
    expect(() => refuseIfNotLoopback("https://base-mainnet.g.alchemy.com/v2/some-key")).toThrow(
      /refusing to start/,
    );
  });

  it("rejects a non-loopback IP address", () => {
    expect(() => refuseIfNotLoopback("http://93.184.216.34:8545")).toThrow(/refusing to start/);
  });

  it("rejects an unparseable URL (fails closed, not open)", () => {
    expect(() => refuseIfNotLoopback("not a url at all")).toThrow(/refusing to start/);
  });

  it.each([
    ["127.0.0.1", "http://127.0.0.1:8545"],
    ["localhost", "http://localhost:8545"],
    ["::1 (bracketed IPv6 loopback)", "http://[::1]:8545"],
  ])("accepts loopback spelling: %s", (_label, url) => {
    expect(() => refuseIfNotLoopback(url)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------------------------
// VEYDRIFT_FORK_IMPERSONATE_ADDRESS validation -- unconditional. The constructor's loopback guard
// runs first and reads the *real* process.env via getRpcUrl(), so these tests stub
// VEYDRIFT_RPC_URL to a loopback value to isolate what they're actually testing.
// ---------------------------------------------------------------------------------------------

describe("ForkImpersonateProvider construction", () => {
  beforeEach(() => {
    vi.stubEnv("VEYDRIFT_RPC_URL", "http://127.0.0.1:8545");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("refuses to start without VEYDRIFT_FORK_IMPERSONATE_ADDRESS", () => {
    expect(() => new ForkImpersonateProvider({} as NodeJS.ProcessEnv)).toThrow(
      /VEYDRIFT_FORK_IMPERSONATE_ADDRESS/,
    );
  });

  it("refuses to start with a malformed address", () => {
    expect(
      () =>
        new ForkImpersonateProvider({
          VEYDRIFT_FORK_IMPERSONATE_ADDRESS: "not-an-address",
        } as NodeJS.ProcessEnv),
    ).toThrow(/not a valid Ethereum address/);
  });

  it("accepts a valid address and exposes it (checksummed) via getAddress()", async () => {
    const provider = new ForkImpersonateProvider({
      VEYDRIFT_FORK_IMPERSONATE_ADDRESS: ANVIL_ACCOUNT_0.toLowerCase(),
    } as NodeJS.ProcessEnv);
    expect(await provider.getAddress()).toBe(ANVIL_ACCOUNT_0);
  });

  it("still refuses on a non-loopback RPC even when the address is valid (guard runs first)", () => {
    vi.stubEnv("VEYDRIFT_RPC_URL", "https://mainnet.base.org");
    expect(
      () =>
        new ForkImpersonateProvider({
          VEYDRIFT_FORK_IMPERSONATE_ADDRESS: ANVIL_ACCOUNT_0,
        } as NodeJS.ProcessEnv),
    ).toThrow(/refusing to start/);
  });

  it("reports honest capabilities -- not a copy of the signing providers' triple", () => {
    const provider = new ForkImpersonateProvider({
      VEYDRIFT_FORK_IMPERSONATE_ADDRESS: ANVIL_ACCOUNT_0,
    } as NodeJS.ProcessEnv);
    expect(provider.capabilities()).toEqual({ canSign: false, canSimulate: false, remotePolicy: false });
  });
});

// ---------------------------------------------------------------------------------------------
// Anvil e2e -- skip-gated on anvil being installed AND VEYDRIFT_FORK_TEST_RPC_URL being set (the
// upstream RPC anvil forks from). Neither is assumed present in CI or on a fresh checkout.
// ---------------------------------------------------------------------------------------------

const e2eEnabled = anvilAvailable && Boolean(FORK_TEST_RPC_URL);

describe.skipIf(!e2eEnabled)("fork-impersonate e2e (real anvil fork)", () => {
  const ANVIL_PORT = 8547;
  const LOCAL_RPC_URL = `http://127.0.0.1:${ANVIL_PORT}`;
  let anvilProcess: ChildProcess;

  async function waitForAnvilReady(url: string, timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    let lastError: unknown;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: [] }),
        });
        if (res.ok) return;
      } catch (err) {
        lastError = err;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`anvil did not become ready at ${url} within ${timeoutMs}ms: ${String(lastError)}`);
  }

  beforeAll(async () => {
    anvilProcess = spawn(
      "anvil",
      ["--fork-url", FORK_TEST_RPC_URL as string, "--port", String(ANVIL_PORT), "--silent"],
      { stdio: "ignore" },
    );
    await waitForAnvilReady(LOCAL_RPC_URL, 25_000);
    vi.stubEnv("VEYDRIFT_RPC_URL", LOCAL_RPC_URL);
  }, 30_000);

  afterAll(() => {
    vi.unstubAllEnvs();
    anvilProcess?.kill();
  }, 10_000);

  it(
    "impersonate -> setBalance -> eth_sendTransaction -> receipt, end to end",
    async () => {
      const provider = new ForkImpersonateProvider({
        VEYDRIFT_FORK_IMPERSONATE_ADDRESS: ANVIL_ACCOUNT_0,
      } as NodeJS.ProcessEnv);
      expect(await provider.getAddress()).toBe(ANVIL_ACCOUNT_0);

      // A plain value transfer -- no dependency on live Veydrift game state, per instruction.
      const tx: UnsignedTx = {
        to: ANVIL_ACCOUNT_1,
        data: "0x",
        value: 1_000_000_000_000_000n, // 0.001 ETH
        chainId: base.id,
      };

      const hash = await provider.signAndSend(tx);
      expect(hash).toMatch(/^0x[0-9a-fA-F]{64}$/);

      // Constructed directly against the local fork -- not tx.ts's memoized getPublicClient()
      // singleton, which would still be bound to whatever RPC first constructed it this process.
      const client = createPublicClient({ chain: base, transport: http(LOCAL_RPC_URL) });
      const receipt = await client.getTransactionReceipt({ hash });
      expect(receipt.status).toBe("success");
      expect(receipt.to?.toLowerCase()).toBe(ANVIL_ACCOUNT_1.toLowerCase());
    },
    20_000,
  );
});

if (!e2eEnabled) {
  describe("fork-impersonate e2e availability", () => {
    const reason = !anvilAvailable
      ? `anvil not found at ${ANVIL_BIN} (or on PATH)`
      : "VEYDRIFT_FORK_TEST_RPC_URL is not set (upstream RPC for anvil --fork-url)";
    it.skip(`${reason} -- e2e fork test skipped`, () => {});
  });
}
