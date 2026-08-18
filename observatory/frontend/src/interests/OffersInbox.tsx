/** The offers inbox: what the system is asking the owner to decide.
 *
 * Presentational by design -- every decision is handed up to the workspace,
 * which owns the client calls and the refresh. That keeps the decision
 * ORCHESTRATION (which is genuinely fiddly for retirement offers, see
 * InterestsWorkspace) in one place, and keeps this file about what an offer
 * looks like.
 *
 * Two rules the UI enforces because the store does:
 *
 *   - A decided offer is never re-decidable. `accepted` is terminal in PR H's
 *     TRANSITIONS table, so a decided offer renders its outcome and no
 *     buttons at all. Rendering an Accept button that is guaranteed to 409 is
 *     worse than rendering none.
 *   - Rejecting is not the same size of action as snoozing. It appends the
 *     offer's key and signal tokens to `blocked_derived_terms` and suppresses
 *     them for 180 days, so the reject button asks once and says what it is
 *     about to block.
 */
import { useState } from "react";
import { useIsMobile } from "../useIsMobile";
import type { InterestEdge, Offer } from "./types";
import { isDecidable, retireTargetKey } from "./types";
import { BidiText, guessLang } from "./Bidi";
import { exactTitle, formatDay } from "../time";
import { OfferProvenance, durabilityBits } from "./Provenance";

/** The bar a retirement offer proposes as the alternative to retiring. */
const LOWER_BAR_TO = 0.78;

export interface OfferDecision {
  offer: Offer;
  action: "accept" | "reject" | "snooze" | "retire" | "lower-bar" | "keep-watching";
  note?: string;
  minScore?: number;
}

interface Props {
  offers: Offer[];
  edges: InterestEdge[];
  /** Offer id currently being written, so its card can show it. */
  busyId: number | null;
  /** Per-offer error, keyed by offer id. */
  errors: Record<number, string>;
  onDecide: (decision: OfferDecision) => void;
  onEdit: (offer: Offer) => void;
  loading: boolean;
}

function dec2(n: number): string {
  return n.toFixed(2).replace(/^0/, "");
}

const KIND_LABEL: Record<string, string> = {
  new: "new", bridge: "bridge", merge: "merge",
  split: "split", revive: "revive", retire: "retire?",
};

