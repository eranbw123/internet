/** The interests workspace: one surface for everything the owner does to the
 * interest set.
 *
 * A fourth top-level surface beside the trace explorer, in its own directory,
 * so that landing it alongside a concurrent frontend rewrite touches almost
 * nothing shared: the only edits outside src/interests/ are one nav entry in
 * App.tsx and, later, one route registration for PR J's endpoints.
 *
 * This component owns the data and every write, and the three views below it
 * are presentational. That is not ceremony -- the decision orchestration is
 * genuinely non-trivial for retirement offers, and it belongs in one readable
 * place rather than smeared across the cards that trigger it:
 *
 *   accept a normal offer   -> decide(accept), and the interest exists
 *   accept a retire offer   -> decide(accept) marks the OFFER decided, but the
 *                              interest it names still has to move to
 *                              `retired`. PR H's accept() builds an interest
 *                              entry from the offer's own key, which for a
 *                              retirement offer is `retire:<something>` -- not
 *                              a real interest -- so the lifecycle move is a
 *                              separate, explicit call. It is issued only if
 *                              the refreshed state shows the interest did not
 *                              already move, so this stays correct whether or
 *                              not PR J's decide endpoint handles it too.
 *   lower the bar instead   -> reject the retirement proposal, then reactivate
 *                              the interest at the new bar
 *   keep watching           -> reject the proposal and undo the auto-pause
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { InterestEdge, InterestPayload, InterestStat, Offer, StatsResponse } from "./types";
import { retireTargetKey } from "./types";
import { client, isMockActive } from "./client";
import { InterestsList } from "./InterestsList";
import { OffersInbox, type OfferDecision } from "./OffersInbox";
import { ConnectionsView } from "./ConnectionsView";
import { InterestEditor, type EditorSubject } from "./InterestEditor";
import "./tokens.fallback.css";
import "./interests.css";

type View = "list" | "offers" | "connections";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function InterestsWorkspace({ onClose }: { onClose?: () => void }) {
  const [view, setView] = useState<View>("list");
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [offers, setOffers] = useState<Offer[]>([]);
  const [edges, setEdges] = useState<InterestEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyOfferId, setBusyOfferId] = useState<number | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [offerErrors, setOfferErrors] = useState<Record<number, string>>({});
  const [editor, setEditor] = useState<EditorSubject | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [s, o, e] = await Promise.all([
      client.interestStats("7d"),
      client.listOffers("offered"),
      // Edges are loaded up front rather than per-view: the inbox needs them
      // too, because a bridge offer's lift lives on the edge and not on the
      // offer row.
      client.listEdges(0),
    ]);
    setStats(s);
    setOffers(o.offers);
    setEdges(e.edges);
    return s;
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    refresh()
      .then(() => { if (!cancelled) setLoadError(null); })
      .catch((err) => { if (!cancelled) setLoadError(message(err)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refresh]);

  const existingKeys = useMemo(() => (stats?.interests ?? []).map((i) => i.key), [stats]);
  const parentOptions = useMemo(
    () => (stats?.interests ?? []).map((i) => ({ key: i.key, title: i.title })),
    [stats],
  );

  function note(text: string) {
    setToast(text);
    window.setTimeout(() => setToast((t) => (t === text ? null : t)), 6000);
  }

  const decide = useCallback(async (d: OfferDecision) => {
    setBusyOfferId(d.offer.id);
    setOfferErrors((e) => ({ ...e, [d.offer.id]: "" }));
    try {
      if (d.action === "accept") {
        const res = await client.decideOffer(d.offer.id, { action: "accept" });
        note(`Accepted ${res.interest_key ?? d.offer.key}. It starts collecting on the next cycle.`);
      } else if (d.action === "snooze") {
        await client.decideOffer(d.offer.id, { action: "snooze" });
        note(`Snoozed ${d.offer.key} for 30 days.`);
      } else if (d.action === "reject") {
        const res = await client.decideOffer(d.offer.id, { action: "reject" });
        const blocked = res.blocked_terms?.length ?? 0;
        note(`Rejected ${d.offer.key}${blocked ? ` and blocked ${blocked} terms for 180 days` : ""}.`);
      } else if (d.action === "retire") {
        const target = retireTargetKey(d.offer);
        await client.decideOffer(d.offer.id, { action: "accept", note: "retire confirmed" });
        const fresh = await client.interestStats("7d");
        const row = fresh.interests.find((i) => i.key === target);
        if (row && row.lifecycle !== "retired") {
          await client.updateInterest(target, { lifecycle: "retired" });
        }
        note(`Retired ${target}. It keeps its history and stops collecting.`);
      } else if (d.action === "lower-bar") {
        const target = retireTargetKey(d.offer);
        await client.decideOffer(d.offer.id, { action: "reject", note: "lowered the bar instead" });
        await client.updateInterest(target, { lifecycle: "active", min_score: d.minScore });
        note(`Kept ${target} at a bar of ${d.minScore?.toFixed(2)}.`);
      } else if (d.action === "keep-watching") {
        const target = retireTargetKey(d.offer);
        await client.decideOffer(d.offer.id, { action: "reject", note: "keep watching" });
        await client.updateInterest(target, { lifecycle: "active" });
        note(`Kept ${target}. The silence clock starts again.`);
      }
      await refresh();
    } catch (err) {
      setOfferErrors((e) => ({ ...e, [d.offer.id]: message(err) }));
    } finally {
      setBusyOfferId(null);
    }
  }, [refresh]);

  /** Opening the editor needs the interest's description and signal lists,
   * which the bulk stats payload does not carry. Fetch them FIRST and open the
   * editor only once they are in hand: an editor that opens empty and fills in
   * a moment later would let a fast save write those empty fields back, wiping
   * the signals the interest matches on. */
  const openEditor = useCallback(async (row: InterestStat) => {
    setBusyKey(row.key);
    setEditorError(null);
    try {
      const detail = await client.interestDetail(row.key);
      setEditor({ mode: "edit", interest: row, detail });
    } catch (err) {
      setLoadError(`Could not load ${row.key}: ${message(err)}`);
    } finally {
      setBusyKey(null);
    }
  }, []);

  const revive = useCallback(async (row: InterestStat) => {
    setBusyKey(row.key);
    try {
      await client.updateInterest(row.key, { lifecycle: "active" });
      note(`${row.key} is collecting again.`);
      await refresh();
    } catch (err) {
      setLoadError(message(err));
    } finally {
      setBusyKey(null);
    }
  }, [refresh]);

  const save = useCallback(async (payload: InterestPayload) => {
    if (!editor) return;
    setSaving(true);
    setEditorError(null);
    try {
      if (editor.mode === "offer") {
        // Edit-then-accept. The edits allowlist is PR H's: everything below is
        // accepted, and `lifecycle` deliberately is not sent -- accept() sets
        // the interest active itself and rejects unknown edit fields.
        await client.decideOffer(editor.offer.id, {
          action: "accept",
          edits: {
            title: payload.title,
            description: payload.description,
            positive_signals: payload.positive_signals,
            negative_signals: payload.negative_signals,
            min_score: payload.min_score,
            sources: payload.sources,
            parent_key: payload.parent_key,
          },
        });
        note(`Accepted ${payload.key} with your edits.`);
      } else if (editor.mode === "create") {
        await client.createInterest(payload);
        note(`Created ${payload.key}.`);
      } else {
        const res = await client.updateInterest(payload.key, payload);
        note(
          `Saved ${payload.key}. Synced at ${res.synced_at.slice(11, 16)}`
          + (res.missions_cancelled ? `, ${res.missions_cancelled} pending missions cancelled.` : "."),
        );
      }
      setEditor(null);
      await refresh();
    } catch (err) {
      setEditorError(message(err));
    } finally {
      setSaving(false);
    }
  }, [editor, refresh]);

  const editorScores = useMemo(() => {
    if (!editor) return [];
    if (editor.mode === "edit") {
      return stats?.interests.find((i) => i.key === editor.interest.key)?.recent_scores ?? [];
    }
    return [];
  }, [editor, stats]);

  const offeredCount = offers.length;
  const totals = stats?.totals;

  return (
    <div className="interests-workspace" data-testid="interests-workspace">
      <header className="ws-header">
        <h1 className="ws-title">Interests</h1>
        <nav className="ws-tabs" role="tablist" aria-label="Interests views">
          <button
            role="tab" aria-selected={view === "list"} type="button"
            className={view === "list" ? "is-selected" : ""}
            onClick={() => setView("list")}
          >
            Active <span className="tab-count">{totals?.active_interests ?? "-"}</span>
          </button>
          <button
            role="tab" aria-selected={view === "offers"} type="button"
            className={view === "offers" ? "is-selected" : ""}
            onClick={() => setView("offers")}
          >
            Offers <span className={`tab-count ${offeredCount ? "tab-count-live" : ""}`}>{offeredCount}</span>
          </button>
          <button
            role="tab" aria-selected={view === "connections"} type="button"
            className={view === "connections" ? "is-selected" : ""}
            onClick={() => setView("connections")}
          >
            Connections <span className="tab-count">{edges.length}</span>
          </button>
        </nav>
        <div className="ws-header-actions">
          <button type="button" className="btn btn-small" onClick={() => setEditor({ mode: "create" })}>
            New interest
          </button>
          {onClose && (
            <button type="button" className="btn btn-small btn-quiet" onClick={onClose}>
              Close
            </button>
          )}
        </div>
      </header>

      {isMockActive() && (
        <p className="ws-mock-banner" role="status">
          <strong>Fixture data.</strong> The write API (PR J) is not wired up yet, so this
          workspace is running on the documented mock client: the funnel numbers are the real
          measured ones, but decisions are held in memory and nothing is written to the database.
        </p>
      )}

      {loadError && <p className="ws-error" role="alert">{loadError}</p>}
      {toast && <p className="ws-toast" role="status">{toast}</p>}

      <div className="ws-body">
        {view === "list" && (
          <InterestsList
            stats={stats}
            loading={loading}
            busyKey={busyKey}
            onEdit={openEditor}
            onRevive={revive}
          />
        )}
        {view === "offers" && (
          <OffersInbox
            offers={offers}
            edges={edges}
            loading={loading}
            busyId={busyOfferId}
            errors={offerErrors}
            onDecide={decide}
            onEdit={(offer) => { setEditorError(null); setEditor({ mode: "offer", offer }); }}
          />
        )}
        {view === "connections" && (
          <ConnectionsView edges={edges} interests={stats?.interests ?? []} loading={loading} />
        )}
      </div>

      {editor && (
        <InterestEditor
          subject={editor}
          recentScores={editorScores}
          existingKeys={existingKeys}
          parentOptions={parentOptions}
          saving={saving}
          error={editorError}
          onCancel={() => setEditor(null)}
          onSave={save}
        />
      )}
    </div>
  );
}
