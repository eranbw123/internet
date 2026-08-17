import { useEffect, useState } from "react";
import { fetchInterest } from "../api";
import type { ID, InterestDetail } from "../types";

interface Props {
  interestKey: string;
  onClose?: () => void;
  /** Seed the graph on one of this interest's discoveries. */
  onSelectDiscovery: (itemId: ID) => void;
  /** The old behaviour, kept as an explicit secondary action: jump to whatever
   * trace this interest most recently appeared in. */
  onShowLatestTrace: () => void;
}

/** What an interest actually is and what it has produced.
 *
 * The backend has served this since the Observatory shipped -- definition,
 * signals, event timeline, generations, missions, discoveries, failures,
 * feedback -- and nothing ever called it: `fetchInterest` was exported and
 * imported by no component. Clicking an interest instead seeded the graph at
 * `{entity_type: 'interests', entity_id}`, which resolves to the newest trace
 * node carrying that entity -- a `match` node 13,763 times out of 13,857, i.e.
 * whichever candidate most recently keyword-matched. That is an arbitrary
 * trace, not an answer to "what is this interest doing?".
 */
export function InterestPanel({ interestKey, onClose, onSelectDiscovery, onShowLatestTrace }: Props) {
  const [detail, setDetail] = useState<InterestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setDetail(null);
    fetchInterest(interestKey)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [interestKey]);

  if (error) return <div className="inspector inspector-error">{error}</div>;
  if (!detail) return <div className="inspector inspector-loading">Loading...</div>;

  const definition = detail.definition as Record<string, unknown>;
  return (
    <div className="inspector interest-panel" data-testid="interest-panel">
      <div className="inspector-header">
        <div className="inspector-title">
          <span className="inspector-node-type">interest</span>
          <span className="inspector-label">{str(definition.title) || interestKey}</span>
        </div>
        {onClose && <button className="inspector-close" onClick={onClose}>Close</button>}
      </div>
      <div className="inspector-body">
        <dl className="overview-facts">
          <dt>Key</dt><dd>{interestKey}</dd>
          <dt>State</dt>
          <dd>
            <span className={`status-chip status-chip-${definition.active ? "ok" : "neutral"}`}>
              {definition.active ? "active" : "inactive"}
            </span>
          </dd>
          {str(definition.layer) && <><dt>Layer</dt><dd>{str(definition.layer)}</dd></>}
          {definition.min_score != null && <><dt>Min score</dt><dd>{str(definition.min_score)}</dd></>}
        </dl>
        {str(definition.description) && <p className="overview-summary">{str(definition.description)}</p>}

        <SignalList title="Positive signals" signals={detail.signals.positive} />
        <SignalList title="Negative signals" signals={detail.signals.negative} />

        <div className="interest-counts">
          <Count n={detail.discoveries.length} label="discoveries" />
          <Count n={detail.missions.length} label="missions" />
          <Count n={detail.generations.length} label="generations" />
          <Count n={detail.failures.length} label="failures" />
          <Count n={detail.feedback.length} label="feedback" />
        </div>

        <div className="interest-actions">
          <button onClick={onShowLatestTrace}>Show latest trace</button>
        </div>

        <RowSection title="Recent discoveries" rows={detail.discoveries.slice(0, 10)} render={(row) => (
          <button
            className="connection-link"
            onClick={() => onSelectDiscovery(row.item_id as ID)}
          >
            {str(row.title) || str(row.url) || `item ${str(row.item_id)}`}
          </button>
        )} meta={(row) => (row.final_score != null ? Number(row.final_score).toFixed(2) : "")} />

        <RowSection title="Recent missions" rows={detail.missions.slice(0, 10)}
          render={(row) => <span>{str(row.label) || str(row.rationale) || `mission ${str(row.id)}`}</span>}
          meta={(row) => str(row.status)} />

        <RowSection title="Recent failures" rows={detail.failures.slice(0, 10)}
          render={(row) => <span>{str(row.label) || str(row.kind)}</span>}
          meta={(row) => str(row.detail).slice(0, 60)} />

        <RowSection title="Feedback" rows={detail.feedback.slice(0, 10)}
          render={(row) => <span>{str(row.title)}</span>}
          meta={(row) => str(row.verdict)} />

        <RowSection title="Timeline" rows={detail.events.slice(-10).reverse()}
          render={(row) => <span>{str(row.event) || str(row.kind)}</span>}
          meta={(row) => str(row.at) || str(row.created_at)} />
      </div>
    </div>
  );
}

function Count({ n, label }: { n: number; label: string }) {
  return <span className="interest-count"><b>{n}</b> {label}</span>;
}

function SignalList({ title, signals }: { title: string; signals: unknown }) {
  const items = Array.isArray(signals) ? signals : [];
  if (items.length === 0) return null;
  return (
    <>
      <div className="overview-section-title">{title}</div>
      <ul className="signal-list">
        {items.map((s, i) => <li key={i}>{String(s)}</li>)}
      </ul>
    </>
  );
}

function RowSection({ title, rows, render, meta }: {
  title: string;
  rows: Record<string, unknown>[];
  render: (row: Record<string, unknown>) => React.ReactNode;
  meta?: (row: Record<string, unknown>) => string;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="overview-connections">
      <div className="overview-section-title">{title}</div>
      <ul>
        {rows.map((row, i) => (
          <li key={i}>
            {render(row)}
            {meta && <span className="interest-row-meta"> {meta(row)}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function str(value: unknown): string {
  return value == null ? "" : String(value);
}
