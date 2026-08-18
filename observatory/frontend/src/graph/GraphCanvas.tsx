import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  ReactFlowProvider, useReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { GraphSeed } from "./useGraphData";
import { useGraphData } from "./useGraphData";
import { formatEdgeLabel } from "./assemble";
import { computeLayout, defaultElk, laneLabel, type LayoutResult, type PositionedNode } from "./elkLayout";
import type { ID } from "../types";

function statusClass(status: string | null): string {
  if (!status) return "";
  if (["running", "pending", "started", "in_progress"].includes(status)) return "status-active";
  if (["error", "failed"].includes(status)) return "status-error";
  if (["ok", "done", "success", "sent"].includes(status)) return "status-ok";
  return "";
}

// db.py's graph() stamps a group's child type into its label as "N
// <child_node_type>" (e.g. "12 match", "18 tool-event") -- there's no
// separate field for it. Turned into plain language instead of showing the
// backend's internal type name + a redundant "N items" meta chip next to it.
const GROUP_NOUNS: Record<string, string> = {
  match: "interest match", "tool-event": "tool call", advisor: "council advisor",
  "peer-review": "peer review", "rejected-angle": "rejected angle", mission: "mission",
  "raw-result": "raw result", "raw-result-dropped": "dropped result",
};
function groupCaption(label: string | null, childCount: number | undefined): string {
  const count = childCount ?? 0;
  const type = /^\d+\s+(.+)$/.exec(label || "")?.[1] ?? "";
  const noun = GROUP_NOUNS[type] || type || "node";
  return `${count} ${count === 1 ? noun : `${noun}s`}`;
}

// pipeline.py stamps a threshold node's label as "{score:.3f} vs {bar:.3f}"
// (see discovery/pipeline.py's threshold_node construction) -- the single
// most important decision in the pipeline (did this clear the bar?)
// otherwise lived only in a small, easily-missed edge label
// (cleared_threshold vs rejected). Parsed straight from the label itself
// (score >= bar) rather than cross-referencing the inbound edge: simpler,
// and correct regardless of whether that edge happens to be present in
// whatever's currently displayed (e.g. mid-focus-mode filtering).
function thresholdVerdict(label: string | null): { text: string; passed: boolean } | null {
  const m = /^([\d.]+)\s+vs\s+([\d.]+)$/.exec(label || "");
  if (!m) return null;
  const passed = parseFloat(m[1]) >= parseFloat(m[2]);
  return { text: `${passed ? "✓" : "✕"} ${m[1]} ${passed ? "≥" : "<"} ${m[2]}`, passed };
}

// Full ISO timestamps ("2026-08-12T07:56:58+00:00") ate roughly half of a
// card's already-tight meta row for a date the card's own position in the
// graph already conveys ("today," implicitly) -- the Inspector's Overview
// tab keeps the full timestamp for whoever actually needs the date.
function shortTime(iso: string | null): string | null {
  const m = /T(\d\d:\d\d:\d\d)/.exec(iso || "");
  return m ? m[1] : iso;
}

function NodeCard({ data }: NodeProps) {
  const n = data as unknown as PositionedNode & { emphasized: boolean; expanded?: boolean; onExpandToggle?: () => void };
  const isGroup = n.node_type === "group";
  const verdict = n.node_type === "threshold" ? thresholdVerdict(n.label) : null;
  const duration = n.started_at && n.finished_at
    ? `${((new Date(n.finished_at).getTime() - new Date(n.started_at).getTime()) / 1000).toFixed(1)}s`
    : null;
  return (
    <div
      className={[
        "node-card", statusClass(n.status), n.emphasized ? "emphasized" : "",
        isGroup ? "node-group" : "", isGroup && n.expanded ? "node-group-expanded" : "",
        verdict ? (verdict.passed ? "threshold-pass" : "threshold-fail") : "",
        n.node_type === "raw-result-dropped" || n.node_type === "duplicate" ? "node-dimmed" : "",
      ].filter(Boolean).join(" ")}
      title={isGroup ? `Click to ${n.expanded ? "collapse" : "expand"}` : undefined}
      data-node-id={n.id}
      data-node-type={n.node_type}
    >
      {/* Layout is left-to-right (ELK elk.direction RIGHT): edges enter on the
          left and leave on the right. Without these Handles React Flow cannot
          anchor (and therefore never draws) any edge into a custom node. */}
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="source" position={Position.Right} isConnectable={false} />
      <div className="node-type">{isGroup ? (n.expanded ? "▾ collapse" : "▸ expand") : n.node_type}</div>
      <div className="node-label">
        {isGroup ? groupCaption(n.label, n.child_count) : (verdict ? verdict.text : (n.label || "(untitled)"))}
      </div>
      {n.summary && <div className="node-summary">{n.summary}</div>}
      <div className="node-meta">
        {n.status && <span className="node-status">{n.status}</span>}
        {duration && <span className="node-duration">{duration}</span>}
        {n.started_at && <span className="node-time">{shortTime(n.started_at)}</span>}
      </div>
    </div>
  );
}

