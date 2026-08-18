import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ModelCallDetail, NodeDetail } from "../types";

// fetchNode is the only thing Inspector pulls from the API layer; mocking it
// keeps these tests free of the network without a fixture server.
const fetchNode = vi.fn();
const fetchPromptTemplate = vi.fn();
vi.mock("../api", () => ({
  fetchNode: (id: unknown) => fetchNode(id),
  fetchPromptTemplate: (id: unknown) => fetchPromptTemplate(id),
}));

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
    input: null, output: null, exact_text: null, model_calls: [], related_model_calls: [],
    entity_row: null, score_weights: null,
    config: null, run: null, inbound_edges: [], outbound_edges: [], row_urls: {}, truncated: false,
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

  it("shows the prompt on a node whose own calls are absent, attributed to its neighbour", async () => {
    // A candidate/threshold/score-debug card carries no model calls of its
    // own -- which is exactly why the prompt used to be invisible on the nodes
    // people actually click.
    fetchNode.mockResolvedValue(detail({
      overview: { ...detail().overview, node_type: "threshold" },
      model_calls: [],
      related_model_calls: [call({
        id: 55, attempt: 2, validation_result: "valid", raw_response_text: "ok",
        exact_user_prompt: "SCORE THIS ITEM", via_node_id: 25686, via_node_type: "score-attempt",
      })],
    }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Prompt" }));
    expect(screen.getByTestId("monospace-content").textContent).toContain("SCORE THIS ITEM");
    expect(screen.getByText(/belongs to the score-attempt node/)).toBeInTheDocument();
  });

  it("does not offer a Prompt tab when no call anywhere carries one", async () => {
    fetchNode.mockResolvedValue(detail({
      model_calls: [call({ id: 1, attempt: 1, exact_user_prompt: null, raw_response_text: "x" })],
    }));
    render(<Inspector nodeId={1} />);
    await screen.findByRole("tab", { name: "Raw response" });
    expect(screen.queryByRole("tab", { name: "Prompt" })).toBeNull();
  });

  it("reports that a diff is impossible when the template moved on", async () => {
    fetchNode.mockResolvedValue(detail({
      model_calls: [call({ id: 9, attempt: 1, exact_user_prompt: "OLD PROMPT", validation_result: "valid", raw_response_text: "x" })],
    }));
    fetchPromptTemplate.mockResolvedValue({
      call_id: 9, role: "scoring", available: true, matches_current: false, diff: [],
      reason: "template has changed since this score was written (stored 92954b87de02, current 7044a467d659)",
    });
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Prompt" }));
    fireEvent.click(screen.getByText("Diff vs current template"));
    expect(await screen.findByText(/template has changed since this score/)).toBeInTheDocument();
  });

  it("breaks a score down into its weighted dimensions", async () => {
    // The threshold card shows one number; the six dimensions behind it used
    // to be reachable only through raw Datasette.
    fetchNode.mockResolvedValue(detail({
      overview: { ...detail().overview, entity_type: "scores", entity_id: "12" },
      entity_row: {
        personal_relevance: 0.82, novelty: 0.4, depth: 0.55, specificity: 0.7,
        importance: 0.3, surprise: 0.25, final_score: 0.577, confidence: 0.8,
        reason: "names the specific evidence",
      },
      score_weights: { personal_relevance: 0.35, novelty: 0.2, depth: 0.15, importance: 0.15, surprise: 0.15 },
    }));
    render(<Inspector nodeId={1} />);
    expect(await screen.findByText("Score breakdown")).toBeInTheDocument();
    expect(screen.getByText("personal relevance")).toBeInTheDocument();
    expect(screen.getByText(/×0.35/)).toBeInTheDocument();
    // specificity is scored but deliberately outside the weighted sum.
    expect(screen.getByText(/unweighted/)).toBeInTheDocument();
    expect(screen.getByText(/names the specific evidence/)).toBeInTheDocument();
  });

  it("names what a duplicate item duplicated", async () => {
    fetchNode.mockResolvedValue(detail({
      overview: { ...detail().overview, entity_type: "candidate_items", entity_id: "5" },
      entity_row: {
        title: "A dup", duplicate_of: 2, dup_reason: "same url",
        duplicate_of_item: { id: 2, title: "The original", url: "https://example.com/x" },
      },
    }));
    render(<Inspector nodeId={1} />);
    expect(await screen.findByText("The original")).toBeInTheDocument();
    expect(screen.getByText(/same url/)).toBeInTheDocument();
  });

  it("shows delivery attempts for a notification node", async () => {
    fetchNode.mockResolvedValue(detail({
      overview: { ...detail().overview, entity_type: "notifications", entity_id: "3" },
      entity_row: { channel: "telegram", attempts: 3, ok: 1, sent_at: null },
    }));
    render(<Inspector nodeId={1} />);
    expect(await screen.findByText("Attempts")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("delivered")).toBeInTheDocument();
  });

  it("renders no entity section when the node has no entity row", async () => {
    fetchNode.mockResolvedValue(detail());
    render(<Inspector nodeId={1} />);
    await screen.findByTestId("inspector");
    expect(screen.queryByText("Score breakdown")).toBeNull();
  });

  it("offers an Exact text tab for render nodes", async () => {
    fetchNode.mockResolvedValue(detail({ exact_text: "the telegram message" }));
    render(<Inspector nodeId={1} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Exact text" }));
    expect(screen.getByTestId("monospace-content").textContent).toContain("the telegram message");
  });
});
