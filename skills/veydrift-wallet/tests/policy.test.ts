import { describe, expect, it } from "vitest";
import {
  AllowAcsDefenseResolutionError,
  AllowAllianceResolutionError,
  AllowCombatResolutionError,
  AllowDelegationResolutionError,
  policyPath,
  resolveAllowAcsDefense,
  resolveAllowAlliance,
  resolveAllowCombat,
  resolveAllowDelegation,
  resolveExpectedSigner,
  resolveExpectedWallet,
  resolveTier,
  resolveVeydriftHome,
  TierResolutionError,
  WalletBindingResolutionError,
} from "../src/policy.js";

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

describe("resolveAllowCombat -- launch-actions plan commit 5: no CLI flag, no env var, ever", () => {
  it("returns true when the policy file's actions.allow_combat is true", () => {
    const allowed = resolveAllowCombat({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_combat: true } }),
    });
    expect(allowed).toBe(true);
  });

  it("returns false when the policy file's actions.allow_combat is false", () => {
    const allowed = resolveAllowCombat({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_combat: false } }),
    });
    expect(allowed).toBe(false);
  });

  it("returns false (never refuses) when no policy file exists at all -- there is no flag to fall back to", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveAllowCombat({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
  });

  it("refuses (never falls back to a permissive default) when the policy file is unparseable JSON", () => {
    expect(() =>
      resolveAllowCombat({ env: { VEYDRIFT_HOME: "/fake" }, readFile: () => "{ not valid json" }),
    ).toThrow(AllowCombatResolutionError);
  });

  it("refuses when actions.allow_combat is missing", () => {
    expect(() =>
      resolveAllowCombat({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: {} }),
      }),
    ).toThrow(/no valid "actions.allow_combat" field/);
  });

  it("refuses when the actions object itself is missing", () => {
    expect(() =>
      resolveAllowCombat({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator" }),
      }),
    ).toThrow(/no valid "actions.allow_combat" field/);
  });

  it("refuses when actions.allow_combat is not a boolean", () => {
    expect(() =>
      resolveAllowCombat({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_combat: "true" } }),
      }),
    ).toThrow(/no valid "actions.allow_combat" field/);
  });

  it("refuses when the policy file exists but errors on read for a reason other than ENOENT", () => {
    const readFile = () => {
      const err = new Error("EACCES: permission denied") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveAllowCombat({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toThrow(AllowCombatResolutionError);
  });

  it("has no --allow-combat CLI flag or VEYDRIFT_ALLOW_COMBAT env var -- ResolveAllowCombatOptions has no such fields", () => {
    // Type-level guarantee, exercised at compile time: ResolveAllowCombatOptions only
    // accepts { env, readFile }. This test documents the guarantee for a reader who
    // isn't checking the type definition directly; there is deliberately no runtime
    // assertion possible for "a parameter does not exist."
    const allowed = resolveAllowCombat({
      env: { VEYDRIFT_HOME: "/fake", VEYDRIFT_ALLOW_COMBAT: "true" } as NodeJS.ProcessEnv,
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_combat: false } }),
    });
    expect(allowed).toBe(false); // the policy file's actual value wins; the bogus env var is never read
  });
});