// A group's expanded children render as chips INSIDE its own (now-grown)
// card rather than as free-floating peer nodes -- see assemble.ts's
// mergeExpansion and elkLayout.ts's chipGrid for why. No Handles: nothing
// ever draws an edge into a chip (the group's own single inbound edge, from
// before it was ever expanded, is still the only connector), so anchors
// would be dead weight.
function ChipNode({ data }: NodeProps) {
  const n = data as unknown as PositionedNode & { emphasized: boolean };
  return (
    <div
      className={`node-chip ${n.isTopChip ? "node-chip-top" : ""} ${n.emphasized ? "emphasized" : ""}`}
      title={n.label || undefined}
      data-node-id={n.id}
      data-node-type={n.node_type}
    >
      <span className="node-chip-label">{n.label || "(untitled)"}</span>
    </div>
  );
}

// One tint per swimlane, reused by the MiniMap so the columns still read
// (faintly) at minimap scale instead of every node blurring into one grey.
const LANE_TINT: Record<string, string> = {
  "interest-state": "#e8c9c9", council: "#d9c9e8", mission: "#c9d4e8",
  "candidate-pipeline": "#c9e0e8", scoring: "#cfe0c9", "delivery-feedback": "#e8ddc9",
};

const LEGEND_ITEMS: { swatch: string; text: string }[] = [
  { swatch: "columns", text: "Columns are pipeline stages, left → right: Interest → Council → Mission → Candidates → Scoring → Delivery." },
  { swatch: "path", text: "Blue path — the route this discovery took, from search hit to Telegram." },
  { swatch: "status", text: "Card left edge: green = ok · amber = running · red = failed." },
  { swatch: "verdict", text: "✓ / ✕ — cleared or missed the score threshold." },
  { swatch: "group", text: "Dashed purple card — a bundle of similar items; click to open it in place." },
  { swatch: "dim", text: "Faded cards — dropped results and duplicates (dead ends)." },
];

function GraphLegend() {
  return (
    <div className="graph-legend" role="note">
      <div className="graph-legend-title">Reading this graph</div>
      <ul>
        {LEGEND_ITEMS.map((item) => (
          <li key={item.swatch}>
            <span className={`legend-swatch legend-swatch-${item.swatch}`} aria-hidden="true" />
            {item.text}
          </li>
        ))}
      </ul>
    </div>
  );
}

// Swimlane bands ride inside the flow as non-interactive nodes so they
// pan/zoom with the graph -- a screen-space overlay drifts out of alignment
// on the first pan. Each band is a full vertical column (see elkLayout.ts's
// partitioned layout); size comes from the node's own `style` (React Flow
// applies that to the wrapper), this component just fills it.
function LaneBandNode({ data }: NodeProps) {
  const d = data as { lane: string; i: number };
  return (
    <div className={`lane-band ${d.i % 2 === 1 ? "lane-band-odd" : ""}`}>
      <div className="lane-band-title">{laneLabel(d.lane)}</div>
    </div>
  );
}

const nodeTypes = { card: NodeCard, chip: ChipNode, laneBand: LaneBandNode };

/** Run `fn` once React Flow has measured and painted the nodes we just handed
 * it. See the layout effect for why two rAFs (and not a bare timer). */
function afterPaint(fn: () => void): void {
  setTimeout(() => {
    requestAnimationFrame(() => {
      requestAnimationFrame(fn);
    });
  }, 30);
}

interface Props {
  seed: GraphSeed | null;
  selectedNodeId: ID | null;
  onSelectNode: (id: ID) => void;
  isMobile?: boolean;
}

