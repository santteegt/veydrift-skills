import { loadPinnedMeta } from "../../src/abi.js";
import type { OnchainPinResult, ProxyCheck, ProxyName } from "../../src/onchain-pin.js";

function check(name: ProxyName, proxy: string, implementation: string): ProxyCheck {
  return {
    name,
    proxy,
    pinnedImplementation: implementation,
    liveImplementation: implementation,
    codeVerified: true,
    ok: true,
    problems: [],
  };
}

/** A fully-passing `OnchainPinResult` built from the real pin, for tests that only care that the
 *  pre-send check ran (never touches the network). Override fields to model a failure. */
export function passingPin(overrides: Partial<OnchainPinResult> = {}): OnchainPinResult {
  const game = loadPinnedMeta("game");
  const alliance = loadPinnedMeta("alliance");
  const deps = game.dependencies!;
  return {
    ok: true,
    dependenciesOk: true,
    game: check("game", game.implementation!.proxy, game.implementation!.address),
    alliance: check("alliance", alliance.implementation!.proxy, alliance.implementation!.address),
    randomnessEngine: check("randomnessEngine", deps.randomnessEngine.proxy, deps.randomnessEngine.implementation),
    moonSystem: check("moonSystem", deps.moonSystem.proxy, deps.moonSystem.implementation),
    appliesTo: deps.appliesTo,
    problems: [],
    warnings: [],
    checkedAt: "2026-09-28T00:00:00.000Z",
    block: "1",
    ...overrides,
  };
}
