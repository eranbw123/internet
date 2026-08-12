// ELK.js-driven layered layout, re-run after every expand/collapse (see
// GraphCanvas.tsx). ELK gives us a horizontal (left-to-right) topological
// layering + crossing-minimized ordering; swimlanes are a CROSS-axis
// (vertical band) concern ELK's layered algorithm doesn't natively express,
// so this module lets ELK own x (and a within-lane ordering hint) and then
// remaps y into content-sized swimlane bands (row-packed by x-collision) as
// a post-pass -- a standard technique for swimlane graphs on top of a
// layered layout engine.
import type { DisplayGraph } from "./assemble";
import { swimlaneRank } from "./assemble";
import type { ID, NodeSummary } from "../types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 72;
const ROW_GAP = 12;
const LANE_GAP = 32;
const H_GAP = 24;

export interface PositionedNode extends NodeSummary {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutResult {
  nodes: PositionedNode[];
  width: number;
  height: number;
  /** Swimlane bands in display order with their computed y offsets, for the lane labels. */
  lanes: { lane: string; top: number }[];
}

// Minimal shape of what we actually read back from ELK -- avoids depending
// on elkjs's full (large) type surface in this module's own signature.
interface ElkLikeNode {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  children?: ElkLikeNode[];
}
interface ElkLikeGraph extends ElkLikeNode {
  children: ElkLikeNode[];
  edges: { id: string; sources: string[]; targets: string[] }[];
}
export interface ElkLike {
  layout(graph: ElkLikeGraph): Promise<ElkLikeGraph>;
}

export async function computeLayout(graph: DisplayGraph, elk: ElkLike): Promise<LayoutResult> {
  const elkGraph: ElkLikeGraph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.spacing.nodeNode": "24",
      "elk.layered.spacing.nodeNodeBetweenLayers": "48",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
    },
    children: graph.nodes.map((n) => ({
      id: String(n.id), width: NODE_WIDTH, height: NODE_HEIGHT,
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}`, sources: [String(e.from)], targets: [String(e.to)],
    })),
  } as ElkLikeGraph & { layoutOptions: Record<string, string> };

  const laidOut = await elk.layout(elkGraph);
  const elkById = new Map((laidOut.children || []).map((c) => [c.id, c]));

  const byLane = new Map<string, NodeSummary[]>();
  for (const n of graph.nodes) {
    const lane = n.swimlane;
    if (!byLane.has(lane)) byLane.set(lane, []);
    byLane.get(lane)!.push(n);
  }
  // Within each lane, order by ELK's own x (then y) so nodes that are
  // topologically close stay visually close once remapped into the band.
  for (const list of byLane.values()) {
    list.sort((a, b) => {
      const ea = elkById.get(String(a.id));
      const eb = elkById.get(String(b.id));
      return (ea?.x ?? 0) - (eb?.x ?? 0) || (ea?.y ?? 0) - (eb?.y ?? 0);
    });
  }

  const laneOrder = [...byLane.keys()].sort((a, b) => swimlaneRank(a) - swimlaneRank(b));

  // Pack each lane into rows: a node stays on the first row where it doesn't
  // horizontally collide with the row's previous node, so a left-to-right
  // chain sits on one tight line and the lane only grows when nodes genuinely
  // overlap in x. Lane heights are content-sized, keeping connected nodes in
  // adjacent lanes close instead of centered in fixed 160px bands.
  const rowByNode = new Map<ID, number>();
  const laneRows = new Map<string, number>();
  for (const [lane, list] of byLane) {
    const rowEnds: number[] = []; // rightmost occupied x per row
    for (const n of list) {
      const x = elkById.get(String(n.id))?.x ?? 0;
      let row = rowEnds.findIndex((end) => end + H_GAP <= x);
      if (row === -1) {
        row = rowEnds.length;
        rowEnds.push(x + NODE_WIDTH);
      } else {
        rowEnds[row] = x + NODE_WIDTH;
      }
      rowByNode.set(n.id, row);
    }
    laneRows.set(lane, rowEnds.length);
  }

  const laneY = new Map<string, number>();
  let cursor = 0;
  const lanes = laneOrder.map((lane) => {
    laneY.set(lane, cursor);
    const top = cursor;
    const rows = laneRows.get(lane) ?? 1;
    cursor += rows * (NODE_HEIGHT + ROW_GAP) - ROW_GAP + LANE_GAP;
    return { lane, top };
  });

  const nodes: PositionedNode[] = graph.nodes.map((n) => ({
    ...n,
    x: elkById.get(String(n.id))?.x ?? 0,
    y: (laneY.get(n.swimlane) ?? 0) + (rowByNode.get(n.id) ?? 0) * (NODE_HEIGHT + ROW_GAP),
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
  }));

  const width = Math.max(0, ...nodes.map((n) => n.x + n.width)) + 40;
  const height = Math.max(0, cursor - LANE_GAP);
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
