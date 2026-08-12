// Pure, framework-free graph-assembly logic -- kept separate from
// GraphCanvas.tsx (the React Flow wiring) precisely so it's unit-testable
// without a DOM/React Flow instance. Everything here operates on the raw
// GraphResponse the /api/graph endpoint returns (see observatory/db.py's
// graph()) plus whatever /api/children has fetched so far.
import type { GraphEdge, GraphResponse, ID, NodeSummary } from "../types";

// node id -> children already fetched via /api/children for that group.
// Keyed by group id (a string like "12:generated:mission" -- see db.py's
// graph(), which mints group ids this same way), never by a real node id.
export type ExpandedGroups = Record<string, NodeSummary[]>;

// `containerId` marks a node as a chip living inside an expanded group's own
// card (elkLayout.ts's chipGrid) rather than a free-floating peer -- see
// mergeExpansion below for why this replaced per-child edges entirely.
export interface DisplayNode extends NodeSummary {
  containerId?: string;
}

export interface DisplayGraph {
  nodes: DisplayNode[];
  edges: GraphEdge[];
}

/** A node's label often ends in " <score>" (match nodes: "key: 0.84";
 * candidates carry no score in their label). Extracted once here so both
 * the sort order below and elkLayout's/GraphCanvas's chip highlighting
 * agree on the same number instead of three independent regexes drifting
 * apart. Returns null (sorts last, never highlighted) when there's nothing
 * that looks like a trailing score. */
export function labelScore(label: string | null | undefined): number | null {
  const m = /:\s*(\d+(?:\.\d+)?)\s*$/.exec(label || "");
  return m ? parseFloat(m[1]) : null;
}

/** Merge lazily-fetched group children into the base graph. A group node
 * that has been expanded keeps its own card on screen (still `node_type
 * === 'group'`, so it stays the click target GraphCanvas uses to collapse
 * back -- there would otherwise be no on-screen affordance left to
 * re-collapse it once its children replaced it) -- but its children no
 * longer become free-floating peer nodes wired by their own edge. Verified
 * live: a fanned-out candidate (6-17 matches, common with today's ~40
 * active interests) dropped its expanded children into the SAME lane
 * column as every other candidate's still-collapsed group card, with a
 * forest of parallel "matched" edges crossing between them -- nothing
 * visually bound a match to the candidate it belonged to. Children now
 * carry `containerId` (the group's own key) instead of gaining a
 * `parent -> child` edge; elkLayout.ts lays them out as a compact chip grid
 * INSIDE the group's own (now-growing) card, so the graph keeps exactly the
 * one edge it already had (into the group) no matter how many children it
 * has. Sorted `labelScore` descending (nulls last) so the match that
 * actually mattered floats to the top instead of sitting at whatever
 * position the API's id order put it in. Their own further descendants
 * stay collapsed/hidden until independently expanded (/api/children only
 * ever returns one level, matching the API). Groups the caller hasn't
 * expanded are left exactly as the API returned them -- this function
 * never drops or invents data, only adds what's been fetched. */
export function mergeExpansion(base: GraphResponse, expanded: ExpandedGroups): DisplayGraph {
  const nodes: DisplayNode[] = [];
  const edges: GraphEdge[] = [...base.edges];
  for (const n of base.nodes) {
    const key = String(n.id);
    nodes.push(n);
    if (n.node_type === "group" && expanded[key]) {
      const children = [...expanded[key]].sort((a, b) => {
        const sa = labelScore(a.label);
        const sb = labelScore(b.label);
        if (sa === null && sb === null) return 0;
        if (sa === null) return 1;
        if (sb === null) return -1;
        return sb - sa;
      });
      for (const child of children) {
        nodes.push({ ...child, containerId: key });
      }
    }
  }
  return { nodes, edges };
}

export interface FocusResult extends DisplayGraph {
  hiddenCount: number;
}

/** "Focus selected path" hides everything off the emphasized path -- it
 * never mutates or drops data from the caller's own state, only computes a
 * filtered VIEW of it (turning focus back off must reproduce the exact
 * input again, byte for byte). An empty/missing path or focusMode=false is
 * a no-op passthrough. */
export function applyFocus(graph: DisplayGraph, emphasizedPath: ID[], focusMode: boolean): FocusResult {
  if (!focusMode || emphasizedPath.length === 0) {
    return { nodes: graph.nodes, edges: graph.edges, hiddenCount: 0 };
  }
  const pathSet = new Set(emphasizedPath.map(String));
  const nodes = graph.nodes.filter((n) => pathSet.has(String(n.id)));
  const visibleIds = new Set(nodes.map((n) => String(n.id)));
  const edges = graph.edges.filter((e) => visibleIds.has(String(e.from)) && visibleIds.has(String(e.to)));
  return { nodes, edges, hiddenCount: graph.nodes.length - nodes.length };
}

