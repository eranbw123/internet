import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { InterestsList } from "./InterestsList";
import { MOCK_LATENCY_MS, mockClient, resetMockState } from "./mockClient";
import type { StatsResponse } from "./types";

let stats: StatsResponse;

beforeEach(async () => {
  MOCK_LATENCY_MS.value = 0;
  resetMockState();
  stats = await mockClient.interestStats();
});

function renderList(over: Partial<Parameters<typeof InterestsList>[0]> = {}) {
  const onEdit = vi.fn();
  const onRevive = vi.fn();
  const onRetire = vi.fn();
  render(
    <InterestsList
      stats={stats}
      loading={false}
      busyKey={null}
      onEdit={onEdit}
      onRevive={onRevive}
      onRetire={onRetire}
      {...over}
    />,
  );
  return { onEdit, onRevive, onRetire };
}

describe("InterestsList", () => {
  it("leads with the aggregate funnel", () => {
    renderList();
    expect(screen.getByText("1,758")).toBeInTheDocument();
    expect(screen.getByText("438")).toBeInTheDocument();
    expect(screen.getByText("612")).toBeInTheDocument();
    expect(screen.getByText(/33 of 46 interests still collecting/)).toBeInTheDocument();
  });

  it("explains why delivered exceeds above-bar instead of leaving a contradiction", () => {
    renderList();
    expect(screen.getByText(/bars in force at the time/)).toBeInTheDocument();
  });

  it("surfaces dead weight as its own filter", () => {
    renderList();
    const link = screen.getByText(/are dead weight/);
    fireEvent.click(link);
    // Every visible row is now one that collected plenty and converted almost
    // nothing -- the thing the whole redesign exists to make visible.
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(within(row).getByText("dead weight")).toBeInTheDocument();
    }
  });

  it("shows all four lifecycle states, not just active/inactive", () => {
    renderList();
    fireEvent.click(screen.getByText(/^All /));
    const seen = new Set(
      screen.getAllByRole("row").slice(1)
        .map((r) => within(r).getAllByText(/^(active|decaying|paused|retired)$/)[0]?.textContent),
    );
    expect(seen).toContain("active");
    expect(seen).toContain("decaying");
    expect(seen).toContain("paused");
    expect(seen).toContain("retired");
  });

  it("warns how close a decaying interest is to being auto-paused", () => {
    renderList();
    fireEvent.click(screen.getByText(/^All /));
    expect(screen.getAllByText(/pauses at 45/).length).toBeGreaterThan(0);
  });

  it("offers a one-click revive only where it applies", () => {
    const { onRevive } = renderList();
    fireEvent.click(screen.getByText(/^Stopped /));
    const paused = screen.getByTestId("interest-row-speculative-fiction-ideas");
    fireEvent.click(within(paused).getByText("Revive"));
    expect(onRevive).toHaveBeenCalledWith(
      expect.objectContaining({ key: "speculative-fiction-ideas" }),
    );

    // A healthy active interest is not offered a revive.
    fireEvent.click(screen.getByText(/^Collecting /));
    const healthy = screen.getByTestId("interest-row-nbis-nebius");
    expect(within(healthy).queryByText("Revive")).not.toBeInTheDocument();
  });

  it("sorts by any funnel column", () => {
    renderList();
    fireEvent.click(screen.getByText("Delivered"));
    const first = screen.getAllByRole("row")[1];
    // nbis-nebius is the best converter in the measured window.
    expect(within(first).getByText("nbis-nebius")).toBeInTheDocument();
  });

  it("filters by key or title", () => {
    renderList();
    fireEvent.change(screen.getByLabelText("Filter by key or title"), {
      target: { value: "nebius" },
    });
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + one match
  });

  it("opens the editor on a row", () => {
    const { onEdit } = renderList();
    const row = screen.getByTestId("interest-row-nbis-nebius");
    fireEvent.click(within(row).getByText("Edit"));
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ key: "nbis-nebius" }));
  });

  it("opens the editor from anywhere on the row, not just the Edit button", () => {
    // A 38x25 button in the last column was the only way in, which on a phone
    // is the hardest thing on the screen to hit.
    const { onEdit } = renderList();
    fireEvent.click(within(screen.getByTestId("interest-row-nbis-nebius")).getByText("NBIS / Nebius"));
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ key: "nbis-nebius" }));
  });

  it("opens the editor from the keyboard on a focused row", () => {
    const { onEdit } = renderList();
    fireEvent.keyDown(screen.getByTestId("interest-row-nbis-nebius"), { key: "Enter" });
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ key: "nbis-nebius" }));
  });

  it("keeps the row a table row for a screen reader", () => {
    // The click shortcut must not cost the table its structure -- a
    // role="button" on the <tr> would take every row and cell relationship
    // away from assistive tech in exchange for a convenience.
    renderList();
    expect(screen.getByTestId("interest-row-nbis-nebius").getAttribute("role")).toBeNull();
  });

  it("asks before stopping an interest, and names the one it will stop", () => {
    const { onRetire } = renderList();
    const row = screen.getByTestId("interest-row-nbis-nebius");
    fireEvent.click(within(row).getByText("Stop"));
    expect(onRetire).not.toHaveBeenCalled();
    expect(within(row).getByText(/Stop collecting/)).toBeInTheDocument();
    expect(within(row).getByText("NBIS / Nebius", { selector: "strong" })).toBeInTheDocument();
    fireEvent.click(within(row).getByText("Stop it"));
    expect(onRetire).toHaveBeenCalledWith(expect.objectContaining({ key: "nbis-nebius" }));
  });

  it("lets the confirmation be backed out of", () => {
    const { onRetire } = renderList();
    const row = screen.getByTestId("interest-row-nbis-nebius");
    fireEvent.click(within(row).getByText("Stop"));
    fireEvent.click(within(row).getByText("Cancel"));
    expect(onRetire).not.toHaveBeenCalled();
    expect(within(row).getByText("Stop")).toBeInTheDocument();
  });

  it("does not open the editor when the row's own buttons are pressed", () => {
    // The row is a click target now, so every control inside it has to stop
    // the event or the editor opens behind whatever action was pressed.
    const { onEdit, onRetire } = renderList();
    const row = screen.getByTestId("interest-row-nbis-nebius");
    fireEvent.click(within(row).getByText("Stop"));
    expect(onEdit).not.toHaveBeenCalled();
    fireEvent.click(within(row).getByText("Cancel"));
    expect(onEdit).not.toHaveBeenCalled();
    expect(onRetire).not.toHaveBeenCalled();
  });
});
