import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { InterestEditor } from "./InterestEditor";
import { MOCK_LATENCY_MS, mockClient, resetMockState } from "./mockClient";
import type { InterestDetailResponse, InterestStat } from "./types";

let row: InterestStat;
let detail: InterestDetailResponse;

beforeEach(async () => {
  MOCK_LATENCY_MS.value = 0;
  resetMockState();
  const stats = await mockClient.interestStats();
  row = stats.interests.find((i) => i.key === "nbis-nebius")!;
  detail = await mockClient.interestDetail("nbis-nebius");
});

function renderEditor(over: Partial<Parameters<typeof InterestEditor>[0]> = {}) {
  const onSave = vi.fn();
  const onCancel = vi.fn();
  render(
    <InterestEditor
      subject={{ mode: "edit", interest: row, detail }}
      recentScores={row.recent_scores}
      existingKeys={["nbis-nebius"]}
      parentOptions={[{ key: "nbis-nebius", title: "NBIS" }]}
      saving={false}
      error={null}
      onSave={onSave}
      onCancel={onCancel}
      {...over}
    />,
  );
  return { onSave, onCancel };
}

describe("InterestEditor", () => {
  it("opens already populated, so a save cannot silently wipe the signals", () => {
    // The bulk stats payload carries no description or signals; they come from
    // the detail endpoint before the editor opens.
    renderEditor();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe(detail.definition.title);
    expect(screen.getByText(detail.signals.positive[0])).toBeInTheDocument();
    expect(screen.getByText(detail.signals.negative[0])).toBeInTheDocument();
  });

  it("previews the bar against the interest's own recent scores", () => {
    renderEditor();
    // 87 collected, 39 above bar at 0.68 -- the same numbers the list shows.
    expect(screen.getByText(/of the last 87 scored items would clear/)).toBeInTheDocument();
    expect(screen.getByText("39")).toBeInTheDocument();
  });

  it("moves the preview as the bar moves, with no round-trip", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Bar value"), { target: { value: "0.95" } });
    const expected = row.recent_scores.filter((s) => s >= 0.95).length;
    expect(expected).toBeLessThan(39);
    expect(screen.getByText(String(expected))).toBeInTheDocument();
  });

  it("reports the legacy-scale coercion rather than silently changing the input", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Bar value"), { target: { value: "75" } });
    expect(screen.getByText(/legacy 0-100 scale and stored it as 0\.75/)).toBeInTheDocument();
  });

  it("blocks saving while a field is invalid", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "" } });
    expect(screen.getByText("Save")).toBeDisabled();
    expect(screen.getByText("A title is required.")).toBeInTheDocument();
  });

  it("saves the coerced bar, not the raw text", () => {
    const { onSave } = renderEditor();
    fireEvent.change(screen.getByLabelText("Bar value"), { target: { value: "75" } });
    fireEvent.click(screen.getByText("Save"));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ min_score: 0.75 }));
  });

  it("adds and removes signals", () => {
    const { onSave } = renderEditor();
    const input = screen.getByLabelText("Positive signals");
    fireEvent.change(input, { target: { value: "neocloud capex" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("neocloud capex")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Remove neocloud capex"));
    expect(screen.queryByText("neocloud capex")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Save"));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ positive_signals: detail.signals.positive }),
    );
  });

  it("hides the key field when editing, since a key is permanent", () => {
    renderEditor();
    expect(screen.queryByLabelText(/^Key/)).not.toBeInTheDocument();
  });

  it("validates a new key against the reserved prefix and existing keys", () => {
    renderEditor({ subject: { mode: "create" }, recentScores: [] });
    const key = screen.getByLabelText(/^Key/);
    fireEvent.change(key, { target: { value: "derived:steam" } });
    expect(screen.getByText(/reserved/)).toBeInTheDocument();
    fireEvent.change(key, { target: { value: "nbis-nebius" } });
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
  });

  it("says there is nothing to preview for an interest with no scores", () => {
    renderEditor({ subject: { mode: "create" }, recentScores: [] });
    expect(screen.getByText(/nothing to preview against/)).toBeInTheDocument();
  });

  it("words itself as an acceptance when editing an offer", async () => {
    const offers = await mockClient.listOffers("offered");
    renderEditor({
      subject: { mode: "offer", offer: offers.offers[0] },
      recentScores: [],
    });
    expect(screen.getByText("Accept with these edits")).toBeInTheDocument();
  });
});
