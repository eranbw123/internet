import { describe, expect, it } from "vitest";
import { RESERVED_KEY_PREFIX, barPreview, coerceThreshold, validateInterest } from "./validation";
import type { InterestPayload } from "./types";

function payload(over: Partial<InterestPayload> & { min_score_raw?: string } = {}) {
  return {
    key: "handheld-gaming",
    title: "Handheld gaming",
    description: "",
    positive_signals: ["steam deck"],
    negative_signals: [],
    min_score: 0.72,
    sources: ["web_search"],
    parent_key: null,
    lifecycle: "active" as const,
    ...over,
  };
}

describe("validateInterest", () => {
  it("accepts a well-formed interest", () => {
    expect(validateInterest(payload()).ok).toBe(true);
  });

  it("refuses the reserved derived-key prefix", () => {
    // db.py's DERIVED_KEY_PREFIX is "derived:", not the "drv:" the design
    // document writes -- the code is the authority here.
    const r = validateInterest(payload({ key: `${RESERVED_KEY_PREFIX}steam` }));
    expect(r.ok).toBe(false);
    expect(r.errors.key).toMatch(/reserved/);
  });

  it("refuses a key that is not a slug", () => {
    expect(validateInterest(payload({ key: "Handheld Gaming" })).errors.key).toBeTruthy();
    expect(validateInterest(payload({ key: "trailing-" })).errors.key).toBeTruthy();
  });

  it("refuses a duplicate key only when creating", () => {
    const opts = { existingKeys: ["handheld-gaming"] };
    expect(validateInterest(payload(), { ...opts, isNew: true }).errors.key).toBeTruthy();
    expect(validateInterest(payload(), opts).errors.key).toBeUndefined();
  });

  it("coerces the legacy 0-100 bar scale instead of rejecting it", () => {
    // interests.py::_threshold treats anything above 1 as the old scale: a
    // stray 75 must not silently mean "never notify".
    expect(coerceThreshold(75)).toBeCloseTo(0.75);
    expect(coerceThreshold(0.75)).toBeCloseTo(0.75);
    const r = validateInterest(payload({ min_score_raw: "75" }));
    expect(r.ok).toBe(true);
    expect(r.warnings.join(" ")).toMatch(/legacy 0-100 scale/);
  });

  it("rejects a bar that is not a number or is out of range", () => {
    expect(validateInterest(payload({ min_score_raw: "abc" })).errors.min_score).toBeTruthy();
    expect(validateInterest(payload({ min_score_raw: "-1" })).errors.min_score).toBeTruthy();
    expect(validateInterest(payload({ min_score_raw: "101" })).errors.min_score).toBeTruthy();
  });

  it("requires at least one source", () => {
    expect(validateInterest(payload({ sources: [] })).errors.sources).toBeTruthy();
  });

  it("requires positive signals only while the interest still collects", () => {
    expect(validateInterest(payload({ positive_signals: [] })).errors.positive_signals).toBeTruthy();
    expect(
      validateInterest(payload({ positive_signals: [], lifecycle: "paused" })).errors.positive_signals,
    ).toBeUndefined();
  });

  it("refuses an interest that is its own parent", () => {
    expect(validateInterest(payload({ parent_key: "handheld-gaming" })).errors.parent_key).toBeTruthy();
  });
});

describe("barPreview", () => {
  it("counts what would clear a candidate bar", () => {
    const scores = [0.9, 0.81, 0.8, 0.79, 0.4];
    expect(barPreview(scores, 0.8)).toEqual({ clears: 3, of: 5 });
    expect(barPreview(scores, 0.79)).toEqual({ clears: 4, of: 5 });
    expect(barPreview(scores, 0.95)).toEqual({ clears: 0, of: 5 });
  });

  it("survives an interest that has never been scored", () => {
    expect(barPreview([], 0.7)).toEqual({ clears: 0, of: 0 });
  });
});
