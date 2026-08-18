import { useCallback, useEffect, useState } from "react";
import { fetchInterests, listRows, type ListFilters } from "../api";
import type { InterestOption, ListResponse, Tab } from "../types";

const TABS: { key: Tab; label: string }[] = [
  { key: "discoveries", label: "Discoveries" },
  { key: "interests", label: "Interests" },
  { key: "generations", label: "Council generations" },
  { key: "missions", label: "Missions" },
  { key: "failed", label: "Failed runs" },
];

interface Props {
  onSelectDiscovery: (row: Record<string, unknown>, tab: Tab) => void;
  /** Omitted in public mode: the button opens "/" without the bearer
   * token, which lands on a 403 behind the tunnel. */
  onOpenRawDb?: () => void;
  /** rowKey() of the currently open row, so the list can show what is selected. */
  selectedRowKey?: string | null;
}

// Per-tab filter state. One shared object meant an interest typed on
// Discoveries silently constrained Generations and Missions too, and the
// discoveries-only trace_complete default rode along invisibly to tabs that
// ignore it -- so returning to Discoveries produced a result set the visible
// controls didn't explain.
const DEFAULT_FILTERS: Record<Tab, ListFilters> = {
  // Most rows in a live discovery.db predate the trace backbone and have no
  // graph behind them; showing them by default buries the inspectable ones.
  discoveries: { trace_complete: "yes" },
  interests: {},
  generations: {},
  missions: {},
  failed: {},
};

const SEARCH_DEBOUNCE_MS = 300;

export function Explorer({ onSelectDiscovery, onOpenRawDb, selectedRowKey }: Props) {
  const [tab, setTab] = useState<Tab>("discoveries");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [filtersByTab, setFiltersByTab] = useState<Record<Tab, ListFilters>>(DEFAULT_FILTERS);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(50);
  const [result, setResult] = useState<ListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [interests, setInterests] = useState<InterestOption[]>([]);
  const filters = filtersByTab[tab];

  const setFilters = useCallback((next: ListFilters) => {
    setFiltersByTab((prev) => ({ ...prev, [tab]: next }));
  }, [tab]);

  // The discoveries search runs LIKE over model_calls.exact_user_prompt --
  // 3,159 rows averaging 14kB, with no index. Firing that per keystroke was a
  // full scan per character typed.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [search]);

  // Fetched once: the filter is a picker now, not a guess-the-exact-key box.
  useEffect(() => {
    let cancelled = false;
    fetchInterests()
      .then((r) => {
        if (!cancelled) setInterests(r.interests);
      })
      .catch(() => {
        // A missing index just means the picker falls back to "all interests";
        // it must never take the whole explorer down with it.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setOffset(0);
  }, [tab, debouncedSearch, filters]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listRows(tab, { search: debouncedSearch, filters, limit, offset })
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
  }, [tab, debouncedSearch, filters, offset, limit]);

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
            {tab === t.key && result ? <span className="tab-count"> {result.total.toLocaleString()}</span> : null}
          </button>
        ))}
        {onOpenRawDb && (
          <button
            role="tab"
            onClick={onOpenRawDb}
            title="Open Datasette over every mounted database -- the trace tables, and any extra ones configured with DISCOVERY_UI_EXTRA_DBS (the raw conversations corpus)"
          >
            Raw databases
          </button>
        )}
      </div>
      <FilterBar tab={tab} filters={filters} interests={interests} onChange={setFilters} />
      <ActiveFilterChips tab={tab} filters={filters} onChange={setFilters} />
      {loading && <div className="explorer-status">Loading...</div>}
      {error && <div className="explorer-status error">{error}</div>}
      <ul className="explorer-rows">
        {result?.rows.map((row, i) => {
          const key = rowKey(tab, row, i);
          return (
            <li
              key={key}
              className={`explorer-row${selectedRowKey === key ? " selected" : ""}`}
              // Rows are activatable controls, so they answer to the keyboard
              // and announce their selected state, not just to the mouse.
              tabIndex={0}
              role="button"
              aria-current={selectedRowKey === key ? "true" : undefined}
              onClick={() => onSelectDiscovery(row, tab)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelectDiscovery(row, tab);
                } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  const sibling = e.key === "ArrowDown"
                    ? e.currentTarget.nextElementSibling
                    : e.currentTarget.previousElementSibling;
                  (sibling as HTMLElement | null)?.focus();
                }
              }}
            >
              <RowSummary tab={tab} row={row} />
            </li>
          );
        })}
        {result && result.rows.length === 0 && !loading && (
          <li className="explorer-empty">
            No rows match.
            {hasActiveFilters(tab, filters) && (
              <> <button className="connection-link" onClick={() => setFilters({})}>Clear filters</button></>
            )}
          </li>
        )}
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
    const sentState = row.notification_ok ? "sent" : row.notification_id ? "failed" : "not-sent";
    return (
      <>
        <div className="row-top">
          <span className="row-time">{at}</span>
          <span className="row-interest">{interest}</span>
        </div>
        <div className="row-title">{String(row.title ?? row.url ?? "(untitled)")}</div>
        <div className="row-bottom">
          <span className="row-score">{row.final_score != null ? Number(row.final_score).toFixed(2) : "unscored"}</span>
          <span className={`row-sent row-sent-${sentState}`}>{sentState === "not-sent" ? "not sent" : sentState}</span>
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
  // Each remaining tab reads a different table, so they get their own
  // templates rather than a shared one that fell back through
  // `label ?? key ?? title ?? id` and rendered a bare integer id for
  // generations, plus a `status` column interests does not even have.
  if (tab === "interests") {
    return (
      <>
        <div className="row-top">
          <span className="row-interest">{String(row.key ?? "")}</span>
          <span className={`row-active row-active-${row.active ? "yes" : "no"}`}>
            {row.active ? "active" : "inactive"}
          </span>
        </div>
        <div className="row-title">{String(row.title ?? row.key ?? "")}</div>
        <div className="row-bottom">
          <span>{String(row.layer ?? "")}</span>
          <span>{String(row.discoveries_count ?? 0)} discoveries</span>
          <span>{String(row.missions_count ?? 0)} missions</span>
        </div>
      </>
    );
  }
  if (tab === "generations") {
    return (
      <>
        <div className="row-top">
          <span className="row-time">{at}</span>
          <span className="row-interest">{interest}</span>
        </div>
        <div className="row-title">
          {String(row.missions_returned ?? 0)} of {String(row.missions_requested ?? 0)} missions
        </div>
        <div className="row-bottom">
          <span>{[row.provider, row.model].filter(Boolean).join("/")}</span>
          <span>{String(row.status ?? "")}</span>
          {row.error ? <span className="row-error">{String(row.error)}</span> : null}
        </div>
      </>
    );
  }
  return (
    <>
      <div className="row-top">
        <span className="row-time">{at}</span>
        <span className="row-interest">{interest}</span>
      </div>
      <div className="row-title">{String(row.label ?? row.rationale ?? row.id ?? "")}</div>
      <div className="row-bottom">
        <span>{String(row.status ?? "")}</span>
        {row.items_returned != null ? <span>{String(row.items_returned)} items</span> : null}
        {row.last_error ? <span className="row-error">{String(row.last_error)}</span> : null}
      </div>
    </>
  );
}