function GraphCanvasInner({ seed, selectedNodeId, onSelectNode, isMobile = false }: Props) {
  const graphData = useGraphData(seed, isMobile);
  const {
    display, base, loading, error, expandGroup, collapseGroup, expandAll, focusMode, setFocusMode,
    expandedKeys, reload,
  } = useAugmentedGraphData(graphData);
  const [layout, setLayout] = useState<LayoutResult | null>(null);
  const [showLegend, setShowLegend] = useState(false);
  const [overflowOpen, setOverflowOpen] = useState(false);
  const { fitView, getViewport, setViewport } = useReactFlow();
  const seedKey = seed ? JSON.stringify(seed) : null;

  // Which seed we have already framed. Refitting is keyed on this rather than
  // on the node count: expanding a group changes the count, and keying the
  // refit on the count meant every expansion threw the camera back to fit-all
  // (measured: zoomed to scale 1.366, expanded a group 96 -> 101 nodes,
  // viewport snapped to translate(17.78px, 142.12px) scale(0.1486)). The
  // original reason for including the count -- that switching between two
  // DIFFERENT graphs of the SAME size must still refit -- is served by the
  // seed key alone, which is what actually identifies "a different graph".
  const fittedSeedRef = useRef<string | null>(null);
  // Frame the story, not the whole world.
  //
  // Fitting the entire graph was measured at scale 0.1486 on a 1400px desktop
  // and 0.100 on a 390px phone -- the latter clamped at minZoom, i.e. it did
  // not even fit, and rendered 220px cards 22px wide. The backend already
  // hands us the antidote: emphasized_path (extended client-side through
  // threshold -> render -> notification) is ~7 nodes describing the route this
  // discovery actually took, and it lays out left-to-right, so framing it
  // lands the reader on the readable chain -- search hit, candidate, score,
  // threshold, Telegram -- with the rest of the run in the periphery.
  //
  // Zoom floors keep the fallback honest: a readable partial view beats an
  // unreadable total one. The global minZoom stays 0.1 so manual zoom-out is
  // unaffected.
  const fitFocusFirst = useCallback(() => {
    const path = graphData.emphasizedPath;
    const options = {
      duration: 200,
      padding: isMobile ? 0.15 : 0.1,
      minZoom: isMobile ? 0.55 : 0.35,
      maxZoom: 1,
    };
    if (path.length > 0) {
      fitView({ ...options, nodes: path.map((id) => ({ id: String(id) })) });
    } else {
      // Run-id seeds (the Failed tab) resolve no emphasized path.
      fitView(options);
    }
  }, [fitView, graphData.emphasizedPath, isMobile]);
  // Set just before a group toggle so the layout that follows can keep that
  // group pinned under the cursor instead of letting the graph reflow away.
  const anchorRef = useRef<{ id: string; x: number; y: number } | null>(null);
  // Set by deliberate view changes (the focus toggle) that SHOULD refit.
  const refitNextRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const elk = await defaultElk();
      const result = await computeLayout(display, elk);
      if (cancelled) return;
      setLayout(result);

      const anchor = anchorRef.current;
      anchorRef.current = null;
      const deliberateRefit = refitNextRef.current;
      refitNextRef.current = false;
      const isNewSeed = seedKey !== fittedSeedRef.current;
      // Only claim the seed as framed once it actually has nodes, so an
      // intermediate empty/loading layout can't swallow the real one's fit.
      if (isNewSeed && result.nodes.length > 0) fittedSeedRef.current = seedKey;

      // The double-rAF -- not a bare timer -- is what fixes the measurement
      // flake: it was reproduced live as a race between React Flow's internal
      // node re-measurement and a bare setTimeout, which sometimes ran before
      // React Flow had measured the new nodes' real DOM size, leaving the
      // viewport transform byte-identical to the previous graph's. Two rAFs
      // guarantee at least one full paint has happened first. Viewport writes
      // are batched the same way, so anchoring goes through it too.
      afterPaint(() => {
        if (cancelled) return;
        if (isNewSeed || deliberateRefit) {
          fitFocusFirst();
          return;
        }
        if (!anchor) return; // e.g. "Expand all": no single anchor, keep the viewport verbatim
        const moved = result.nodes.find((n) => String(n.id) === anchor.id);
        if (!moved) return;
        // Counter-translate by however far the anchored group drifted, in
        // screen space, so the card grows in place under the pointer.
        const { x, y, zoom } = getViewport();
        setViewport({ x: x - (moved.x - anchor.x) * zoom, y: y - (moved.y - anchor.y) * zoom, zoom });
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [display]); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-fit when the viewport itself changes size (phone rotation, window
  // resize, drawer open/close) so the graph never sits off-screen. Uses the
  // same focus-first framing as the initial fit, so a rotation doesn't quietly
  // demote the reader to the unreadable fit-all view.
  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const onResize = () => {
      clearTimeout(t);
      t = setTimeout(fitFocusFirst, 150);
    };
    window.addEventListener("resize", onResize);
    return () => {
      clearTimeout(t);
      window.removeEventListener("resize", onResize);
    };
  }, [fitFocusFirst]);

  const emphasizedSet = useMemo(() => new Set(graphData.emphasizedPath.map(String)), [graphData.emphasizedPath]);

  const flowNodes: Node[] = useMemo(() => {
    if (!layout) return [];
    const bandNodes: Node[] = layout.lanes.map((band, i) => ({
      id: `band:${band.lane}`,
      type: "laneBand",
      position: { x: band.left, y: band.top },
      style: { width: band.width, height: band.height },
      zIndex: -10,
      data: { lane: band.lane, i },
      draggable: false,
      selectable: false,
      focusable: false,
    }));
    return bandNodes.concat(layout.nodes.map((n) => {
      const isGroup = n.node_type === "group";
      return {
        id: String(n.id),
        type: n.isChip ? "chip" : "card",
        position: { x: n.x, y: n.y },
        // Containers grow with their chip count and chips are smaller than a
        // full card -- both need their real size on the flow node itself
        // (React Flow applies `style` to the node wrapper), not the old
        // fixed 220x72 every card used to share.
        style: { width: n.width, height: n.height },
        zIndex: n.isChip ? 10 : undefined,
        data: {
          ...n,
          emphasized: emphasizedSet.has(String(n.id)),
          expanded: isGroup ? expandedKeys.has(String(n.id)) : undefined,
          onExpandToggle: isGroup
            ? () => (expandedKeys.has(String(n.id)) ? collapseGroup(String(n.id)) : expandGroup(String(n.id)))
            : undefined,
        },
        selected: String(n.id) === String(selectedNodeId),
      };
    }));
  }, [layout, emphasizedSet, selectedNodeId, expandGroup, collapseGroup, expandedKeys]);

  const nodeById = useMemo(() => new Map(display.nodes.map((n) => [String(n.id), n])), [display.nodes]);

  // A group's id is a synthetic pseudo-id (assemble.ts's ExpandedGroups key
  // shape, "<parent>:<relationship>:<child_node_type>") that never exists in
  // trace_nodes -- plugin.py's /api/node/<id> route only matches [0-9]+, so
  // it can't even resolve the URL. Previously only "lane" captions were
  // excluded from onSelectNode, so a single click on any collapsed group
  // card (the common case: 5 advisors, 5 peer-reviewers, N match/raw-result
  // siblings) sent the Inspector to fetch a node id the backend rejects
  // outright, surfacing as a bare "Not Found" error with no indication the
  // click even did the right, discoverable thing -- the real action
  // (expand/collapse) required an undiscoverable double-click instead. Group
  // cards now expand/collapse on the same single click every other node
  // selects with.
  function handleNodeClick(_evt: unknown, node: Node) {
    if (node.type === "laneBand") return;
    const d = node.data as { node_type?: string; onExpandToggle?: () => void };
    if (d.node_type === "group") {
      // Remember where this group sits *before* the relayout, so the effect
      // above can pin it in place once the new positions come back.
      const current = layout?.nodes.find((n) => String(n.id) === node.id);
      if (current) anchorRef.current = { id: node.id, x: current.x, y: current.y };
      d.onExpandToggle?.();
    } else {
      onSelectNode(node.id);
    }
  }

  // Toggling focus deliberately changes what is on screen, so it is one of the
  // few things that SHOULD refit -- explicitly, rather than implicitly via a
  // node count that also moves for reasons the user didn't ask for.
  function toggleFocus() {
    refitNextRef.current = true;
    setFocusMode((v: boolean) => !v);
  }

  const flowEdges: Edge[] = useMemo(
    () => display.edges.map((e, i) => ({
      id: `${e.from}-${e.to}-${e.relationship}-${i}`,
      source: String(e.from),
      target: String(e.to),
      // "" (a restating relationship, see assemble.ts's formatEdgeLabel)
      // becomes undefined so React Flow skips rendering an empty label pill
      // rather than a blank rounded rect floating on the edge.
      label: formatEdgeLabel(e, nodeById.get(String(e.to))) || undefined,
      animated: false,
      className: emphasizedSet.has(String(e.from)) && emphasizedSet.has(String(e.to)) ? "edge-emphasized" : "",
    })),
    [display.edges, nodeById, emphasizedSet],
  );

  return (
    <div className="graph-canvas">
      {/* Primary actions stay on the bar; the rest collapse into an overflow
          menu on a phone, where six buttons wrapped to two rows and ate the
          height the graph itself needed. */}
      <div className="graph-toolbar">
        <button onClick={toggleFocus} aria-pressed={focusMode}>
          {focusMode ? "Show full run" : "Focus: this discovery's path"}
        </button>
        {focusMode && display.hiddenCount > 0 && (
          <span className="graph-toolbar-note">· {display.hiddenCount} nodes hidden</span>
        )}
        <button onClick={fitFocusFirst}>Fit to view</button>
        <button className="toolbar-overflow-only" onClick={expandAll}>Expand all</button>
        <button className="toolbar-overflow-only" onClick={reload}>Reset layout</button>
        <button
          className="toolbar-overflow-only graph-toolbar-spacer-left"
          onClick={() => setShowLegend((v) => !v)}
          aria-pressed={showLegend}
        >
          Legend
        </button>
        {isMobile && (
          <div className="toolbar-overflow">
            <button onClick={() => setOverflowOpen((v) => !v)} aria-expanded={overflowOpen} aria-label="More graph actions">⋯</button>
            {overflowOpen && (
              <div className="toolbar-overflow-menu" role="menu">
                <button role="menuitem" onClick={() => { setOverflowOpen(false); expandAll(); }}>Expand all</button>
                <button role="menuitem" onClick={() => { setOverflowOpen(false); reload(); }}>Reset layout</button>
                <button role="menuitem" onClick={() => { setOverflowOpen(false); setShowLegend((v) => !v); }}>Legend</button>
              </div>
            )}
          </div>
        )}
      </div>
      <div className="graph-canvas-body">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        // No `fitView`/`fitViewOptions` props: React Flow's own mount-time fit
        // would race the layout effect's focus-first fit and frame the whole
        // world for a beat before we reframe it.
        panOnScroll
        zoomOnPinch
        minZoom={0.1}
        maxZoom={2}
        nodesConnectable={false}
        defaultEdgeOptions={{
          type: "smoothstep",
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: "#8b98a9" },
        }}
      >
        <Background gap={24} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="graph-minimap"
          nodeColor={(node) => {
            // Bands themselves stay transparent in the minimap -- painting
            // their own tint would just wash out the real node colors sitting
            // on top of them at that scale.
            if (node.type === "laneBand") return "transparent";
            const swimlane = (node.data as { swimlane?: string })?.swimlane;
            return (swimlane && LANE_TINT[swimlane]) || "#b9c6d8";
          }}
          nodeStrokeColor="#8b98a9"
          maskColor="rgba(28, 37, 48, 0.08)"
        />
      </ReactFlow>
      {loading && <div className="graph-status">Loading trace…</div>}
      {error && <div className="graph-status graph-status-error">Couldn't load this trace: {error}</div>}
      {!base && !loading && !error && (
        <div className="graph-empty">Select a discovery from the list to load its trace.</div>
      )}
      {showLegend && <GraphLegend />}
      </div>
    </div>
  );
}

// Small adapter so GraphCanvasInner's JSX stays uncluttered: exposes the
// hook's `expanded` map as a plain Set of expanded group ids (all this
// component needs to know is "is this group open").
function useAugmentedGraphData(graphData: ReturnType<typeof useGraphData>) {
  const expandedKeys = useMemo(() => new Set(Object.keys(graphData.expanded)), [graphData.expanded]);
  return { ...graphData, expandedKeys };
}

export function GraphCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <GraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
