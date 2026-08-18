import { useEffect, useMemo, useState } from "react";
import { Explorer, rowKey } from "./explorer/Explorer";
import { GraphCanvas } from "./graph/GraphCanvas";
import { Inspector } from "./inspector/Inspector";
import { InterestPanel } from "./interest/InterestPanel";
import { CompareView } from "./compare/CompareView";
import { InterestsWorkspace } from "./interests/InterestsWorkspace";
import { formatHash, parseHash, readBootstrap } from "./deepLink";
import { useIsMobile } from "./useIsMobile";
import { MobileNav, type MobileSurface } from "./MobileNav";
import { useScrollCollapse } from "./useScrollCollapse";
import { ThemeToggle } from "./ThemeToggle";
import type { GraphSeed } from "./graph/useGraphData";
import type { ID, Tab } from "./types";

const MIN_INSPECTOR_WIDTH = 280;

/** The explorer's three desktop panes, as the three positions of the phone's
 * segmented switcher. Order matters: it reads left-to-right as the journey
 * through the app -- pick a result, look at its trace, read the detail. */
type ExplorePane = "results" | "graph" | "details";
const PANES: ExplorePane[] = ["results", "graph", "details"];
const PANE_LABEL: Record<ExplorePane, string> = {
  results: "Results",
  graph: "Trace",
  details: "Details",
};
const SURFACE_TITLE: Record<MobileSurface, string> = {
  explore: "Explore",
  interests: "Interests",
  offers: "Suggested for you",
  connections: "Connections",
  compare: "Compare",
};
const INSPECTOR_WIDTH_KEY = "observatory-inspector-width";