/** Human-readable edge label -- or "" for relationships that would just
 * repeat what the target card already says. The wire format only ever
 * carries a short, fixed relationship word + an optional ordinal (see
 * trace_edges' schema -- there is no free-text edge field); a first pass at
 * this function rendered every relationship as a label, and verified live
 * against a dense real trace that this painted the canvas with clutter --
 * "matched" repeated 12 times over a single expanded group, "generated"
 * labeling every edge in the graph, edge labels visually overlapping the
 * cards they pointed at. Silencing the restating relationships (GraphCanvas
 * turns "" into `label: undefined` so React Flow doesn't render an empty
 * pill) let the labels that actually carry information -- which branch
 * something took, not just that it existed -- read clearly instead of
 * drowning in repetition. This incidentally also fixes a real artifact: a
 * "generated" edge into a group pseudo-node rendered literally as
 * "generated group" (`toNode.node_type` IS `"group"`), never a useful type
 * name -- now silent like every other `generated` edge. */
export function formatEdgeLabel(edge: GraphEdge, toNode: NodeSummary | undefined): string {
  switch (edge.relationship) {
    case "generated":
    case "matched":
    case "scored":
    case "normalized_to":
    case "executed":
    case "sent":
    case "rendered":
    case "selected":
      return "";
    case "returned":
      return edge.ordinal !== null && edge.ordinal !== undefined ? `result #${edge.ordinal + 1}` : "returned";
    case "duplicate_of":
      return "duplicate of";
    case "retried_as":
      return "retried as";
    case "feedback_on":
      return "feedback on";
    case "cleared_threshold":
      return "cleared threshold";
    case "rejected":
    case "deferred":
    case "failed":
      // Real branch outcomes (this candidate/score took a DIFFERENT path
      // than the happy one), not restatements -- stay visible.
      return edge.relationship;
    default:
      return edge.relationship.replace(/_/g, " ");
  }
}

/** The backend's own `emphasized_path` (see observatory/db.py's graph())
 * stops at whatever entity the seed resolved to -- for a candidate/score
 * seed that's the score-attempt, one hop short of the part the owner
 * actually cares about: did it clear the threshold, and did it reach
 * Telegram? Verified live on a real SENT discovery: the backend path ended
 * at "score-attempt", leaving the threshold pass, the render, and the
 * notification all unhighlighted and outside Focus mode -- "the discovery's
 * route" told an incomplete story for the one case (a delivered item) where
 * the full story is the whole point. Extending forward from the path's own
 * last node (BFS over every outbound edge, since the trace graph is a DAG
 * in practice) is safe and general: for a candidate/score seed it reaches
 * exactly threshold -> render -> notification (verified); for an
 * interest/match seed the path already ends at a leaf, so this is a no-op
 * (verified). Pure and DOM-free so it's unit-testable without a graph
 * instance. */
export function extendPath(edges: GraphEdge[], path: ID[]): ID[] {
  if (path.length === 0) return path;
  const outByFrom = new Map<string, ID[]>();
  for (const e of edges) {
    const key = String(e.from);
    if (!outByFrom.has(key)) outByFrom.set(key, []);
    outByFrom.get(key)!.push(e.to);
  }
  const visited = new Set(path.map(String));
  const extended = [...path];
  const queue = [path[path.length - 1]];
  while (queue.length) {
    const current = queue.shift()!;
    for (const next of outByFrom.get(String(current)) ?? []) {
      const key = String(next);
      if (visited.has(key)) continue;
      visited.add(key);
      extended.push(next);
      queue.push(next);
    }
  }
  return extended;
}

/** node_type -> display order for the six named swimlanes; anything else
 * (db.py's swimlane() fallback of 'other') sorts last. Order matches the
 * plan's own listing: Personal/interest state, Council, Mission execution,
 * Candidate pipeline, Scoring/value, Delivery/feedback. */
export const SWIMLANE_ORDER = [
  "interest-state", "council", "mission", "candidate-pipeline", "scoring", "delivery-feedback", "other",
];

export function swimlaneRank(swimlane: string): number {
  const i = SWIMLANE_ORDER.indexOf(swimlane);
  return i === -1 ? SWIMLANE_ORDER.length : i;
}
