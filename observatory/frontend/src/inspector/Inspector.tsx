import { useEffect, useState } from "react";
import { fetchNode } from "../api";
import type { ID, ModelCallDetail, NodeDetail } from "../types";
import { MonospaceViewer } from "./MonospaceViewer";

// Tab identity is its label; only tabs whose content is actually non-empty
// for the selected node are offered (most nodes carry one or two payloads,
// so a fixed 9-tab strip was mostly dead buttons). Timing and database-row
// links live on the Overview now instead of dedicated tabs.
const PAYLOAD_TABS = [
  "Exact input", "Exact output", "Raw response", "Parsed JSON",
  "Reasoning record", "Configuration",
] as const;
type InspectorTab = "Overview" | (typeof PAYLOAD_TABS)[number];

interface Props {
  nodeId: ID | null;
  onClose?: () => void;
  onSelectNode?: (id: ID) => void;
}

function availableTabs(detail: NodeDetail): InspectorTab[] {
  const call = detail.model_calls[0];
  const present: Record<(typeof PAYLOAD_TABS)[number], boolean> = {
    "Exact input": jsonText(detail.input) !== "",
    "Exact output": jsonText(detail.output) !== "",
    "Raw response": !!call?.raw_response_text,
    "Parsed JSON": jsonText(call?.parsed_response_json) !== "",
    "Reasoning record": detail.model_calls.length > 0,
    "Configuration": jsonText(detail.config) !== "",
  };
  return ["Overview", ...PAYLOAD_TABS.filter((t) => present[t])];
}

