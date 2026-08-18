import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { OfferProvenance } from "./Provenance";
import { MOCK_EDGES, MOCK_OFFERS } from "./mockData";
import type { Offer } from "./types";

function offer(key: string): Offer {
  const found = MOCK_OFFERS.find((o) => o.key === key);
  if (!found) throw new Error(`fixture missing ${key}`);
  return found;
}

describe("OfferProvenance", () => {
  it("shows the durability line that separates a real interest from an errand", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    expect(screen.getByText(/42 conversations/)).toBeInTheDocument();
    expect(screen.getByText(/7 months/)).toBeInTheDocument();
  });

  it("shows quotes inline and names the conversations they came from", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    // Evidence is the body of the card, not a tooltip: the first quotes are
    // rendered without any interaction at all.
    expect(screen.getByText(/I keep dying on the same Isaac challenge/)).toBeInTheDocument();
    expect(screen.getByText("Isaac Best Challenge Unlocks")).toBeInTheDocument();
    expect(screen.getByText("5 quotes from 5 conversations")).toBeInTheDocument();
  });

  it("renders a Hebrew quote rtl inside the otherwise-LTR card", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    const hebrew = screen.getByText(/יש דרך להוריד את צריכת הסוללה/);
    expect(hebrew).toHaveAttribute("dir", "rtl");
    expect(hebrew).toHaveAttribute("lang", "he");
  });

  it("keeps the first three quotes and expands the rest on request", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    expect(screen.queryByText(/Hades vs Dead Cells/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Show 2 more quotes"));
    expect(screen.getByText(/Hades vs Dead Cells/)).toBeInTheDocument();
  });

  it("falls back to the conversation id when no title was stored", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    fireEvent.click(screen.getByText("Show 2 more quotes"));
    // PR H's importer persists conversation_id only; the UI must stay useful.
    expect(screen.getByText("c-7410")).toBeInTheDocument();
  });

  it("shows the ranking arithmetic, and it reconciles with the composite", () => {
    const o = offer("handheld-and-roguelike-gaming");
    render(<OfferProvenance offer={o} edges={MOCK_EDGES} />);
    expect(screen.getByText(/Why it ranks/)).toBeInTheDocument();
    // .30 x .92 = .276, the largest single contribution
    expect(screen.getByText(".276")).toBeInTheDocument();
    // The displayed total must round to the score on the card, or the
    // breakdown is decoration rather than evidence.
    expect(screen.getByText(/rounds to \.86/)).toBeInTheDocument();
    expect(o.score).toBe(0.86);
  });

  it("answers 'don't I already track this?' explicitly", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    expect(screen.getByText(/Nothing close/)).toBeInTheDocument();
    expect(screen.getByText(/complex-systems-emergent-behavior at \.08/)).toBeInTheDocument();
  });

  it("shows a near-miss as kept separate rather than hiding it", () => {
    render(<OfferProvenance offer={offer("performance-supplements-evidence")} edges={MOCK_EDGES} />);
    expect(screen.getByText("hypersomnia-offlabel-pharmacology")).toBeInTheDocument();
    expect(screen.getAllByText(/kept separate/).length).toBeGreaterThan(0);
  });

  it("reads a bridge offer's lift off the interest_edges row, not the offer", () => {
    // The offer table has no lift column; the bridge_offer edge between the two
    // parents does.
    render(<OfferProvenance offer={offer("cognition-in-competitive-games")} edges={MOCK_EDGES} />);
    expect(screen.getByText(/Bridges two interests/)).toBeInTheDocument();
    expect(screen.getByText(/Lift 3\.1 between them/)).toBeInTheDocument();
    expect(screen.getByText(/serendipity slot/)).toBeInTheDocument();
  });

  it("justifies a retirement offer from the sweep snapshot in score_terms", () => {
    // PR H stores no funnel column: _raise_retire_offer puts the numbers in
    // score_terms, and that is where the UI reads them.
    const o = offer("retire:speculative-fiction-ideas");
    render(<OfferProvenance offer={o} edges={MOCK_EDGES} />);
    const steps = screen.getByRole("list");
    // 36 collected, 36 scored, 0 above bar -- the sweep's own snapshot.
    expect(within(steps).getAllByText("36")).toHaveLength(2);
    const zero = within(steps).getByText("above bar").closest("li")!;
    expect(within(zero).getByText("0")).toBeInTheDocument();
    expect(zero.className).toContain("funnel-zero");
    expect(screen.getByText(/47 days without a single item/)).toBeInTheDocument();
    // It names the interest, not its own retire:-prefixed key.
    expect(screen.getByText("speculative-fiction-ideas")).toBeInTheDocument();
  });

  it("credits the generation run, and says so differently for a sweep", () => {
    render(<OfferProvenance offer={offer("handheld-and-roguelike-gaming")} edges={MOCK_EDGES} />);
    expect(screen.getByText(/artifact 9c41f2/)).toBeInTheDocument();
  });

  it("says a retirement offer came from the sweep, having no artifact", () => {
    render(<OfferProvenance offer={offer("retire:speculative-fiction-ideas")} edges={MOCK_EDGES} />);
    expect(screen.getByText(/raised by the decay sweep/)).toBeInTheDocument();
  });
});
