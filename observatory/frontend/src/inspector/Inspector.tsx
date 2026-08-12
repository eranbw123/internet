import { useEffect, useState } from "react";
import { fetchNode } from "../api";
import type { ID, NodeDetail } from "../types";
import { MonospaceViewer } from "./MonospaceViewer";

const TABS = [
  "Overview", "Exact input", "Exact output", "Raw response", "Parsed JSON",
  "Reasoning record", "Configuration", "Timing/usage", "Database rows",
] as const;
type InspectorTab = (typeof TABS)[number];

interface Props {
  nodeId: ID | null;
  onClose?: () => void;
}

export function Inspector({ nodeId, onClose }: Props) {
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
        if (!cancelled) setDetail(d);
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

  return (
    <div className="inspector" data-testid="inspector">
      <div className="inspector-header">
        <div className="inspector-title">{detail.overview.node_type}: {detail.overview.label}</div>
        {onClose && <button className="inspector-close" onClick={onClose}>Close</button>}
      </div>
      <div className="inspector-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      <div className="inspector-body">
        {tab === "Overview" && (
          <div className="inspector-overview">
            <div>Status: {detail.overview.status}</div>
            <div>Started: {detail.overview.started_at}</div>
            <div>Finished: {detail.overview.finished_at}</div>
            {detail.overview.error && <div className="error">Error: {detail.overview.error}</div>}
            <div>Summary: {detail.overview.summary}</div>
            {detail.overview.entity_type && detail.row_urls[`${detail.overview.entity_type}:${detail.overview.entity_id}`] && (
              <a
                href={detail.row_urls[`${detail.overview.entity_type}:${detail.overview.entity_id}`]}
                target="_blank" rel="noreferrer"
              >
                Open {detail.overview.entity_type} row in datasette
              </a>
            )}
            {/* source-result nodes carry their exact_text (raw result) and,
                when available, a source URL in output_json -- shown here so
                the source-of-truth position is visible without switching tabs */}
            {isSourceResult(detail) && <SourceResultSummary detail={detail} />}
          </div>
        )}
        {tab === "Exact input" && <MonospaceViewer text={jsonText(detail.input)} json truncated={detail.truncated} filename="input.json" />}
        {tab === "Exact output" && <MonospaceViewer text={jsonText(detail.output)} json truncated={detail.truncated} filename="output.json" />}
        {tab === "Raw response" && <MonospaceViewer text={call?.raw_response_text} truncated={detail.truncated} filename="raw_response.txt" />}
        {tab === "Parsed JSON" && <MonospaceViewer text={jsonText(call?.parsed_response_json)} json truncated={detail.truncated} filename="parsed.json" />}
        {tab === "Reasoning record" && <ReasoningRecord detail={detail} />}
        {tab === "Configuration" && <MonospaceViewer text={jsonText(detail.config)} json filename="config.json" />}
        {tab === "Timing/usage" && <TimingUsage detail={detail} />}
        {tab === "Database rows" && <DatabaseRows detail={detail} />}
      </div>
    </div>
  );
}

function isSourceResult(detail: NodeDetail): boolean {
  return detail.overview.node_type === "raw-result" || detail.overview.node_type === "raw-result-dropped";
}

function SourceResultSummary({ detail }: { detail: NodeDetail }) {
  const output = (detail.output ?? {}) as Record<string, unknown>;
  return (
    <div className="source-result">
      {output.url != null && <div>Source URL: {String(output.url)}</div>}
      {output.position != null && <div>Position in results: #{String(output.position)}</div>}
    </div>
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
        </div>
      ))}
    </div>
  );
}

function TimingUsage({ detail }: { detail: NodeDetail }) {
  return (
    <div className="timing-usage">
      <div>Started: {detail.overview.started_at}</div>
      <div>Finished: {detail.overview.finished_at}</div>
      {detail.model_calls.map((c) => (
        <div key={c.id} className="timing-call">
          <div>Attempt {c.attempt}: {c.started_at} - {c.finished_at}</div>
          <MonospaceViewer text={jsonText(c.usage)} json filename={`usage-attempt-${c.attempt}.json`} />
        </div>
      ))}
    </div>
  );
}

function DatabaseRows({ detail }: { detail: NodeDetail }) {
  const urls = Object.entries(detail.row_urls);
  return (
    <div className="database-rows">
      {urls.length === 0 && <div>No linked database rows for this node.</div>}
      <ul>
        {urls.map(([key, url]) => (
          <li key={key}>
            <a href={url} target="_blank" rel="noreferrer">{key}</a>
          </li>
        ))}
        {detail.model_calls.map((c) => (
          <li key={`mc-${c.id}`}>
            <a href={c.row_url} target="_blank" rel="noreferrer">model_calls:{c.id}</a>
          </li>
        ))}
      </ul>
    </div>
  );
}

function jsonText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
