import { beforeEach, describe, expect, it } from "vitest";
import { MOCK_LATENCY_MS, mockClient, resetMockState } from "./mockClient";

/** These test the FIXTURE, which is worth doing for two reasons: the workspace
 * ships on it, and it encodes PR H's semantics, so a divergence here is an
 * early warning that the UI is assuming something the store does not do. */
beforeEach(() => {
  MOCK_LATENCY_MS.value = 0;
  resetMockState();
});

describe("the measured fixture", () => {
  it("reconciles with the numbers it claims to come from", async () => {
    const stats = await mockClient.interestStats();
    expect(stats.totals.collected).toBe(1758);
    expect(stats.totals.above_bar).toBe(438);
    expect(stats.totals.delivered).toBe(612);
    expect(stats.totals.active_interests).toBe(33);
    expect(stats.totals.total_interests).toBe(46);
  });

  it("carries the measured per-interest anchors", async () => {
    const { interests } = await mockClient.interestStats();
    const by = (k: string) => interests.find((i) => i.key === k)!;
    expect(by("nbis-nebius")).toMatchObject({ collected: 87, above_bar: 39 });
    expect(by("conversation-memory-compression")).toMatchObject({ collected: 55, above_bar: 28 });
    expect(by("speculative-fiction-ideas")).toMatchObject({ collected: 36, above_bar: 0 });
  });

  it("keeps recent_scores consistent with the above_bar column beside it", async () => {
    // The editor's bar preview counts recent_scores; if it disagreed with the
    // funnel row, the preview would quietly lie.
    const { interests } = await mockClient.interestStats();
    for (const row of interests.filter((i) => i.collected > 0 && i.collected <= 120)) {
      const clears = row.recent_scores.filter((s) => s >= row.min_score).length;
      expect(clears, row.key).toBe(row.above_bar);
    }
  });

  it("bars sit in the measured post-rebalance range", async () => {
    const { interests } = await mockClient.interestStats();
    const bars = interests.filter((i) => i.active).map((i) => i.min_score);
    expect(Math.min(...bars)).toBeGreaterThanOrEqual(0.68);
    expect(Math.max(...bars)).toBeLessThanOrEqual(0.84);
  });
});

describe("offer decisions", () => {
  it("accepting an offer creates the interest, with no invented history", async () => {
    const before = await mockClient.interestStats();
    expect(before.interests.some((i) => i.key === "handheld-and-roguelike-gaming")).toBe(false);

    const res = await mockClient.decideOffer(101, { action: "accept" });
    expect(res).toMatchObject({ ok: true, status: "accepted", interest_key: "handheld-and-roguelike-gaming" });

    const after = await mockClient.interestStats();
    const created = after.interests.find((i) => i.key === "handheld-and-roguelike-gaming")!;
    expect(created.lifecycle).toBe("active");
    expect(created.collected).toBe(0);
    expect(created.above_bar).toBe(0);
  });

  it("refuses to re-decide a decided offer, the way the store does", async () => {
    await mockClient.decideOffer(101, { action: "accept" });
    // offers.py TRANSITIONS: accepted -> frozenset(), a terminal state.
    await expect(mockClient.decideOffer(101, { action: "reject" }))
      .rejects.toThrow(/not a legal transition/);
  });

  it("drops an accepted offer out of the inbox", async () => {
    expect((await mockClient.listOffers("offered")).offers).toHaveLength(4);
    await mockClient.decideOffer(101, { action: "accept" });
    const keys = (await mockClient.listOffers("offered")).offers.map((o) => o.key);
    expect(keys).not.toContain("handheld-and-roguelike-gaming");
    expect(keys).toHaveLength(3);
  });

  it("orders the inbox strongest first, unranked last", async () => {
    const { offers } = await mockClient.listOffers("offered");
    expect(offers.map((o) => o.key)).toEqual([
      "handheld-and-roguelike-gaming",
      "cognition-in-competitive-games",
      "performance-supplements-evidence",
      "retire:speculative-fiction-ideas",
    ]);
  });

  it("reports the terms a rejection blocks for 180 days", async () => {
    const res = await mockClient.decideOffer(103, { action: "reject" });
    expect(res.status).toBe("rejected");
    expect(res.blocked_terms).toContain("performance-supplements-evidence");
    expect(res.blocked_terms!.length).toBeGreaterThan(1);
  });

  it("snoozes for PR H's 30-day default", async () => {
    const res = await mockClient.decideOffer(102, { action: "snooze" });
    expect(res.status).toBe("snoozed");
    const snoozed = (await mockClient.listOffers("snoozed")).offers
      .find((o) => o.key === "cognition-in-competitive-games")!;
    const days = (new Date(snoozed.snoozed_until!).getTime() - Date.now()) / 864e5;
    expect(days).toBeGreaterThan(28);
    expect(days).toBeLessThan(31);
  });

  it("retires the interest a retire: offer NAMES, not the offer's own key", async () => {
    // The trap: the offer key is `retire:speculative-fiction-ideas`, which is
    // not an interest. Retiring by offer key would silently do nothing.
    await mockClient.decideOffer(104, { action: "accept" });
    const { interests } = await mockClient.interestStats();
    expect(interests.find((i) => i.key === "retire:speculative-fiction-ideas")).toBeUndefined();
    const target = interests.find((i) => i.key === "speculative-fiction-ideas")!;
    expect(target.lifecycle).toBe("retired");
    expect(target.active).toBe(false);
  });
});

