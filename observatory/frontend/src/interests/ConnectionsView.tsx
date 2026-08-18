/** Interest-to-interest connections.
 *
 * Reuses the Observatory's existing graph stack (@xyflow/react + elkjs) rather
 * than adding a dependency -- the same two libraries the trace explorer
 * already ships, laid out with ELK's `stress` algorithm because this graph has
 * no direction or ranking to encode: it is a similarity mesh, not a pipeline.
 *
 * What the picture encodes, and why:
 *   - node size = items that cleared the bar, so the interests actually
 *     producing something are visibly bigger than the ones that are not;
 *   - edge width = weight, edge dash = kind;
 *   - clicking an edge shows its evidence, because the number on an edge is
 *     the whole question here.
 *
 * The measurement that shaped this: raw keyword co-occurrence is useless as a
 * connection signal in this system -- 97% of items match two or more
 * interests, mean 10.7 -- so the top raw pair (578 shared items) is an
 * artefact of a loose matcher, not a relationship. Edges are therefore
 * lift-normalised, and the evidence panel shows lift AND the raw shared count
 * so the difference between them stays visible instead of being averaged away.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { EdgeKind, InterestEdge, InterestStat } from "./types";
import { defaultElk, type ElkLike } from "../graph/elkLayout";
import { useThemeTokens } from "../useThemeTokens";
import { BidiText, Quote, guessLang } from "./Bidi";

const NODE_W = 190;
const NODE_H = 56;

const TOKENS = [
  "--accent", "--fg-muted", "--fg-faint", "--border", "--surface", "--group-fg",
] as const;

const EDGE_DASH: Record<EdgeKind, string | undefined> = {
  co_engagement: undefined,
  semantic: "6 4",
  bridge_offer: "2 3",
  parent: "12 5",
};

const KIND_LABEL: Record<EdgeKind, string> = {
  co_engagement: "co-engagement",
  semantic: "semantic",
  bridge_offer: "proposed bridge",
  parent: "parent",
};

interface NodeData extends Record<string, unknown> {
  label: string;
  keyName: string;
  aboveBar: number;
  lifecycle: string;
  dead: boolean;
}

function InterestNode({ data, selected }: NodeProps) {
  const d = data as NodeData;
  return (
    <div className={`cx-node ${d.dead ? "cx-dead" : ""} ${selected ? "cx-selected" : ""}`}>
      {/* React Flow needs handles for edges to attach; they are visually
          suppressed because this graph has no direction to show. */}
      <Handle type="target" position={Position.Left} className="cx-handle" />
      <BidiText className="cx-node-title" block lang={guessLang(d.label)}>{d.label}</BidiText>
      <div className="cx-node-meta">
        <span className={`chip chip-${d.lifecycle === "active" ? "ok" : d.lifecycle === "decaying" ? "warn" : "muted"}`}>
          {d.lifecycle}
        </span>
        <span className="cx-node-count">{d.aboveBar} above bar</span>
      </div>
      <Handle type="source" position={Position.Right} className="cx-handle" />
    </div>
  );
}

const nodeTypes = { interest: InterestNode };

interface Props {
  edges: InterestEdge[];
  interests: InterestStat[];
  loading: boolean;
}

