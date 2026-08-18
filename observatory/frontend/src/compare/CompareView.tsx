import { useEffect, useState } from "react";
import { fetchCompare, fetchRuns } from "../api";
import type { CompareResponse, ID, RunOption } from "../types";
import { MonospaceViewer } from "../inspector/MonospaceViewer";
import { GraphCanvas } from "../graph/GraphCanvas";

interface Props {
  /** Optional: on a phone the bottom tab bar is the way out of this surface,
   * so there is no in-view Close button to render. */
  onClose?: () => void;
  /** Pre-filled from "Compare this run" in the Inspector, so the view can be
   * opened with an input already in hand instead of demanding a number the UI
   * never showed anywhere. */
  initialA?: string;
  /** Opens a node from either pane in the main inspector. Without it the
   * compare panes' cards were wired to a no-op -- a dead click on every card. */
  onSelectNode?: (id: ID) => void;
}

/** Pick two traces/generations/model calls -> side-by-side flowcharts (kind
 * = 'run', keyed by run id) or side-by-side prompt/response diff (kind =
 * 'model_call'), all sourced from GET /observatory/api/compare -- see
 * observatory/db.py's compare(). `kind=generation` is a documented,
 * intentional gap (PROJECT_STATE.md) -- the API 400s on it today. */
export function CompareView({ onClose, initialA, onSelectNode }: Props) {
  const [kind, setKind] = useState<"run" | "model_call">("run");
  const [a, setA] = useState(initialA ?? "");
  const [b, setB] = useState("");
  const [result, setResult] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunOption[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetchRuns()
      .then((r) => {
        if (!cancelled) setRuns(r.runs);
      })
      .catch(() => {
        // The text inputs still work; a missing index just means no picker.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!a || !b) {
      setResult(null);
      return;
    }
    let cancelled = false;
    setError(null);
    fetchCompare(kind, a, b)
      .then((r) => {
        if (!cancelled) setResult(r);
      })
      .catch((e) => {
        if (!cancelled) {
          setResult(null);
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [kind, a, b]);

  return (
    <div className="compare-view" data-testid="compare-view">
      <div className="compare-header">
        <select value={kind} onChange={(e) => setKind(e.target.value as "run" | "model_call")}>
          <option value="run">Compare traces (run)</option>
          <option value="model_call">Compare model calls</option>
        </select>
        {kind === "run" && runs.length > 0 ? (
          <>
            <RunPicker label="A" runs={runs} value={a} onChange={setA} />
            <RunPicker label="B" runs={runs} value={b} onChange={setB} />
          </>
        ) : (
          <>
            <input placeholder="A id" aria-label="A id" value={a} onChange={(e) => setA(e.target.value)} />
            <input placeholder="B id" aria-label="B id" value={b} onChange={(e) => setB(e.target.value)} />
          </>
        )}
        {onClose && <button onClick={onClose}>Close compare</button>}
      </div>
      {error && <div className="compare-error">{error}</div>}
      {/* Until both sides are picked this rendered nothing at all -- on a
          phone that is a full screen of blank white under two dropdowns, which
          reads as a broken page rather than as a screen waiting for input. */}
      {!error && !result && (
        <div className="ws-empty" data-testid="compare-empty">
          <p><strong>Pick two {kind === "run" ? "runs" : "model calls"} to compare.</strong></p>
          <p>
            {kind === "run"
              ? "Choose a run in A and another in B. You get the two traces side by side, plus"
                + " what changed between them: which nodes only one run has, and which shared"
                + " nodes differ."
              : "Enter two model-call ids. You get the two calls' prompts and responses diffed"
                + " against each other."}
          </p>
        </div>
      )}
      {result?.kind === "run" && <RunCompare result={result} a={a} b={b} onSelectNode={onSelectNode} />}
      {result?.kind === "model_call" && <ModelCallCompare result={result} />}
    </div>
  );
}

function RunPicker({ label, runs, value, onChange }: {
  label: string; runs: RunOption[]; value: string; onChange: (v: string) => void;
}) {
  return (
    <select aria-label={`${label} run`} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">run {label}…</option>
      {runs.map((r) => (
        <option key={r.id} value={String(r.id)}>
          #{r.id} · {r.kind} · {r.status}{r.node_count != null ? ` · ${r.node_count} nodes` : ""}
        </option>
      ))}
    </select>
  );
}

function RunCompare({ result, a, b, onSelectNode }: {
  result: Extract<CompareResponse, { kind: "run" }>; a: string; b: string; onSelectNode?: (id: ID) => void;
}) {
  return (
    <div className="compare-run">
      <div className="compare-split">
        <div className="compare-pane">
          <h3>Run {a}</h3>
          <GraphCanvas seed={{ run_id: Number(a) }} selectedNodeId={null} onSelectNode={onSelectNode ?? (() => {})} />
        </div>
        <div className="compare-pane">
          <h3>Run {b}</h3>
          <GraphCanvas seed={{ run_id: Number(b) }} selectedNodeId={null} onSelectNode={onSelectNode ?? (() => {})} />
        </div>
      </div>
      <div className="compare-diffs">
        <DiffSection title="Nodes" data={result.nodes} />
        <DiffSection title="Edges" data={result.edges} />
        <div className="diff-section">
          <h4>Changed context fields</h4>
          <MonospaceViewer text={result.context_diff.join("\n")} filename="context-diff.txt" />
        </div>
        <DiffSection title="Changed mission branches" data={result.mission_branches_diff} />
        <DiffSection title="Changed score decisions" data={result.score_decisions_diff} />
        <DiffSection title="Changed delivery outcome" data={result.delivery_outcome_diff} />
      </div>
    </div>
  );
}

function DiffSection({ title, data }: { title: string; data: Record<string, unknown> }) {
  return (
    <div className="diff-section" data-testid={`diff-${title}`}>
      <h4>{title}</h4>
      <MonospaceViewer text={JSON.stringify(data, null, 2)} json filename={`${title.toLowerCase()}-diff.json`} />
    </div>
  );
}

function ModelCallCompare({ result }: { result: Extract<CompareResponse, { kind: "model_call" }> }) {
  return (
    <div className="compare-model-call">
      <div className="diff-section" data-testid="prompt-diff">
        <h4>Prompt diff</h4>
        <MonospaceViewer text={result.prompt_diff.join("\n")} filename="prompt-diff.txt" />
      </div>
      <div className="diff-section" data-testid="response-diff">
        <h4>Response diff</h4>
        <MonospaceViewer text={result.response_diff.join("\n")} filename="response-diff.txt" />
      </div>
    </div>
  );
}
