/** Why an offer exists -- rendered as the body of the card, not as a tooltip.
 *
 * The design's line is "provenance is the interface", and the reason is
 * concrete: the owner is being asked to add something to a system that will
 * then spend real search budget and real attention on it. Deciding needs the
 * evidence, and evidence that lives behind a hover is evidence nobody reads.
 * So every offer shows, inline and by default:
 *
 *   1. WHICH CONVERSATIONS, and WHAT WAS ACTUALLY SAID. Verbatim quotes in the
 *      owner's own words, dated, attributed to a conversation, in the language
 *      they were written in. This is the part that makes an offer feel earned
 *      rather than generated, so it comes first and it is never collapsed to
 *      nothing -- the expander hides quotes four and beyond, never the first
 *      three.
 *   2. THE ARITHMETIC. Every ranking term with its weight, its value, and the
 *      product, adding up to the composite on the card. Shown as numbers that
 *      reconcile, because a score you cannot check is a score you cannot
 *      argue with.
 *   3. WHAT IT SITS NEXT TO. The similarity list against existing interests:
 *      either "nothing close" or "close to X, kept separate", which is the
 *      question the owner actually has ("don't I already track this?").
 *   4. THE RUN IT CAME FROM. Artifact hash + generation time, the ladder's
 *      provenance convention.
 *
 * Retirement offers get a different body, because their justification is a
 * funnel rather than a corpus: see RetireSnapshot.
 */
import { useState } from "react";
import type { EvidenceQuote, InterestEdge, Offer, ScoreTerms } from "./types";
import { SCORE_TERM_WEIGHTS, retireTargetKey } from "./types";
import { BidiText, Quote, guessLang } from "./Bidi";
import { useIsMobile } from "../useIsMobile";
import { exactTitle, formatDay } from "../time";

/** How many quotes are visible before the expander. Three is enough to show a
 * theme recurring across time without turning the inbox into a reading task --
 * the inbox is meant to be a two-minute ritual. */
const QUOTES_SHOWN = 3;

// Was `toLocaleDateString(undefined, ...)`, i.e. whatever zone the browser
// happens to be in -- accidentally right on the owner's machine and wrong
// anywhere else. The zone is a decision, so it is made in one place.
function fmtDate(iso: string): string {
  return formatDay(iso);
}

/** ".92" rather than "0.92": these are all 0-1 and the leading zero is noise
 * in a dense column of them. */
function dec2(n: number): string {
  return n.toFixed(2).replace(/^0/, "");
}

function months(n?: number): string {
  if (!n) return "";
  return n === 1 ? "1 month" : `${n} months`;
}

interface EvidenceProps {
  quotes: EvidenceQuote[];
  conversationCount: number;
}

export function EvidenceList({ quotes, conversationCount }: EvidenceProps) {
  const [expanded, setExpanded] = useState(false);
  if (quotes.length === 0) return null;
  const shown = expanded ? quotes : quotes.slice(0, QUOTES_SHOWN);
  const hidden = quotes.length - shown.length;

  return (
    <section className="prov-section">
      <h4 className="prov-heading">
        In your own words
        <span className="prov-count">
          {quotes.length} {quotes.length === 1 ? "quote" : "quotes"}
          {conversationCount > 0 && ` from ${conversationCount} conversations`}
        </span>
      </h4>
      <ol className="prov-quotes">
        {shown.map((q, i) => (
          <li className="prov-quote-row" key={`${q.conversation_id}-${q.date}-${i}`}>
            <div className="prov-quote-meta">
              <time dateTime={q.date} title={exactTitle(q.date)}>{fmtDate(q.date)}</time>
              {/* The conversation this came from. PR H persists only an id, so
                  the title renders when present and the id stands in when not
                  -- either way the owner can find the original. */}
              {q.conversation_title ? (
                <BidiText className="prov-conv" lang={guessLang(q.conversation_title)}>
                  {q.conversation_title}
                </BidiText>
              ) : (
                <span className="prov-conv prov-conv-id" title="conversation id (no title stored)">
                  {q.conversation_id || "unknown conversation"}
                </span>
              )}
              {q.depth > 0 && (
                <span className="prov-depth" title="how far into the conversation this went">
                  depth {dec2(q.depth)}
                </span>
              )}
            </div>
            <Quote lang={q.lang}>{q.quote}</Quote>
          </li>
        ))}
      </ol>
      {hidden > 0 && (
        <button type="button" className="link-button" onClick={() => setExpanded(true)}>
          Show {hidden} more {hidden === 1 ? "quote" : "quotes"}
        </button>
      )}
      {expanded && quotes.length > QUOTES_SHOWN && (
        <button type="button" className="link-button" onClick={() => setExpanded(false)}>
          Show fewer
        </button>
      )}
    </section>
  );
}

