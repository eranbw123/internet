import { useEffect, useState } from "react";
import { listRows, type ListFilters } from "../api";
import type { ListResponse, Tab } from "../types";

const TABS: { key: Tab; label: string }[] = [
  { key: "discoveries", label: "Discoveries" },
  { key: "interests", label: "Interests" },
  { key: "generations", label: "Council generations" },
  { key: "missions", label: "Missions" },
  { key: "failed", label: "Failed runs" },
];

interface Props {
  onSelectDiscovery: (row: Record<string, unknown>) => void;
  onOpenRawDb: () => void;
}

export function Explorer({ onSelectDiscovery, onOpenRawDb }: Props) {
  const [tab, setTab] = useState<Tab>("discoveries");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<ListFilters>({});
  const [offset, setOffset] = useState(0);
  const [limit] = useState(50);
  const [result, setResult] = useState<ListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setOffset(0);
  }, [tab, search, filters]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listRows(tab, { search, filters, limit, offset })
      .then((r) => {
        if (!cancelled) setResult(r);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, search, filters, offset, limit]);

  return (
    <div className="explorer" data-testid="explorer">
      <div className="explorer-search">
        <input
          type="search"
          placeholder="Search title, url, prompt text..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div className="explorer-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? "active" : ""}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
        <button role="tab" onClick={onOpenRawDb}>Raw database</button>
      </div>
      <FilterBar tab={tab} filters={filters} onChange={setFilters} />
      {loading && <div className="explorer-status">Loading...</div>}
      {error && <div className="explorer-status error">{error}</div>}
      <ul className="explorer-rows">
        {result?.rows.map((row, i) => (
          <li key={i} className="explorer-row" onClick={() => onSelectDiscovery(row)}>
            <RowSummary tab={tab} row={row} />
          </li>
        ))}
        {result && result.rows.length === 0 && !loading && <li className="explorer-empty">No rows match.</li>}
      </ul>
      {result && (
        <div className="explorer-pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Prev</button>
          <span>{offset + 1}-{Math.min(offset + limit, result.total)} of {result.total}</span>
          <button disabled={offset + limit >= result.total} onClick={() => setOffset(offset + limit)}>Next</button>
        </div>
      )}
    </div>
  );
}

function RowSummary({ tab, row }: { tab: Tab; row: Record<string, unknown> }) {
  const at = (row.scored_at || row.created_at || row.at || row.first_seen_at || "") as string;
  const interest = (row.interest_key || row.generation_interest_key || "") as string;
  if (tab === "discoveries") {
    return (
      <>
        <div className="row-top">
          <span className="row-time">{at}</span>
          <span className="row-interest">{interest}</span>
        </div>
        <div className="row-title">{String(row.title ?? row.url ?? "(untitled)")}</div>
        <div className="row-bottom">
          <span className="row-score">{row.final_score != null ? Number(row.final_score).toFixed(2) : "unscored"}</span>
          <span className="row-sent">{row.notification_ok ? "sent" : row.notification_id ? "failed" : "not sent"}</span>
          <span className="row-feedback">{String(row.feedback_verdict ?? "")}</span>
        </div>
      </>
    );
  }
  if (tab === "failed") {
    return (
      <>
        <div className="row-top">
          <span className="row-time">{at}</span>
          <span className="row-kind">{String(row.kind ?? "")}</span>
        </div>
        <div className="row-title">{String(row.label ?? "")}</div>
        <div className="row-bottom">{String(row.detail ?? "")}</div>
      </>
    );
  }
  return (
    <>
      <div className="row-top">
        <span className="row-time">{at}</span>
        <span className="row-interest">{interest}</span>
      </div>
      <div className="row-title">{String(row.label ?? row.key ?? row.title ?? row.id ?? "")}</div>
      <div className="row-bottom">{String(row.status ?? "")}</div>
    </>
  );
}

function FilterBar({ tab, filters, onChange }: { tab: Tab; filters: ListFilters; onChange: (f: ListFilters) => void }) {
  function set(key: keyof ListFilters, value: string) {
    onChange({ ...filters, [key]: value || undefined });
  }
  return (
    <div className="explorer-filters">
      <input placeholder="interest key" value={filters.interest || ""} onChange={(e) => set("interest", e.target.value)} />
      {tab === "discoveries" && (
        <>
          <select value={filters.sent || ""} onChange={(e) => set("sent", e.target.value)}>
            <option value="">any Telegram state</option>
            <option value="yes">sent</option>
            <option value="no">not sent</option>
          </select>
          <select value={filters.trace_complete || ""} onChange={(e) => set("trace_complete", e.target.value)}>
            <option value="">any trace state</option>
            <option value="yes">trace complete</option>
            <option value="no">trace missing</option>
          </select>
        </>
      )}
      {tab === "failed" && (
        <select value={filters.failure_stage || ""} onChange={(e) => set("failure_stage", e.target.value)}>
          <option value="">any failure stage</option>
          <option value="scoring_error">scoring error</option>
          <option value="mission_failed">mission failed</option>
          <option value="generation_failed">generation failed</option>
          <option value="duplicate">duplicate</option>
          <option value="prefilter_rejected">prefilter rejected</option>
          <option value="below_threshold">below threshold</option>
        </select>
      )}
    </div>
  );
}
