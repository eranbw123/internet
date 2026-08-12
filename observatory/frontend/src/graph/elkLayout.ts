// ELK.js-driven layered layout, re-run after every expand/collapse (see
// GraphCanvas.tsx). Swimlanes are ELK's own partitioning constraint -- one
// partition per swimlane rank -- so ELK lays out BOTH axes itself: x for
// partition/topological order (which IS pipeline stage here, left to right)
// and y for within-partition ordering + crossing minimization. An earlier
// version of this module discarded ELK's y and re-packed nodes into
// horizontal rows instead, but pipeline stage tracks x almost exactly in
// this app's real traces, so every horizontal band ended up empty except
// its own x-window -- a diagonal staircase, not swimlanes (verified against
// live production data, see PROJECT_STATE.md). Partitioning fixes this at
// the source: lanes become real vertical COLUMNS, and "fit to view" reads
// left-to-right the way the pipeline actually runs, mission to Telegram.
import type { DisplayGraph } from "./assemble";
import { swimlaneRank } from "./assemble";
import type { ID, NodeSummary } from "../types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 72;
const BAND_PAD_X = 24; // outermost bands' left/right padding past their own nodes
const BAND_PAD_V = 48; // top/bottom padding -- top also clears room for the band title

export interface PositionedNode extends NodeSummary {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LaneBand {
  lane: string;
  left: number;
  width: number;
  top: number;
  height: number;
}

export interface LayoutResult {
  nodes: PositionedNode[];
  width: number;
  height: number;
  /** Swimlane bands as vertical columns, left to right, for the lane titles. */
  lanes: LaneBand[];
}

// Minimal shape of what we actually read back from ELK -- avoids depending
// on elkjs's full (large) type surface in this module's own signature.
interface ElkLikeNode {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  layoutOptions?: Record<string, string>;
  children?: ElkLikeNode[];
}
interface ElkLikeGraph extends ElkLikeNode {
  children: ElkLikeNode[];
  edges: { id: string; sources: string[]; targets: string[] }[];
}
export interface ElkLike {
  layout(graph: ElkLikeGraph): Promise<ElkLikeGraph>;
}

function buildElkGraph(graph: DisplayGraph, partitioned: boolean): ElkLikeGraph {
  const layoutOptions: Record<string, string> = {
    "elk.algorithm": "layered",
    "elk.direction": "RIGHT",
    "elk.spacing.nodeNode": "14",
    "elk.layered.spacing.nodeNodeBetweenLayers": "64",
    "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
    "elk.spacing.edgeNode": "16",
  };
  if (partitioned) layoutOptions["elk.partitioning.activate"] = "true";
  return {
    id: "root",
    layoutOptions,
    children: graph.nodes.map((n) => ({
      id: String(n.id), width: NODE_WIDTH, height: NODE_HEIGHT,
      ...(partitioned
        ? { layoutOptions: { "elk.partitioning.partition": String(swimlaneRank(n.swimlane)) } }
        : {}),
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}`, sources: [String(e.from)], targets: [String(e.to)],
    })),
  } as ElkLikeGraph & { layoutOptions: Record<string, string> };
}

export async function computeLayout(graph: DisplayGraph, elk: ElkLike): Promise<LayoutResult> {
  let laidOut: ElkLikeGraph;
  try {
    laidOut = await elk.layout(buildElkGraph(graph, true));
  } catch {
    // Partitioning requires every edge to point from a lower partition to a
    // higher (or equal) one -- true of every relationship/type pair in the
    // live DB today (verified by SQL over trace_edges), but not guaranteed
    // forever (e.g. a future feedback_on edge). Retry once unpartitioned so
    // the graph still renders -- row-order layout, not columns -- rather
    // than throwing and showing nothing.
    laidOut = await elk.layout(buildElkGraph(graph, false));
  }
  const elkById = new Map((laidOut.children || []).map((c) => [c.id, c]));

  const nodes: PositionedNode[] = graph.nodes.map((n) => {
    const elkNode = elkById.get(String(n.id));
    return {
      ...n,
      x: elkNode?.x ?? 0,
      y: elkNode?.y ?? 0,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    };
  });

  const byLane = new Map<string, PositionedNode[]>();
  for (const n of nodes) {
    if (!byLane.has(n.swimlane)) byLane.set(n.swimlane, []);
    byLane.get(n.swimlane)!.push(n);
  }
  const laneOrder = [...byLane.keys()].sort((a, b) => swimlaneRank(a) - swimlaneRank(b));
  // Partitioning guarantees each lane's node x-extents come out in rank
  // order and never interleave with another lane's -- verified against the
  // live app across every discovery shape tried (simple, deep-chain, wide
  // fan-out, council generation).
  const extents = laneOrder.map((lane) => {
    const list = byLane.get(lane)!;
    return {
      lane,
      minX: Math.min(...list.map((n) => n.x)),
      maxX: Math.max(...list.map((n) => n.x + n.width)),
    };
  });

  const minNodeY = nodes.length ? Math.min(...nodes.map((n) => n.y)) : 0;
  const maxNodeY = nodes.length ? Math.max(...nodes.map((n) => n.y + n.height)) : 0;
  const top = Math.min(0, minNodeY) - BAND_PAD_V;
  const height = maxNodeY - top + BAND_PAD_V;

  // Band boundaries sit at the MIDPOINT between adjacent lanes' extents, so
  // bands tile edge-to-edge with no gap or overlap; outermost bands get flat
  // padding since there's no neighbor to split with.
  const lanes: LaneBand[] = extents.map((e, i) => {
    const left = i === 0 ? e.minX - BAND_PAD_X : (extents[i - 1].maxX + e.minX) / 2;
    const right = i === extents.length - 1 ? e.maxX + BAND_PAD_X : (e.maxX + extents[i + 1].minX) / 2;
    return { lane: e.lane, left, width: right - left, top, height };
  });

  const width = Math.max(0, ...nodes.map((n) => n.x + n.width)) + BAND_PAD_X;
  return { nodes, width, height, lanes };
}

let _defaultElk: ElkLike | null = null;
export async function defaultElk(): Promise<ElkLike> {
  if (!_defaultElk) {
    // elkjs's bundled entry ships as a plain JS UMD module with no bundled
    // .d.ts on this exact subpath -- imported untyped (`as unknown as ...`)
    // and immediately narrowed to our own minimal ElkLike shape (above)
    // rather than trusting elkjs's own (unverified in this sandbox, see
    // PROJECT_STATE.md) type surface.
    const mod = (await import("elkjs/lib/elk.bundled.js")) as unknown as { default: new () => ElkLike };
    _defaultElk = new mod.default();
  }
  return _defaultElk;
}

export function laneLabel(swimlane: ID): string {
  const labels: Record<string, string> = {
    "interest-state": "Personal / interest state",
    council: "Council",
    mission: "Mission execution",
    "candidate-pipeline": "Candidate pipeline",
    scoring: "Scoring / value",
    "delivery-feedback": "Delivery / feedback",
    other: "Other",
  };
  return labels[String(swimlane)] || String(swimlane);
}
