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
import type { DisplayGraph, DisplayNode } from "./assemble";
import { swimlaneRank } from "./assemble";
import type { ID } from "../types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 72;
const BAND_PAD_X = 24; // outermost bands' left/right padding past their own nodes
const BAND_PAD_V = 48; // top/bottom padding -- top also clears room for the band title

// Chip grid sizing for an expanded group's children (assemble.ts's
// containerId) -- rendered INSIDE the group's own card instead of as
// free-floating peers, see assemble.ts's mergeExpansion for why.
export const CHIP_WIDTH = 190;
export const CHIP_HEIGHT = 40;
const CHIP_GAP = 8;
const GROUP_HEADER = 40; // room for the group card's own header row above the grid

export function chipGrid(count: number): { cols: number; rows: number; width: number; height: number } {
  const cols = count <= 6 ? 1 : count <= 16 ? 2 : 3;
  const rows = Math.ceil(count / cols);
  return {
    cols, rows,
    width: Math.max(NODE_WIDTH, cols * (CHIP_WIDTH + CHIP_GAP) + CHIP_GAP),
    height: GROUP_HEADER + rows * (CHIP_HEIGHT + CHIP_GAP) + CHIP_GAP,
  };
}

export interface PositionedNode extends DisplayNode {
  x: number;
  y: number;
  width: number;
  height: number;
  isChip?: boolean;
  isTopChip?: boolean; // the first (best-scoring) chip in its container -- see chipsByContainer's sort order
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

function buildElkGraph(
  graph: DisplayGraph, partitioned: boolean, sizeOf: (n: DisplayNode) => { width: number; height: number },
  vertical = false,
): ElkLikeGraph {
  const layoutOptions: Record<string, string> = {
    "elk.algorithm": "layered",
    // A phone is 393 wide and 852 tall, so the pipeline runs DOWN there: the
    // same partitioned layered layout, rotated a quarter turn. Laid out
    // RIGHT, a seven-node trace is ~2800px wide, which at a readable zoom
    // shows three cards and needs a horizontal drag for the rest -- measured
    // on the live app. Laid out DOWN it is ~220px wide, so it fits the screen
    // at zoom 1 and you scroll through it the way you scroll everything else.
    "elk.direction": vertical ? "DOWN" : "RIGHT",
    "elk.spacing.nodeNode": "14",
    "elk.layered.spacing.nodeNodeBetweenLayers": "64",
    "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
    "elk.spacing.edgeNode": "16",
  };
  if (partitioned) layoutOptions["elk.partitioning.activate"] = "true";
  return {
    id: "root",
    layoutOptions,
    children: graph.nodes.map((n) => {
      const size = sizeOf(n);
      return {
        id: String(n.id), width: size.width, height: size.height,
        ...(partitioned
          ? { layoutOptions: { "elk.partitioning.partition": String(swimlaneRank(n.swimlane)) } }
          : {}),
      };
    }),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}`, sources: [String(e.from)], targets: [String(e.to)],
    })),
  } as ElkLikeGraph & { layoutOptions: Record<string, string> };
}

export async function computeLayout(
  graph: DisplayGraph, elk: ElkLike, vertical = false,
): Promise<LayoutResult> {
  // Chips never enter ELK's own graph as independent nodes -- they render
  // inside their container's card instead, so ELK only needs to reserve
  // space for the container's own (possibly grown) footprint. Bucketing
  // first means the container's ELK size and the chips' eventual grid
  // placement both come from the exact same chipGrid(count) call.
  const chipsByContainer = new Map<string, DisplayNode[]>();
  const topLevel: DisplayNode[] = [];
  for (const n of graph.nodes) {
    if (n.containerId != null) {
      const key = String(n.containerId);
      if (!chipsByContainer.has(key)) chipsByContainer.set(key, []);
      chipsByContainer.get(key)!.push(n);
    } else {
      topLevel.push(n);
    }
  }
  const sizeOf = (n: DisplayNode): { width: number; height: number } => {
    const chips = chipsByContainer.get(String(n.id));
    return chips ? chipGrid(chips.length) : { width: NODE_WIDTH, height: NODE_HEIGHT };
  };

  const topGraph: DisplayGraph = { nodes: topLevel, edges: graph.edges };
  let laidOut: ElkLikeGraph;
  try {
    laidOut = await elk.layout(buildElkGraph(topGraph, true, sizeOf, vertical));
  } catch {
    // Partitioning requires every edge to point from a lower partition to a
    // higher (or equal) one -- true of every relationship/type pair in the
    // live DB today (verified by SQL over trace_edges), but not guaranteed
    // forever (e.g. a future feedback_on edge). Retry once unpartitioned so
    // the graph still renders -- row-order layout, not columns -- rather
    // than throwing and showing nothing.
    laidOut = await elk.layout(buildElkGraph(topGraph, false, sizeOf, vertical));
  }
  const elkById = new Map((laidOut.children || []).map((c) => [c.id, c]));

  const topPositioned: PositionedNode[] = topLevel.map((n) => {
    const elkNode = elkById.get(String(n.id));
    const size = sizeOf(n);
    return { ...n, x: elkNode?.x ?? 0, y: elkNode?.y ?? 0, width: size.width, height: size.height };
  });

  // Chips render row-major inside their now-positioned container, using the
  // identical chipGrid() math the container was sized by above.
  const chipPositioned: PositionedNode[] = [];
  for (const container of topPositioned) {
    const chips = chipsByContainer.get(String(container.id));
    if (!chips) continue;
    const { cols } = chipGrid(chips.length);
    chips.forEach((chip, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      chipPositioned.push({
        ...chip,
        x: container.x + CHIP_GAP + col * (CHIP_WIDTH + CHIP_GAP),
        y: container.y + GROUP_HEADER + CHIP_GAP + row * (CHIP_HEIGHT + CHIP_GAP),
        width: CHIP_WIDTH, height: CHIP_HEIGHT, isChip: true, isTopChip: i === 0,
      });
    });
  }

  const nodes: PositionedNode[] = [...topPositioned, ...chipPositioned];

  const byLane = new Map<string, PositionedNode[]>();
  for (const n of topPositioned) {
    if (!byLane.has(n.swimlane)) byLane.set(n.swimlane, []);
    byLane.get(n.swimlane)!.push(n);
  }
  const laneOrder = [...byLane.keys()].sort((a, b) => swimlaneRank(a) - swimlaneRank(b));
  // Partitioning guarantees each lane's node x-extents come out in rank
  // order and never interleave with another lane's -- verified against the
  // live app across every discovery shape tried (simple, deep-chain, wide
  // fan-out, council generation).
  // Bands run across the axis the pipeline does NOT advance along: columns
  // when it flows RIGHT, rows when it flows DOWN. Everything below is the one
  // calculation with `along` = the pipeline axis and `across` = the other, so
  // the two orientations cannot drift apart.
  const along = vertical
    ? { min: (n: PositionedNode) => n.y, max: (n: PositionedNode) => n.y + n.height, pad: BAND_PAD_V }
    : { min: (n: PositionedNode) => n.x, max: (n: PositionedNode) => n.x + n.width, pad: BAND_PAD_X };
  const across = vertical
    ? { min: (n: PositionedNode) => n.x, max: (n: PositionedNode) => n.x + n.width, pad: BAND_PAD_X }
    : { min: (n: PositionedNode) => n.y, max: (n: PositionedNode) => n.y + n.height, pad: BAND_PAD_V };

  const extents = laneOrder.map((lane) => {
    const list = byLane.get(lane)!;
    return {
      lane,
      minA: Math.min(...list.map(along.min)),
      maxA: Math.max(...list.map(along.max)),
    };
  });

  const minAcross = topPositioned.length ? Math.min(...topPositioned.map(across.min)) : 0;
  const maxAcross = topPositioned.length ? Math.max(...topPositioned.map(across.max)) : 0;
  const acrossStart = Math.min(0, minAcross) - across.pad;
  const acrossSize = maxAcross - acrossStart + across.pad;

  // Band boundaries sit at the MIDPOINT between adjacent lanes' extents, so
  // bands tile edge-to-edge with no gap or overlap; outermost bands get flat
  // padding since there's no neighbor to split with.
  const lanes: LaneBand[] = extents.map((e, i) => {
    const start = i === 0 ? e.minA - along.pad : (extents[i - 1].maxA + e.minA) / 2;
    const end = i === extents.length - 1 ? e.maxA + along.pad : (e.maxA + extents[i + 1].minA) / 2;
    return vertical
      ? { lane: e.lane, left: acrossStart, width: acrossSize, top: start, height: end - start }
      : { lane: e.lane, left: start, width: end - start, top: acrossStart, height: acrossSize };
  });

  const width = Math.max(0, ...topPositioned.map((n) => n.x + n.width)) + BAND_PAD_X;
  const height = Math.max(0, ...topPositioned.map((n) => n.y + n.height)) + BAND_PAD_V;
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
