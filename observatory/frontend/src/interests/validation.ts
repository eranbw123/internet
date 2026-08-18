/** Editor validation, mirroring what the server actually enforces.
 *
 * The rules come from `discovery/interests.py` (`_to_interest`, `_threshold`)
 * and `discovery/db.py`, not from the design prose -- where the two disagree,
 * the code wins. Two places they disagree, both handled here:
 *
 *   - the reserved prefix is `derived:` (db.py DERIVED_KEY_PREFIX), not the
 *     `drv:` the design document writes;
 *   - `_threshold` does not REJECT an out-of-range bar, it COERCES it: any
 *     value above 1 is treated as the old 0-100 scale and divided by 100,
 *     because a hand-edited `75` silently meaning "never notify" was a real
 *     bug class. The editor mirrors the coercion and tells the owner it
 *     happened, rather than either rejecting valid legacy input or silently
 *     changing what they typed.
 *
 * Validation here is a courtesy, never the boundary: the write API validates
 * again server-side, and this module exists so the owner learns about a
 * problem while typing instead of after a failed save.
 */
import type { InterestPayload, Lifecycle } from "./types";
import { isCollecting } from "./types";

/** db.py DERIVED_KEY_PREFIX -- reserved for interests the ladder derives, so
 * an owner-authored key may never claim one. */
export const RESERVED_KEY_PREFIX = "derived:";

/** Collectors that exist (discovery/collectors). */
export const KNOWN_SOURCES = ["web_search", "youtube", "stocks"] as const;

/** interests.json keys are slugs: they end up in URLs, deep links, Telegram
 * messages and CLI arguments, so they stay lowercase ASCII. Titles carry the
 * human text (and may be any script); keys do not. */
const KEY_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export interface FieldErrors {
  key?: string;
  title?: string;
  min_score?: string;
  sources?: string;
  positive_signals?: string;
  parent_key?: string;
}

export interface ValidationResult {
  errors: FieldErrors;
  /** Non-blocking notices -- the coercion notice lives here. */
  warnings: string[];
  ok: boolean;
}

/** `interests.py::_threshold`. Returns the value the server would store. */
export function coerceThreshold(raw: number): number {
  return raw > 1 ? raw / 100 : raw;
}

export interface ValidateOptions {
  /** Existing keys, so a create cannot collide. Omit when editing in place. */
  existingKeys?: string[];
  /** True when creating; a key may not be changed after creation. */
  isNew?: boolean;
}

export function validateInterest(
  payload: Partial<InterestPayload> & { min_score_raw?: string },
  opts: ValidateOptions = {},
): ValidationResult {
  const errors: FieldErrors = {};
  const warnings: string[] = [];

  const key = (payload.key ?? "").trim();
  if (!key) {
    errors.key = "A key is required.";
  } else if (key.startsWith(RESERVED_KEY_PREFIX)) {
    errors.key = `'${RESERVED_KEY_PREFIX}' is reserved for derived interests.`;
  } else if (!KEY_RE.test(key)) {
    errors.key = "Lowercase letters, digits and single hyphens only (e.g. handheld-gaming).";
  } else if (opts.isNew && opts.existingKeys?.includes(key)) {
    errors.key = `An interest with the key '${key}' already exists.`;
  }

  if (!(payload.title ?? "").trim()) {
    errors.title = "A title is required.";
  }

  // The bar: parse the raw string when the editor supplies one, so "0.72" and
  // "72" are both understood and the coercion can be reported.
  const rawText = payload.min_score_raw;
  const parsed = rawText !== undefined ? Number(rawText) : payload.min_score;
  if (parsed === undefined || parsed === null || Number.isNaN(parsed)) {
    errors.min_score = "The bar must be a number.";
  } else if (parsed < 0) {
    errors.min_score = "The bar cannot be negative.";
  } else if (parsed > 100) {
    errors.min_score = "The bar must be 0-1 (or 0-100 on the legacy scale).";
  } else {
    const coerced = coerceThreshold(parsed);
    if (parsed > 1) {
      warnings.push(
        `Read ${parsed} as the legacy 0-100 scale and stored it as ${coerced.toFixed(2)}.`,
      );
    }
    if (coerced > 0.98) {
      warnings.push("A bar this high will almost never deliver anything.");
    }
  }

  const sources = payload.sources ?? [];
  if (sources.length === 0) {
    errors.sources = "Pick at least one source.";
  } else {
    const unknown = sources.filter((s) => !KNOWN_SOURCES.includes(s as typeof KNOWN_SOURCES[number]));
    if (unknown.length > 0) {
      warnings.push(`No collector exists for: ${unknown.join(", ")}.`);
    }
  }

  // An interest that is still collecting needs something to match on. A
  // paused or retired one does not -- it is not being matched against.
  const lifecycle = (payload.lifecycle ?? "active") as Lifecycle;
  if (isCollecting(lifecycle) && (payload.positive_signals ?? []).length === 0) {
    errors.positive_signals = "An active interest needs at least one positive signal.";
  }

  if (payload.parent_key && payload.parent_key === key) {
    errors.parent_key = "An interest cannot be its own parent.";
  }

  return { errors, warnings, ok: Object.keys(errors).length === 0 };
}

/** "at this bar, N of the last M scored items would clear" -- the editor's
 * live preview, and the single most useful number when tuning a bar (it is how
 * the 2026-08-13 rebalance was done by hand).
 *
 * Counted client-side from `InterestStat.recent_scores` so it updates as the
 * slider moves, with no round-trip per keystroke. */
export function barPreview(recentScores: number[], bar: number): { clears: number; of: number } {
  return {
    clears: recentScores.filter((s) => s >= bar).length,
    of: recentScores.length,
  };
}
