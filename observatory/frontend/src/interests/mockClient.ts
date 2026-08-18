/** In-memory implementation of InterestsClient.
 *
 * This exists so PR L is demonstrable while PR J (the write API) is still
 * being written. It is not a stub returning constants: it holds mutable state
 * and implements PR H's real semantics, so working the workspace for a few
 * minutes exercises the same state transitions the live one will --
 *
 *   - offer moves are checked against PR H's TRANSITIONS table, so an already
 *     decided offer is refused here exactly as the server refuses it (409),
 *     and the UI's error path is a real path rather than dead code;
 *   - accepting a normal offer creates the interest, with zero funnel history,
 *     because that is the truth about an interest that has never collected;
 *   - accepting a `retire:` offer retires the interest the offer NAMES, which
 *     is not the offer's own key;
 *   - rejecting returns the blocked terms, because rejection has a 180-day
 *     consequence the owner should see;
 *   - editing a bar recomputes above_bar from that interest's actual scores,
 *     so the list view moves the way the live one will.
 *
 * What it deliberately does NOT do is anything PR J owns: no interests.json
 * rewrite, no sync v2, no interest_events row, no real mission cancellation.
 * Their observable results (`synced_at`, `missions_cancelled`) come back as
 * plausible values so the UI that displays them is exercised.
 */
import type {
  DecideRequest, DecideResponse, EdgesResponse, GenerateResponse, InterestPayload,
  InterestDetailResponse, InterestStat, Lifecycle, Offer, OffersResponse, OfferStatus,
  SaveResponse, StatsResponse,
} from "./types";
import { LIFECYCLE_TRANSITIONS, OFFER_TRANSITIONS, isCollecting, retireTargetKey } from "./types";
import type { InterestsClient } from "./client";
import { ManageApiError } from "./client";
import { MOCK_EDGES, MOCK_OFFERS } from "./mockData";
import { MOCK_INTERESTS } from "./mockStats";
import { MOCK_DETAILS } from "./mockDetails";

/** The live data window the measured aggregates cover. */
const WINDOW_FROM = "2026-08-08";
const WINDOW_TO = "2026-08-13";
/** PR H Rules.snooze_days. */
const DEFAULT_SNOOZE_DAYS = 30;
/** PR H Rules.reject_block_days -- how long a rejection suppresses its terms. */
export const REJECT_BLOCK_DAYS = 180;

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

let offers: Offer[] = clone(MOCK_OFFERS);
let interests: InterestStat[] = clone(MOCK_INTERESTS);

/** Tests call this so each case starts from the fixture. */
export function resetMockState() {
  offers = clone(MOCK_OFFERS);
  interests = clone(MOCK_INTERESTS);
}

/** A touch of latency, so loading states are real code paths rather than
 * branches nobody ever renders. Tests set this to 0. */
export const MOCK_LATENCY_MS = { value: 180 };

function delay<T>(value: T): Promise<T> {
  if (MOCK_LATENCY_MS.value <= 0) return Promise.resolve(value);
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_LATENCY_MS.value));
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d+Z$/, "Z");
}

/** PR H's `blocked_terms_for`: a rejection blocks the offer key and its signal
 * tokens, for reject_block_days. */
function blockedTermsFor(offer: Offer): string[] {
  const terms = new Set<string>([offer.key]);
  for (const signal of offer.positive_signals) {
    for (const token of signal.toLowerCase().split(/[^a-z0-9֐-׿]+/)) {
      if (token.length >= 4) terms.add(token);
    }
  }
  return [...terms].sort();
}

function recomputeDeadWeight(row: InterestStat) {
  row.above_bar = row.recent_scores.filter((s) => s >= row.min_score).length;
  row.dead_weight = isCollecting(row.lifecycle)
    && row.collected >= 30
    && row.above_bar / Math.max(1, row.collected) < 0.08;
}