export function App() {
  const bootstrap = useMemo(() => readBootstrap(), []);
  // A hash wins over the server-rendered bootstrap: it is the more specific
  // thing the user actually navigated to (a shared link, or their own reload).
  const initialHash = useMemo(() => parseHash(window.location.hash), []);
  const [seed, setSeed] = useState<GraphSeed | null>(
    initialHash.seed
      ?? (bootstrap.focus?.kind === "score" ? { entity_type: "scores", entity_id: bootstrap.focus.score_id } : null),
  );
  const [selectedNodeId, setSelectedNodeId] = useState<ID | null>(
    initialHash.nodeId ?? bootstrap.focus?.node_id ?? null,
  );
  const [compareOpen, setCompareOpen] = useState(false);
  // The interests workspace is a full surface, not a pane: it replaces the
  // explorer/graph/inspector layout while open (see PR L). Kept as one piece
  // of state and one conditional below, so the concurrent frontend rewrite has
  // the smallest possible thing to rebase.
  const [interestsOpen, setInterestsOpen] = useState(
    () => new URLSearchParams(window.location.search).has("interests"),
  );
  const [compareRunId, setCompareRunId] = useState<string | null>(null);
  // The phone's navigation state. On a phone the app is one full-screen
  // surface at a time driven by the bottom tab bar (MobileNav), and the
  // explorer's three desktop panes become three positions of a segmented
  // switcher -- see the `mobile navigation` comment on the render below.
  const [surface, setSurface] = useState<MobileSurface>(
    () => (new URLSearchParams(window.location.search).has("interests") ? "interests" : "explore"),
  );
  const [explorePane, setExplorePane] = useState<ExplorePane>("results");
  const [offerCount, setOfferCount] = useState<number | null>(null);
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

  // Keep the URL describing what is on screen, so a reload restores it and the
  // view can be linked to. replaceState, not a hash assignment: this reflects
  // state rather than navigating, and shouldn't stack history entries.
  useEffect(() => {
    const hash = formatHash({
      seed: seed
        ? { entity_type: seed.entity_type, entity_id: seed.entity_id != null ? String(seed.entity_id) : undefined, run_id: seed.run_id }
        : null,
      nodeId: selectedNodeId != null ? String(selectedNodeId) : null,
    });
    if (hash !== window.location.hash) {
      window.history.replaceState(null, "", hash || window.location.pathname);
    }
  }, [seed, selectedNodeId]);

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
    setSelectedInterest(null);
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
        // This branch sets selection state directly instead of going through
        // selectNode(), so it also has to move the phone to the pane that
        // shows the result -- otherwise the Inspector content existed but
        // stayed invisible and the tap read as "nothing happened".
        if (isMobile) setExplorePane("details");
        return;
      }
    } else if (tab === "discoveries" && row.item_id != null) {
      setSeed({ entity_type: "candidate_items", entity_id: row.item_id as string | number });
    } else if (tab === "extractor") {
      // An extractor run is not a traced entity -- it has no trace_nodes row
      // to seed a graph from. The offers it produced are the thing worth
      // looking at, and they live in the inbox, so the row goes there.
      setSelectedInterest(null);
      setSelectedNodeId(null);
      if (isMobile) setSurface("offers");
      else setInterestsOpen(true);
      return;
    } else if (tab === "generations" && row.id != null) {
      setSeed({ entity_type: "search_generations", entity_id: row.id as string | number });
    } else if (tab === "missions" && row.id != null) {
      setSeed({ entity_type: "search_missions", entity_id: row.id as string | number });
    }
    setSelectedNodeId(null);
    // A trace row seeds the graph, so that is the pane worth showing.
    if (isMobile) setExplorePane("graph");
  }

  function selectNode(id: ID) {
    setSelectedNodeId(id);
    setSelectedInterest(null);
    if (isMobile) setExplorePane("details");
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
    return (
      <Inspector
        nodeId={selectedNodeId}
        onClose={onClose}
        onSelectNode={selectNode}
        onCompareRun={(runId) => {
          setCompareRunId(String(runId));
          setCompareOpen(true);
        }}
      />
    );
  }

  function openRawDb() {
    window.open("/", "_blank", "noopener");
  }

  /* --- mobile navigation ---------------------------------------------------

     A phone gets a different information architecture, not a squeezed copy of
     the desktop one. Measured on a 393x852 iPhone 15, the desktop shell put
     the whole app behind a 32x26 hamburger and opened on an empty graph
     canvas reading "Select a discovery from the list" -- a first screen with
     no content on it at all.

     The model here is the platform-native one:

       * a bottom tab bar carries the five destinations (MobileNav), so the
         offers inbox -- the surface the owner most wants on the sofa -- is one
         thumb tap from anywhere, with its pending count always visible;
       * the explorer's three desktop panes become three positions of a
         segmented switcher, one full-screen pane at a time. Selecting a row
         advances the switcher itself (a trace row -> Graph, an interest ->
         Details), so navigation is automatic going in and the switcher is the
         way back. That replaces the old off-canvas drawer + bottom sheet,
         which had no visible back affordance and left a dead 118px strip of
         graph beside the drawer.

     The interests workspace stays mounted for the whole session on a phone:
     switching tabs is then instant rather than a refetch and a spinner, and
     the tab bar can show the offer count from launch instead of only after
     you visit the inbox. */
  // Declared unconditionally (hooks cannot sit behind the isMobile branch);
  // `enabled` makes it inert on a desktop, where nothing collapses.
  const chromeCollapsed = useScrollCollapse(isMobile, surface + ":" + explorePane);

  if (isMobile) {
    const wsView = surface === "offers" ? "offers" : surface === "connections" ? "connections" : "list";
    const onWorkspace = surface === "interests" || surface === "offers" || surface === "connections";
    return (
      <div
        className={`app mobile ${chromeCollapsed ? "is-chrome-collapsed" : ""}`}
        data-testid="app"
      >
        <header className="app-header">
          <span className="app-title">{SURFACE_TITLE[surface]}</span>
          <ThemeToggle />
        </header>
        <div className="app-body">
          {surface === "explore" && (
            <div className="pane-mobile" data-testid="explore-surface">
              <nav className="pane-switch" role="tablist" aria-label="Explorer panes" data-testid="pane-switch">
                {PANES.map((p) => (
                  <button
                    key={p}
                    type="button"
                    role="tab"
                    data-pane={p}
                    aria-selected={explorePane === p}
                    className={explorePane === p ? "is-selected" : ""}
                    onClick={() => setExplorePane(p)}
                  >
                    {PANE_LABEL[p]}
                  </button>
                ))}
              </nav>
              {/* Results keeps its own filter/page state, so it is hidden
                  rather than unmounted. The graph is mounted on demand: React
                  Flow measures its container at mount, and a container that is
                  display:none measures 0x0, which is how a fitted graph ends
                  up at an unusable zoom. */}
              <div className="pane-mobile-body" hidden={explorePane !== "results"}>
                <Explorer
                  onSelectDiscovery={selectDiscovery}
                  onOpenRawDb={bootstrap.public ? undefined : openRawDb}
                  selectedRowKey={selectedRowKey}
                />
              </div>
              {explorePane === "graph" && (
                <div className="pane-mobile-body">
                  <GraphCanvas seed={seed} selectedNodeId={selectedNodeId} onSelectNode={selectNode} isMobile />
                </div>
              )}
              {explorePane === "details" && (
                <div className="pane-mobile-body pane-mobile-details">{rightPane()}</div>
              )}
            </div>
          )}
          {surface === "compare" && (
            <div className="pane-mobile">
              <div className="pane-mobile-body">
                <CompareView
                  initialA={compareRunId ?? undefined}
                  onSelectNode={(id) => {
                    setSurface("explore");
                    selectNode(id);
                  }}
                />
              </div>
            </div>
          )}
          <div className="surface-holder" hidden={!onWorkspace}>
            <InterestsWorkspace
              view={wsView}
              onViewChange={(v) => setSurface(v === "list" ? "interests" : v)}
              onOfferCount={setOfferCount}
              chromeless
            />
          </div>
        </div>
        <MobileNav surface={surface} onSelect={setSurface} offerCount={offerCount} />
      </div>
    );
  }

  return (
    <div className={`app desktop ${resizing ? "resizing" : ""}`} data-testid="app">
      <header className="app-header">
        <span className="app-title">Observatory</span>
        <button onClick={() => setInterestsOpen((v) => !v)}>
          {interestsOpen ? "Close interests" : "Interests"}
        </button>
        <button onClick={() => setCompareOpen((v) => !v)}>{compareOpen ? "Close compare" : "Compare"}</button>
        <ThemeToggle />
      </header>
      {interestsOpen ? (
        <div className="app-body">
          <InterestsWorkspace onClose={() => setInterestsOpen(false)} />
        </div>
      ) : (
      <div className="app-body">
        <div className="pane pane-explorer">
          <Explorer
            onSelectDiscovery={selectDiscovery}
            onOpenRawDb={bootstrap.public ? undefined : openRawDb}
            selectedRowKey={selectedRowKey}
          />
        </div>
        <div className="pane pane-graph">
          {compareOpen ? (
            <CompareView
              onClose={() => setCompareOpen(false)}
              initialA={compareRunId ?? undefined}
              onSelectNode={(id) => {
                setCompareOpen(false);
                selectNode(id);
              }}
            />
          ) : (
            <GraphCanvas seed={seed} selectedNodeId={selectedNodeId} onSelectNode={selectNode} />
          )}
        </div>
        <div className="pane-resizer" title="Drag to resize" onPointerDown={startInspectorResize} />
        <div className="pane pane-inspector" style={{ width: inspectorWidth }}>
          {rightPane()}
        </div>
      </div>
      )}
    </div>
  );
}