interface TermsProps {
  terms: ScoreTerms;
  score: number | null;
}

/** The ranking arithmetic, laid out so it can be checked by eye: each term's
 * value as a bar, its weight, and the product it contributes. The products sum
 * to the composite shown on the card. */
export function ScoreTermsBreakdown({ terms, score }: TermsProps) {
  const isMobile = useIsMobile();
  const rows = SCORE_TERM_WEIGHTS
    .map(({ term, weight, label }) => ({
      label, weight,
      value: typeof terms[term] === "number" ? (terms[term] as number) : null,
    }))
    .filter((r) => r.value !== null) as { label: string; weight: number; value: number }[];
  if (rows.length === 0) return null;

  const total = rows.reduce((s, r) => s + r.weight * r.value, 0);

  const heading = (
    <>
      Why it ranks {score !== null ? dec2(score) : ""}
      <span className="prov-count">weight x value</span>
    </>
  );
  const body = (
    <>
      <ul className="prov-terms">
        {rows.map((r) => (
          <li className="prov-term" key={r.label}>
            <span className="prov-term-label">{r.label}</span>
            <span className="prov-term-bar" aria-hidden="true">
              <span className="prov-term-fill" style={{ width: `${Math.round(r.value * 100)}%` }} />
            </span>
            <span className="prov-term-math">
              <span className="prov-term-weight">{dec2(r.weight)}</span>
              <span className="prov-term-times">x</span>
              <span className="prov-term-value">{dec2(r.value)}</span>
              <span className="prov-term-eq">=</span>
              <span className="prov-term-product">{(r.weight * r.value).toFixed(3).replace(/^0/, "")}</span>
            </span>
          </li>
        ))}
      </ul>
      <div className="prov-term-total">
        <span>total</span>
        <strong>{total.toFixed(3).replace(/^0/, "")}</strong>
        <span className="prov-muted">rounds to {dec2(total)}</span>
      </div>
    </>
  );

  // On a phone this arithmetic sits between the quotes -- which the owner
  // reads -- and Accept/Reject, which they came to press. It is the one part
  // of the card that is analyst detail rather than sofa reading, so it folds
  // away there and stays open everywhere else.
  if (isMobile) {
    return (
      <details className="prov-section prov-fold">
        <summary className="prov-heading">{heading}</summary>
        {body}
      </details>
    );
  }
  return (
    <section className="prov-section">
      <h4 className="prov-heading">{heading}</h4>
      {body}
    </section>
  );
}

interface SimilarityProps {
  offer: Offer;
}

/** "Don't I already track this?" answered explicitly. Anything shown here sat
 * below the importer's dedup threshold (0.70) and was deliberately kept
 * separate, so the number matters. */
