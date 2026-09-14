/**
 * Security-relevant values this engine resolves from `$VEYDRIFT_HOME/policy.json` rather
 * than from a caller-supplied flag: the enforcing **tier** (`resolveTier`, the original purpose
 * of this module) and two boolean feature flags gated the same stricter way --
 * `resolveAllowCombat` (launch-actions plan, commit 5) and `resolveAllowAlliance` (the alliance
 * feature). Same threat model for all three: `walletctl` must not trust a caller-asserted value
 * as authoritative for `checkAllowlist` -- that would let a fully compromised `veydrift-agent`
 * (which is exactly who §6.4's allowlist exists to defend against) simply assert whatever it
 * wants, regardless of what its own policy actually authorizes.
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
 * `resolveAllowCombat`/`resolveAllowAlliance` follow the same policy-file-is-authoritative shape
 * but are deliberately stricter in one respect -- see `resolveAllowCombat`'s own doc comment
 * below for exactly how and why (both share the same `resolveBooleanActionFlag` implementation;
 * `resolveAllowAlliance` doesn't repeat the rationale, only the field name and error type differ).
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isTier, TIERS, type Tier } from "./allowlist.js";

export class TierResolutionError extends Error {}

export class AllowCombatResolutionError extends Error {}

export class AllowAllianceResolutionError extends Error {}

export class AllowAcsDefenseResolutionError extends Error {}

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

export interface ResolveActionFlagOptions {
  env?: NodeJS.ProcessEnv;
  /** Injectable so tests never touch the real filesystem. Same contract as
   *  `ResolveTierOptions.readFile`: must throw an Error with `.code === "ENOENT"` when the
   *  file is absent. */
  readFile?: (path: string) => string;
}

/** Shared by `resolveAllowCombat` and `resolveAllowAlliance` below -- both are "read one boolean
 *  off `actions` in `policy.json`, refuse-on-malformed, default-false-on-absent, no CLI-flag/env
 *  fallback ever" with nothing else differing but the field name and which error type gets
 *  thrown. See `resolveAllowCombat`'s doc comment for the full rules/rationale; not repeated at
 *  each call site so a future third flag doesn't have to re-justify the same shape again. */
export type ResolveAllowCombatOptions = ResolveActionFlagOptions;
export type ResolveAllowAllianceOptions = ResolveActionFlagOptions;
export type ResolveAllowAcsDefenseOptions = ResolveActionFlagOptions;

function resolveBooleanActionFlag(
  fieldName: string,
  ErrorCtor: new (message: string) => Error,
  opts: ResolveActionFlagOptions,
): boolean {
  const env = opts.env ?? process.env;
  const readFile = opts.readFile ?? ((p: string) => readFileSync(p, "utf8"));
  const path = policyPath(env);

  let raw: string | undefined;
  try {
    raw = readFile(path);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") {
      throw new ErrorCtor(
        `could not read policy file at "${path}": ${(err as Error).message}. Refusing rather ` +
          `than falling back to a permissive default on an unreadable security policy.`,
      );
    }
    // No policy file at all -- the flag defaults to false. There is no --allow-* flag and no
    // matching env var to fall back to, on purpose (see resolveAllowCombat's doc comment).
    return false;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new ErrorCtor(
      `policy file at "${path}" is not valid JSON: ${(err as Error).message}. Refusing rather ` +
        `than falling back to a permissive default on a malformed security policy.`,
    );
  }

  const actions = (parsed as { actions?: unknown } | null)?.actions;
  const value = (actions as Record<string, unknown> | null)?.[fieldName];
  if (typeof value !== "boolean") {
    throw new ErrorCtor(
      `policy file at "${path}" has no valid "actions.${fieldName}" field (got ` +
        `${JSON.stringify(value)}; must be a boolean). Refusing rather than falling back to a ` +
        `permissive default on an ambiguous security policy.`,
    );
  }
  return value;
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
  return resolveBooleanActionFlag("allow_combat", AllowCombatResolutionError, opts);
}