describe("interest writes", () => {
  it("recounts above_bar when the bar moves, from that interest's own scores", async () => {
    const before = (await mockClient.interestStats()).interests.find((i) => i.key === "nbis-nebius")!;
    expect(before.above_bar).toBe(39);

    await mockClient.updateInterest("nbis-nebius", { min_score: 0.95 });
    const after = (await mockClient.interestStats()).interests.find((i) => i.key === "nbis-nebius")!;
    expect(after.min_score).toBe(0.95);
    expect(after.above_bar).toBeLessThan(39);
    expect(after.above_bar).toBe(before.recent_scores.filter((s) => s >= 0.95).length);
  });

  it("derives `active` from lifecycle rather than tracking it separately", async () => {
    await mockClient.updateInterest("nbis-nebius", { lifecycle: "paused" });
    const row = (await mockClient.interestStats()).interests.find((i) => i.key === "nbis-nebius")!;
    expect(row.active).toBe(false);
    expect(row.lifecycle).toBe("paused");
  });

  it("cancels pending missions when an interest stops collecting", async () => {
    const res = await mockClient.updateInterest("nbis-nebius", { lifecycle: "paused" });
    expect(res.missions_cancelled).toBeGreaterThan(0);
  });

  it("refuses an illegal lifecycle move", async () => {
    // LIFECYCLE_TRANSITIONS: retired -> {active} only.
    await mockClient.updateInterest("nbis-nebius", { lifecycle: "retired" });
    await expect(mockClient.updateInterest("nbis-nebius", { lifecycle: "paused" }))
      .rejects.toThrow(/not a legal transition/);
  });

  it("revives a paused interest in one call", async () => {
    const paused = (await mockClient.interestStats()).interests
      .find((i) => i.lifecycle === "paused")!;
    await mockClient.updateInterest(paused.key, { lifecycle: "active" });
    const after = (await mockClient.interestStats()).interests.find((i) => i.key === paused.key)!;
    expect(after.lifecycle).toBe("active");
    expect(after.active).toBe(true);
  });

  it("refuses to create a key that already exists", async () => {
    await expect(mockClient.createInterest({
      key: "nbis-nebius", title: "dupe", description: "", positive_signals: ["x"],
      negative_signals: [], min_score: 0.7, sources: ["web_search"], parent_key: null,
      lifecycle: "active",
    })).rejects.toThrow(/already exists/);
  });
});

describe("interest detail", () => {
  it("serves the description and signals the editor needs", async () => {
    const d = await mockClient.interestDetail("nbis-nebius");
    expect(d.definition.key).toBe("nbis-nebius");
    expect((d.definition.description ?? "").length).toBeGreaterThan(0);
    expect(d.signals.positive.length).toBeGreaterThan(0);
  });
});

describe("edges", () => {
  it("filters by minimum weight", async () => {
    const all = await mockClient.listEdges(0);
    const strong = await mockClient.listEdges(0.7);
    expect(strong.edges.length).toBeLessThan(all.edges.length);
    expect(strong.edges.every((e) => e.weight >= 0.7)).toBe(true);
  });

  it("keeps the loose-matcher artefact visible: big shared count, modest lift", async () => {
    const { edges } = await mockClient.listEdges(0);
    const artefact = edges.find(
      (e) => e.a === "personal-knowledge-graphs" && e.b === "conversation-memory-compression",
    )!;
    expect(artefact.evidence.shared_items).toBe(578);
    expect(artefact.evidence.lift).toBeLessThan(2);
  });
});