export function SimilarityNote({ offer }: SimilarityProps) {
  const top = [...offer.similarity].sort((a, b) => b.sim - a.sim);
  return (
    <section className="prov-section">
      <h4 className="prov-heading">Against what you already track</h4>
      {top.length === 0 || top[0].sim < 0.1 ? (
        <p className="prov-line">
          Nothing close.
          {top.length > 0 && ` Nearest is ${top[0].key} at ${dec2(top[0].sim)} similarity.`}
        </p>
      ) : (
        <ul className="prov-sim-list">
          {top.slice(0, 3).map((s) => (
            <li key={s.key} className="prov-sim">
              <code className="key-chip">{s.key}</code>
              <span className="prov-sim-bar" aria-hidden="true">
                <span className="prov-sim-fill" style={{ width: `${Math.round(s.sim * 100)}%` }} />
              </span>
              <span className="prov-muted">{dec2(s.sim)} similar - kept separate</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

interface BridgeProps {
  offer: Offer;
  edges: InterestEdge[];
}

/** A bridge offer's own justification: the two interests it connects, and the
 * measured lift between them. The offer row carries no lift column, so it is
 * read from the `bridge_offer` edge recorded against both parents. */
export function BridgeNote({ offer, edges }: BridgeProps) {
  if (offer.kind !== "bridge" || offer.related_keys.length < 2) return null;
  const [a, b] = offer.related_keys;
  const edge = edges.find(
    (e) => e.kind === "bridge_offer"
      && ((e.a === a && e.b === b) || (e.a === b && e.b === a)),
  );
  return (
    <section className="prov-section">
      <h4 className="prov-heading">Bridges two interests you already have</h4>
      <p className="prov-line prov-bridge">
        <code className="key-chip">{a}</code>
        <span className="prov-bridge-x">x</span>
        <code className="key-chip">{b}</code>
      </p>
      <p className="prov-line prov-muted">
        {edge?.evidence.lift !== undefined
          ? `Lift ${edge.evidence.lift.toFixed(1)} between them - they co-occur ${edge.evidence.lift.toFixed(1)}x more than chance.`
          : "No lift recorded between them yet."}
        {offer.exploratory && " Surfaced in this run's serendipity slot rather than by rank."}
      </p>
    </section>
  );
}

/** A retirement offer's justification is the sweep's funnel snapshot, which PR
 * H stores in `score_terms` -- there is no separate funnel column. */
export function RetireSnapshot({ offer }: { offer: Offer }) {
  const t = offer.score_terms;
  const collected = typeof t.collected === "number" ? t.collected : 0;
  const scored = typeof t.scored === "number" ? t.scored : 0;
  const aboveBar = typeof t.above_bar === "number" ? t.above_bar : 0;
  const silent = typeof t.silent_days === "number" ? t.silent_days : null;
  const target = retireTargetKey(offer);

  return (
    <section className="prov-section">
      <h4 className="prov-heading">What it has produced</h4>
      <ol className="funnel-steps funnel-steps-retire">
        <li><span className="funnel-n">{collected}</span><span className="funnel-l">collected</span></li>
        <li><span className="funnel-n">{scored}</span><span className="funnel-l">scored</span></li>
        <li className={aboveBar === 0 ? "funnel-zero" : ""}>
          <span className="funnel-n">{aboveBar}</span><span className="funnel-l">above bar</span>
        </li>
      </ol>
      <p className="prov-line">
        {silent !== null && (
          <>
            <code className="key-chip">{target}</code>
            {` has gone ${silent} days without a single item clearing its bar. `}
          </>
        )}
        It was auto-paused at 45 days and stopped collecting; retiring it closes the file.
      </p>
    </section>
  );
}

interface ProvenanceProps {
  offer: Offer;
  edges: InterestEdge[];
}

/** Durability, in the one line that answers "is this a real interest or a
 * passing errand?" -- the distinction the whole generator design turns on. */
export function DurabilityLine({ offer }: { offer: Offer }) {
  const d = offer.durability;
  if (!d || d.n_convs === undefined) return null;
  const bits = [
    `${d.n_convs} conversations`,
    months(d.active_months),
    d.span_days ? `over ${d.span_days} days` : "",
    d.recency_days !== undefined ? `last one ${d.recency_days} days ago` : "",
  ].filter(Boolean);
  return <p className="prov-durability">{bits.join(" · ")}</p>;
}

export function OfferProvenance({ offer, edges }: ProvenanceProps) {
  const isRetire = offer.kind === "retire";
  return (
    <div className="prov">
      {isRetire ? (
        <RetireSnapshot offer={offer} />
      ) : (
        <>
          <DurabilityLine offer={offer} />
          <EvidenceList quotes={offer.evidence} conversationCount={offer.source_conversations.length} />
          <BridgeNote offer={offer} edges={edges} />
          <ScoreTermsBreakdown terms={offer.score_terms} score={offer.score} />
          <SimilarityNote offer={offer} />
        </>
      )}
      <footer className="prov-foot">
        {offer.artifact_sha256 ? (
          <>
            <span title={offer.artifact_sha256}>artifact {offer.artifact_sha256.slice(0, 6)}</span>
            <span className="prov-dot">·</span>
            <span>generated {fmtDate(offer.generated_at)}</span>
          </>
        ) : (
          <span>raised by the decay sweep {fmtDate(offer.created_at)}</span>
        )}
      </footer>
    </div>
  );
}