function totalsFor(rows: InterestStat[]) {
  const collecting = rows.filter((r) => isCollecting(r.lifecycle));
  return {
    collected: collecting.reduce((s, r) => s + r.collected, 0),
    matched: collecting.reduce((s, r) => s + r.matched, 0),
    above_bar: collecting.reduce((s, r) => s + r.above_bar, 0),
    delivered: collecting.reduce((s, r) => s + r.delivered, 0),
    active_interests: collecting.length,
    total_interests: rows.length,
    dead_weight: collecting.filter((r) => r.dead_weight).length,
  };
}

/** An interest accepted from an offer has no funnel history: it has never
 * collected anything. Showing zeros is correct, and is what the live one will
 * do -- inventing traffic for it would be a lie the owner then acts on. */
function interestFromOffer(offer: Offer, edits?: DecideRequest["edits"]): InterestStat {
  return {
    key: offer.key,
    title: edits?.title ?? offer.title,
    layer: "owner",
    lifecycle: "active",
    active: true,
    min_score: edits?.min_score ?? offer.suggested_min_score ?? 0.7,
    sources: edits?.sources ?? offer.suggested_sources,
    parent_key: edits?.parent_key ?? offer.parent_key,
    collected: 0,
    matched: 0,
    above_bar: 0,
    delivered: 0,
    daily_above_bar: [0, 0, 0, 0, 0, 0],
    last_delivered_at: null,
    silence_days: 0,
    dead_weight: false,
    recent_scores: [],
  };
}

/** Mirrors PR H's `_transition`: a move not in the table is refused, and the
 * refusal is the same 409 the write API will return. */
function requireTransition(offer: Offer, to: OfferStatus) {
  if (!OFFER_TRANSITIONS[offer.status].includes(to)) {
    throw new ManageApiError(
      409,
      `offer '${offer.key}': ${offer.status} -> ${to} is not a legal transition`,
    );
  }
}

