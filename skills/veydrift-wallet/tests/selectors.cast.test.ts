/**
 * Live cross-check against `cast sig` (foundry), independent of our own encoder. Skips (rather
 * than fails) if foundry isn't installed at ~/.foundry/bin in whatever environment runs the
 * tests -- the hardcoded cross-check in tests/abi.test.ts already records the ground truth this
 * produced when it was run for this work package.
 */
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { getSelectorForSignature } from "../src/abi.js";

const CAST_BIN = join(homedir(), ".foundry", "bin", "cast");
const castAvailable = existsSync(CAST_BIN);

function castSig(signature: string): string {
  return execFileSync(CAST_BIN, ["sig", signature], { encoding: "utf8" }).trim();
}

describe.skipIf(!castAvailable)("live `cast sig` cross-check", () => {
  const signatures = [
    "startBuildingUpgrade(uint256,uint8)",
    "startResearch(uint256,uint8)",
    "resolveFleetMission(uint256)",
    "settlePlanet(uint256)",
    "startDefenseProduction(uint256,uint8,uint32)",
    "attackProtectionStatus(address,uint256)",
    "launchInterplanetaryMissileAttack(uint256,uint256,uint8,uint32)",
  ];

  it.each(signatures)("%s matches `cast sig` output", (sig) => {
    expect(getSelectorForSignature(sig)).toBe(castSig(sig));
  });
});

if (!castAvailable) {
  describe("cast availability", () => {
    it.skip(`foundry not found at ${CAST_BIN} -- live cross-check skipped`, () => {});
  });
}