describe("resolveAllowAlliance -- alliance feature: no CLI flag, no env var, ever (same shape as resolveAllowCombat, different field/error type)", () => {
  it("returns true when the policy file's actions.allow_alliance is true", () => {
    const allowed = resolveAllowAlliance({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "economy", actions: { allow_alliance: true } }),
    });
    expect(allowed).toBe(true);
  });

  it("returns false when the policy file's actions.allow_alliance is false", () => {
    const allowed = resolveAllowAlliance({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "economy", actions: { allow_alliance: false } }),
    });
    expect(allowed).toBe(false);
  });

  it("returns false (never refuses) when no policy file exists at all -- there is no flag to fall back to", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveAllowAlliance({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
  });

  it("refuses (never falls back to a permissive default) when the policy file is unparseable JSON", () => {
    expect(() =>
      resolveAllowAlliance({ env: { VEYDRIFT_HOME: "/fake" }, readFile: () => "{ not valid json" }),
    ).toThrow(AllowAllianceResolutionError);
  });

  it("refuses when actions.allow_alliance is missing", () => {
    expect(() =>
      resolveAllowAlliance({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "economy", actions: {} }),
      }),
    ).toThrow(/no valid "actions.allow_alliance" field/);
  });

  it("refuses when the actions object itself is missing", () => {
    expect(() =>
      resolveAllowAlliance({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "economy" }),
      }),
    ).toThrow(/no valid "actions.allow_alliance" field/);
  });

  it("refuses when actions.allow_alliance is not a boolean", () => {
    expect(() =>
      resolveAllowAlliance({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "economy", actions: { allow_alliance: "true" } }),
      }),
    ).toThrow(/no valid "actions.allow_alliance" field/);
  });

  it("refuses when the policy file exists but errors on read for a reason other than ENOENT", () => {
    const readFile = () => {
      const err = new Error("EACCES: permission denied") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveAllowAlliance({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toThrow(
      AllowAllianceResolutionError,
    );
  });

  it("has no --allow-alliance CLI flag or VEYDRIFT_ALLOW_ALLIANCE env var -- ResolveAllowAllianceOptions has no such fields", () => {
    const allowed = resolveAllowAlliance({
      env: { VEYDRIFT_HOME: "/fake", VEYDRIFT_ALLOW_ALLIANCE: "true" } as NodeJS.ProcessEnv,
      readFile: () => JSON.stringify({ version: 1, tier: "economy", actions: { allow_alliance: false } }),
    });
    expect(allowed).toBe(false); // the policy file's actual value wins; the bogus env var is never read
  });

  it("allow_combat and allow_alliance are independent -- one being true does not imply the other", () => {
    const readFile = () =>
      JSON.stringify({ version: 1, tier: "operator", actions: { allow_combat: true, allow_alliance: false } });
    expect(resolveAllowCombat({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(true);
    expect(resolveAllowAlliance({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
  });
});

describe("resolveAllowAcsDefense -- ACS defense coordination feature: no CLI flag, no env var, ever (same shape as resolveAllowCombat/resolveAllowAlliance, different field/error type)", () => {
  it("returns true when the policy file's actions.allow_acs_defense is true", () => {
    const allowed = resolveAllowAcsDefense({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_acs_defense: true } }),
    });
    expect(allowed).toBe(true);
  });

  it("returns false when the policy file's actions.allow_acs_defense is false", () => {
    const allowed = resolveAllowAcsDefense({
      env: { VEYDRIFT_HOME: "/fake" },
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_acs_defense: false } }),
    });
    expect(allowed).toBe(false);
  });

  it("returns false (never refuses) when no policy file exists at all -- there is no flag to fall back to", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveAllowAcsDefense({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
  });

  it("refuses (never falls back to a permissive default) when the policy file is unparseable JSON", () => {
    expect(() =>
      resolveAllowAcsDefense({ env: { VEYDRIFT_HOME: "/fake" }, readFile: () => "{ not valid json" }),
    ).toThrow(AllowAcsDefenseResolutionError);
  });

  it("refuses when actions.allow_acs_defense is missing", () => {
    expect(() =>
      resolveAllowAcsDefense({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: {} }),
      }),
    ).toThrow(/no valid "actions.allow_acs_defense" field/);
  });

  it("refuses when the actions object itself is missing", () => {
    expect(() =>
      resolveAllowAcsDefense({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator" }),
      }),
    ).toThrow(/no valid "actions.allow_acs_defense" field/);
  });

  it("refuses when actions.allow_acs_defense is not a boolean", () => {
    expect(() =>
      resolveAllowAcsDefense({
        env: { VEYDRIFT_HOME: "/fake" },
        readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_acs_defense: "true" } }),
      }),
    ).toThrow(/no valid "actions.allow_acs_defense" field/);
  });

  it("refuses when the policy file exists but errors on read for a reason other than ENOENT", () => {
    const readFile = () => {
      const err = new Error("EACCES: permission denied") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveAllowAcsDefense({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toThrow(
      AllowAcsDefenseResolutionError,
    );
  });

  it("has no --allow-acs-defense CLI flag or VEYDRIFT_ALLOW_ACS_DEFENSE env var -- ResolveAllowAcsDefenseOptions has no such fields", () => {
    const allowed = resolveAllowAcsDefense({
      env: { VEYDRIFT_HOME: "/fake", VEYDRIFT_ALLOW_ACS_DEFENSE: "true" } as NodeJS.ProcessEnv,
      readFile: () => JSON.stringify({ version: 1, tier: "operator", actions: { allow_acs_defense: false } }),
    });
    expect(allowed).toBe(false); // the policy file's actual value wins; the bogus env var is never read
  });

  it("allow_combat, allow_alliance, and allow_acs_defense are independent -- any one being true does not imply the others", () => {
    const readFile = () =>
      JSON.stringify({
        version: 1,
        tier: "operator",
        actions: { allow_combat: true, allow_alliance: false, allow_acs_defense: false },
      });
    expect(resolveAllowCombat({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(true);
    expect(resolveAllowAlliance({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
    expect(resolveAllowAcsDefense({ env: { VEYDRIFT_HOME: "/fake" }, readFile })).toBe(false);
  });
});

describe("resolveExpectedWallet", () => {
  const env = { VEYDRIFT_HOME: "/fake" };
  const wallet = "0x224aba5d489675a7bd3ce07786fada466b46fa0f";

  it("returns null when no policy file exists (standalone use)", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveExpectedWallet({ env, readFile })).toBeNull();
  });

  it("returns policy.json's wallet", () => {
    const readFile = () => JSON.stringify({ tier: "economy", wallet });
    expect(resolveExpectedWallet({ env, readFile })).toBe(wallet);
  });

  it.each([
    ["missing", {}],
    ["not a string", { wallet: 42 }],
    ["not an address", { wallet: "0x1234" }],
  ])("refuses when wallet is %s", (_label, policy) => {
    const readFile = () => JSON.stringify({ tier: "economy", ...policy });
    expect(() => resolveExpectedWallet({ env, readFile })).toThrow(WalletBindingResolutionError);
  });

  it("refuses on unparseable or unreadable policy", () => {
    expect(() => resolveExpectedWallet({ env, readFile: () => "{nope" })).toThrow(WalletBindingResolutionError);
    const eacces = () => {
      const err = new Error("EACCES") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveExpectedWallet({ env, readFile: eacces })).toThrow(WalletBindingResolutionError);
  });
});

describe("resolveAllowDelegation -- no CLI flag, no env var, ever (same shape as the sibling flag resolvers)", () => {
  const env = { VEYDRIFT_HOME: "/fake" };
  const policy = (actions: unknown) => () => JSON.stringify({ version: 1, tier: "economy", actions });

  it("returns true / false as the policy file's actions.allow_delegation says", () => {
    expect(resolveAllowDelegation({ env, readFile: policy({ allow_delegation: true }) })).toBe(true);
    expect(resolveAllowDelegation({ env, readFile: policy({ allow_delegation: false }) })).toBe(false);
  });

  it("returns false (never refuses) when no policy file exists -- there is no flag to fall back to", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveAllowDelegation({ env, readFile })).toBe(false);
  });

  it.each([
    ["unparseable JSON", () => "{ not valid json"],
    ["a missing field", policy({})],
    ["a non-boolean field", policy({ allow_delegation: "true" })],
    ["a missing actions block", () => JSON.stringify({ version: 1, tier: "economy" })],
  ])("refuses (never a permissive default) on %s", (_label, readFile) => {
    expect(() => resolveAllowDelegation({ env, readFile })).toThrow(AllowDelegationResolutionError);
  });

  it("refuses on an unreadable (non-ENOENT) policy file", () => {
    const eacces = () => {
      const err = new Error("EACCES") as NodeJS.ErrnoException;
      err.code = "EACCES";
      throw err;
    };
    expect(() => resolveAllowDelegation({ env, readFile: eacces })).toThrow(AllowDelegationResolutionError);
  });

  it("its options interface has no CLI-flag or env-override field (the exact footgun --tier has)", () => {
    const opts: Parameters<typeof resolveAllowDelegation>[0] = { env, readFile: policy({ allow_delegation: true }) };
    expect(Object.keys(opts).sort()).toEqual(["env", "readFile"]);
  });
});

describe("resolveExpectedSigner -- who send must sign as, and which player that signer must act as", () => {
  const env = { VEYDRIFT_HOME: "/fake" };
  const wallet = "0x224aba5d489675a7bd3ce07786fada466b46fa0f";
  const delegate = "0x00000000000000000000000000000000000000d1";
  const read = (extra: Record<string, unknown>) => () => JSON.stringify({ tier: "economy", wallet, ...extra });

  it("returns null when no policy file exists (standalone use)", () => {
    const readFile = (p: string) => {
      throw enoent(p);
    };
    expect(resolveExpectedSigner({ env, readFile })).toBeNull();
  });

  it("without policy.signer the signer IS the wallet (unchanged behaviour), not delegated", () => {
    expect(resolveExpectedSigner({ env, readFile: read({}) })).toEqual({ wallet, signer: wallet, delegated: false });
    expect(resolveExpectedSigner({ env, readFile: read({ signer: null }) })).toEqual({ wallet, signer: wallet, delegated: false });
  });

  it("with policy.signer the signer is that delegate and the wallet stays the player", () => {
    expect(resolveExpectedSigner({ env, readFile: read({ signer: delegate }) })).toEqual({ wallet, signer: delegate, delegated: true });
  });

  it.each([
    ["not a string", { signer: 42 }],
    ["not an address", { signer: "0x1234" }],
    ["equal to wallet (omit it to sign as the wallet)", { signer: wallet.toUpperCase().replace("0X", "0x") }],
  ])("refuses when signer is %s", (_label, extra) => {
    expect(() => resolveExpectedSigner({ env, readFile: read(extra) })).toThrow(WalletBindingResolutionError);
  });

  it("keeps resolveExpectedWallet's refusals: a missing wallet, unparseable or unreadable policy", () => {
    expect(() => resolveExpectedSigner({ env, readFile: () => JSON.stringify({ tier: "economy" }) })).toThrow(WalletBindingResolutionError);
    expect(() => resolveExpectedSigner({ env, readFile: () => "{nope" })).toThrow(WalletBindingResolutionError);
  });

  it("resolveExpectedWallet is unaffected by policy.signer (still just the wallet)", () => {
    expect(resolveExpectedWallet({ env, readFile: read({ signer: delegate }) })).toBe(wallet);
  });

  it("has no CLI-flag or env override", () => {
    const opts: Parameters<typeof resolveExpectedSigner>[0] = { env, readFile: read({}) };
    expect(Object.keys(opts).sort()).toEqual(["env", "readFile"]);
  });
});