export function Inspector({ nodeId, onClose, onSelectNode }: Props) {
  const [tab, setTab] = useState<InspectorTab>("Overview");
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (nodeId == null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    fetchNode(nodeId)
      .then((d) => {
        if (!cancelled) {
          setDetail(d);
          // Keep the current tab across node switches when it still applies
          // (comparing siblings), otherwise fall back to Overview.
          setTab((t) => (availableTabs(d).includes(t) ? t : "Overview"));
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (nodeId == null) {
    return <div className="inspector inspector-empty" data-testid="inspector">Select a node to inspect it.</div>;
  }
  if (error) return <div className="inspector inspector-error">{error}</div>;
  if (!detail) return <div className="inspector inspector-loading">Loading...</div>;

  const call = detail.model_calls[0]; // most nodes have at most one; multi-attempt nodes show every attempt in Reasoning record
  const tabs = availableTabs(detail);

  return (
    <div className="inspector" data-testid="inspector">
      <div className="inspector-header">
        <div className="inspector-title">
          <span className="inspector-node-type">{detail.overview.node_type}</span>
          <span className="inspector-label">{detail.overview.label}</span>
        </div>
        {onClose && <button className="inspector-close" onClick={onClose}>Close</button>}
      </div>
      {tabs.length > 1 && (
        <div className="inspector-tabs" role="tablist">
          {tabs.map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </div>
      )}
      <div className="inspector-body">
        {tab === "Overview" && <Overview detail={detail} onSelectNode={onSelectNode} />}
        {tab === "Exact input" && <MonospaceViewer text={jsonText(detail.input)} json truncated={detail.truncated} filename="input.json" />}
        {tab === "Exact output" && <MonospaceViewer text={jsonText(detail.output)} json truncated={detail.truncated} filename="output.json" />}
        {tab === "Raw response" && <MonospaceViewer text={call?.raw_response_text} truncated={detail.truncated} filename="raw_response.txt" />}
        {tab === "Parsed JSON" && <MonospaceViewer text={jsonText(call?.parsed_response_json)} json truncated={detail.truncated} filename="parsed.json" />}
        {tab === "Reasoning record" && <ReasoningRecord detail={detail} />}
        {tab === "Configuration" && <MonospaceViewer text={jsonText(detail.config)} json filename="config.json" />}
      </div>
    </div>
  );
}

function Overview({ detail, onSelectNode }: { detail: NodeDetail; onSelectNode?: (id: ID) => void }) {
  const o = detail.overview;
  const entityUrl = o.entity_type ? detail.row_urls[`${o.entity_type}:${o.entity_id}`] : undefined;
  const preview = primaryPayload(detail);
  return (
    <div className="inspector-overview">
      {o.error && <div className="overview-error">{o.error}</div>}
      {o.summary && <p className="overview-summary">{o.summary}</p>}
      <dl className="overview-facts">
        {o.status && (
          <>
            <dt>Status</dt>
            <dd><span className={`status-chip status-chip-${chipKind(o.status)}`}>{o.status}</span></dd>
          </>
        )}
        {o.started_at && (
          <>
            <dt>Started</dt>
            <dd>{o.started_at}</dd>
          </>
        )}
        {duration(o.started_at, o.finished_at) && (
          <>
            <dt>Duration</dt>
            <dd>{duration(o.started_at, o.finished_at)}</dd>
          </>
        )}
        {isSourceResult(detail) && <SourceResultFacts detail={detail} />}
        {detail.model_calls.map((c) => (
          <CallFacts key={c.id} call={c} many={detail.model_calls.length > 1} />
        ))}
      </dl>
      <Connections
        title="Came from"
        edges={detail.inbound_edges.map((e) => ({ id: e.from, relationship: e.relationship, label: e.from_label, node_type: e.from_node_type }))}
        onSelectNode={onSelectNode}
      />
      <Connections
        title="Led to"
        edges={detail.outbound_edges.map((e) => ({ id: e.to, relationship: e.relationship, label: e.to_label, node_type: e.to_node_type }))}
        onSelectNode={onSelectNode}
      />
      {preview ? (
        <div className="overview-preview">
          <div className="overview-section-title">{preview.title}</div>
          <MonospaceViewer text={preview.text} json={preview.json} truncated={detail.truncated} filename={preview.filename} />
        </div>
      ) : (
        <div className="overview-nothing">No payloads recorded for this node.</div>
      )}
      {(entityUrl || detail.model_calls.length > 0) && (
        <div className="overview-links">
          <div className="overview-section-title">Database rows</div>
          {entityUrl && (
            <a href={entityUrl} target="_blank" rel="noreferrer">{o.entity_type}:{String(o.entity_id)}</a>
          )}
          {detail.model_calls.map((c) => (
            <a key={c.id} href={c.row_url} target="_blank" rel="noreferrer">model_calls:{c.id}</a>
          ))}
        </div>
      )}
    </div>
  );
}

/** One most-informative payload, previewed inline so a click on a node shows
 * its substance immediately instead of an empty pane and a hunt through tabs. */
function primaryPayload(detail: NodeDetail): { title: string; text: string; json: boolean; filename: string } | null {
  const call = detail.model_calls[0];
  if (detail.exact_text) return { title: "Exact text", text: detail.exact_text, json: false, filename: "exact_text.txt" };
  const output = jsonText(detail.output);
  if (output) return { title: "Output", text: output, json: true, filename: "output.json" };
  if (call?.raw_response_text) {
    return { title: "Raw model response", text: call.raw_response_text, json: false, filename: "raw_response.txt" };
  }
  const input = jsonText(detail.input);
  if (input) return { title: "Input", text: input, json: true, filename: "input.json" };
  return null;
}

function CallFacts({ call, many }: { call: ModelCallDetail; many: boolean }) {
  const label = many ? `LLM call #${call.attempt}` : "LLM call";
  const dur = duration(call.started_at, call.finished_at);
  return (
    <>
      <dt>{label}</dt>
      <dd>
        {call.provider}/{call.model} · {call.call_role}
        {dur && <> · {dur}</>}
        {call.validation_result && <> · {call.validation_result}</>}
        {call.error && <span className="error"> · {call.error}</span>}
      </dd>
    </>
  );
}

function Connections({ title, edges, onSelectNode }: {
  title: string;
  edges: { id: ID; relationship: string; label: string; node_type: string }[];
  onSelectNode?: (id: ID) => void;
}) {
  if (edges.length === 0) return null;
  return (
    <div className="overview-connections">
      <div className="overview-section-title">{title}</div>
      <ul>
        {edges.map((e, i) => (
          <li key={i}>
            <span className="connection-rel">{e.relationship.replace(/_/g, " ")}</span>
            {onSelectNode ? (
              <button className="connection-link" onClick={() => onSelectNode(e.id)}>
                {e.node_type}: {e.label}
              </button>
            ) : (
              <span>{e.node_type}: {e.label}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function chipKind(status: string): string {
  if (["error", "failed"].includes(status)) return "error";
  if (["running", "pending", "started", "in_progress"].includes(status)) return "active";
  if (["ok", "done", "success", "sent"].includes(status)) return "ok";
  return "neutral";
}

function duration(started: string | null, finished: string | null): string | null {
  if (!started || !finished) return null;
  const ms = new Date(finished).getTime() - new Date(started).getTime();
  if (!isFinite(ms) || ms < 0) return null;
  return ms < 10000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms / 1000)}s`;
}

function isSourceResult(detail: NodeDetail): boolean {
  return detail.overview.node_type === "raw-result" || detail.overview.node_type === "raw-result-dropped";
}

function SourceResultFacts({ detail }: { detail: NodeDetail }) {
  const output = (detail.output ?? {}) as Record<string, unknown>;
  return (
    <>
      {output.url != null && (
        <>
          <dt>Source URL</dt>
          <dd><a href={String(output.url)} target="_blank" rel="noreferrer">{String(output.url)}</a></dd>
        </>
      )}
      {output.position != null && (
        <>
          <dt>Position</dt>
          <dd>#{String(output.position)} in results</dd>
        </>
      )}
    </>
  );
}

function ReasoningRecord({ detail }: { detail: NodeDetail }) {
  if (detail.model_calls.length === 0) {
    return <div className="reasoning-empty">No model call attached to this node.</div>;
  }
  return (
    <div className="reasoning-record">
      {detail.model_calls.map((c) => (
        <div key={c.id} className="reasoning-attempt">
          <div className="reasoning-attempt-header">
            attempt {c.attempt} -- {c.provider}/{c.model} -- {c.call_role} -- {c.validation_result || "unvalidated"}
            {c.error && <span className="error"> -- {c.error}</span>}
          </div>
          <details open={detail.model_calls.length === 1}>
            <summary>Exact system + user prompt</summary>
            <MonospaceViewer text={[c.exact_system_prompt, c.exact_user_prompt].filter(Boolean).join("\n\n---\n\n")} filename={`prompt-attempt-${c.attempt}.txt`} />
          </details>
          {jsonText(c.usage) !== "" && (
            <details>
              <summary>Usage</summary>
              <MonospaceViewer text={jsonText(c.usage)} json filename={`usage-attempt-${c.attempt}.json`} />
            </details>
          )}
        </div>
      ))}
    </div>
  );
}

function jsonText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
