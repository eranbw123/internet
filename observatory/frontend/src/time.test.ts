/** The timezone contract.
 *
 * The test that matters most here is the DST pair. A hardcoded "+3" passes
 * every summer date and is silently an hour wrong from late October, which is
 * exactly the mistake this module exists to prevent -- and exactly the kind of
 * bug that ships because it was written in August.
 */
import { describe, expect, it } from "vitest";
import { TIME_ZONE, exactTitle, formatClock, formatDay, formatInstant } from "./time";

describe("time", () => {
  it("names an IANA zone rather than an offset", () => {
    expect(TIME_ZONE).toBe("Asia/Jerusalem");
  });

  it("renders a stored UTC instant in Israel summer time (IDT, +3)", () => {
    // 2026-08-18T14:23:10Z is 17:23:10 in Jerusalem.
    expect(formatClock("2026-08-18T14:23:10+00:00")).toBe("17:23:10");
    expect(formatInstant("2026-08-18T14:23:10+00:00")).toContain("17:23:10");
    expect(formatInstant("2026-08-18T14:23:10+00:00")).toContain("Aug");
    expect(formatInstant("2026-08-18T14:23:10+00:00")).toContain("2026");
  });

  it("renders the SAME UTC clock time an hour earlier in winter (IST, +2)", () => {
    // The whole point: identical UTC wall-clock, different local answer. A
    // fixed +3 would return 17:23:10 here too and be an hour wrong for roughly
    // five months of every year.
    expect(formatClock("2026-01-18T14:23:10+00:00")).toBe("16:23:10");
    expect(formatClock("2026-08-18T14:23:10+00:00")).toBe("17:23:10");
  });

  it("crosses midnight into the next local day", () => {
    // 22:30 UTC in summer is 01:30 the FOLLOWING day in Jerusalem, so the date
    // has to move with the clock rather than being taken from the raw string.
    expect(formatInstant("2026-08-18T22:30:00+00:00")).toContain("19 Aug");
    expect(formatClock("2026-08-18T22:30:00+00:00")).toBe("01:30:00");
  });

  it("accepts both offset spellings the database actually stores", () => {
    // candidate_items/scores write "+00:00"; interest_offers.generated_at
    // writes "Z". Both are UTC and must render identically.
    expect(formatClock("2026-08-18T13:17:59Z"))
      .toBe(formatClock("2026-08-18T13:17:59+00:00"));
  });

  it("treats a naive timestamp as the engine's UTC, not as local time", () => {
    // No column measured in production is naive, but a runtime that read one
    // as local time would be silently hours off -- worse than displaying UTC
    // honestly, and impossible to notice by looking.
    expect(formatClock("2026-08-18T14:23:10")).toBe(formatClock("2026-08-18T14:23:10Z"));
  });

  it("keeps a date-only value on its own day", () => {
    // Evidence quotes carry a day, not an instant. Shifting one by a zone is
    // how a quote from the 4th starts displaying as the 3rd.
    expect(formatDay("2026-08-04")).toBe("4 Aug 2026");
    expect(formatDay("2026-01-01")).toBe("1 Jan 2026");
  });

  it("formats a full timestamp as a day when asked for one", () => {
    expect(formatDay("2026-08-18T22:30:00+00:00")).toBe("19 Aug 2026");
  });

  it("keeps the exact instant available for a title attribute", () => {
    const title = exactTitle("2026-08-18T14:23:10+00:00");
    expect(title).toContain("2026-08-18T14:23:10.000Z");
    expect(title).toContain("Asia/Jerusalem");
  });

  it("passes unparseable or empty input through instead of printing garbage", () => {
    expect(formatInstant("")).toBe("");
    expect(formatInstant(null)).toBe("");
    expect(formatInstant(undefined)).toBe("");
    expect(formatInstant("not a date")).toBe("not a date");
    expect(formatClock("never")).toBe("never");
    expect(exactTitle("nope")).toBeUndefined();
  });
});
