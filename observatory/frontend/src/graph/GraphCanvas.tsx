import { useEffect, useMemo, useState } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { GraphSeed } from "./useGraphData";
import { useGraphData } from "./useGraphData";
import { formatEdgeLabel } from "./assemble";
import { computeLayout, defaultElk, laneLabel, LANE_HEIGHT, LANE_GAP, type PositionedNode } from "./elkLayout";
import type { ID } from "../types";

function statusClass(status: string | null): string {
  if (!status) return "";
  if (["running", "pending", "started", "in_progress"].includes(status)) return "status-active";
  if (["error", "failed"].includes(status)) return "status-error";
  if (["ok", "done", "success", "sent"].includes(status)) return "status-ok";
  return "";
}

function NodeCard({ data }: NodeProps) {
  const n = data as unknown as PositionedNode & { emphasized: boolean; onExpandToggle?: () => void };
  const isGroup = n.node_type === "group";
  const duration = n.started_at && n.finished_at
    ? `${((new Date(n.finished_at).getTime() - new Date(n.started_at).getTime()) / 1000).toFixed(1)}s`
    : null;
  return (
    <div
      className={`node-card ${statusClass(n.status)} ${n.emphasized ? "emphasized" : ""} ${isGroup ? "node-group" : ""}`}
      onDoubleClick={n.onExpandToggle}
      title={isGroup ? "double-click to expand" : undefined}
      data-node-id={n.id}
      data-node-type={n.node_type}
    >
      <div className="node-type">{isGroup ? `group: ${n.child_node_type}` : n.node_type}</div>
      <div className="node-label">{n.label || "(untitled)"}</div>
      {n.summary && <div className="node-summary">{n.summary}</div>}
      <div className="node-meta">
        {n.status && <span className="node-status">{n.status}</span>}
        {duration && <span className="node-duration">{duration}</span>}
        {isGroup && <span className="node-child-count">{n.child_count} items</span>}
        {n.started_at && <span className="node-time">{n.started_at}</span>}
      </div>
    </div>
  );
}

const nodeTypes = { card: NodeCard };

interface Props {
  seed: GraphSeed | null;
  selectedNodeId: ID | null;
  onSelectNode: (id: ID) => void;
}

function GraphCanvasInner({ seed, selectedNodeId, onSelectNode }: Props) {
  const graphData = useGraphData(seed);
  const { display, base, expandGroup, collapseGroup, expandAll, focusMode, setFocusMode, expandedKeys, reload } =
    useAugmentedGraphData(graphData);
  const [layout, setLayout] = useState<{ nodes: PositionedNode[]; width: number; height: number } | null>(null);
  const { fitView } = useReactFlow();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const elk = await defaultElk();
      const result = await computeLayout(display, elk);
      if (!cancelled) setLayout(result);
    })();
    return () => {
      cancelled = true;
    };
  }, [display]);

  useEffect(() => {
    if (layout) setTimeout(() => fitView({ duration: 200 }), 50);
  }, [layout?.nodes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const emphasizedSet = useMemo(() => new Set(graphData.emphasizedPath.map(String)), [graphData.emphasizedPath]);

  const flowNodes: Node[] = useMemo(() => {
    if (!layout) return [];
    return layout.nodes.map((n) => ({
      id: String(n.id),
      type: "card",
      position: { x: n.x, y: n.y },
      data: {
        ...n,
        emphasized: emphasizedSet.has(String(n.id)),
        onExpandToggle: n.node_type === "group"
          ? () => (expandedKeys.has(String(n.id)) ? collapseGroup(String(n.id)) : expandGroup(String(n.id)))
          : undefined,
      },
      selected: String(n.id) === String(selectedNodeId),
    }));
  }, [layout, emphasizedSet, selectedNodeId, expandGroup, collapseGroup, expandedKeys]);

  const nodeById = useMemo(() => new Map(display.nodes.map((n) => [String(n.id), n])), [display.nodes]);

  const flowEdges: Edge[] = useMemo(
    () => display.edges.map((e, i) => ({
      id: `${e.from}-${e.to}-${e.relationship}-${i}`,
      source: String(e.from),
      target: String(e.to),
      label: formatEdgeLabel(e, nodeById.get(String(e.to))),
      animated: false,
      className: emphasizedSet.has(String(e.from)) && emphasizedSet.has(String(e.to)) ? "edge-emphasized" : "",
    })),
    [display.edges, nodeById, emphasizedSet],
  );

  const laneOrder = useMemo(() => {
    const seen = new Set<string>();
    for (const n of display.nodes) seen.add(n.swimlane);
    return [...seen];
  }, [display.nodes]);

  return (
    <div className="graph-canvas">
      <div className="graph-toolbar">
        <button onClick={expandAll}>Expand all</button>
        <button onClick={() => setFocusMode((v: boolean) => !v)} aria-pressed={focusMode}>
          {focusMode ? "Focused (showing selected path only)" : "Focus selected path"}
        </button>
        <button onClick={() => fitView({ duration: 200 })}>Fit to view</button>
        <button onClick={reload}>Reset layout</button>
      </div>
      <div className="graph-lanes">
        {laneOrder.map((lane, i) => (
          <div key={lane} className="lane-label" style={{ top: i * (LANE_HEIGHT + LANE_GAP) }}>
            {laneLabel(lane)}
          </div>
        ))}
      </div>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodeClick={(_evt, node) => onSelectNode(node.id)}
        fitView
        panOnScroll
        zoomOnPinch
        minZoom={0.1}
        maxZoom={2}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable />
      </ReactFlow>
      {!base && <div className="graph-empty">Select a discovery from the list to load its trace.</div>}
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