export const mockClient: InterestsClient = {
  listOffers(status: OfferStatus = "offered"): Promise<OffersResponse> {
    const rows = offers
      .filter((o) => o.status === status)
      // PR H's list_offers ordering: strongest first, unranked last.
      .sort((x, y) => (y.score ?? -1) - (x.score ?? -1));
    return delay({ offers: clone(rows) });
  },

  decideOffer(id: number, req: DecideRequest): Promise<DecideResponse> {
    const offer = offers.find((o) => o.id === id);
    if (!offer) return Promise.reject(new ManageApiError(404, `no offer ${id}`));
    const at = nowIso();

    try {
      if (req.action === "accept") {
        requireTransition(offer, "accepted");
        if (offer.kind === "retire") {
          // The offer names the interest; its own key is `retire:<that>`.
          const target = interests.find((i) => i.key === retireTargetKey(offer));
          if (target) {
            target.lifecycle = "retired";
            target.active = false;
            target.dead_weight = false;
          }
        } else if (!interests.some((i) => i.key === offer.key)) {
          interests.push(interestFromOffer(offer, req.edits));
        }
        offer.status = "accepted";
        offer.decided_at = at;
        offer.decided_note = req.note ?? "";
        return delay({ ok: true, interest_key: offer.key, status: offer.status });
      }

      if (req.action === "snooze") {
        requireTransition(offer, "snoozed");
        const days = req.snooze_days ?? DEFAULT_SNOOZE_DAYS;
        offer.status = "snoozed";
        offer.snoozed_until = new Date(Date.now() + days * 864e5).toISOString().slice(0, 10);
        offer.decided_note = req.note ?? "";
        return delay({ ok: true, status: offer.status });
      }

      requireTransition(offer, "rejected");
      const blocked = blockedTermsFor(offer);
      offer.status = "rejected";
      offer.decided_at = at;
      offer.decided_note = req.note ?? "";
      return delay({ ok: true, status: offer.status, blocked_terms: blocked });
    } catch (err) {
      return Promise.reject(err);
    }
  },

  interestStats(window = "7d"): Promise<StatsResponse> {
    return delay({
      window,
      from: WINDOW_FROM,
      to: WINDOW_TO,
      interests: clone(interests),
      totals: totalsFor(interests),
    });
  },

  interestDetail(key: string): Promise<InterestDetailResponse> {
    const row = interests.find((i) => i.key === key);
    if (!row) return Promise.reject(new ManageApiError(404, `no interest ${key}`));
    const detail = MOCK_DETAILS[key];
    return delay({
      definition: {
        key: row.key,
        title: row.title,
        description: detail?.description ?? "",
        min_score: row.min_score,
        active: row.active,
        layer: row.layer,
        parent_key: row.parent_key,
      },
      signals: {
        positive: detail?.positive_signals ?? [],
        negative: detail?.negative_signals ?? [],
      },
    });
  },

  createInterest(payload: InterestPayload): Promise<SaveResponse> {
    if (interests.some((i) => i.key === payload.key)) {
      return Promise.reject(new ManageApiError(409, `interest ${payload.key} already exists`));
    }
    interests.push({
      key: payload.key,
      title: payload.title,
      layer: "owner",
      lifecycle: payload.lifecycle,
      active: isCollecting(payload.lifecycle),
      min_score: payload.min_score,
      sources: payload.sources,
      parent_key: payload.parent_key,
      collected: 0, matched: 0, above_bar: 0, delivered: 0,
      daily_above_bar: [0, 0, 0, 0, 0, 0],
      last_delivered_at: null,
      silence_days: 0,
      dead_weight: false,
      recent_scores: [],
    });
    return delay({ ok: true, key: payload.key, synced_at: nowIso(), missions_cancelled: 0 });
  },

  updateInterest(key: string, payload: Partial<InterestPayload>): Promise<SaveResponse> {
    const row = interests.find((i) => i.key === key);
    if (!row) return Promise.reject(new ManageApiError(404, `no interest ${key}`));

    if (payload.lifecycle !== undefined && payload.lifecycle !== row.lifecycle) {
      const legal: Lifecycle[] = LIFECYCLE_TRANSITIONS[row.lifecycle];
      if (!legal.includes(payload.lifecycle)) {
        return Promise.reject(new ManageApiError(
          409,
          `interest '${key}': ${row.lifecycle} -> ${payload.lifecycle} is not a legal transition`,
        ));
      }
      row.lifecycle = payload.lifecycle;
      // `active` is derived from lifecycle server-side, never set separately.
      row.active = isCollecting(payload.lifecycle);
    }
    if (payload.title !== undefined) row.title = payload.title;
    if (payload.sources !== undefined) row.sources = payload.sources;
    if (payload.parent_key !== undefined) row.parent_key = payload.parent_key;
    if (payload.min_score !== undefined) {
      // The bar is the one field with an immediate visible consequence:
      // recount the window against the new bar, exactly as the live server
      // will when the list view is next fetched.
      row.min_score = payload.min_score;
    }
    recomputeDeadWeight(row);
    // Leaving the collecting states cancels the interest's PENDING missions
    // (sync v2, PR I).
    const cancelled = payload.lifecycle !== undefined && !isCollecting(payload.lifecycle) ? 2 : 0;
    return delay({ ok: true, key, synced_at: nowIso(), missions_cancelled: cancelled });
  },

  listEdges(minWeight = 0.2): Promise<EdgesResponse> {
    return delay({ edges: clone(MOCK_EDGES.filter((e) => e.weight >= minWeight)) });
  },

  generateOffers(): Promise<GenerateResponse> {
    // Re-rank only, no LLM: a snoozed offer whose horizon has passed wakes
    // back into the inbox (PR H's `wake`).
    let offered = 0;
    const today = new Date().toISOString().slice(0, 10);
    for (const o of offers) {
      if (o.status === "snoozed" && o.snoozed_until && o.snoozed_until <= today) {
        o.status = "offered";
        o.snoozed_until = null;
        offered += 1;
      }
    }
    return delay({ ok: true, offered });
  },
};
