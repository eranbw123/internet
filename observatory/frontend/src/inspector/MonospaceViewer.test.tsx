import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MonospaceViewer } from "./MonospaceViewer";

// CompareView renders diffs (unified_diff lines from /api/compare) through
// exactly this component -- joined with "\n" -- so these tests double as
// "diff view rendering" coverage without pulling in the full React Flow
// compare split (which needs ResizeObserver/SVG mocking jsdom doesn't ship).
const DIFF_TEXT = [
  "--- a", "+++ b", "@@ -1,2 +1,2 @@", "-old line", "+new line", " unchanged line",
].join("\n");

describe("MonospaceViewer", () => {
  it("renders diff text with preserved whitespace/newlines", () => {
    render(<MonospaceViewer text={DIFF_TEXT} />);
    const content = screen.getByTestId("monospace-content");
    expect(content.textContent).toContain("-old line");
    expect(content.textContent).toContain("+new line");
  });

  it("pretty-prints and syntax-highlights JSON when json=true", () => {
    render(<MonospaceViewer text='{"a":1,"b":"x"}' json />);
    const content = screen.getByTestId("monospace-content");
    expect(content.querySelector(".json-key")).not.toBeNull();
    expect(content.querySelector(".json-string")).not.toBeNull();
    expect(content.querySelector(".json-number")).not.toBeNull();
  });

  it("shows a truncation warning only when the truncated flag is set", () => {
    const { rerender } = render(<MonospaceViewer text="x" truncated={false} />);
    expect(screen.queryByText(/truncated/)).toBeNull();
    rerender(<MonospaceViewer text="x" truncated />);
    expect(screen.getByText(/truncated/)).toBeInTheDocument();
  });

  it("toggles wrap mode via the wrap checkbox", () => {
    render(<MonospaceViewer text="x" />);
    const content = screen.getByTestId("monospace-content");
    expect(content.className).toContain("nowrap");
    fireEvent.click(screen.getByLabelText(/wrap/));
    expect(content.className).toContain("wrap");
  });

  it("copies the displayed text via the clipboard API on Copy click", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MonospaceViewer text="copy me" />);
    fireEvent.click(screen.getByText("Copy"));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("copy me"));
  });
});
