// Thin fetch wrapper over the task-2 JSON API. Every function issues a
// read-only GET (no mutation route exists -- see plugin.py) and throws
// ApiError on a non-2xx response so callers can render a message instead of
// crashing the tree.
import type {
  CompareResponse, GraphResponse, InterestDetail, InterestOption, ListResponse,
  NodeDetail, PromptTemplate, Tab,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function getJson<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const resp = await fetch(url.toString(), { method: "GET" });
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      const body = await resp.json();
      message = body.error || message;
    } catch {
      // non-JSON error body -- keep statusText
    }
    throw new ApiError(resp.status, message);
  }
  return resp.json() as Promise<T>;
}

export interface ListFilters {
  interest?: string; layer?: string; source?: string; provider?: string; model?: string;
  mission?: string; sent?: string; feedback?: string; date_from?: string; date_to?: string;
  failure_stage?: string; trace_complete?: string;
  /** Interests tab only: "yes" | "no". 13 of 46 live interests are
   * deactivated and were previously indistinguishable and unfilterable. */
  active?: string;
}

export function listRows(
  tab: Tab, opts: { filters?: ListFilters; search?: string; limit?: number; offset?: number } = {},
): Promise<ListResponse> {
  return getJson<ListResponse>("/observatory/api/list", {
    tab, search: opts.search, limit: opts.limit, offset: opts.offset, ...opts.filters,
  });
}

export function fetchGraph(
  seed: { entity_type?: string; entity_id?: string | number; run_id?: number },
): Promise<GraphResponse> {
  return getJson<GraphResponse>("/observatory/api/graph", seed);
}

export function fetchChildren(
  seed: { node_id?: string | number; group?: string },
): Promise<{ children: import("./types").NodeSummary[] }> {
  return getJson("/observatory/api/children", seed);
}

export function fetchNode(nodeId: string | number): Promise<NodeDetail> {
  return getJson<NodeDetail>(`/observatory/api/node/${nodeId}`);
}

/** The whole interest list, cheaply -- enough to populate a picker. Separate
 * from listRows('interests'), which runs four COUNT subqueries per row and is
 * capped at MAX_LIMIT=50 (there are already 46). */
export function fetchInterests(): Promise<{ interests: InterestOption[] }> {
  return getJson<{ interests: InterestOption[] }>("/observatory/api/interests");
}

export function fetchInterest(key: string): Promise<InterestDetail> {
  return getJson<InterestDetail>(`/observatory/api/interest/${encodeURIComponent(key)}`);
}

export function fetchCompare(kind: "run" | "model_call", a: string | number, b: string | number): Promise<CompareResponse> {
  return getJson<CompareResponse>("/observatory/api/compare", { kind, a, b });
}

export function fetchPromptTemplate(callId: string | number): Promise<PromptTemplate> {
  return getJson<PromptTemplate>("/observatory/api/prompt-template", { call: callId });
}
