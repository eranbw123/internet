import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { OffersInbox } from "./OffersInbox";
import { MOCK_EDGES, MOCK_OFFERS } from "./mockData";
import type { Offer } from "./types";

function offer(key: string): Offer {
  return MOCK_OFFERS.find((o) => o.key === key)!;
}

function renderInbox(offers: Offer[], over: Partial<Parameters<typeof OffersInbox>[0]> = {}) {
  const onDecide = vi.fn();
  const onEdit = vi.fn();
  render(
    <OffersInbox
      offers={offers}
      edges={MOCK_EDGES}
      busyId={null}
      errors={{}}
      loading={false}
      onDecide={onDecide}
      onEdit={onEdit}
      {...over}
    />,
  );
  return { onDecide, onEdit };
}

describe("OffersInbox", () => {
  it("offers the four decisions on a live offer", () => {
    renderInbox([offer("handheld-and-roguelike-gaming")]);
    expect(screen.getByText("Accept")).toBeInTheDocument();
    expect(screen.getByText("Edit and accept")).toBeInTheDocument();
    expect(screen.getByText("Snooze 30d")).toBeInTheDocument();
    expect(screen.getByText("Reject")).toBeInTheDocument();
  });

  it("never offers a decision on an already-decided offer", () => {
    // `accepted` is terminal in PR H's TRANSITIONS: a button here would be a
    // guaranteed 409.
    renderInbox([{ ...offer("handheld-and-roguelike-gaming"), status: "accepted" }]);
    expect(screen.queryByText("Accept")).not.toBeInTheDocument();
    expect(screen.queryByText("Reject")).not.toBeInTheDocument();
    expect(screen.getByText("This decision is final.")).toBeInTheDocument();
  });

  it("still allows a decision on a snoozed offer", () => {
    renderInbox([{ ...offer("handheld-and-roguelike-gaming"), status: "snoozed", snoozed_until: "2026-09-16" }]);
    expect(screen.getByText("Accept")).toBeInTheDocument();
    // Rendered in the owner's calendar, like every other date in the UI.
    // ("Sept" is en-GB's abbreviation for September; the other months are
    // three letters.)
    expect(screen.getByText(/16 Sept 2026/)).toBeInTheDocument();
  });

  it("asks before rejecting, and says what rejection blocks", () => {
    const { onDecide } = renderInbox([offer("handheld-and-roguelike-gaming")]);
    fireEvent.click(screen.getByText("Reject"));
    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.getByText(/blocks/)).toBeInTheDocument();
    expect(screen.getByText(/180 days/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Reject and block"));
    expect(onDecide).toHaveBeenCalledWith(expect.objectContaining({ action: "reject" }));
  });

  it("lets the owner back out of a rejection", () => {
    const { onDecide } = renderInbox([offer("handheld-and-roguelike-gaming")]);
    fireEvent.click(screen.getByText("Reject"));
    fireEvent.click(screen.getByText("Cancel"));
    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.getByText("Accept")).toBeInTheDocument();
  });

  it("gives a retirement offer its own affordance, not accept/reject", () => {
    const { onDecide } = renderInbox([offer("retire:speculative-fiction-ideas")]);
    expect(screen.queryByText("Accept")).not.toBeInTheDocument();
    expect(screen.getByText("Retire it")).toBeInTheDocument();
    expect(screen.getByText("Keep watching")).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Lower bar to/));
    expect(onDecide).toHaveBeenCalledWith(
      expect.objectContaining({ action: "lower-bar", minScore: 0.78 }),
    );
  });

  it("heads a retirement offer with the interest it names", () => {
    renderInbox([offer("retire:speculative-fiction-ideas")]);
    const card = screen.getByTestId("offer-retire:speculative-fiction-ideas");
    // The header chip carries the INTEREST key, never the retire:-prefixed
    // offer key -- the owner is deciding about the interest, not about a row.
    const header = card.querySelector(".offer-head .key-chip")!;
    expect(header).toHaveTextContent("speculative-fiction-ideas");
    expect(header.textContent).not.toContain("retire:");
  });

  it("marks the run's serendipity pick", () => {
    renderInbox([offer("cognition-in-competitive-games")]);
    expect(screen.getByText("serendipity")).toBeInTheDocument();
  });

  it("surfaces a per-offer error instead of failing silently", () => {
    renderInbox([offer("handheld-and-roguelike-gaming")], {
      errors: { 101: "offer 'x': accepted -> rejected is not a legal transition" },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/not a legal transition/);
  });

  it("disables the buttons on the offer currently being written", () => {
    renderInbox([offer("handheld-and-roguelike-gaming")], { busyId: 101 });
    expect(screen.getByText("Accept")).toBeDisabled();
  });

  it("explains an empty inbox rather than looking broken", () => {
    // An empty inbox has to say what WOULD appear here. "Nothing to decide."
    // was accurate and useless: the owner read it, found no other mention of
    // suggesting interests anywhere in the UI, and concluded the Observatory
    // could not do it at all.
    renderInbox([]);
    expect(screen.getByText("No suggestions right now.")).toBeInTheDocument();
    expect(screen.getByText(/proposes new interests/)).toBeInTheDocument();
    expect(screen.getByText(/accept, reject or snooze/)).toBeInTheDocument();
    expect(screen.getByText(/at most five per run/)).toBeInTheDocument();
  });

  it("distinguishes a proposal to drop an interest from one to add", () => {
    // Opposite actions must not look alike. They previously differed only by a
    // small "retire?" chip in the corner of an otherwise identical card.
    renderInbox([{ ...MOCK_OFFERS[0], kind: "new" }]);
    expect(screen.getByText(/Proposing a NEW interest/)).toBeInTheDocument();
    cleanup();
    renderInbox([{
      ...MOCK_OFFERS[0], kind: "retire", key: "retire:some-interest",
      related_keys: ["some-interest"],
    }]);
    expect(screen.getByText(/Proposing to STOP an interest/)).toBeInTheDocument();
  });
});
