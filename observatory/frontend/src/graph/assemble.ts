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

export interface DisplayGraph {
  nodes: NodeSummary[];
  edges: GraphEdge[];
}

/** Merge lazily-fetched group children into the base graph. A group node
 * that has been expanded keeps its own card on screen (still `node_type
 * === 'group'`, so it stays the double-click target GraphCanvas uses to
 * collapse back -- there would otherwise be no on-screen affordance left to
 * re-collapse it once its children replaced it) alongside its real children
 * (their own further descendants stay collapsed/hidden until independently
 * expanded -- /api/children only ever returns one level, matching the API).
 * Groups the caller hasn't expanded are left exactly as the API returned
 * them -- this function never drops or invents data, only adds what's been
 * fetched. */
export function mergeExpansion(base: GraphResponse, expanded: ExpandedGroups): DisplayGraph {
  const groupsById = new Map(base.groups.map((g) => [g.group, g]));
  const nodes: NodeSummary[] = [];
  const edges: GraphEdge[] = [...base.edges];
  for (const n of base.nodes) {
    const key = String(n.id);
    nodes.push(n);
    if (n.node_type === "group" && expanded[key]) {
      const group = groupsById.get(key);
      for (const child of expanded[key]) {
        nodes.push(child);
        if (group) {
          edges.push({ from: group.parent_node_id, to: child.id, relationship: group.relationship, ordinal: null });
        }
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

/** Human-readable edge label. The wire format only ever carries a short,
 * fixed relationship word + an optional ordinal (see trace_edges' schema --
 * there is no free-text edge field), so anything more specific ('matched at
 * 0.91', '0.84 >= historical threshold 0.75', 'filtered because ...') comes
 * from the TARGET node's own label/summary, which is where task 1's pipeline
 * wiring puts that text (see PROJECT_STATE.md's pipeline-wiring notes: the
 * threshold/match/prefilter nodes are what actually carry the human
 * sentence, edges only carry the relationship + ordinal that connects them). */
export function formatEdgeLabel(edge: GraphEdge, toNode: NodeSummary | undefined): string {
  switch (edge.relationship) {
    case "generated":
      return toNode ? `generated ${toNode.node_type}` : "generated";
    case "returned":
      return edge.ordinal !== null && edge.ordinal !== undefined ? `result #${edge.ordinal + 1}` : "returned";
    case "duplicate_of":
      return "duplicate of";
    case "retried_as":
      return "retried as";
    case "feedback_on":
      return "feedback on";
    case "normalized_to":
      return "normalized to";
    case "matched":
    case "rejected":
    case "deferred":
    case "cleared_threshold":
    case "scored":
    case "sent":
    case "failed":
    case "selected":
    case "executed":
      // The target card already renders its own label/summary -- repeating it
      // on the edge painted long unreadable text strips across the canvas, so
      // the edge carries only the relationship word.
      return edge.relationship.replace(/_/g, " ");
    default:
      return edge.relationship.replace(/_/g, " ");
  }
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
