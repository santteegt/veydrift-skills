import { describe, expect, it } from "vitest";
import { policyPath, resolveTier, resolveVeydriftHome, TierResolutionError } from "../src/policy.js";

function enoent(path: string): NodeJS.ErrnoException {
  const err = new Error(`ENOENT: no such file or directory, open '${path}'`) as NodeJS.ErrnoException;
  err.code = "ENOENT";
  return err;
}

describe("resolveVeydriftHome / policyPath", () => {
  it("defaults to ~/.veydrift when VEYDRIFT_HOME is unset", () => {
    const home = resolveVeydriftHome({});
    expect(home.endsWith("/.veydrift")).toBe(true);
  });

  it("honors VEYDRIFT_HOME when set", () => {
    expect(resolveVeydriftHome({ VEYDRIFT_HOME: "/tmp/some-home" })).toBe("/tmp/some-home");
    expect(policyPath({ VEYDRIFT_HOME: "/tmp/some-home" })).toBe("/tmp/some-home/policy.json");
  });
});

describe("resolveTier -- FIX 3: tier is read from policy.json, never asserted by the caller", () => {
  it("uses the policy file's tier when no --tier/VEYDRIFT_TIER is supplied", () => {
    const tier = resolveTier({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "economy" }),
    });
    expect(tier).toBe("economy");
  });

  it("accepts a --tier that agrees with the policy file", () => {
    const tier = resolveTier({
      cliFlag: "operator",
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "operator" }),
    });
    expect(tier).toBe("operator");
  });

  it("refuses when --tier disagrees with the policy file -- a compromised agent cannot escalate by passing --tier", () => {
    expect(() =>
      resolveTier({
        cliFlag: "operator",
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "advisor" }),
      }),
    ).toThrow(TierResolutionError);

    try {
      resolveTier({
        cliFlag: "operator",
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "advisor" }),
      });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(TierResolutionError);
      expect((err as Error).message).toMatch(/advisor/);
      expect((err as Error).message).toMatch(/operator/);
    }
  });

  it("refuses when VEYDRIFT_TIER (not just --tier) disagrees with the policy file", () => {
    expect(() =>
      resolveTier({
        env: { VEYDRIFT_HOME: "/fake", VEYDRIFT_TIER: "operator" },
        readFile: () => JSON.stringify({ version: 1, tier: "economy" }),
      }),
    ).toThrow(/tier disagreement/);
  });

  it("falls back to --tier, defaulting to advisor, when no policy file exists at all", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveTier({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe("advisor");
    expect(resolveTier({ cliFlag: "economy", env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe("economy");
    expect(resolveTier({ env: { VEYDRIFT_HOME: "/fake", VEYDRIFT_TIER: "operator" }, readFile })).toBe("operator");
  });

  it("refuses (never falls back to a permissive default) when the policy file is unparseable JSON", () => {
    expect(() =>
      resolveTier({ env: { VEYDRIFT_HOME: "/fake" }, readFile: () => "{ not valid json" }),
    ).toThrow(TierResolutionError);
  });

  it("refuses when the policy file has no tier field", () => {
    expect(() =>
      resolveTier({ env: { VEYDRIFT_HOME: "/fake" }, readFile: () => JSON.stringify({ version: 1 }) }),
    ).toThrow(/no valid "tier" field/);
  });

  it("refuses when the policy file's tier is not a recognized value", () => {
    expect(() =>
      resolveTier({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "superadmin" }),
      }),
    ).toThrow(/no valid "tier" field/);
  });

  it("refuses when the policy file exists but errors on read for a reason other than ENOENT", () => {
    const readFile = () => {
      const err = new Error("EACCES: permission denied") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveTier({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toThrow(TierResolutionError);
  });

  it("refuses an invalid --tier when there is no policy file to fall back to disagreement logic", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(() => resolveTier({ cliFlag: "superadmin", env: { VEYDRIFT_HOME: "/fake" }, readFile })).toThrow(
      /Invalid tier/,
    );
  });
});
