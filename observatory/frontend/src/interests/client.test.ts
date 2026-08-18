/** The one piece of real logic in the network seam: translating PR J's funnel
 * payload into the shape the components were written against.
 *
 * Worth testing on its own because both of its jobs are silent when they go
 * wrong. A missed rename shows up as `undefined` in a table cell, and a
 * sparkline built straight from the server's sparse list is not empty or
 * broken -- it is subtly, plausibly wrong, with idle stretches compressed out
 * so a dying interest draws the same shape as a healthy one.
 */
import { describe, expect, it } from "vitest";
import { adaptStats } from "./client";

const GENERATED_AT = "2026-08-18T12:00:00Z";

function wire(overrides: Record<string, unknown> = {}) {
  return {
    window: "7d",
    window_days: 7,
    generated_at: GENERATED_AT,
    totals: {
      interests: 33, active: 31, collected: 900, matched: 22564,
      scored: 2120, above_bar: 140, notified: 96, dead_weight: 4,
      auto_paused: 1,
    },
    interests: [
      {
        key: "narcolepsy-eds",
        title: "Narcolepsy",
        lifecycle: "active",
        active: true,
        min_score: 0.72,
        collected: 12,
        matched: 340,
        scored: 30,
        above_bar: 8,
        notified: 5,
        sparkline: [
          { date: "2026-08-13", above_bar: 2 },
          { date: "2026-08-17", above_bar: 3 },
        ],
        recent_scores: [0.91, 0.5],
        dead_weight: false,
      },
    ],
    ...overrides,
  } as never;
}

describe("adaptStats", () => {
  it("renames the server's funnel vocabulary to the UI's", () => {
    const row = adaptStats(wire()).interests[0];
    expect(row.delivered).toBe(5);
    expect(row.matched).toBe(340);
    // Everything not renamed passes through untouched.
    expect(row.key).toBe("narcolepsy-eds");
    expect(row.above_bar).toBe(8);
    expect(row.recent_scores).toEqual([0.91, 0.5]);
    expect(row.lifecycle).toBe("active");
  });

  it("maps the totals block, including the two counts that change name", () => {
    const t = adaptStats(wire()).totals;
    expect(t.delivered).toBe(96);          // totals.notified
    expect(t.total_interests).toBe(33);    // totals.interests
    expect(t.active_interests).toBe(31);   // totals.active
    expect(t.matched).toBe(22564);
    expect(t.dead_weight).toBe(4);
  });

  it("fills the sparkline's missing days back in rather than compressing them", () => {
    // The server ships only days that have a count. Drawing that list directly
    // would put 13 Aug's 2 immediately beside 17 Aug's 3 and hide the four
    // silent days between them -- which is exactly the signal the sparkline
    // exists to show. The seven buckets are 12-18 Aug: the window ends at
    // `to`, so today is the last bucket rather than falling off the end.
    const daily = adaptStats(wire()).interests[0].daily_above_bar;
    expect(daily).toHaveLength(7);
    expect(daily.reduce((a, b) => a + b, 0)).toBe(5);
    expect(daily).toEqual([0, 2, 0, 0, 0, 3, 0]);
  });

  it("derives the window bounds the header prints", () => {
    const stats = adaptStats(wire());
    expect(stats.to).toBe("2026-08-18");
    expect(stats.from).toBe("2026-08-11");
    expect(stats.window).toBe("7d");
  });

  it("opens an unbounded window at the oldest day there is data for", () => {
    // `all` has no fixed start. Falling back to a constant would print a
    // "from" date that predates the engine.
    const stats = adaptStats(wire({ window: "all", window_days: null }));
    expect(stats.from).toBe("2026-08-13");
    expect(stats.to).toBe("2026-08-18");
  });

  it("survives an interest that has never produced anything", () => {
    const raw = wire();
    (raw as never as { interests: Record<string, unknown>[] }).interests[0].sparkline = [];
    const row = adaptStats(raw).interests[0];
    expect(row.daily_above_bar).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });
});
