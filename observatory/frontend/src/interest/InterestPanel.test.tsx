import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { InterestDetail } from "../types";

const fetchInterest = vi.fn();
vi.mock("../api", () => ({ fetchInterest: (key: string) => fetchInterest(key) }));

const { InterestPanel } = await import("./InterestPanel");

function detail(over: Partial<InterestDetail> = {}): InterestDetail {
  return {
    definition: {
      id: 4, key: "narcolepsy-eds", title: "Narcolepsy & EDS", active: 1,
      layer: "owner", min_score: 0.8, description: "sleep research",
    },
    provenance: null,
    signals: { positive: ["orexin", "hypocretin"], negative: ["listicle"] },
    events: [],
    generations: [{ id: 1 }],
    missions: [{ id: 2, label: "search orexin", status: "done" }],
    discoveries: [{ item_id: 77, title: "A finding", final_score: 0.83 }],
    failures: [],
    feedback: [],
    ...over,
  };
}

describe("InterestPanel", () => {
  beforeEach(() => {
    fetchInterest.mockReset();
    fetchInterest.mockResolvedValue(detail());
  });

  it("shows the definition the dead endpoint was always able to serve", async () => {
    render(<InterestPanel interestKey="narcolepsy-eds" onSelectDiscovery={() => {}} onShowLatestTrace={() => {}} />);
    expect(await screen.findByText("Narcolepsy & EDS")).toBeInTheDocument();
    expect(screen.getByText("sleep research")).toBeInTheDocument();
    expect(screen.getByText("0.8")).toBeInTheDocument();
    expect(screen.getByText("orexin")).toBeInTheDocument();
  });

  it("marks a deactivated interest", async () => {
    fetchInterest.mockResolvedValue(detail({
      definition: { id: 4, key: "retired", title: "Retired", active: 0 },
    }));
    render(<InterestPanel interestKey="retired" onSelectDiscovery={() => {}} onShowLatestTrace={() => {}} />);
    expect(await screen.findByText("inactive")).toBeInTheDocument();
  });

  it("seeds the graph on a discovery rather than an arbitrary match node", async () => {
    const onSelectDiscovery = vi.fn();
    render(<InterestPanel interestKey="narcolepsy-eds" onSelectDiscovery={onSelectDiscovery} onShowLatestTrace={() => {}} />);
    fireEvent.click(await screen.findByText("A finding"));
    expect(onSelectDiscovery).toHaveBeenCalledWith(77);
  });

  it("keeps the old seed-the-graph behaviour as an explicit secondary action", async () => {
    const onShowLatestTrace = vi.fn();
    render(<InterestPanel interestKey="narcolepsy-eds" onSelectDiscovery={() => {}} onShowLatestTrace={onShowLatestTrace} />);
    fireEvent.click(await screen.findByText("Show latest trace"));
    expect(onShowLatestTrace).toHaveBeenCalled();
  });

  it("reports a failed fetch instead of rendering an empty shell", async () => {
    fetchInterest.mockRejectedValue(new Error("no such interest"));
    render(<InterestPanel interestKey="nope" onSelectDiscovery={() => {}} onShowLatestTrace={() => {}} />);
    expect(await screen.findByText("no such interest")).toBeInTheDocument();
  });

  it("omits sections with nothing in them", async () => {
    render(<InterestPanel interestKey="narcolepsy-eds" onSelectDiscovery={() => {}} onShowLatestTrace={() => {}} />);
    await screen.findByText("Recent discoveries");
    expect(screen.queryByText("Recent failures")).toBeNull();
    expect(screen.queryByText("Feedback")).toBeNull();
  });
});
