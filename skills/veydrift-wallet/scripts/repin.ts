#!/usr/bin/env tsx
/**
 * Re-pin tool. The on-chain drift check (`walletctl verify-abi`) refuses every write once the
 * deployed contracts stop matching the pin; this is how a maintainer moves the pin forward safely.
 *
 *   npm run repin -- confirm --out <contracts>/out
 *   npm run repin -- write   --out <contracts>/out --commit <40-hex sha> [--build-location "<text>"]
 *
 * `--out` is a forge `out/` directory built from the candidate commit with the pinned foundry
 * settings (`forge build --skip test --skip script`; see references/abi-pinning.md).
 *
 * `confirm` proves the build is what is deployed: every contract reachable from the pinned proxies
 * (router, modules, nested modules, libraries, and the external dependencies) must match a forge
 * artifact byte-for-byte outside the immutable and link-placeholder positions. `write` runs
 * `confirm` first and refuses to write anything unless it passes; it then regenerates the pinned
 * ABI files and both PINNED*.json files from the artifacts and the live chain.
 *
 * After `write`, three things stay manual on purpose (each is a tripwire, not busywork): the
 * expected hash/commit constants in tests/abi.test.ts, the agent's `PINNED_ABI_HASH` /
 * `KNOWN_STALE_BACKEND_ABI_HASH` in guard.py, and the docs.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getAddress, keccak256, parseAbi } from "viem";
import { computeAbiHash, fetchLiveRuntimeConfig, loadPinnedMeta, type PinnedMeta } from "../src/abi.js";
import { EIP1967_IMPLEMENTATION_SLOT } from "../src/onchain-pin.js";
import { confirmDeployment, loadArtifactCodes } from "../src/repin.js";
import { getPublicClient } from "../src/rpc.js";

const ABI_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "abi");
const PROXY_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103" as const;

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

function die(message: string): never {
  console.error(message);
  process.exit(1);
}

const client = getPublicClient();

async function implementationOf(proxy: string): Promise<string> {
  const raw = await client.getStorageAt({ address: getAddress(proxy), slot: EIP1967_IMPLEMENTATION_SLOT });
  const tail = raw?.slice(-40) ?? "";
  if (!tail || /^0+$/.test(tail)) die(`${proxy} has an empty EIP-1967 implementation slot (not a proxy?)`);
  return getAddress(`0x${tail}`);
}

async function codeHashOf(address: string): Promise<string> {
  const code = await client.getCode({ address: getAddress(address) });
  if (!code || code === "0x") die(`${address} has no code`);
  return keccak256(code);
}

async function runConfirm(outDir: string) {
  const game = loadPinnedMeta("game");
  const alliance = loadPinnedMeta("alliance");
  const deps = game.dependencies;
  if (!game.implementation || !alliance.implementation || !deps) die("PINNED.json has no implementation/dependencies block to start from");
  const artifacts = loadArtifactCodes(outDir);
  console.log(`${artifacts.length} artifacts with runtime code loaded from ${outDir}`);
  const result = await confirmDeployment({
    client,
    roots: { game: game.implementation.proxy, alliance: alliance.implementation.proxy },
    artifacts,
    extraImplementations: {
      randomnessEngine: await implementationOf(deps.randomnessEngine.proxy),
      moonSystem: await implementationOf(deps.moonSystem.proxy),
    },
  });
  for (const m of result.matched) console.log(`  MATCH   ${m.address}  ${String(m.bytes).padStart(6)}B  ${m.artifact}   [${m.via}]`);
  for (const u of result.unmatched) console.log(`  NO MATCH ${u.address}  ${String(u.bytes).padStart(6)}B  [${u.via}]`);
  console.log(`\n${result.matched.length}/${result.matched.length + result.unmatched.length} contracts match the build`);
  return result;
}

function readAbi(outDir: string, file: string, name: string): { abi: unknown[]; methodIdentifiers: Record<string, string> } {
  const path = join(outDir, `${file}.sol`, `${name}.json`);
  if (!existsSync(path)) die(`missing forge artifact ${path}`);
  const artifact = JSON.parse(readFileSync(path, "utf8")) as { abi: unknown[]; methodIdentifiers: Record<string, string> };
  return { abi: artifact.abi, methodIdentifiers: artifact.methodIdentifiers };
}

async function runWrite(outDir: string, commit: string) {
  if (!/^[0-9a-f]{40}$/.test(commit)) die("--commit must be the full 40-hex commit SHA");
  const short = commit.slice(0, 7);
  const result = await runConfirm(outDir);
  if (!result.ok) die("\nNOT re-pinning: the build does not match what is deployed. Fix the commit/build and re-run `confirm`.");

  const previous = loadPinnedMeta("game");
  const previousAlliance = loadPinnedMeta("alliance");
  const deps = previous.dependencies!;
  const now = new Date().toISOString();
  const block = Number(await client.getBlockNumber());
  const config = await fetchLiveRuntimeConfig();

  const gameArtifact = readAbi(outDir, "VeydriftGame", "VeydriftGame");
  const allianceArtifact = readAbi(outDir, "VeydriftAllianceSystem", "VeydriftAllianceSystem");
  const delegationArtifact = readAbi(outDir, "IVeydriftDelegation", "IVeydriftDelegation");
  const files = {
    game: `VeydriftGame.${short}.json`,
    alliance: `VeydriftAllianceSystem.${short}.json`,
    delegation: `VeydriftDelegation.${short}.json`,
  };
  const json = (value: unknown) => JSON.stringify(value, null, 2) + "\n";

  let forgeVersion: string | undefined;
  try {
    forgeVersion = execFileSync("forge", ["--version"], { encoding: "utf8" }).split("\n")[0]?.trim();
  } catch {
    // informational only
  }
  const source = (artifactPath: string): Record<string, unknown> => ({
    repo: "https://github.com/Borodutch/veydrift.git",
    commit,
    buildLocation: arg("build-location") ?? `forge build of ${resolve(outDir, "..")}`,
    buildCommand: "forge build --skip test --skip script",
    ...(forgeVersion ? { forgeVersion } : {}),
    artifactPath,
    confirmation: {
      status: "confirmed",
      method:
        `Runtime code of every contract reachable from the game and alliance implementations (${result.matched.length} contracts, ` +
        "including modules, nested modules, libraries and the Randomness/Moon implementations) matched forge artifacts of this commit, " +
        "masking immutableReferences and linkReferences (`npm run repin -- confirm`).",
      verifiedAt: now,
      atBlock: block,
    },
  });

  const gameProxy = previous.implementation!.proxy;
  const allianceProxy = previousAlliance.implementation!.proxy;
  const gameImplementation = result.implementations.game!;
  const allianceImplementation = result.implementations.alliance!;
  const randomnessProxy = getAddress(
    (await client.readContract({
      address: getAddress(gameProxy),
      abi: parseAbi(["function randomnessEngine() view returns (address)"]),
      functionName: "randomnessEngine",
    })) as string,
  );
  const moonProxy = getAddress((config.moonContractAddress as string | undefined) ?? deps.moonSystem.proxy);

  const gameHash = computeAbiHash(gameArtifact.abi as never);
  const delegationHash = computeAbiHash(delegationArtifact.abi as never);
  const gameMeta: PinnedMeta = {
    commit,
    artifact: files.game,
    abiHash: gameHash,
    foundry: previous.foundry,
    fetchedAt: now,
    source: source("packages/contracts/out/VeydriftGame.sol/VeydriftGame.json"),
    implementation: {
      proxy: gameProxy,
      proxyKind: previous.implementation!.proxyKind,
      slot: EIP1967_IMPLEMENTATION_SLOT,
      address: gameImplementation,
      codeHash: await codeHashOf(gameImplementation),
      proxyAdmin: getAddress(`0x${(await client.getStorageAt({ address: getAddress(gameProxy), slot: PROXY_ADMIN_SLOT }))!.slice(-40)}`),
      observedAt: now,
      observedAtBlock: block,
    },
    dependencies: {
      randomnessEngine: {
        ...deps.randomnessEngine,
        proxy: randomnessProxy,
        implementation: result.implementations.randomnessEngine!,
        codeHash: await codeHashOf(result.implementations.randomnessEngine!),
      },
      moonSystem: {
        ...deps.moonSystem,
        proxy: moonProxy,
        implementation: result.implementations.moonSystem!,
        codeHash: await codeHashOf(result.implementations.moonSystem!),
      },
      appliesTo: deps.appliesTo,
    },
    backendReported: {
      deploymentAbiHash: config.backend?.build?.deploymentAbiHash ?? "",
      deploymentCommit: config.backend?.build?.deploymentCommit ?? "",
      deploymentTimestamp: String(config.backend?.build?.deploymentTimestamp ?? ""),
      note:
        "What /runtime-config reported at re-pin time. If it differs from this pin's abiHash it is KNOWN-STALE: the guard treats it as " +
        "advisory (never the authority) and stays silent while the backend keeps reporting exactly this value.",
    },
    supplemental: [
      {
        name: "VeydriftDelegation",
        file: files.delegation,
        abiHash: delegationHash,
        artifactPath: "packages/contracts/out/IVeydriftDelegation.sol/IVeydriftDelegation.json",
        note: previous.supplemental?.[0]?.note,
      },
    ],
    note: `abi/${files.game} holds { abi, methodIdentifiers } from the forge artifact above; abiHash = sha256(JSON.stringify(abi)), compact separators, forge key order.`,
  };
  const allianceMeta: PinnedMeta = {
    commit,
    artifact: files.alliance,
    abiHash: computeAbiHash(allianceArtifact.abi as never),
    foundry: previousAlliance.foundry,
    fetchedAt: now,
    source: source("packages/contracts/out/VeydriftAllianceSystem.sol/VeydriftAllianceSystem.json"),
    implementation: {
      proxy: allianceProxy,
      proxyKind: previousAlliance.implementation!.proxyKind,
      slot: EIP1967_IMPLEMENTATION_SLOT,
      address: allianceImplementation,
      codeHash: await codeHashOf(allianceImplementation),
      observedAt: now,
      observedAtBlock: block,
    },
    note: `abi/${files.alliance} holds { abi, methodIdentifiers } from the forge artifact above. /runtime-config exposes no alliance ABI hash, so this pin is verified on-chain (see \`implementation\`), not against a backend hash.`,
  };

  writeFileSync(join(ABI_DIR, files.game), json(gameArtifact));
  writeFileSync(join(ABI_DIR, files.alliance), json(allianceArtifact));
  writeFileSync(join(ABI_DIR, files.delegation), json(delegationArtifact));
  writeFileSync(join(ABI_DIR, "PINNED.json"), json(gameMeta));
  writeFileSync(join(ABI_DIR, "PINNED.alliance.json"), json(allianceMeta));

  const keep = new Set(Object.values(files));
  for (const name of readdirSync(ABI_DIR)) {
    if (/^Veydrift(Game|AllianceSystem|Delegation)\.[0-9a-f]{7}\.json$/.test(name) && !keep.has(name)) {
      rmSync(join(ABI_DIR, name));
      console.log(`removed superseded ${name}`);
    }
  }

  console.log(`\nwrote ${Object.values(files).join(", ")}, PINNED.json, PINNED.alliance.json`);
  console.log(`game ABI hash:       ${gameHash}`);
  console.log(`backend now reports: ${gameMeta.backendReported?.deploymentAbiHash || "(nothing)"} (${gameMeta.backendReported?.deploymentAbiHash === gameHash ? "matches" : "KNOWN-STALE"})`);
  console.log("\nStill to do by hand (each is a deliberate tripwire):");
  console.log("  1. tests/abi.test.ts        EXPECTED_HASH / EXPECTED_COMMIT / STALE_BACKEND_HASH");
  console.log("  2. veydrift-agent guard.py  PINNED_ABI_HASH and KNOWN_STALE_BACKEND_ABI_HASH");
  console.log("  3. docs                     references/abi-pinning.md, CHANGELOG.md");
  console.log("  4. re-run both suites, then `VEYDRIFT_LIVE_TESTS=1 npm test` and `walletctl verify-abi`");
}

const command = process.argv[2];
const out = arg("out");
if (!out) die("usage: repin.ts confirm|write --out <forge out dir> [--commit <sha>]");
if (command === "confirm") {
  const result = await runConfirm(resolve(out));
  process.exit(result.ok ? 0 : 1);
} else if (command === "write") {
  const commit = arg("commit");
  if (!commit) die("write needs --commit <40-hex sha>");
  await runWrite(resolve(out), commit);
} else {
  die("usage: repin.ts confirm|write --out <forge out dir> [--commit <sha>]");
}