/**
 * Resolve whether alliance membership actions (`createAlliance`, `inviteMember`,
 * `acceptInvite`, `leaveAlliance`, etc. -- the 15 functions in `allowlist.ts`'s
 * `ALLIANCE_SIGNATURES`) are permitted. Same shape and same threat model as
 * `resolveAllowCombat` above -- read `policy.json`'s `actions.allow_alliance`, no CLI flag or
 * env var ever, `false` on ENOENT, throw on anything malformed/ambiguous. The one substantive
 * difference from combat is not in this function at all: alliance actions are gated at
 * `economy` tier, not `operator` -- that's `allowlist.ts`'s selector-check branch's concern, not
 * this resolver's (this function only ever answers "is the flag true," never "at which tier").
 *
 * Callers (`allowlist.ts`'s `checkAllowlist`) invoke this lazily -- only once a decoded
 * transaction's selector is actually one of the 15 alliance functions -- so a malformed or
 * absent `allow_alliance` field never blocks an unrelated transaction.
 */
export function resolveAllowAlliance(opts: ResolveAllowAllianceOptions = {}): boolean {
  return resolveBooleanActionFlag("allow_alliance", AllowAllianceResolutionError, opts);
}

/**
 * Resolve whether ACS defense coordination actions (AcsDefend/Intercept mission types on
 * `launchFleetMission`, `launchDefenseHold`, and `openDefenseIntent` -- `allowlist.ts`'s
 * `ACS_MISSION_TYPES`/`DEFENSE_HOLD_SIGNATURES`/`ACS_ALLIANCE_SIGNATURES`) are permitted.
 * Same shape and same threat model as `resolveAllowCombat`/`resolveAllowAlliance` above --
 * read `policy.json`'s `actions.allow_acs_defense`, no CLI flag or env var ever, `false`
 * on ENOENT, throw on anything malformed/ambiguous. The tier floor these functions need
 * (operator for AcsDefend/Intercept/DefenseHold, economy-or-above for openDefenseIntent)
 * is `allowlist.ts`'s selector-check branches' concern, not this resolver's -- this
 * function only ever answers "is the flag true," never "at which tier."
 *
 * Callers invoke this lazily -- only once a decoded transaction's selector/mission-type
 * is actually one of these four -- so a malformed or absent `allow_acs_defense` field
 * never blocks an unrelated transaction.
 */
export function resolveAllowAcsDefense(opts: ResolveAllowAcsDefenseOptions = {}): boolean {
  return resolveBooleanActionFlag("allow_acs_defense", AllowAcsDefenseResolutionError, opts);
}

export class WalletBindingResolutionError extends Error {}

export interface ResolveExpectedWalletOptions {
  env?: NodeJS.ProcessEnv;
  /** Injectable so tests never touch the real filesystem. Same contract as
   *  `ResolveTierOptions.readFile`. */
  readFile?: (path: string) => string;
}

/**
 * Resolve the address `send` must sign as: `policy.json`'s top-level `wallet` -- the same
 * account `veydrift-agent` reads state for and simulates as. `sendTx` refuses when the
 * provider's key derives a different address, so a stray keystore or env key for another
 * account can never sign a transaction that was planned and simulated for this one.
 *
 * Rules (same shape as `resolveTier`):
 *   1. Policy file does not exist (ENOENT) -> `null`: standalone use, no binding to check.
 *   2. Policy file unreadable/unparseable, or `wallet` missing/not a 20-byte hex address ->
 *      refuse (throw). A malformed policy is never treated as absent.
 *   3. Otherwise -> that address (as written; comparison is case-insensitive).
 *
 * No CLI flag or env var can override it.
 */
export function resolveExpectedWallet(opts: ResolveExpectedWalletOptions = {}): `0x${string}` | null {
  const env = opts.env ?? process.env;
  const readFile = opts.readFile ?? ((p: string) => readFileSync(p, "utf8"));
  const path = policyPath(env);

  let raw: string;
  try {
    raw = readFile(path);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw new WalletBindingResolutionError(
      `could not read policy file at "${path}": ${(err as Error).message}. Refusing rather than ` +
        `skipping the signer-address check on an unreadable security policy.`,
    );
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new WalletBindingResolutionError(
      `policy file at "${path}" is not valid JSON: ${(err as Error).message}. Refusing rather than ` +
        `skipping the signer-address check on a malformed security policy.`,
    );
  }

  const wallet = (parsed as { wallet?: unknown } | null)?.wallet;
  if (typeof wallet !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(wallet)) {
    throw new WalletBindingResolutionError(
      `policy file at "${path}" has no valid "wallet" field (got ${JSON.stringify(wallet)}; must be ` +
        `a 0x-prefixed 20-byte address). Refusing rather than skipping the signer-address check.`,
    );
  }
  return wallet as `0x${string}`;
}
