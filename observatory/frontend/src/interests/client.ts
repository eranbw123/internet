/** The ONE network seam for the interests workspace.
 *
 * Every call the workspace makes goes through `InterestsClient`. Two
 * implementations satisfy it:
 *
 *   httpClient  -- the real thing, hitting the section 7.3 endpoints that PR J
 *                  is implementing in observatory/manage.py. Complete and
 *                  ready; it has simply never been run against a live server,
 *                  because that server does not exist yet.
 *   mockClient  -- in-memory fixtures (mockData.ts) with realistic latency and
 *                  real mutation semantics, so the whole workspace is
 *                  demonstrable today.
 *
 * INTEGRATION (the small, obvious change PR J unblocks):
 *   1. flip USE_MOCK_CLIENT to false
 *   2. delete the `mockClient` import and mockData.ts / mockClient.ts
 * Nothing else in the workspace refers to fetch, to a URL, or to the mock.
 * That is the entire swap.
 *
 * The `?interests=live` / `?interests=mock` query override exists so the two
 * can be compared side by side during integration without a rebuild.
 */
import type {
  DecideRequest, DecideResponse, EdgesResponse, GenerateResponse, InterestDetailResponse,
  InterestPayload, OffersResponse, OfferStatus, SaveResponse, StatsResponse,
} from "./types";
import { mockClient } from "./mockClient";

/** Flip to false when PR J lands. See INTEGRATION above. */
export const USE_MOCK_CLIENT = true;

/** Mirrors ../api.ts's ApiError, kept local so the interests surface does not
 * reach into the read-only trace API's module for an error class. */
export class ManageApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ManageApiError";
  }
}

export interface InterestsClient {
  /** GET /observatory/api/offers?status=offered */
  listOffers(status?: OfferStatus): Promise<OffersResponse>;
  /** POST /observatory/api/offers/<id>/decide */
  decideOffer(id: number, req: DecideRequest): Promise<DecideResponse>;
  /** GET /observatory/api/interests/stats?window=7d */
  interestStats(window?: string): Promise<StatsResponse>;
  /** GET /observatory/api/interest/<key> -- the EXISTING read-only detail
   * endpoint, not a PR J addition. The editor loads description + signals
   * from it, which the bulk stats payload deliberately omits. */
  interestDetail(key: string): Promise<InterestDetailResponse>;
  /** POST /observatory/api/interests */
  createInterest(payload: InterestPayload): Promise<SaveResponse>;
  /** POST /observatory/api/interests/<key> -- update, or {active:false} to retire */
  updateInterest(key: string, payload: Partial<InterestPayload>): Promise<SaveResponse>;
  /** GET /observatory/api/edges?min_weight=.2 */
  listEdges(minWeight?: number): Promise<EdgesResponse>;
  /** POST /observatory/api/offers/generate -- selector re-rank, no LLM */
  generateOffers(): Promise<GenerateResponse>;
}

async function request<T>(
  path: string,
  init?: { method?: string; body?: unknown; params?: Record<string, string | number | undefined> },
): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(init?.params ?? {})) {
    if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
  }
  const method = init?.method ?? "GET";
  const resp = await fetch(url.toString(), {
    method,
    headers: init?.body !== undefined ? { "content-type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      const body = await resp.json();
      message = body.error || message;
    } catch {
      // non-JSON error body -- keep statusText
    }
    // 403 is the documented refusal for writes in --public mode
    // (DISCOVERY_UI_ALLOW_PUBLIC_WRITES=1 overrides server-side); surface it
    // as itself so the UI can say so rather than showing a bare "Forbidden".
    throw new ManageApiError(resp.status, message);
  }
  return resp.json() as Promise<T>;
}

/** The real section 7.3 client. Untested against a live server by
 * construction -- PR J is being written concurrently against this same spec. */
export const httpClient: InterestsClient = {
  listOffers(status = "offered") {
    return request<OffersResponse>("/observatory/api/offers", { params: { status } });
  },
  decideOffer(id, req) {
    return request<DecideResponse>(`/observatory/api/offers/${id}/decide`, {
      method: "POST", body: req,
    });
  },
  interestStats(window = "7d") {
    return request<StatsResponse>("/observatory/api/interests/stats", { params: { window } });
  },
  interestDetail(key) {
    return request<InterestDetailResponse>(`/observatory/api/interest/${encodeURIComponent(key)}`);
  },
  createInterest(payload) {
    return request<SaveResponse>("/observatory/api/interests", { method: "POST", body: payload });
  },
  updateInterest(key, payload) {
    return request<SaveResponse>(`/observatory/api/interests/${encodeURIComponent(key)}`, {
      method: "POST", body: payload,
    });
  },
  listEdges(minWeight = 0.2) {
    return request<EdgesResponse>("/observatory/api/edges", { params: { min_weight: minWeight } });
  },
  generateOffers() {
    return request<GenerateResponse>("/observatory/api/offers/generate", { method: "POST" });
  },
};

function overrideFromUrl(): "live" | "mock" | null {
  if (typeof window === "undefined") return null;
  const v = new URLSearchParams(window.location.search).get("interests");
  return v === "live" || v === "mock" ? v : null;
}

function pick(): InterestsClient {
  const override = overrideFromUrl();
  if (override === "live") return httpClient;
  if (override === "mock") return mockClient;
  return USE_MOCK_CLIENT ? mockClient : httpClient;
}

/** What every component imports. */
export const client: InterestsClient = {
  listOffers: (s) => pick().listOffers(s),
  decideOffer: (id, r) => pick().decideOffer(id, r),
  interestStats: (w) => pick().interestStats(w),
  interestDetail: (k) => pick().interestDetail(k),
  createInterest: (p) => pick().createInterest(p),
  updateInterest: (k, p) => pick().updateInterest(k, p),
  listEdges: (m) => pick().listEdges(m),
  generateOffers: () => pick().generateOffers(),
};

/** True when the workspace is showing fixtures rather than live data -- the UI
 * says so out loud (a banner), because a workspace that silently shows fake
 * funnel numbers is worse than no workspace. */
export function isMockActive(): boolean {
  const override = overrideFromUrl();
  if (override) return override === "mock";
  return USE_MOCK_CLIENT;
}
