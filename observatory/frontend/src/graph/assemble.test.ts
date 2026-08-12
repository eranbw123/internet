import { describe, expect, it } from "vitest";
import { applyFocus, formatEdgeLabel, labelScore, mergeExpansion } from "./assemble";
import type { GraphResponse, NodeSummary } from "../types";

function node(overrides: Partial<NodeSummary>): NodeSummary {
  return {
    id: 1, run_id: 1, node_type: "candidate", swimlane: "candidate-pipeline",
    entity_type: null, entity_id: null, label: "n", status: "ok", summary: null,
    started_at: null, finished_at: null, error: null,
    ...overrides,
  };
}

// Mirrors observatory/db.py's graph() output shape for a small tick: a
// generation node fans out to a collapsed group of 6 raw-result siblings,
// plus one node NOT part of that group (a mission), matching the plan's own
// example ('a generation node's generated children are one council-context
// node plus 3 different mission nodes').
function fixture(): GraphResponse {
  return {
    run_id: 1, run_ids: [1], focus_node_id: 20,
    nodes: [
      node({ id: 1, node_type: "generation", swimlane: "council", label: "gen" }),
      node({ id: 2, node_type: "mission", swimlane: "mission", label: "mission A" }),
      node({
        id: "10:returned:raw-result", node_type: "group", swimlane: "mission",
        label: "6 raw-result", child_count: 6,
      }),
      node({ id: 20, node_type: "threshold", swimlane: "scoring", label: "0.84 >= 0.75", summary: "0.84 >= historical threshold 0.75" }),
    ],
    edges: [
      { from: 1, to: 2, relationship: "generated", ordinal: 0 },
      { from: 2, to: "10:returned:raw-result", relationship: "returned", ordinal: 0 },
      { from: 2, to: 20, relationship: "scored", ordinal: null },
    ],
    groups: [
      { group: "10:returned:raw-result", parent_node_id: 2, relationship: "returned", child_node_type: "raw-result", child_count: 6 },
    ],
    emphasized_path: [1, 2, 20],
  };
}

describe("mergeExpansion", () => {
  it("leaves an unexpanded group exactly as the API returned it", () => {
    const g = fixture();
    const merged = mergeExpansion(g, {});
    expect(merged.nodes.map((n) => n.id)).toEqual([1, 2, "10:returned:raw-result", 20]);
    expect(merged.edges).toHaveLength(3);
  });

  it("adds fetched children as chips inside the group's own card, sorted best-score-first, with no new edges", () => {
    const g = fixture();
    const children: NodeSummary[] = [
      node({ id: 101, node_type: "raw-result", swimlane: "mission", label: "low: 0.12" }),
      node({ id: 102, node_type: "raw-result", swimlane: "mission", label: "high: 0.91" }),
      node({ id: 103, node_type: "raw-result", swimlane: "mission", label: "no score here" }),
    ];
    const merged = mergeExpansion(g, { "10:returned:raw-result": children });
    const ids = merged.nodes.map((n) => n.id);
    // the group card itself is still on screen -- it's the only click target
    // GraphCanvas has to collapse back via collapseGroup()
    expect(ids).toContain("10:returned:raw-result");
    expect(ids).toEqual(expect.arrayContaining([1, 2, 101, 102, 103, 20]));
    // every child carries the group's own key as containerId, not a fresh edge --
    // a candidate's matches interleaving with an unrelated candidate's collapsed
    // group in the same lane column (verified live) is exactly what per-child
    // edges + free-floating peer nodes produced; a shared containerId is what
    // elkLayout.ts's chip grid keys off instead.
    const chipped = merged.nodes.filter((n) => n.containerId === "10:returned:raw-result");
    expect(chipped.map((n) => n.id)).toEqual([102, 101, 103]); // 0.91, 0.12, then no-score last
    // no edges added at all -- the group's own single inbound edge is the
    // only connector, regardless of how many children it has
    expect(merged.edges).toHaveLength(g.edges.length);
    expect(merged.nodes).toHaveLength(g.nodes.length + 3);
  });

  it("collapsing back (omitting the group from `expanded`) reproduces the original graph", () => {
    const g = fixture();
    const children: NodeSummary[] = [node({ id: 101, node_type: "raw-result" })];
    const expandedMerge = mergeExpansion(g, { "10:returned:raw-result": children });
    const collapsedAgain = mergeExpansion(g, {});
    expect(collapsedAgain).not.toEqual({ nodes: expandedMerge.nodes, edges: expandedMerge.edges });
    expect(collapsedAgain.nodes.map((n) => n.id)).toEqual(g.nodes.map((n) => n.id));
  });
});

