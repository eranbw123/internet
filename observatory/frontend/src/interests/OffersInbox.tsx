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
import type { InterestEdge, Offer } from "./types";
import { isDecidable, retireTargetKey } from "./types";
import { BidiText, guessLang } from "./Bidi";
import { OfferProvenance } from "./Provenance";

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
          <span className="chip chip-warn">snoozed until {offer.snoozed_until}</span>
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
      <p className="offer-desc">
        <BidiText lang={guessLang(offer.description)}>{offer.description}</BidiText>
      </p>

      <OfferProvenance offer={offer} edges={edges} />

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
            <button type="button" className="btn" disabled={busy} onClick={() => onEdit(offer)}>
              Edit and accept
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
          New suggestions appear after the extractor runs; it proposes at most five per run,
          so an empty inbox is the normal state rather than a sign something is broken.
        </p>
      </div>
    );
  }
  return (
    <div className="offers-inbox" data-testid="offers-inbox">
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
