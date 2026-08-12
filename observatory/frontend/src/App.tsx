import { useEffect, useMemo, useState } from "react";
import { Explorer } from "./explorer/Explorer";
import { GraphCanvas } from "./graph/GraphCanvas";
import { Inspector } from "./inspector/Inspector";
import { CompareView } from "./compare/CompareView";
import { readBootstrap } from "./deepLink";
import type { GraphSeed } from "./graph/useGraphData";
import type { ID, Tab } from "./types";

const MOBILE_BREAKPOINT = 480; // iPhone-width class of device

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= MOBILE_BREAKPOINT);
  useEffect(() => {
    function onResize() {
      setIsMobile(window.innerWidth <= MOBILE_BREAKPOINT);
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return isMobile;
}

export function App() {
  const bootstrap = useMemo(() => readBootstrap(), []);
  const [seed, setSeed] = useState<GraphSeed | null>(
    bootstrap.focus?.kind === "score" ? { entity_type: "scores", entity_id: bootstrap.focus.score_id } : null,
  );
  const [selectedNodeId, setSelectedNodeId] = useState<ID | null>(bootstrap.focus?.node_id ?? null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const isMobile = useIsMobile();

  function selectDiscovery(row: Record<string, unknown>, tab: Tab) {
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
      }
    } else if (tab === "discoveries" && row.item_id != null) {
      setSeed({ entity_type: "candidate_items", entity_id: row.item_id as string | number });
    } else if (tab === "interests" && row.id != null) {
      setSeed({ entity_type: "interests", entity_id: row.id as string | number });
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
    if (isMobile) setSheetOpen(true);
  }

  function openRawDb() {
    window.open("/", "_blank", "noopener");
  }

  return (
    <div className={`app ${isMobile ? "mobile" : "desktop"}`} data-testid="app">
      <header className="app-header">
        <button className="drawer-toggle" onClick={() => setDrawerOpen((v) => !v)} aria-label="Toggle explorer">☰</button>
        <span className="app-title">Observatory</span>
        <button onClick={() => setCompareOpen((v) => !v)}>{compareOpen ? "Close compare" : "Compare"}</button>
      </header>
      <div className="app-body">
        <div className={`pane pane-explorer ${isMobile ? "drawer" : ""} ${drawerOpen ? "open" : ""}`}>
          <Explorer onSelectDiscovery={selectDiscovery} onOpenRawDb={openRawDb} />
        </div>
        {isMobile && drawerOpen && <div className="drawer-scrim" onClick={() => setDrawerOpen(false)} />}
        <div className="pane pane-graph">
          {compareOpen ? (
            <CompareView onClose={() => setCompareOpen(false)} />
          ) : (
            <GraphCanvas seed={seed} selectedNodeId={selectedNodeId} onSelectNode={selectNode} />
          )}
        </div>
        {!isMobile && (
          <div className="pane pane-inspector">
            <Inspector nodeId={selectedNodeId} />
          </div>
        )}
      </div>
      {isMobile && (
        <div className={`bottom-sheet ${sheetOpen ? "open" : ""}`} data-testid="bottom-sheet">
          <div className="bottom-sheet-handle" onClick={() => setSheetOpen(false)} />
          <Inspector nodeId={selectedNodeId} onClose={() => setSheetOpen(false)} />
        </div>
      )}
    </div>
  );
}
