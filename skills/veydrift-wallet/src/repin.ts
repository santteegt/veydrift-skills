/**
 * Re-pin tooling: prove which forge build is deployed behind the pinned proxies.
 *
 * A pin is only as good as the claim "this ABI describes what is deployed". The deployed system is
 * a router (`VeydriftGame`) that reaches its modules and libraries through `immutable` addresses,
 * and those modules embed further modules, so comparing the router's bytecode alone proves almost
 * nothing -- the logic that actually changes (delegation, batch production) lives in the modules.
 * `confirmDeployment` therefore walks EVERY contract reachable from the proxies' implementations:
 * it extracts the contract addresses embedded in each contract's runtime code, fetches their code,
 * and matches each one against the forge artifacts of the candidate build, masking the two kinds of
 * bytes forge leaves unresolved in an artifact (`immutableReferences` -- filled at construction --
 * and `linkReferences` -- library placeholders). Only when every reachable contract matches is the
 * commit "confirmed".
 *
 * Pure functions plus one injected-client orchestrator, so the matching logic is unit-tested with
 * synthetic bytecode rather than needing a chain.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { getAddress } from "viem";
import { EIP1967_IMPLEMENTATION_SLOT } from "./onchain-pin.js";

export interface ArtifactCode {
  /** `File:Contract`, e.g. `VeydriftGame:VeydriftGame`. */
  name: string;
  code: Uint8Array;
  /** 1 = compare this byte, 0 = skip (immutable or link placeholder). Same length as `code`. */
  mask: Uint8Array;
}

interface ForgeArtifactJson {
  deployedBytecode?: {
    object?: string;
    immutableReferences?: Record<string, Array<{ start: number; length: number }>>;
    linkReferences?: Record<string, Record<string, Array<{ start: number; length: number }>>>;
  };
}

/** Parse one forge artifact's runtime bytecode into (bytes, compare-mask). `undefined` for an
 *  artifact with no runtime code (interfaces, abstract contracts). */
export function parseArtifactCode(name: string, artifact: ForgeArtifactJson): ArtifactCode | undefined {
  const deployed = artifact.deployedBytecode;
  const object = deployed?.object;
  if (!object || object === "0x" || object.length < 10) return undefined;
  let hex = object.slice(2);
  const masked: Array<[number, number]> = [];
  for (const refs of Object.values(deployed.immutableReferences ?? {})) {
    for (const ref of refs) masked.push([ref.start, ref.length]);
  }
  for (const libs of Object.values(deployed.linkReferences ?? {})) {
    for (const refs of Object.values(libs)) {
      for (const ref of refs) {
        masked.push([ref.start, ref.length]);
        // An unlinked library appears as a `__$<hash>$__` placeholder, which is not hex.
        hex = hex.slice(0, 2 * ref.start) + "0".repeat(2 * ref.length) + hex.slice(2 * (ref.start + ref.length));
      }
    }
  }
  if (!/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2 !== 0) return undefined;
  const code = Uint8Array.from(Buffer.from(hex, "hex"));
  const mask = new Uint8Array(code.length).fill(1);
  for (const [start, length] of masked) mask.fill(0, start, Math.min(start + length, mask.length));
  return { name, code, mask };
}

/** Load every artifact with runtime code from a forge `out/` directory (`<File>.sol/<Name>.json`). */
export function loadArtifactCodes(outDir: string): ArtifactCode[] {
  const artifacts: ArtifactCode[] = [];
  for (const dir of readdirSync(outDir)) {
    if (!dir.endsWith(".sol")) continue;
    const dirPath = join(outDir, dir);
    if (!statSync(dirPath).isDirectory()) continue;
    for (const file of readdirSync(dirPath)) {
      if (!file.endsWith(".json")) continue;
      let parsed: ForgeArtifactJson;
      try {
        parsed = JSON.parse(readFileSync(join(dirPath, file), "utf8")) as ForgeArtifactJson;
      } catch {
        continue;
      }
      const artifact = parseArtifactCode(`${dir.slice(0, -4)}:${file.slice(0, -5)}`, parsed);
      if (artifact) artifacts.push(artifact);
    }
  }
  return artifacts;
}

/** True when `onchain` equals the artifact's code at every unmasked byte (and has the same length). */
export function maskedEqual(onchain: Uint8Array, artifact: ArtifactCode): boolean {
  if (onchain.length !== artifact.code.length) return false;
  for (let i = 0; i < onchain.length; i++) {
    if (artifact.mask[i] === 1 && onchain[i] !== artifact.code[i]) return false;
  }
  return true;
}

