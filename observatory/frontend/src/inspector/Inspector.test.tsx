import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ModelCallDetail, NodeDetail } from "../types";

// fetchNode is the only thing Inspector pulls from the API layer; mocking it
// keeps these tests free of the network without a fixture server.
const fetchNode = vi.fn();
vi.mock("../api", () => ({ fetchNode: (id: unknown) => fetchNode(id) }));

const { Inspector, bestCall } = await import("./Inspector");

function call(over: Partial<ModelCallDetail> & { id: number; attempt: number }): ModelCallDetail {
  return {
    call_role: "scoring", provider: "claude_chat", model: "sonnet",
    exact_system_prompt: null, exact_user_prompt: "PROMPT", exact_schema_json: null,
    exact_parameters_json: null, raw_response_text: null, parsed_response_json: null,
    validation_result: null, usage: null, provider_request_id: null,
    started_at: null, finished_at: null, error: null, row_url: "/db/model_calls/1",
    ...over,
  };
}

function detail(over: Partial<NodeDetail> = {}): NodeDetail {
  return {
    overview: {
      id: 1, run_id: 7, node_type: "score-attempt", swimlane: "scoring",
      entity_type: null, entity_id: null, label: "an item", status: "ok",
      summary: null, started_at: null, finished_at: null, error: null,
    },
    input: null, output: null, exact_text: null, model_calls: [], config: null,
    run: null, inbound_edges: [], outbound_edges: [], row_urls: {}, truncated: false,
    ...over,
  };
}

// The shape measured across the live DB: attempt #1 errored with no response
// text, attempt #2 carries the answer. Reading calls[0] showed the failure.
const ERRORED_THEN_VALID = [
  call({ id: 10, attempt: 1, error: "overloaded_error", raw_response_text: null }),
  call({ id: 11, attempt: 2, validation_result: "valid", raw_response_text: "THE ANSWER", parsed_response_json: { final_score: 0.82 } }),
];

describe("bestCall", () => {
  it("returns undefined when there are no calls", () => {
    expect(bestCall([])).toBeUndefined();
  });

  it("prefers the valid attempt over an earlier errored one", () => {
    expect(bestCall(ERRORED_THEN_VALID)?.id).toBe(11);
  });

  it("prefers the LAST valid attempt when several validated", () => {
    const calls = [
      call({ id: 1, attempt: 1, validation_result: "valid", raw_response_text: "first" }),
      call({ id: 2, attempt: 2, validation_result: "valid", raw_response_text: "second" }),
    ];
    expect(bestCall(calls)?.id).toBe(2);
  });

  it("falls back to the last attempt carrying any response text", () => {
    const calls = [
      call({ id: 1, attempt: 1, raw_response_text: null, error: "boom" }),
      call({ id: 2, attempt: 2, raw_response_text: "unvalidated but present" }),
    ];
    expect(bestCall(calls)?.id).toBe(2);
  });

  it("falls back to the final attempt when every attempt failed outright", () => {
    const calls = [
      call({ id: 1, attempt: 1, error: "boom" }),
      call({ id: 2, attempt: 2, error: "boom again" }),
    ];
    expect(bestCall(calls)?.id).toBe(2);
  });
});

describe("Inspector", () => {
  beforeEach(() => {
    fetchNode.mockReset();
  });

  it("offers Raw response / Parsed JSON when a LATER attempt carries them", async () => {
    fetchNode.mockResolvedValue(detail({ model_calls: ERRORED_THEN_VALID }));
    render(<Inspector nodeId={1} />);
    // Regression guard for the calls[0] bug: these tabs used to be absent
    // entirely for ~89.5% of scored nodes.
    expect(await screen.findByRole("tab", { name: "Raw response" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Parsed JSON" })).toBeInTheDocument();
  });

  it("shows the valid attempt's response by default, not the errored first one", async () => {
    fetchNode.mockResolvedValue(detail({ model_calls: ERRORED_THEN_VALID }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Raw response" }));
    expect(screen.getByTestId("monospace-content").textContent).toContain("THE ANSWER");
  });

  it("lets the attempt picker switch to the errored attempt and explains the gap", async () => {
    fetchNode.mockResolvedValue(detail({ model_calls: ERRORED_THEN_VALID }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Raw response" }));
    fireEvent.click(screen.getByRole("tab", { name: /attempt 1/ }));
    expect(screen.getByText(/returned no response text/)).toBeInTheDocument();
    expect(screen.getByText(/overloaded_error/)).toBeInTheDocument();
  });

  it("does not show the attempt picker for a single-attempt node", async () => {
    fetchNode.mockResolvedValue(detail({
      model_calls: [call({ id: 1, attempt: 1, validation_result: "valid", raw_response_text: "only" })],
    }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Raw response" }));
    expect(screen.queryByRole("tab", { name: /attempt 1/ })).toBeNull();
  });

  it("recovers from a failed fetch when a different node is selected", async () => {
    fetchNode.mockRejectedValueOnce(new Error("boom"));
    const { rerender } = render(<Inspector nodeId={1} />);
    expect(await screen.findByText("boom")).toBeInTheDocument();

    // The sticky-error bug: without setError(null) the panel rendered "boom"
    // forever, over every subsequently selected node.
    fetchNode.mockResolvedValue(detail({ overview: { ...detail().overview, label: "recovered" } }));
    rerender(<Inspector nodeId={2} />);
    expect(await screen.findByText("recovered")).toBeInTheDocument();
    expect(screen.queryByText("boom")).toBeNull();
  });

  it("renders the run id, kind and status", async () => {
    fetchNode.mockResolvedValue(detail({ run: { id: 187, kind: "web-tick", status: "done" } }));
    render(<Inspector nodeId={1} />);
    expect(await screen.findByText("Run")).toBeInTheDocument();
    expect(screen.getByText(/#187 · web-tick/)).toBeInTheDocument();
  });

  it("offers an Exact text tab for render nodes", async () => {
    fetchNode.mockResolvedValue(detail({ exact_text: "the telegram message" }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Exact text" }));
    expect(screen.getByTestId("monospace-content").textContent).toContain("the telegram message");
  });
});
