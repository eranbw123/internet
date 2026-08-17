import { useEffect, useMemo, useState } from "react";
import { Explorer, rowKey } from "./explorer/Explorer";
import { GraphCanvas } from "./graph/GraphCanvas";
import { Inspector } from "./inspector/Inspector";
import { InterestPanel } from "./interest/InterestPanel";
import { CompareView } from "./compare/CompareView";
import { readBootstrap } from "./deepLink";
import { useIsMobile } from "./useIsMobile";
import type { GraphSeed } from "./graph/useGraphData";
import type { ID, Tab } from "./types";

const MIN_INSPECTOR_WIDTH = 280;
const INSPECTOR_WIDTH_KEY = "observatory-inspector-width";

export function App() {
  const bootstrap = useMemo(() => readBootstrap(), []);
  const [seed, setSeed] = useState<GraphSeed | null>(
    bootstrap.focus?.kind === "score" ? { entity_type: "scores", entity_id: bootstrap.focus.score_id } : null,
  );
  const [selectedNodeId, setSelectedNodeId] = useState<ID | null>(bootstrap.focus?.node_id ?? null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [selectedRowKey, setSelectedRowKey] = useState<string | null>(null);
  // Both halves, because they address different things: the panel is fetched
  // by key (/api/interest/<key>), while trace_nodes carry the numeric id as
  // their entity_id.
  const [selectedInterest, setSelectedInterest] = useState<{ key: string; id: ID } | null>(null);
  const isMobile = useIsMobile();
  const [inspectorWidth, setInspectorWidth] = useState(() => {
    const saved = Number(localStorage.getItem(INSPECTOR_WIDTH_KEY));
    return Number.isFinite(saved) && saved >= MIN_INSPECTOR_WIDTH ? saved : 380;
  });
  const [resizing, setResizing] = useState(false);

  useEffect(() => {
    localStorage.setItem(INSPECTOR_WIDTH_KEY, String(inspectorWidth));
  }, [inspectorWidth]);

  function startInspectorResize(e: React.PointerEvent) {
    e.preventDefault();
    setResizing(true);
    const onMove = (ev: PointerEvent) => {
      // The inspector is the rightmost pane, so its width is the distance
      // from the pointer to the right window edge; keep enough room for the
      // graph pane no matter how far left the user drags.
      const max = Math.max(MIN_INSPECTOR_WIDTH, window.innerWidth - 480);
      setInspectorWidth(Math.min(Math.max(window.innerWidth - ev.clientX, MIN_INSPECTOR_WIDTH), max));
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function selectDiscovery(row: Record<string, unknown>, tab: Tab) {
    setSelectedRowKey(rowKey(tab, row, -1));
    if (tab !== "interests") setSelectedInterest(null);
    // Each explorer tab's row.id is a primary key from a DIFFERENT table
    // (interests.id, search_generations.id, search_missions.id, ...) --
    // entity_id is only unique WITHIN one entity_type (see db.py's
    // _seed_node_ids / PROJECT_STATE.md's task-2 repair pass 1 for the
    // server-side version of this same bug class), so the seed's
    // entity_type must be chosen per-tab, never inferred from row shape
    // alone. The "failed" tab's rows already carry their own resolved
    // entity_type/entity_id (see db.py's _FAILED_UNION) -- use those as-is.
    if (tab === "failed") {
      if (row.entity_type != null && row.entity_id != null) {
        setSeed({ entity_type: row.entity_type as string, entity_id: row.entity_id as string | number });
      } else if (row.run_id != null) {
        // A few _FAILED_UNION kinds (duplicate, prefilter_rejected -- see
        // observatory/db.py) carry no entity link at all, only a bare
        // node_id/run_id -- verified live that clicking one of these rows
        // previously did nothing whatsoever, since entity_type/entity_id
        // both being null failed the branch above silently. fetchGraph
        // already accepts a run_id-only seed (the deep-link path uses the
        // same shape), so falling back to it here still loads the row's own
        // graph, with the Inspector opened directly on its node.
        setSeed({ run_id: row.run_id as number });
        setSelectedNodeId(row.node_id as ID);
        if (isMobile) {
          setDrawerOpen(false);
          // This branch sets selection state directly instead of going through
          // selectNode(), so it also has to open the sheet -- otherwise the
          // Inspector content existed but stayed invisible and the tap read as
          // "nothing happened".
          setSheetOpen(true);
        }
        return;
      }
    } else if (tab === "discoveries" && row.item_id != null) {
      setSeed({ entity_type: "candidate_items", entity_id: row.item_id as string | number });
    } else if (tab === "interests" && row.key != null && row.id != null) {
      // Opens the interest itself rather than seeding the graph. Seeding
      // resolved to the newest trace node carrying this entity, which is a
      // `match` node 13,763 times out of 13,857 -- i.e. whichever candidate
      // most recently keyword-matched, an arbitrary trace. The panel keeps a
      // "Show latest trace" button for when that IS what you wanted.
      setSelectedInterest({ key: row.key as string, id: row.id as ID });
      setSelectedNodeId(null);
      if (isMobile) {
        setDrawerOpen(false);
        setSheetOpen(true);
      }
      return;
    } else if (tab === "generations" && row.id != null) {
      setSeed({ entity_type: "search_generations", entity_id: row.id as string | number });
    } else if (tab === "missions" && row.id != null) {
      setSeed({ entity_type: "search_missions", entity_id: row.id as string | number });
    }
    setSelectedNodeId(null);
    if (isMobile) setDrawerOpen(false);
  }

  function selectNode(id: ID) {
    setSelectedNodeId(id);
    setSelectedInterest(null);
    if (isMobile) setSheetOpen(true);
  }

  /** The right-hand pane shows an interest when one is open, otherwise the
   * node inspector -- one pane, two subjects, never both at once. */
  function rightPane(onClose?: () => void) {
    if (selectedInterest) {
      return (
        <InterestPanel
          interestKey={selectedInterest.key}
          onClose={onClose}
          onSelectDiscovery={(itemId) => {
            setSelectedInterest(null);
            setSeed({ entity_type: "candidate_items", entity_id: itemId });
            setSelectedNodeId(null);
          }}
          onShowLatestTrace={() => {
            setSeed({ entity_type: "interests", entity_id: selectedInterest.id });
            setSelectedInterest(null);
          }}
        />
      );
    }
    return <Inspector nodeId={selectedNodeId} onClose={onClose} onSelectNode={selectNode} />;
  }

  function openRawDb() {
    window.open("/", "_blank", "noopener");
  }

  return (
    <div className={`app ${isMobile ? "mobile" : "desktop"} ${resizing ? "resizing" : ""}`} data-testid="app">
      <header className="app-header">
        <button className="drawer-toggle" onClick={() => setDrawerOpen((v) => !v)} aria-label="Toggle explorer">☰</button>
        <span className="app-title">Observatory</span>
        <button onClick={() => setCompareOpen((v) => !v)}>{compareOpen ? "Close compare" : "Compare"}</button>
      </header>
      <div className="app-body">
        <div className={`pane pane-explorer ${isMobile ? "drawer" : ""} ${drawerOpen ? "open" : ""}`}>
          <Explorer onSelectDiscovery={selectDiscovery} onOpenRawDb={openRawDb} selectedRowKey={selectedRowKey} />
        </div>
        {isMobile && drawerOpen && <div className="drawer-scrim" onClick={() => setDrawerOpen(false)} />}
        <div className="pane pane-graph">
          {compareOpen ? (
            <CompareView onClose={() => setCompareOpen(false)} />
          ) : (
            <GraphCanvas seed={seed} selectedNodeId={selectedNodeId} onSelectNode={selectNode} isMobile={isMobile} />
          )}
        </div>
        {!isMobile && (
          <>
            <div className="pane-resizer" title="Drag to resize" onPointerDown={startInspectorResize} />
            <div className="pane pane-inspector" style={{ width: inspectorWidth }}>
              {rightPane()}
            </div>
          </>
        )}
      </div>
      {isMobile && (
        <div className={`bottom-sheet ${sheetOpen ? "open" : ""}`} data-testid="bottom-sheet">
          <div className="bottom-sheet-handle" onClick={() => setSheetOpen(false)} />
          {rightPane(() => setSheetOpen(false))}
        </div>
      )}
    </div>
  );
}
