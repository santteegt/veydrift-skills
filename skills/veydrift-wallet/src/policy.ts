/**
 * Tier resolution: `walletctl` must not trust a caller-asserted `--tier`/`VEYDRIFT_TIER` as the
 * enforcing tier for `checkAllowlist` -- that would let a fully compromised `veydrift-agent`
 * (which is exactly who §6.4's allowlist exists to defend against) simply pass `--tier operator`
 * regardless of what its own policy actually authorizes.
 *
 * Fix: the tier is read from `$VEYDRIFT_HOME/policy.json` (the same file `veydrift-agent` reads,
 * `docs/SPEC.md` §2.1) -- never from this process's own CLI flag or env var, except as a
 * fallback when no policy file exists at all (e.g. running this engine standalone in a context
 * that has no `veydrift-agent` install alongside it).
 *
 * Rules (see references/tx-safety.md for the write-up, including the honest residual limit):
 *   1. Policy file exists and parses with a valid `tier` -> that tier is authoritative.
 *      - If a caller-supplied --tier/VEYDRIFT_TIER is ALSO present and disagrees -> refuse.
 *        Never silently prefer either value.
 *   2. Policy file does not exist (ENOENT) -> fall back to --tier/VEYDRIFT_TIER, default
 *      "advisor" -- identical to this engine's old, sole behavior.
 *   3. Policy file exists but is unreadable/unparseable/has no valid tier -> refuse outright.
 *      A malformed security policy must never be treated as "absent" and fall through to a
 *      permissive default.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isTier, TIERS, type Tier } from "./allowlist.js";

export class TierResolutionError extends Error {}

const DEFAULT_VEYDRIFT_HOME = "~/.veydrift";

/** Mirrors veydrift-agent's `veydrift_home()` (state.py): $VEYDRIFT_HOME env, else ~/.veydrift.
 *  Only `~` at the very start is expanded (matching Python's `Path.expanduser()` semantics). */
export function resolveVeydriftHome(env: NodeJS.ProcessEnv = process.env): string {
  const raw = env.VEYDRIFT_HOME?.trim() || DEFAULT_VEYDRIFT_HOME;
  if (raw === "~") return homedir();
  if (raw.startsWith("~/")) return join(homedir(), raw.slice(2));
  return raw;
}

export function policyPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(resolveVeydriftHome(env), "policy.json");
}

export interface ResolveTierOptions {
  /** The `--tier` CLI flag, if the caller passed one. */
  cliFlag?: string;
  env?: NodeJS.ProcessEnv;
  /** Injectable so tests never touch the real filesystem or the real $HOME/.veydrift. Must throw
   *  an Error with `.code === "ENOENT"` (matching Node's fs errors) when the file is absent. */
  readFile?: (path: string) => string;
}

/**
 * Resolve the enforcing tier. Never trusts `cliFlag`/`VEYDRIFT_TIER` over the policy file --
 * see the module doc comment above for the exact precedence rules.
 *
 * Throws `TierResolutionError` (never returns a fallback) on: an unreadable-for-a-reason-other-
 * than-"missing" policy file, unparseable JSON, a missing/invalid `tier` field, or a caller/policy
 * tier disagreement. Callers (cli.ts) must treat any thrown error here as "exit non-zero, sign
 * nothing" -- exactly like an allowlist rejection.
 */
export function resolveTier(opts: ResolveTierOptions = {}): Tier {
  const env = opts.env ?? process.env;
  const readFile = opts.readFile ?? ((p: string) => readFileSync(p, "utf8"));
  const path = policyPath(env);
  const callerTier = opts.cliFlag ?? env.VEYDRIFT_TIER;

  let raw: string | undefined;
  try {
    raw = readFile(path);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") {
      throw new TierResolutionError(
        `could not read policy file at "${path}": ${(err as Error).message}. Refusing rather ` +
          `than falling back to a permissive default on an unreadable security policy.`,
      );
    }
    // No policy file at all -- fall back to the caller-supplied tier (old behavior).
    const fallback = callerTier ?? "advisor";
    if (!isTier(fallback)) {
      throw new TierResolutionError(`Invalid tier "${fallback}". Must be one of: ${TIERS.join(", ")}.`);
    }
    return fallback;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new TierResolutionError(
      `policy file at "${path}" is not valid JSON: ${(err as Error).message}. Refusing rather ` +
        `than falling back to a permissive default on a malformed security policy.`,
    );
  }

  const tierValue = (parsed as { tier?: unknown } | null)?.tier;
  if (typeof tierValue !== "string" || !isTier(tierValue)) {
    throw new TierResolutionError(
      `policy file at "${path}" has no valid "tier" field (got ${JSON.stringify(tierValue)}; ` +
        `must be one of: ${TIERS.join(", ")}). Refusing rather than falling back to a permissive ` +
        `default on a malformed security policy.`,
    );
  }

  if (callerTier !== undefined && callerTier !== tierValue) {
    throw new TierResolutionError(
      `tier disagreement: policy file ("${path}") says tier="${tierValue}", but the caller ` +
        `supplied --tier/VEYDRIFT_TIER="${callerTier}". Refusing to guess which is correct -- ` +
        `fix the disagreement (either drop --tier/VEYDRIFT_TIER, or make it match the policy).`,
    );
  }

  return tierValue;
}
