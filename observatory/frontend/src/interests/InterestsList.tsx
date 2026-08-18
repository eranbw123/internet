/** The active-interests list: the funnel table, live.
 *
 * One row per interest, showing what it actually produced --
 * collected -> matched -> above bar -> delivered -- because the finding that
 * motivated this whole design is that some interests produce nothing and
 * nobody could see which. Five of them are dead weight; one collected 36 items
 * and cleared the bar zero times. A list that showed only names and bars would
 * hide exactly that.
 *
 * Three things this table is deliberate about:
 *
 *  1. `matched` is shown even though it is enormous (mean 10.7 matches per
 *     item; 97% of items match two or more interests). The gap between matched
 *     and collected IS the "matching is too loose" finding. Hiding it would
 *     make the table tidier and less true.
 *  2. `delivered` can exceed `above bar`, and the table says why rather than
 *     letting it read as a bug: the notifications went out under the bars in
 *     force at the time, and every bar rose by 0.08 on 2026-08-13, while
 *     `above bar` recomputes the window at today's bars.
 *  3. Lifecycle is four states, not two. An interest fades through `decaying`
 *     (30 idle days) before it is auto-paused (45), and a paused one can be
 *     revived in one click -- so the row shows where in that slide it is,
 *     while there is still time to act.
 */
import { useMemo, useState } from "react";
import type { InterestStat, Lifecycle, StatsResponse } from "./types";
import { isCollecting } from "./types";
import { BidiText, guessLang } from "./Bidi";

type SortKey = "title" | "collected" | "matched" | "above_bar" | "delivered" | "conversion" | "min_score";
type Filter = "collecting" | "dead" | "stopped" | "all";

interface Props {
  stats: StatsResponse | null;
  loading: boolean;
  onEdit: (row: InterestStat) => void;
  onRevive: (row: InterestStat) => void;
  busyKey: string | null;
}

function conversion(row: InterestStat): number {
  return row.collected === 0 ? 0 : row.above_bar / row.collected;
}

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

const LIFECYCLE_CHIP: Record<Lifecycle, string> = {
  active: "chip-ok",
  decaying: "chip-warn",
  paused: "chip-muted",
  retired: "chip-muted",
};

/** Daily above-bar counts across the window. Bars rather than a line: with six
 * points and plenty of zeros, a line implies interpolation between days that
 * did not happen. */
function Sparkline({ values, dead }: { values: number[]; dead: boolean }) {
  const max = Math.max(1, ...values);
  const label = values.join(", ");
  if (values.every((v) => v === 0)) {
    return <span className="spark spark-empty" title="no items above bar in this window">-</span>;
  }
  return (
    <span
      className={`spark ${dead ? "spark-dead" : ""}`}
      role="img"
      aria-label={`daily above-bar counts: ${label}`}
      title={`daily above-bar: ${label}`}
    >
      {values.map((v, i) => (
        <span
          key={i}
          className="spark-bar"
          style={{ height: `${Math.max(8, Math.round((v / max) * 100))}%` }}
        />
      ))}
    </span>
  );
}