function Graph({ edges, interests, loading }: Props) {
  const tokens = useThemeTokens(TOKENS);
  const [minWeight, setMinWeight] = useState(0.4);
  const [selectedEdge, setSelectedEdge] = useState<InterestEdge | null>(null);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }> | null>(null);

  const byKey = useMemo(
    () => new Map(interests.map((i) => [i.key, i])),
    [interests],
  );

  const shown = useMemo(
    () => edges.filter((e) => e.weight >= minWeight),
    [edges, minWeight],
  );

  const nodeKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const e of shown) { keys.add(e.a); keys.add(e.b); }
    return [...keys];
  }, [shown]);

  // ELK runs off the render path and writes positions back; until it answers,
  // nothing is drawn (a pre-layout flash of stacked nodes at 0,0 is worse
  // than a beat of "laying out").
  useEffect(() => {
    let cancelled = false;
    if (nodeKeys.length === 0) { setPositions({}); return; }
    (async () => {
      const elk: ElkLike = await defaultElk();
      const graph = {
        id: "root",
        layoutOptions: {
          "elk.algorithm": "stress",
          "elk.stress.desiredEdgeLength": "150",
          "elk.spacing.nodeNode": "36",
          // The interest graph is not one connected mesh -- it is several
          // clusters (sleep/pharmacology, AI infra, social psych) with no
          // edges between them. Left alone, stress flings those components to
          // opposite corners and fitView then shrinks everything to
          // illegibility. Packing them puts the whole picture on screen at a
          // scale where the labels can still be read.
          "elk.separateConnectedComponents": "true",
          "elk.spacing.componentComponent": "70",
        },
        children: nodeKeys.map((k) => ({ id: k, width: NODE_W, height: NODE_H })),
        edges: shown.map((e, i) => ({ id: `e${i}`, sources: [e.a], targets: [e.b] })),
      };
      const laid = await elk.layout(graph);
      if (cancelled) return;
      const out: Record<string, { x: number; y: number }> = {};
      for (const child of laid.children || []) {
        out[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
      }
      setPositions(out);
    })().catch(() => { if (!cancelled) setPositions({}); });
    return () => { cancelled = true; };
  }, [nodeKeys, shown]);

  const rfNodes: Node[] = useMemo(() => {
    if (!positions) return [];
    return nodeKeys.map((key) => {
      const stat = byKey.get(key);
      return {
        id: key,
        type: "interest",
        position: positions[key] ?? { x: 0, y: 0 },
        data: {
          label: stat?.title ?? key,
          keyName: key,
          aboveBar: stat?.above_bar ?? 0,
          lifecycle: stat?.lifecycle ?? "proposed",
          dead: stat?.dead_weight ?? false,
        } satisfies NodeData,
      };
    });
  }, [positions, nodeKeys, byKey]);

  // The only colour literals in this PR live in the `||` fallbacks below, and
  // they exist because React Flow takes colours as JS props rather than CSS:
  // if getComputedStyle ever returned an empty string (a token renamed, the
  // stylesheet not yet applied) an edge with no stroke would be invisible,
  // which is a worse failure than a slightly-wrong colour. They are never
  // reached while tokens.fallback.css or PR K's palette is loaded.
  const rfEdges: Edge[] = useMemo(() => shown.map((e, i) => {
    const isSelected = selectedEdge === e;
    return {
      id: `e${i}`,
      source: e.a,
      target: e.b,
      // Colours come from the resolved tokens, so the graph layer themes with
      // the page instead of freezing whatever was right at build time.
      style: {
        stroke: e.kind === "bridge_offer"
          ? (tokens["--group-fg"] || undefined)
          : isSelected ? (tokens["--accent"] || undefined) : (tokens["--fg-faint"] || undefined),
        strokeWidth: 1 + e.weight * 4,
        strokeDasharray: EDGE_DASH[e.kind],
        opacity: isSelected ? 1 : 0.85,
      },
      label: e.evidence.lift !== undefined ? `${e.evidence.lift.toFixed(1)}x` : undefined,
      labelStyle: { fill: tokens["--fg-muted"] || undefined, fontSize: 10 },
      labelBgStyle: { fill: tokens["--surface"] || undefined },
      labelBgPadding: [3, 2] as [number, number],
    };
  }), [shown, tokens, selectedEdge]);

  const onEdgeClick = useCallback((_: unknown, edge: Edge) => {
    const idx = Number(edge.id.slice(1));
    setSelectedEdge(shown[idx] ?? null);
  }, [shown]);

  if (loading) return <p className="ws-loading">Loading connections...</p>;

  return (
    <div className="connections" data-testid="connections-view">
      <div className="cx-controls">
        <label className="cx-weight">
          Minimum weight <strong>{minWeight.toFixed(2)}</strong>
          <input
            type="range" min={0} max={1} step={0.01} value={minWeight}
            onChange={(e) => setMinWeight(Number(e.target.value))}
          />
        </label>
        <span className="prov-muted">{shown.length} of {edges.length} edges, {nodeKeys.length} interests</span>
        <ul className="cx-legend">
          {(Object.keys(KIND_LABEL) as EdgeKind[]).map((k) => (
            <li key={k}>
              <span className={`cx-legend-line cx-legend-${k}`} aria-hidden="true" />
              {KIND_LABEL[k]}
            </li>
          ))}
        </ul>
      </div>

      <div className="cx-body">
        <div className="cx-canvas">
          {positions === null ? (
            <p className="ws-loading">Laying out...</p>
          ) : (
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              onEdgeClick={onEdgeClick}
              onPaneClick={() => setSelectedEdge(null)}
              fitView
              /* Cap the zoom-out: fitView alone shrank a 30-node stress layout
                 until the labels were unreadable, which is a picture of
                 nothing. Better to open at a legible scale and let the user
                 pan. */
              fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
              proOptions={{ hideAttribution: true }}
              minZoom={0.2}
            >
              <Background color={tokens["--border"] || undefined} gap={20} />
              <Controls showInteractive={false} />
            </ReactFlow>
          )}
        </div>

        <aside className="cx-inspector">
          {selectedEdge ? (
            <>
              <h3 className="cx-inspector-title">
                <code className="key-chip">{selectedEdge.a}</code>
                <span className="prov-bridge-x">x</span>
                <code className="key-chip">{selectedEdge.b}</code>
              </h3>
              <dl className="cx-facts">
                <dt>Kind</dt><dd>{KIND_LABEL[selectedEdge.kind]}</dd>
                <dt>Weight</dt><dd>{selectedEdge.weight.toFixed(2)}</dd>
                {selectedEdge.evidence.lift !== undefined && (
                  <>
                    <dt>Lift</dt>
                    <dd>
                      {selectedEdge.evidence.lift.toFixed(1)}x
                      <span className="prov-muted"> more than chance</span>
                    </dd>
                  </>
                )}
                {selectedEdge.evidence.shared_items !== undefined && (
                  <>
                    <dt>Shared items</dt>
                    <dd>
                      {selectedEdge.evidence.shared_items}
                      <span className="prov-muted"> raw co-match, not a relationship on its own</span>
                    </dd>
                  </>
                )}
                {selectedEdge.computed_at && (
                  <><dt>Computed</dt><dd>{selectedEdge.computed_at.slice(0, 10)}</dd></>
                )}
              </dl>
              {selectedEdge.evidence.note && (
                <p className="prov-line prov-muted">{selectedEdge.evidence.note}</p>
              )}
              {selectedEdge.evidence.quotes && selectedEdge.evidence.quotes.length > 0 && (
                <section className="prov-section">
                  <h4 className="prov-heading">Bridging conversations</h4>
                  <ol className="prov-quotes">
                    {selectedEdge.evidence.quotes.map((q, i) => (
                      <li className="prov-quote-row" key={i}>
                        <div className="prov-quote-meta">
                          <time dateTime={q.date}>{q.date}</time>
                          {q.conversation_title && (
                            <BidiText className="prov-conv" lang={guessLang(q.conversation_title)}>
                              {q.conversation_title}
                            </BidiText>
                          )}
                        </div>
                        <Quote lang={q.lang}>{q.quote}</Quote>
                      </li>
                    ))}
                  </ol>
                </section>
              )}
            </>
          ) : (
            <p className="prov-muted cx-hint">
              Select an edge to see what connects the two interests -- the measured lift, the
              raw shared-item count behind it, and any bridging conversations.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}

export function ConnectionsView(props: Props) {
  return (
    <ReactFlowProvider>
      <Graph {...props} />
    </ReactFlowProvider>
  );
}
