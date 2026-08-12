import { useEffect, useMemo, useState } from "react";
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
      {/* Layout is left-to-right (ELK elk.direction RIGHT): edges enter on the
          left and leave on the right. Without these Handles React Flow cannot
          anchor (and therefore never draws) any edge into a custom node. */}
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="source" position={Position.Right} isConnectable={false} />
      {/* Group pseudo-nodes carry no `child_node_type` field of their own --
          db.py's graph() stamps that info into the node's `label` instead
          ("N <child_node_type>", see db.py's group-node construction), which
          is already rendered below, so the type row just says "group" here. */}
      <div className="node-type">{isGroup ? "group" : n.node_type}</div>
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

// Swimlane captions ride inside the flow as non-interactive nodes so they
// pan/zoom with the graph -- a screen-space overlay drifts out of alignment
// on the first pan.
function LaneLabelNode({ data }: NodeProps) {
  return <div className="lane-label">{(data as { text: string }).text}</div>;
}

const nodeTypes = { card: NodeCard, lane: LaneLabelNode };

interface Props {
  seed: GraphSeed | null;
  selectedNodeId: ID | null;
  onSelectNode: (id: ID) => void;
}

function GraphCanvasInner({ seed, selectedNodeId, onSelectNode }: Props) {
  const graphData = useGraphData(seed);
  const { display, base, expandGroup, collapseGroup, expandAll, focusMode, setFocusMode, expandedKeys, reload } =
    useAugmentedGraphData(graphData);
  const [layout, setLayout] = useState<LayoutResult | null>(null);
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

  // Re-fit when the viewport itself changes size (phone rotation, window
  // resize, drawer open/close) so the graph never sits off-screen.
  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const onResize = () => {
      clearTimeout(t);
      t = setTimeout(() => fitView({ duration: 150 }), 150);
    };
    window.addEventListener("resize", onResize);
    return () => {
      clearTimeout(t);
      window.removeEventListener("resize", onResize);
    };
  }, [fitView]);

  const emphasizedSet = useMemo(() => new Set(graphData.emphasizedPath.map(String)), [graphData.emphasizedPath]);

  const flowNodes: Node[] = useMemo(() => {
    if (!layout) return [];
    const laneNodes: Node[] = layout.lanes.map(({ lane, top }) => ({
      id: `lane:${lane}`,
      type: "lane",
      position: { x: -190, y: top },
      data: { text: laneLabel(lane) },
      draggable: false,
      selectable: false,
      focusable: false,
    }));
    return laneNodes.concat(layout.nodes.map((n) => ({
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
    })));
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
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodeClick={(_evt, node) => node.type !== "lane" && onSelectNode(node.id)}
        fitView
        fitViewOptions={{ padding: 0.05, maxZoom: 1 }}
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
          nodeColor="#b9c6d8"
          nodeStrokeColor="#8b98a9"
          maskColor="rgba(28, 37, 48, 0.08)"
        />
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