describe("labelScore", () => {
  it("extracts a trailing ': N.NN' score", () => {
    expect(labelScore("speculative-fiction-ideas: 0.24")).toBe(0.24);
  });
  it("extracts a trailing integer score", () => {
    expect(labelScore("some-key: 1")).toBe(1);
  });
  it("returns null when there is no trailing score", () => {
    expect(labelScore("Concept Embedding Models: Beyond the Trade-Off")).toBeNull();
  });
  it("returns null for null/empty labels", () => {
    expect(labelScore(null)).toBeNull();
    expect(labelScore("")).toBeNull();
  });
});

describe("applyFocus", () => {
  it("hides every node off the emphasized path without touching the source graph", () => {
    const g = fixture();
    const merged = mergeExpansion(g, {});
    const focused = applyFocus(merged, g.emphasized_path, true);
    expect(focused.nodes.map((n) => n.id)).toEqual([1, 2, 20]);
    expect(focused.hiddenCount).toBe(1); // the group node is off-path
    // source graph (merged.nodes) is untouched -- focus is a view, not a mutation
    expect(merged.nodes).toHaveLength(4);
  });

  it("turning focus back off reproduces the full input exactly", () => {
    const g = fixture();
    const merged = mergeExpansion(g, {});
    applyFocus(merged, g.emphasized_path, true); // simulate having focused once
    const unfocused = applyFocus(merged, g.emphasized_path, false);
    expect(unfocused.nodes).toEqual(merged.nodes);
    expect(unfocused.edges).toEqual(merged.edges);
    expect(unfocused.hiddenCount).toBe(0);
  });

  it("is a no-op when there is no emphasized path", () => {
    const g = fixture();
    const merged = mergeExpansion(g, {});
    const focused = applyFocus(merged, [], true);
    expect(focused.nodes).toEqual(merged.nodes);
  });
});

describe("formatEdgeLabel", () => {
  it("renders 'generated <type>' for a generated edge", () => {
    const label = formatEdgeLabel({ from: 1, to: 2, relationship: "generated", ordinal: 0 }, node({ node_type: "mission" }));
    expect(label).toBe("generated mission");
  });

  it("renders 'result #N' (1-indexed) for a returned edge", () => {
    const label = formatEdgeLabel({ from: 1, to: 2, relationship: "returned", ordinal: 3 }, undefined);
    expect(label).toBe("result #4");
  });

  it("renders 'duplicate of' for a duplicate_of edge", () => {
    expect(formatEdgeLabel({ from: 1, to: 2, relationship: "duplicate_of", ordinal: null }, undefined)).toBe("duplicate of");
  });

  it("keeps threshold/match/rejection edges to the relationship word (card shows the summary)", () => {
    const label = formatEdgeLabel(
      { from: 1, to: 20, relationship: "cleared_threshold", ordinal: null },
      node({ summary: "0.84 >= historical threshold 0.75" }),
    );
    expect(label).toBe("cleared threshold");
  });

  it("falls back to a humanized relationship when the target node is missing", () => {
    expect(formatEdgeLabel({ from: 1, to: 2, relationship: "cleared_threshold", ordinal: null }, undefined)).toBe("cleared threshold");
  });
});