/** A stable identity per row so React keys survive reordering and the
 * selected row can be highlighted. Each tab's rows come from a different
 * table, so there is no single id column to rely on. */
export function rowKey(tab: Tab, row: Record<string, unknown>, index: number): string {
  const id = row.item_id ?? row.id ?? row.node_id ?? row.key;
  return id != null ? `${tab}:${String(id)}` : `${tab}:idx${index}`;
}

const FILTER_LABELS: Partial<Record<keyof ListFilters, string>> = {
  interest: "interest", sent: "telegram", trace_complete: "trace",
  failure_stage: "stage", active: "active", layer: "layer",
  provider: "provider", model: "model",
};

function hasActiveFilters(tab: Tab, filters: ListFilters): boolean {
  return Object.keys(activeEntries(tab, filters)).length > 0;
}

/** Filters worth showing as a dismissible chip -- i.e. everything currently
 * narrowing the list, including the trace_complete default, which was
 * previously applied invisibly. */
function activeEntries(_tab: Tab, filters: ListFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (value) out[key] = value;
  }
  return out;
}

function ActiveFilterChips({ tab, filters, onChange }: {
  tab: Tab; filters: ListFilters; onChange: (f: ListFilters) => void;
}) {
  const entries = Object.entries(activeEntries(tab, filters));
  if (entries.length === 0) return null;
  return (
    <div className="filter-chips">
      {entries.map(([key, value]) => (
        <button
          key={key}
          className="filter-chip"
          title={`Remove the ${key} filter`}
          onClick={() => onChange({ ...filters, [key]: undefined })}
        >
          {FILTER_LABELS[key as keyof ListFilters] ?? key}: {value} ✕
        </button>
      ))}
      <button className="filter-chip filter-chip-clear" onClick={() => onChange({})}>clear all</button>
    </div>
  );
}

function FilterBar({ tab, filters, interests, onChange }: {
  tab: Tab; filters: ListFilters; interests: InterestOption[]; onChange: (f: ListFilters) => void;
}) {
  function set(key: keyof ListFilters, value: string) {
    onChange({ ...filters, [key]: value || undefined });
  }
  return (
    <div className="explorer-filters">
      {/* A picker, not a free-text box. The filter is an exact, case-sensitive
          key match server-side (it.key = ?), and nothing in the UI ever listed
          the valid keys -- so any human-typed value silently returned "No rows
          match", which is indistinguishable from broken. */}
      {tab !== "interests" && (
        <select value={filters.interest || ""} onChange={(e) => set("interest", e.target.value)} aria-label="Interest">
          <option value="">all interests</option>
          {interests.map((i) => (
            <option key={i.key} value={i.key}>{i.title || i.key}{i.active ? "" : " (inactive)"}</option>
          ))}
        </select>
      )}
      {tab === "interests" && (
        <select value={filters.active || ""} onChange={(e) => set("active", e.target.value)} aria-label="Active state">
          <option value="">all interests</option>
          <option value="yes">active only</option>
          <option value="no">inactive only</option>
        </select>
      )}
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
