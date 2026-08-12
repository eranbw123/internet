import { useEffect, useState } from "react";
import { fetchCompare } from "../api";
import type { CompareResponse } from "../types";
import { MonospaceViewer } from "../inspector/MonospaceViewer";
import { GraphCanvas } from "../graph/GraphCanvas";

interface Props {
  onClose: () => void;
}

/** Pick two traces/generations/model calls -> side-by-side flowcharts (kind
 * = 'run', keyed by run id) or side-by-side prompt/response diff (kind =
 * 'model_call'), all sourced from GET /observatory/api/compare -- see
 * observatory/db.py's compare(). `kind=generation` is a documented,
 * intentional gap (PROJECT_STATE.md) -- the API 400s on it today. */
export function CompareView({ onClose }: Props) {
  const [kind, setKind] = useState<"run" | "model_call">("run");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [result, setResult] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

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
        <input placeholder="A id" value={a} onChange={(e) => setA(e.target.value)} />
        <input placeholder="B id" value={b} onChange={(e) => setB(e.target.value)} />
        <button onClick={onClose}>Close compare</button>
      </div>
      {error && <div className="compare-error">{error}</div>}
      {result?.kind === "run" && <RunCompare result={result} a={a} b={b} />}
      {result?.kind === "model_call" && <ModelCallCompare result={result} />}
    </div>
  );
}

function RunCompare({ result, a, b }: { result: Extract<CompareResponse, { kind: "run" }>; a: string; b: string }) {
  return (
    <div className="compare-run">
      <div className="compare-split">
        <div className="compare-pane">
          <h3>Run {a}</h3>
          <GraphCanvas seed={{ run_id: Number(a) }} selectedNodeId={null} onSelectNode={() => {}} />
        </div>
        <div className="compare-pane">
          <h3>Run {b}</h3>
          <GraphCanvas seed={{ run_id: Number(b) }} selectedNodeId={null} onSelectNode={() => {}} />
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