function OfferCard({
  offer, edges, busy, error, onDecide, onEdit,
}: {
  offer: Offer; edges: InterestEdge[]; busy: boolean; error?: string;
  onDecide: Props["onDecide"]; onEdit: Props["onEdit"];
}) {
  const [confirmingReject, setConfirmingReject] = useState(false);
  const isRetire = offer.kind === "retire";
  const decidable = isDecidable(offer.status);
  const isMobile = useIsMobile();

  /* On a phone the description and the whole provenance block fold away
     behind one summary. Measured at 393x852: a single offer ran to 2.4
     screens, so the inbox at its target of ten was 24 screens of scrolling
     with the Accept/Reject buttons two screens below every title, and no way
     to see how many were waiting. Folded, a card is ~250px and the whole
     queue is three screens you can triage with a thumb; one tap opens the
     evidence for the one you actually want to read, which is the reading the
     owner asked to be comfortable rather than the reading he has to scroll
     past nine times.

     The summary is not a bare "details" label: it carries the durability
     facts (how many conversations, over how long, how recently), which is the
     line that answers "is this a real interest or a passing errand?" -- so a
     folded card still says enough to decide against. */
  const body = (
    <>
      <p className="offer-desc">
        <BidiText lang={guessLang(offer.description)}>{offer.description}</BidiText>
      </p>
      <OfferProvenance offer={offer} edges={edges} />
    </>
  );
  const bits = durabilityBits(offer);

  return (
    <article
      className={`offer-card offer-kind-${offer.kind} ${busy ? "is-busy" : ""}`}
      data-testid={`offer-${offer.key}`}
    >
      <header className="offer-head">
        <span className={`chip kind-chip kind-${offer.kind}`}>{KIND_LABEL[offer.kind] ?? offer.kind}</span>
        <code className="key-chip">
          {isRetire ? retireTargetKey(offer) : offer.key}
        </code>
        {offer.exploratory && (
          <span className="chip chip-group" title="the run's reserved serendipity slot">
            serendipity
          </span>
        )}
        {offer.status === "snoozed" && offer.snoozed_until && (
          <span className="chip chip-warn" title={exactTitle(offer.snoozed_until)}>
            snoozed until {formatDay(offer.snoozed_until)}
          </span>
        )}
        {offer.score !== null && (
          <span className="offer-score" title="composite offer score">{dec2(offer.score)}</span>
        )}
      </header>

      <p className={`offer-intent ${isRetire ? "offer-intent-drop" : "offer-intent-add"}`}>
        {isRetire
          ? "Proposing to STOP an interest you already have"
          : "Proposing a NEW interest, from your conversations"}
      </p>
      <h3 className="offer-title">
        <BidiText lang={guessLang(offer.title)}>{offer.title}</BidiText>
      </h3>
      {isMobile ? (
        <details className="offer-why">
          <summary>
            <span className="offer-why-label">
              {isRetire ? "Why it is being proposed for retirement" : "Why this one"}
            </span>
            {bits.length > 0 && <span className="offer-why-bits">{bits.join(" · ")}</span>}
          </summary>
          {body}
        </details>
      ) : (
        body
      )}

      {error && <p className="offer-error" role="alert">{error}</p>}

      {!decidable ? (
        <footer className="offer-decided">
          <span className={`chip chip-${offer.status === "accepted" ? "ok" : "muted"}`}>
            {offer.status}
          </span>
          {offer.decided_note && <span className="prov-muted">"{offer.decided_note}"</span>}
          <span className="prov-muted">This decision is final.</span>
        </footer>
      ) : confirmingReject ? (
        <footer className="offer-actions offer-confirm">
          <p className="offer-confirm-text">
            Rejecting blocks <code>{offer.key}</code> and its signal terms for 180 days, so it
            will not be offered again in that window.
          </p>
          <div className="offer-buttons">
            <button
              type="button" className="btn btn-danger" disabled={busy}
              onClick={() => { setConfirmingReject(false); onDecide({ offer, action: "reject" }); }}
            >
              Reject and block
            </button>
            <button type="button" className="btn" onClick={() => setConfirmingReject(false)}>
              Cancel
            </button>
          </div>
        </footer>
      ) : isRetire ? (
        <footer className="offer-actions">
          <div className="offer-buttons">
            <button
              type="button" className="btn btn-danger" disabled={busy}
              onClick={() => onDecide({ offer, action: "retire" })}
            >
              Retire it
            </button>
            <button
              type="button" className="btn" disabled={busy}
              onClick={() => onDecide({ offer, action: "lower-bar", minScore: LOWER_BAR_TO })}
            >
              Lower bar to {dec2(LOWER_BAR_TO)} instead
            </button>
            <button
              type="button" className="btn" disabled={busy}
              onClick={() => onDecide({ offer, action: "keep-watching" })}
            >
              Keep watching
            </button>
          </div>
        </footer>
      ) : (
        <footer className="offer-actions">
          <div className="offer-buttons">
            <button
              type="button" className="btn btn-primary" disabled={busy}
              onClick={() => onDecide({ offer, action: "accept" })}
            >
              Accept
            </button>
            {/* Accept keeps a full-width row of its own; the other three
                share one. Four stacked full-width buttons cost 220px of a
                426px card on a phone, over half of every card in a
                ten-offer inbox, and the rule they were following ("one
                consequential decision per full-width button") was written
                against a wrapped row of 30px buttons. Three across 345px is
                115x44 each -- comfortably over the tap floor -- and the only
                destructive one, Reject, already asks again before it blocks
                anything. */}
            <div className="offer-buttons-row">
              <button type="button" className="btn" disabled={busy} onClick={() => onEdit(offer)}>
                Edit
              </button>
              <button
                type="button" className="btn" disabled={busy}
                onClick={() => onDecide({ offer, action: "snooze" })}
              >
                Snooze 30d
              </button>
              <button
                type="button" className="btn btn-quiet" disabled={busy}
                onClick={() => setConfirmingReject(true)}
              >
                Reject
              </button>
            </div>
          </div>
        </footer>
      )}
    </article>
  );
}

export function OffersInbox({ offers, edges, busyId, errors, onDecide, onEdit, loading }: Props) {
  if (loading) return <p className="ws-loading">Loading offers...</p>;
  if (offers.length === 0) {
    return (
      <div className="ws-empty">
        <p><strong>No suggestions right now.</strong></p>
        <p>
          This is where the system proposes new interests, drawn from themes that keep
          coming back in your own conversations &mdash; and where it proposes dropping ones
          that have stopped producing. You accept, reject or snooze each one.
        </p>
        <p className="prov-muted">
          New suggestions appear after the extractor runs, so an empty inbox is the normal
          state rather than a sign something is broken.
        </p>
      </div>
    );
  }
  return (
    <div className="offers-inbox" data-testid="offers-inbox">
      {/* How many are waiting. Without it the inbox is an unbounded scroll:
          you cannot tell whether you are three offers from the end or
          thirteen, which is the difference between triaging it now and
          putting it off. Deliberately derived from the list rather than any
          per-run cap -- the extractor's ceiling is the extractor's business
          and has already moved once. */}
      <p className="offers-count" data-testid="offers-count">
        <strong>{offers.length}</strong> {offers.length === 1 ? "suggestion" : "suggestions"} waiting
      </p>
      {offers.map((offer) => (
        <OfferCard
          key={offer.id}
          offer={offer}
          edges={edges}
          busy={busyId === offer.id}
          error={errors[offer.id]}
          onDecide={onDecide}
          onEdit={onEdit}
        />
      ))}
    </div>
  );
}
