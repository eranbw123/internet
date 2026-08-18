import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { Tab } from "../types";

const listRows = vi.fn();
const fetchInterests = vi.fn();
vi.mock("../api", () => ({
  listRows: (...args: unknown[]) => listRows(...args),
  fetchInterests: () => fetchInterests(),
}));

const { Explorer, rowKey } = await import("./Explorer");

const INTERESTS = [
  { key: "narcolepsy-eds", title: "Narcolepsy & EDS", active: 1, layer: "owner" },
  { key: "retired-topic", title: "Retired Topic", active: 0, layer: "owner" },
];

function listResponse(rows: Record<string, unknown>[], tab: Tab = "discoveries") {
  return { tab, limit: 50, offset: 0, total: rows.length, rows };
}

describe("Explorer", () => {
  beforeEach(() => {
    listRows.mockReset();
    fetchInterests.mockReset();
    listRows.mockResolvedValue(listResponse([]));
    fetchInterests.mockResolvedValue({ interests: INTERESTS });
  });

  it("offers interests as a picker rather than an exact-key text box", async () => {
    // The backend match is `it.key = ?` -- exact and case-sensitive -- and the
    // UI never listed the valid keys, so any typed value silently returned
    // "No rows match", which is indistinguishable from broken.
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    const select = await screen.findByLabelText("Interest");
    expect(select.tagName).toBe("SELECT");
    expect(await screen.findByRole("option", { name: "Narcolepsy & EDS" })).toBeInTheDocument();
  });

  it("marks deactivated interests in the picker", async () => {
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    expect(await screen.findByRole("option", { name: /Retired Topic \(inactive\)/ })).toBeInTheDocument();
  });

  it("drops the interest picker on the Extractor runs tab", async () => {
    // A run belongs to no interest -- it is one artifact covering all of them
    // -- so the picker would filter on a column the rows do not have. The
    // artifact digest is the only handle, and the search box already takes it.
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Extractor runs/ }));
    expect(screen.queryByLabelText("Interest")).toBeNull();
    await waitFor(() => {
      expect(listRows).toHaveBeenCalledWith("extractor", expect.anything());
    });
  });

  it("keeps filter state per tab instead of leaking it across tabs", async () => {
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    const select = await screen.findByLabelText("Interest");
    fireEvent.change(select, { target: { value: "narcolepsy-eds" } });
    await waitFor(() => {
      expect(listRows).toHaveBeenCalledWith("discoveries", expect.objectContaining({
        filters: expect.objectContaining({ interest: "narcolepsy-eds" }),
      }));
    });

    listRows.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: /Missions/ }));
    await waitFor(() => {
      const calls = listRows.mock.calls;
      const call = calls[calls.length - 1];
      expect(call?.[0]).toBe("missions");
      expect(call?.[1].filters.interest).toBeUndefined();
      // The discoveries-only trace_complete default must not ride along either.
      expect(call?.[1].filters.trace_complete).toBeUndefined();
    });
  });

  it("shows active filters as chips that can be dismissed", async () => {
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    // The trace_complete default was previously applied invisibly.
    const chip = await screen.findByTitle("Remove the trace_complete filter");
    expect(chip).toBeInTheDocument();
    listRows.mockClear();
    fireEvent.click(chip);
    await waitFor(() => {
      const calls = listRows.mock.calls;
      expect(calls[calls.length - 1][1].filters.trace_complete).toBeUndefined();
    });
  });

  it("debounces search instead of scanning per keystroke", async () => {
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    await screen.findByLabelText("Interest");
    listRows.mockClear();
    const box = screen.getByPlaceholderText(/Search title/);
    for (const value of ["a", "ab", "abc"]) {
      fireEvent.change(box, { target: { value } });
    }
    // The discoveries search LIKEs over 3,159 prompts averaging 14kB with no
    // index; one scan per character typed is what this avoids.
    await waitFor(() => {
      expect(listRows.mock.calls.some((c) => c[1].search === "abc")).toBe(true);
    });
    expect(listRows.mock.calls.filter((c) => c[1].search === "ab")).toHaveLength(0);
  });

  it("renders extractor runs with what the run produced", async () => {
    listRows.mockResolvedValue(listResponse([
      {
        artifact_sha256: "f5019b0271c6e3c9390c8b95cc0e46714ffb651836b84868aa4def34d0d7fc90",
        generated_at: "2026-08-18T15:48:32Z", imported_at: "2026-08-18T15:51:12+00:00",
        offers: 5, waiting: 3, accepted: 1, rejected: 1, top_score: 0.823,
      },
    ], "extractor"));
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Extractor runs/ }));
    expect(await screen.findByText(/5 candidates/)).toBeInTheDocument();
    expect(screen.getByText(/3 still waiting/)).toBeInTheDocument();
    expect(screen.getByText("2 decided")).toBeInTheDocument();
    // The artifact digest is the run's only identity -- show enough of it to
    // match against the file on disk.
    expect(screen.getByText("f5019b0271c6")).toBeInTheDocument();
  });

  it("takes an extractor run to the offers it produced, not to a graph", async () => {
    const onSelect = vi.fn();
    listRows.mockResolvedValue(listResponse([
      { artifact_sha256: "abc123def456789", generated_at: "2026-08-18T15:48:32Z", offers: 2, waiting: 2 },
    ], "extractor"));
    render(<Explorer onSelectDiscovery={onSelect} onOpenRawDb={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Extractor runs/ }));
    fireEvent.click((await screen.findByText(/2 candidates/)).closest("li")!);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ artifact_sha256: "abc123def456789" }), "extractor");
  });

  it("activates a row from the keyboard", async () => {
    const onSelect = vi.fn();
    listRows.mockResolvedValue(listResponse([{ item_id: 7, title: "A discovery" }]));
    render(<Explorer onSelectDiscovery={onSelect} onOpenRawDb={() => {}} />);
    const row = await screen.findByText("A discovery");
    fireEvent.keyDown(row.closest("li")!, { key: "Enter" });
    expect(onSelect).toHaveBeenCalled();
  });

  it("marks the selected row", async () => {
    listRows.mockResolvedValue(listResponse([{ item_id: 7, title: "A discovery" }]));
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} selectedRowKey="discoveries:7" />);
    const row = (await screen.findByText("A discovery")).closest("li")!;
    expect(row.getAttribute("aria-current")).toBe("true");
  });

  it("survives the interest index failing", async () => {
    fetchInterests.mockRejectedValue(new Error("nope"));
    listRows.mockResolvedValue(listResponse([{ item_id: 1, title: "Still here" }]));
    render(<Explorer onSelectDiscovery={() => {}} onOpenRawDb={() => {}} />);
    expect(await screen.findByText("Still here")).toBeInTheDocument();
  });
});

describe("rowKey", () => {
  it("keys each tab's rows by that tab's own id column", () => {
    expect(rowKey("discoveries", { item_id: 7 }, 0)).toBe("discoveries:7");
    expect(rowKey("generations", { id: 3 }, 0)).toBe("generations:3");
    expect(rowKey("extractor", { artifact_sha256: "deadbeef" }, 0)).toBe("extractor:deadbeef");
    expect(rowKey("failed", { node_id: 9 }, 0)).toBe("failed:9");
  });

  it("falls back to the index only when a row carries no id at all", () => {
    expect(rowKey("failed", {}, 4)).toBe("failed:idx4");
  });
});