export function InterestsList({ stats, loading, onEdit, onRevive, busyKey }: Props) {
  const [sort, setSort] = useState<SortKey>("collected");
  const [asc, setAsc] = useState(false);
  const [filter, setFilter] = useState<Filter>("collecting");
  const [query, setQuery] = useState("");

  const rows = useMemo(() => {
    const all = stats?.interests ?? [];
    let out = all.filter((r) => {
      if (filter === "collecting") return isCollecting(r.lifecycle);
      if (filter === "dead") return r.dead_weight;
      if (filter === "stopped") return !isCollecting(r.lifecycle);
      return true;
    });
    const q = query.trim().toLowerCase();
    if (q) out = out.filter((r) => r.key.toLowerCase().includes(q) || r.title.toLowerCase().includes(q));
    const dir = asc ? 1 : -1;
    return [...out].sort((a, b) => {
      if (sort === "title") return a.title.localeCompare(b.title) * dir;
      if (sort === "conversion") return (conversion(a) - conversion(b)) * dir;
      return ((a[sort] as number) - (b[sort] as number)) * dir;
    });
  }, [stats, sort, asc, filter, query]);

  function header(key: SortKey, label: string, title?: string) {
    const active = sort === key;
    return (
      <th
        scope="col"
        className={`sortable ${active ? "sorted" : ""}`}
        aria-sort={active ? (asc ? "ascending" : "descending") : "none"}
        title={title}
      >
        <button
          type="button"
          onClick={() => {
            if (active) setAsc(!asc);
            else { setSort(key); setAsc(false); }
          }}
        >
          {label}{active && <span className="sort-caret">{asc ? "^" : "v"}</span>}
        </button>
      </th>
    );
  }

  if (loading) return <p className="ws-loading">Loading interests...</p>;
  if (!stats) return null;

  const t = stats.totals;

  return (
    <div className="interests-list" data-testid="interests-list">
      <div className="list-summary">
        <div className="summary-funnel">
          <ol className="funnel-steps">
            <li><span className="funnel-n">{t.collected.toLocaleString()}</span><span className="funnel-l">collected</span></li>
            <li><span className="funnel-n">{t.matched.toLocaleString()}</span><span className="funnel-l">matched</span></li>
            <li><span className="funnel-n">{t.above_bar.toLocaleString()}</span><span className="funnel-l">above bar</span></li>
            <li><span className="funnel-n">{t.delivered.toLocaleString()}</span><span className="funnel-l">delivered</span></li>
          </ol>
          <p className="summary-note">
            {t.active_interests} of {t.total_interests} interests still collecting,
            {" "}{stats.from} to {stats.to}.
            {t.dead_weight > 0 && (
              <>
                {" "}
                <button type="button" className="link-button" onClick={() => setFilter("dead")}>
                  {t.dead_weight} are dead weight
                </button>.
              </>
            )}
          </p>
          <p className="summary-note prov-muted">
            Delivered exceeds above-bar because those notifications went out under the bars in
            force at the time; every bar rose by 0.08 on 2026-08-13, and above-bar is recounted
            at today's bars.
          </p>
        </div>
      </div>

      <div className="list-controls">
        <div className="filter-tabs" role="tablist" aria-label="Filter interests">
          {([
            ["collecting", `Collecting ${t.active_interests}`],
            ["dead", `Dead weight ${t.dead_weight}`],
            ["stopped", `Stopped ${t.total_interests - t.active_interests}`],
            ["all", `All ${t.total_interests}`],
          ] as [Filter, string][]).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={filter === value}
              className={filter === value ? "is-selected" : ""}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <input
          type="search"
          className="list-search"
          placeholder="Filter by key or title"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter by key or title"
        />
      </div>

      <div className="table-scroll">
        <table className="funnel-table">
          <thead>
            <tr>
              {header("title", "Interest")}
              <th scope="col">State</th>
              {header("min_score", "Bar")}
              {header("collected", "Collected", "items collected with this interest as origin")}
              {header("matched", "Matched", "items that keyword-matched this interest")}
              {header("above_bar", "Above bar", "scored items clearing the bar, recounted at today's bar")}
              {header("delivered", "Delivered", "notifications actually sent")}
              {header("conversion", "Conv.", "above bar / collected")}
              <th scope="col">Daily above bar</th>
              <th scope="col"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.key}
                className={`${row.dead_weight ? "row-dead" : ""} ${busyKey === row.key ? "is-busy" : ""}`}
                data-testid={`interest-row-${row.key}`}
              >
                <th scope="row" className="cell-interest">
                  <BidiText className="row-title" block lang={guessLang(row.title)}>{row.title}</BidiText>
                  <code className="key-chip">{row.key}</code>
                  {row.parent_key && <span className="prov-muted"> under {row.parent_key}</span>}
                </th>
                <td>
                  <span className={`chip ${LIFECYCLE_CHIP[row.lifecycle]}`}>{row.lifecycle}</span>
                  {row.lifecycle === "decaying" && row.silence_days !== null && (
                    <span className="cell-sub" title="auto-pause at 45 silent days">
                      {row.silence_days}d silent, pauses at 45
                    </span>
                  )}
                  {row.dead_weight && <span className="chip chip-error">dead weight</span>}
                </td>
                <td className="num">{row.min_score.toFixed(2)}</td>
                <td className="num">{row.collected.toLocaleString()}</td>
                <td className="num num-muted">{row.matched.toLocaleString()}</td>
                <td className={`num ${row.above_bar === 0 && row.collected > 0 ? "num-zero" : ""}`}>
                  {row.above_bar}
                </td>
                <td className="num">{row.delivered}</td>
                <td className="num">{row.collected ? pct(conversion(row)) : "-"}</td>
                <td><Sparkline values={row.daily_above_bar} dead={row.dead_weight} /></td>
                <td className="cell-actions">
                  <button type="button" className="btn btn-small" onClick={() => onEdit(row)}>
                    Edit
                  </button>
                  {(row.lifecycle === "paused" || row.lifecycle === "decaying") && (
                    <button
                      type="button"
                      className="btn btn-small"
                      disabled={busyKey === row.key}
                      title="undo the auto-pause and start the silence clock again"
                      onClick={() => onRevive(row)}
                    >
                      Revive
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && <p className="ws-empty">No interests match this filter.</p>}
    </div>
  );
}
