/**
 * Two security-relevant values this engine resolves from `$VEYDRIFT_HOME/policy.json` rather
 * than from a caller-supplied flag: the enforcing **tier** (`resolveTier`, the original purpose
 * of this module) and, since the launch-actions plan's commit 5, whether **combat** is permitted
 * (`resolveAllowCombat`). Same threat model for both: `walletctl` must not trust a caller-
 * asserted value for either as authoritative for `checkAllowlist` -- that would let a fully
 * compromised `veydrift-agent` (which is exactly who §6.4's allowlist exists to defend against)
 * simply assert whatever it wants, regardless of what its own policy actually authorizes.
 *
 * Fix, for tier: read `$VEYDRIFT_HOME/policy.json` (the same file `veydrift-agent` reads,
 * `docs/SPEC.md` §2.1) -- never from this process's own CLI flag or env var, except as a
 * fallback when no policy file exists at all (e.g. running this engine standalone in a context
 * that has no `veydrift-agent` install alongside it).
 *
 * Rules for `resolveTier` (see references/tx-safety.md for the write-up, including the honest
 * residual limit):
 *   1. Policy file exists and parses with a valid `tier` -> that tier is authoritative.
 *      - If a caller-supplied --tier/VEYDRIFT_TIER is ALSO present and disagrees -> refuse.
 *        Never silently prefer either value.
 *   2. Policy file does not exist (ENOENT) -> fall back to --tier/VEYDRIFT_TIER, default
 *      "advisor" -- identical to this engine's old, sole behavior.
 *   3. Policy file exists but is unreadable/unparseable/has no valid tier -> refuse outright.
 *      A malformed security policy must never be treated as "absent" and fall through to a
 *      permissive default.
 *
 * `resolveAllowCombat` follows the same policy-file-is-authoritative shape but is deliberately
 * stricter in one respect -- see its own doc comment below for exactly how and why.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isTier, TIERS, type Tier } from "./allowlist.js";

export class TierResolutionError extends Error {}

export class AllowCombatResolutionError extends Error {}

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

export interface ResolveAllowCombatOptions {
  env?: NodeJS.ProcessEnv;
  /** Injectable so tests never touch the real filesystem. Same contract as
   *  `ResolveTierOptions.readFile`: must throw an Error with `.code === "ENOENT"` when the
   *  file is absent. */
  readFile?: (path: string) => string;
}

/**
 * Resolve whether combat (currently: the Attack mission type on `launchFleetMission`) is
 * permitted. Launch-actions plan, commit 5 -- `policy.json`'s `actions.allow_combat` was
 * previously read and ignored everywhere in this codebase (`veydrift-agent`'s `AGENTS.md` §5:
 * "combat stays unreachable by code, not by config"); this is the wallet-engine half of making
 * it a real, independently-checked second layer of enforcement, mirroring `resolveTier` above.
 *
 * **Deliberately stricter than `resolveTier` in one respect: there is no CLI flag and no
 * environment variable for this, ever.** `resolveTier` falls back to a caller-supplied
 * `--tier`/`VEYDRIFT_TIER` when no policy file exists, because that fallback is legitimately
 * needed for standalone use of this engine. Copying that shape here -- letting a caller assert
 * `--allow-combat` or `VEYDRIFT_ALLOW_COMBAT` -- would let a process that controls its own
 * environment simply assert combat is allowed, which is exactly the documented `--tier`
 * footgun (see references/tx-safety.md's residual-limit section) widened from "assert operator"
 * to "assert operator *and* combat." No such flag/env var exists anywhere in this module's
 * public surface, on purpose.
 *
 * Rules:
 *   1. Policy file does not exist (ENOENT) -> `false`. Not a fallback to any caller-asserted
 *      value (there isn't one) -- the safe default when there is no policy to consult at all.
 *   2. Policy file exists but is unreadable/unparseable, or `actions.allow_combat` is missing
 *      or not a boolean -> refuse outright (throw). Same "a malformed security policy must
 *      never be treated as absent and fall through to a permissive default" rule `resolveTier`
 *      already applies to `tier` -- extended here to `allow_combat` specifically, even though
 *      the field's own Python-side model default is `False`: an ambiguous value is not evidence
 *      the operator chose `false`, it's evidence the policy can't be trusted for this decision.
 *   3. Policy file exists, parses, and `actions.allow_combat` is a genuine boolean -> that
 *      value is authoritative, whichever way it reads.
 *
 * Callers (`allowlist.ts`'s `checkAllowlist`) invoke this lazily -- only once a decoded
 * `launchFleetMission` mission type is actually Attack -- so a malformed or absent
 * `allow_combat` field never blocks an unrelated (non-combat) transaction. See `checkAllowlist`'s
 * own doc comment for why that laziness matters.
 */
export function resolveAllowCombat(opts: ResolveAllowCombatOptions = {}): boolean {
  const env = opts.env ?? process.env;
  const readFile = opts.readFile ?? ((p: string) => readFileSync(p, "utf8"));
  const path = policyPath(env);

  let raw: string | undefined;
  try {
    raw = readFile(path);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") {
      throw new AllowCombatResolutionError(
        `could not read policy file at "${path}": ${(err as Error).message}. Refusing rather ` +
          `than falling back to a permissive default on an unreadable security policy.`,
      );
    }
    // No policy file at all -- combat defaults to false. There is no --allow-combat flag and
    // no VEYDRIFT_ALLOW_COMBAT env var to fall back to, on purpose (see the doc comment above).
    return false;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new AllowCombatResolutionError(
      `policy file at "${path}" is not valid JSON: ${(err as Error).message}. Refusing rather ` +
        `than falling back to a permissive default on a malformed security policy.`,
    );
  }

  const actions = (parsed as { actions?: unknown } | null)?.actions;
  const allowCombatValue = (actions as { allow_combat?: unknown } | null)?.allow_combat;
  if (typeof allowCombatValue !== "boolean") {
    throw new AllowCombatResolutionError(
      `policy file at "${path}" has no valid "actions.allow_combat" field (got ` +
        `${JSON.stringify(allowCombatValue)}; must be a boolean). Refusing rather than falling ` +
        `back to a permissive default on an ambiguous security policy.`,
    );
  }
  return allowCombatValue;
}
