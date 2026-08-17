// Mirrors observatory/db.py's return shapes exactly -- see that module's
// docstrings for the authoritative field-by-field contract. Kept loose
// (mostly `unknown`/`Record<string, unknown>` for row payloads) since these
// are raw SQLite rows redacted+forwarded, not a schema this frontend owns.

export type ID = number | string;

export interface NodeSummary {
  id: ID;
  run_id: number | null;
  node_type: string;
  swimlane: string;
  entity_type: string | null;
  entity_id: string | null;
  label: string | null;
  status: string | null;
  summary: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  child_count?: number; // present only on node_type === 'group'
}

export interface GraphEdge {
  from: ID;
  to: ID;
  relationship: string;
  ordinal: number | null;
}

export interface GraphGroup {
  group: string;
  parent_node_id: ID;
  relationship: string;
  child_node_type: string;
  child_count: number;
}

export interface GraphResponse {
  run_id: number | null;
  run_ids: number[];
  focus_node_id: ID | null;
  nodes: NodeSummary[];
  edges: GraphEdge[];
  groups: GraphGroup[];
  emphasized_path: ID[];
}

export interface ListResponse {
  tab: string;
  limit: number;
  offset: number;
  total: number;
  rows: Record<string, unknown>[];
}

export interface ModelCallDetail {
  id: number;
  call_role: string;
  attempt: number;
  provider: string;
  model: string;
  exact_system_prompt: string | null;
  exact_user_prompt: string | null;
  exact_schema_json: unknown;
  exact_parameters_json: unknown;
  raw_response_text: string | null;
  parsed_response_json: unknown;
  validation_result: string | null;
  usage: unknown;
  provider_request_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  row_url: string;
  // Present only on related_model_calls: which adjacent node this call was
  // borrowed from, so the UI can attribute it instead of implying it hung off
  // the node being inspected.
  via_node_id?: ID;
  via_node_type?: string;
}

/** /api/prompt-template?call=<id>. `available` is false for call roles with no
 * stable module-level template (council, mission_search); `matches_current` is
 * false when scores.prompt_hash predates the current template, in which case
 * `diff` is empty and `reason` explains why. */
export interface PromptTemplate {
  call_id: number;
  role: string;
  available: boolean;
  reason?: string;
  fingerprint?: string;
  stored_prompt_hash?: string | null;
  matches_current?: boolean;
  template?: string;
  diff: string[];
}

export interface NodeDetail {
  overview: NodeSummary;
  input: unknown;
  output: unknown;
  exact_text: string | null;
  model_calls: ModelCallDetail[];
  /** Calls borrowed from the adjacent node that explains this one; populated
   * only when the node has no calls of its own. */
  related_model_calls: ModelCallDetail[];
  /** The operational row this node points at (scores, candidate_items,
   * search_missions, ...) -- whitelisted server-side, redacted like everything
   * else. Null when the node has no entity or the table isn't whitelisted. */
  entity_row: Record<string, unknown> | null;
  /** discovery.models.WEIGHTS, present only alongside a scores row, so
   * sub-scores can be shown with the weighting that produced final_score. */
  score_weights: Record<string, number> | null;
  config: unknown;
  run: { id: number; kind: string; status: string } | null;
  inbound_edges: { from: ID; relationship: string; from_label: string; from_node_type: string }[];
  outbound_edges: { to: ID; relationship: string; to_label: string; to_node_type: string }[];
  row_urls: Record<string, string>;
  truncated: boolean;
}

/** /api/runs -- enough of a run to pick it out of a list. */
export interface RunOption {
  id: number;
  kind: string;
  status: string;
  started_at: string | null;
  node_count: number | null;
}

/** /api/interests -- just enough of an interest to populate a picker. */
export interface InterestOption {
  key: string;
  title: string | null;
  active: number;
  layer: string | null;
}

export interface InterestDetail {
  definition: Record<string, unknown>;
  provenance: unknown;
  signals: { positive: unknown; negative: unknown };
  events: Record<string, unknown>[];
  generations: Record<string, unknown>[];
  missions: Record<string, unknown>[];
  discoveries: Record<string, unknown>[];
  failures: Record<string, unknown>[];
  feedback: Record<string, unknown>[];
}

export interface RunCompareResponse {
  kind: "run";
  a_run_id: number;
  b_run_id: number;
  nodes: { added: unknown[][]; removed: unknown[][]; changed: { key: unknown[]; a: NodeSummary; b: NodeSummary }[] };
  edges: { added: unknown[][]; removed: unknown[][] };
  context_diff: string[];
  mission_branches_diff: { added: string[]; removed: string[] };
  score_decisions_diff: { added: string[]; removed: string[] };
  delivery_outcome_diff: { added: unknown[][]; removed: unknown[][] };
}

export interface ModelCallCompareResponse {
  kind: "model_call";
  a_id: number;
  b_id: number;
  prompt_diff: string[];
  response_diff: string[];
}

export type CompareResponse = RunCompareResponse | ModelCallCompareResponse;

export type Tab = "discoveries" | "interests" | "generations" | "missions" | "failed";

// Bootstrap payload embedded by plugin.py's _shell_html() as
// <script id="observatory-bootstrap" type="application/json">...</script>,
// either {"focus": null} (plain /observatory/) or
// {"focus": {"kind": "score", "node_id": ..., "run_id": ..., "score_id": ...}}
// (the /observatory/trace/score/<id> deep link).
export interface BootstrapFocus {
  kind: "score";
  node_id: ID;
  run_id: number;
  score_id: number;
}

export interface Bootstrap {
  focus: BootstrapFocus | null;
  /** True when the server is running with --public, i.e. every request needs
   * the bearer token. The raw-database button opens "/" with no token, which
   * lands on a 403 there, so it is hidden instead. */
  public?: boolean;
}