/** Every contract-like address constant embedded in runtime code: `PUSH20 <addr>`, and the
 *  zero-padded `PUSH32` form the optimizer emits for immutables. Skips values with a `0x0000`
 *  prefix (precompiles and ordinary small constants). Walks opcodes properly so a `PUSH` immediate
 *  is never misread as an opcode. */
export function embeddedAddresses(code: Uint8Array): string[] {
  const found = new Set<string>();
  let i = 0;
  while (i < code.length) {
    const op = code[i] as number;
    if (op >= 0x60 && op <= 0x7f) {
      const length = op - 0x5f;
      const immediate = code.subarray(i + 1, i + 1 + length);
      let candidate: Uint8Array | undefined;
      if (length === 20) candidate = immediate;
      else if (length === 32 && immediate.subarray(0, 12).every((b) => b === 0)) candidate = immediate.subarray(12);
      if (candidate && candidate.length === 20 && !(candidate[0] === 0 && candidate[1] === 0)) {
        found.add(getAddress(`0x${Buffer.from(candidate).toString("hex")}`));
      }
      i += 1 + length;
    } else {
      i += 1;
    }
  }
  return [...found];
}

export interface ConfirmClient {
  getStorageAt(args: { address: `0x${string}`; slot: `0x${string}` }): Promise<`0x${string}` | undefined>;
  getCode(args: { address: `0x${string}` }): Promise<`0x${string}` | undefined>;
}

export interface ConfirmedContract {
  address: string;
  bytes: number;
  artifact: string;
  /** How it was reached: the root label, or the matched contract that embeds it. */
  via: string;
}

export interface ConfirmResult {
  /** Every reachable contract matched an artifact. */
  ok: boolean;
  matched: ConfirmedContract[];
  unmatched: Array<{ address: string; bytes: number; via: string }>;
  /** Implementation address read from each root proxy's EIP-1967 slot. */
  implementations: Record<string, string>;
}

function hexToBytes(hex: string): Uint8Array {
  return Uint8Array.from(Buffer.from(hex.slice(2), "hex"));
}

/**
 * Match every contract reachable from `roots` (proxy address per label) against `artifacts`.
 * `extraImplementations` are checked as roots too (e.g. an external dependency's implementation
 * that is not embedded anywhere). Contract addresses whose code is empty (EOAs, stray constants)
 * are ignored rather than counted as unmatched.
 */
export async function confirmDeployment(opts: {
  client: ConfirmClient;
  roots: Record<string, string>;
  artifacts: ArtifactCode[];
  extraImplementations?: Record<string, string>;
}): Promise<ConfirmResult> {
  const { client, roots, artifacts } = opts;
  const byLength = new Map<number, ArtifactCode[]>();
  for (const artifact of artifacts) {
    const bucket = byLength.get(artifact.code.length) ?? [];
    bucket.push(artifact);
    byLength.set(artifact.code.length, bucket);
  }

  const seen = new Set<string>();
  const matched: ConfirmedContract[] = [];
  const unmatched: ConfirmResult["unmatched"] = [];
  const implementations: Record<string, string> = {};

  const visit = async (address: string, via: string): Promise<void> => {
    const key = address.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    const raw = await client.getCode({ address: getAddress(address) });
    if (!raw || raw === "0x") return;
    const code = hexToBytes(raw);
    const hit = (byLength.get(code.length) ?? []).find((artifact) => maskedEqual(code, artifact));
    if (!hit) {
      unmatched.push({ address: getAddress(address), bytes: code.length, via });
      return;
    }
    matched.push({ address: getAddress(address), bytes: code.length, artifact: hit.name, via });
    for (const embedded of embeddedAddresses(code)) await visit(embedded, hit.name);
  };

  for (const [label, proxy] of Object.entries(roots)) {
    const slot = await client.getStorageAt({ address: getAddress(proxy), slot: EIP1967_IMPLEMENTATION_SLOT });
    const tail = slot && /^0x[0-9a-fA-F]{64}$/.test(slot) ? slot.slice(-40) : "";
    if (!tail || /^0+$/.test(tail)) {
      unmatched.push({ address: getAddress(proxy), bytes: 0, via: `${label}: EIP-1967 slot empty` });
      continue;
    }
    const implementation = getAddress(`0x${tail}`);
    implementations[label] = implementation;
    await visit(implementation, `${label} implementation`);
  }
  for (const [label, implementation] of Object.entries(opts.extraImplementations ?? {})) {
    implementations[label] = getAddress(implementation);
    await visit(implementation, `${label} implementation`);
  }
  return { ok: unmatched.length === 0 && matched.length > 0, matched, unmatched, implementations };
}
