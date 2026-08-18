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
 * INTEGRATION: done. PR J landed, USE_MOCK_CLIENT is false, and the workspace
 * runs against the real endpoints. The mock is kept rather than deleted --
 * `?interests=mock` still reaches it, which is how the two are compared when a
 * number in the UI looks wrong, and it is what the component tests run
 * against so they need no server.
 *
 * One thing the swap needed beyond the flag: `adaptStats` below. PR J serves
 * the funnel under the names the Python side has always used, and this module
 * owns the translation to the names the components were written against. The
 * alternative -- renaming the fields in the components -- would have spread
 * the server's vocabulary across a dozen files.
 */
import type {
  DecideRequest, DecideResponse, EdgesResponse, GenerateResponse, InterestDetailResponse,
  InterestPayload, InterestStat, OffersResponse, OfferStatus, SaveResponse, StatsResponse,
} from "./types";
import { mockClient } from "./mockClient";
import { formatDay } from "../time";

/** PR J has landed; the workspace is live. `?interests=mock` still overrides. */
export const USE_MOCK_CLIENT = false;

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

/** The wire shape PR J actually serves for one funnel row.
 *
 * It differs from `InterestStat` in three places, all of them the Python
 * side's older vocabulary rather than a disagreement about meaning:
 * `notified` is what the UI calls `delivered`, `sparkline` is a sparse list of
 * {date, above_bar} rather than a dense array, and the response carries
 * `window_days` + `generated_at` where the UI wants a `from`/`to` pair. */
interface WireStat {
  key: string;
  notified: number;
  sparkline: { date: string; above_bar: number }[];
  [field: string]: unknown;
}

interface WireStats {
  window: string;
  window_days: number | null;
  generated_at: string;
  totals: Record<string, number>;
  interests: WireStat[];
}

const DAY_MS = 86_400_000;

/** Sparse {date, above_bar} points -> one value per day across the window,
 * oldest first.
 *
 * Two things this has to get right, both of which fail quietly:
 *
 *   - The server ships only days that HAVE a count (90 zeros per interest
 *     would dwarf the payload). Drawing that list directly would compress the
 *     idle stretches out and make a dying interest draw the same shape as a
 *     healthy one, so the zeros go back in.
 *   - The last bucket is `to`, not `to - 1`. The window is anchored at its
 *     end: a "7d" window is the seven days ENDING today, and an array that
 *     stopped at yesterday would hide today's items entirely -- on the one
 *     view whose job is to show whether an interest is still producing.
 */
function densify(
  points: { date: string; above_bar: number }[],
  from: Date,
  to: Date,
): number[] {
  const byDate = new Map(points.map((p) => [p.date, p.above_bar]));
  const days = Math.max(1, Math.round((to.getTime() - from.getTime()) / DAY_MS));
  const out: number[] = [];
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(to.getTime() - i * DAY_MS);
    out.push(byDate.get(d.toISOString().slice(0, 10)) ?? 0);
  }
  return out;
}

/** The server's funnel payload in the names the components use. */
export function adaptStats(raw: WireStats): StatsResponse {
  const to = new Date(raw.generated_at);
  // `all` has no fixed start, so the window opens at the oldest day any
  // interest actually has data for rather than at an arbitrary constant.
  const earliest = raw.interests
    .flatMap((r) => r.sparkline.map((p) => p.date))
    .sort()[0];
  const from = raw.window_days
    ? new Date(to.getTime() - raw.window_days * DAY_MS)
    : new Date(earliest ?? to.toISOString().slice(0, 10));

  const interests = raw.interests.map((row) => {
    const { notified, sparkline, ...rest } = row;
    return {
      ...rest,
      delivered: notified,
      daily_above_bar: densify(sparkline, from, to),
    } as unknown as InterestStat;
  });

  return {
    window: raw.window,
    // Displayed, so local. The Date objects above stay UTC-keyed because
    // densify() matches them against the server's sparkline buckets, which are
    // substr(created_at, 1, 10) -- UTC days.
    from: formatDay(from.toISOString()),
    to: formatDay(to.toISOString()),
    interests,
    totals: {
      collected: raw.totals.collected ?? 0,
      matched: raw.totals.matched ?? 0,
      above_bar: raw.totals.above_bar ?? 0,
      delivered: raw.totals.notified ?? 0,
      active_interests: raw.totals.active ?? 0,
      total_interests: raw.totals.interests ?? 0,
      dead_weight: raw.totals.dead_weight ?? 0,
    },
  };
}

/** The real section 7.3 client. */
export const httpClient: InterestsClient = {
  listOffers(status = "offered") {
    return request<OffersResponse>("/observatory/api/offers", { params: { status } });
  },
  decideOffer(id, req) {
    return request<DecideResponse>(`/observatory/api/offers/${id}/decide`, {
      method: "POST", body: req,
    });
  },
  async interestStats(window = "7d") {
    return adaptStats(
      await request<WireStats>("/observatory/api/interests/stats", { params: { window } }),
    );
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
