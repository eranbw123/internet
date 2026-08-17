import { Fragment, useEffect, useMemo, useState } from "react";
import { fetchNode, fetchPromptTemplate } from "../api";
import { labelScore } from "../graph/assemble";
import type { ID, ModelCallDetail, NodeDetail, PromptTemplate } from "../types";
import { MonospaceViewer } from "./MonospaceViewer";
import { splitPromptSections } from "./promptSections";

// Tab identity is its label; only tabs whose content is actually non-empty
// for the selected node are offered (most nodes carry one or two payloads,
// so a fixed 9-tab strip was mostly dead buttons). Timing and database-row
// links live on the Overview now instead of dedicated tabs.
const PAYLOAD_TABS = [
  "Prompt", "Exact text", "Exact input", "Exact output", "Raw response", "Parsed JSON",
  "Reasoning record", "Configuration",
] as const;
type InspectorTab = "Overview" | (typeof PAYLOAD_TABS)[number];

/** Tabs whose content is scoped to ONE model call, so they follow the attempt
 * picker rather than the node as a whole. */
const CALL_SCOPED_TABS: InspectorTab[] = ["Prompt", "Raw response", "Parsed JSON"];

interface Props {
  nodeId: ID | null;
  onClose?: () => void;
  onSelectNode?: (id: ID) => void;
  /** Opens Compare with this run pre-filled. Compare asks for run ids as raw
   * numbers, and this is the only place in the UI one is ever shown. */
  onCompareRun?: (runId: number) => void;
}

/** The attempt a reader means when they say "what did the model answer?".
 *
 * db.py returns model_calls in chronological order, and retried calls are the
 * norm rather than the exception (measured: 89.5% of call-bearing nodes have
 * more than one attempt, and attempt #1 is typically the errored one with
 * raw_response_text NULL). Reading calls[0] therefore showed the *failure* --
 * or, since the payload tabs were gated on that same call, showed nothing at
 * all and left the real answer unreachable in the UI.
 *
 * Preference order: the last attempt that validated cleanly, else the last
 * attempt that produced any response text at all, else the last attempt (so a
 * node whose every attempt errored still reports its final error).
 */
export function bestCall(calls: ModelCallDetail[]): ModelCallDetail | undefined {
  if (calls.length === 0) return undefined;
  const withResponse = calls.filter((c) => c.raw_response_text != null);
  const valid = withResponse.filter((c) => c.validation_result === "valid" && !c.error);
  return valid[valid.length - 1] ?? withResponse[withResponse.length - 1] ?? calls[calls.length - 1];
}

// A call-scoped tab is offered when ANY attempt carries that payload, not just
// the selected one -- so switching attempts moves content in and out of a
// stable tab strip instead of making tabs appear and vanish underfoot.
/** Calls the payload tabs can read: the node's own, or -- when it has none --
 * the ones borrowed from the adjacent node that explains it. */
export function promptCalls(detail: NodeDetail): ModelCallDetail[] {
  return detail.model_calls.length > 0 ? detail.model_calls : (detail.related_model_calls ?? []);
}

function availableTabs(detail: NodeDetail): InspectorTab[] {
  const calls = promptCalls(detail);
  const present: Record<(typeof PAYLOAD_TABS)[number], boolean> = {
    "Prompt": calls.some((c) => !!c.exact_user_prompt),
    "Exact text": (detail.exact_text ?? "") !== "",
    "Exact input": jsonText(detail.input) !== "",
    "Exact output": jsonText(detail.output) !== "",
    "Raw response": calls.some((c) => !!c.raw_response_text),
    "Parsed JSON": calls.some((c) => jsonText(c.parsed_response_json) !== ""),
    "Reasoning record": calls.length > 0,
    "Configuration": jsonText(detail.config) !== "",
  };
  return ["Overview", ...PAYLOAD_TABS.filter((t) => present[t])];
}

export function Inspector({ nodeId, onClose, onSelectNode, onCompareRun }: Props) {
  const [tab, setTab] = useState<InspectorTab>("Overview");
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedCallId, setSelectedCallId] = useState<number | null>(null);

  useEffect(() => {
    if (nodeId == null) {
      setDetail(null);
      setError(null);
      return;
    }
    let cancelled = false;
    // Clearing the error here (and on success) is what keeps one failed fetch
    // from bricking the panel: without it the `if (error)` branch below won
    // forever, over every subsequently selected node, for the whole session.
    setError(null);
    fetchNode(nodeId)
      .then((d) => {
        if (!cancelled) {
          setDetail(d);
          setError(null);
          setSelectedCallId(bestCall(promptCalls(d))?.id ?? null);
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

  const calls = promptCalls(detail);
  const call = calls.find((c) => c.id === selectedCallId) ?? bestCall(calls);
  const tabs = availableTabs(detail);
  const showAttempts = calls.length > 1 && CALL_SCOPED_TABS.includes(tab);

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
        {showAttempts && (
          <AttemptPicker calls={calls} selectedId={call?.id ?? null} onSelect={setSelectedCallId} />
        )}
        {tab === "Overview" && <Overview detail={detail} onSelectNode={onSelectNode} onCompareRun={onCompareRun} />}
        {tab === "Prompt" && <PromptTab call={call} onSelectNode={onSelectNode} />}
        {tab === "Exact text" && <MonospaceViewer text={detail.exact_text} truncated={detail.truncated} filename="exact_text.txt" />}
        {tab === "Exact input" && <MonospaceViewer text={jsonText(detail.input)} json truncated={detail.truncated} filename="input.json" />}
        {tab === "Exact output" && <MonospaceViewer text={jsonText(detail.output)} json truncated={detail.truncated} filename="output.json" />}
        {tab === "Raw response" && (
          call?.raw_response_text
            ? <MonospaceViewer text={call.raw_response_text} truncated={detail.truncated} filename="raw_response.txt" />
            : <AttemptEmpty call={call} what="returned no response text" />
        )}
        {tab === "Parsed JSON" && (
          jsonText(call?.parsed_response_json) !== ""
            ? <MonospaceViewer text={jsonText(call?.parsed_response_json)} json truncated={detail.truncated} filename="parsed.json" />
            : <AttemptEmpty call={call} what="produced no parsed JSON" />
        )}
        {tab === "Reasoning record" && <ReasoningRecord detail={detail} />}
        {tab === "Configuration" && <MonospaceViewer text={jsonText(detail.config)} json filename="config.json" />}
      </div>
    </div>
  );
}

/** The prompt, front and centre.
 *
 * This is the thing the Observatory most needed to show and least did: it was
 * reachable only on score-attempt nodes, only under "Reasoning record", and
 * only inside a collapsed <details> -- on a node type nobody clicks, since the
 * cards that tell the story (candidate, threshold, score-debug) carry no model
 * calls of their own. The backend now lends those nodes their neighbour's
 * calls, and this tab renders the result in one click from anywhere on the
 * scoring path.
 */
function PromptTab({ call, onSelectNode }: { call: ModelCallDetail | undefined; onSelectNode?: (id: ID) => void }) {
  const [template, setTemplate] = useState<PromptTemplate | null>(null);
  const [templateError, setTemplateError] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);

  useEffect(() => {
    setTemplate(null);
    setTemplateError(null);
    setShowDiff(false);
  }, [call?.id]);

  if (!call) return <div className="reasoning-empty">No model call attached to this node.</div>;
  const text = [call.exact_system_prompt, call.exact_user_prompt].filter(Boolean).join("\n\n---\n\n");
  if (!text) return <AttemptEmpty call={call} what="recorded no prompt" />;

  async function loadTemplate() {
    if (!call) return;
    try {
      const t = await fetchPromptTemplate(call.id);
      setTemplate(t);
      setShowDiff(true);
    } catch (e) {
      setTemplateError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="prompt-tab">
      {call.via_node_id != null && (
        <div className="prompt-provenance">
          This prompt belongs to the {call.via_node_type} node that produced this one
          {onSelectNode ? (
            <> — <button className="connection-link" onClick={() => onSelectNode(call.via_node_id!)}>open #{call.via_node_id}</button></>
          ) : (
            <> (#{call.via_node_id})</>
          )}
        </div>
      )}
      <div className="prompt-actions">
        <button onClick={showDiff ? () => setShowDiff(false) : loadTemplate}>
          {showDiff ? "Hide template diff" : "Diff vs current template"}
        </button>
        {templateError && <span className="error"> {templateError}</span>}
      </div>
      {showDiff && template && (
        template.available && template.matches_current ? (
          <div className="prompt-diff">
            <div className="overview-section-title">Differences from the current template</div>
            <MonospaceViewer
              text={template.diff.length > 0 ? template.diff.join("\n") : "(identical to the template apart from substitutions)"}
              filename="prompt-vs-template.diff"
            />
          </div>
        ) : (
          <div className="prompt-diff-unavailable">{template.reason}</div>
        )
      )}
      <FoldedPrompt text={text} attempt={call.attempt} />
    </div>
  );
}

/** A 27kB prompt is mostly the ~40 interest definitions, identical for every
 * item scored in a run. Folding along the prompt's own XML-ish blocks beats
 * both endless scrolling and pulling in a virtualization dependency (27kB in
 * one <pre> renders fine; it is the scrolling that was the problem). */
function FoldedPrompt({ text, attempt }: { text: string; attempt: number }) {
  const sections = useMemo(() => splitPromptSections(text), [text]);
  if (sections.length <= 1) {
    return <MonospaceViewer text={text} large filename={`prompt-attempt-${attempt}.txt`} />;
  }
  return (
    <div className="prompt-sections">
      {sections.map((section, i) =>
        section.tag ? (
          <details key={i} open={section.defaultOpen}>
            <summary>
              &lt;{section.tag}&gt; <span className="prompt-section-size">{section.text.length.toLocaleString()} chars</span>
            </summary>
            <MonospaceViewer text={section.text} large filename={`prompt-${section.tag}.txt`} />
          </details>
        ) : (
          <MonospaceViewer key={i} text={section.text} large filename={`prompt-attempt-${attempt}-part${i}.txt`} />
        ),
      )}
    </div>
  );
}

/** Chips across the top of a call-scoped tab: which attempt am I reading?
 * Multi-attempt nodes are the common case, so the retry history has to be
 * navigable, not just summarised in prose further down the panel. */
function AttemptPicker({ calls, selectedId, onSelect }: {
  calls: ModelCallDetail[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <div className="attempt-picker" role="tablist" aria-label="Model call attempts">
      {calls.map((c) => (
        <button
          key={c.id}
          role="tab"
          aria-selected={c.id === selectedId}
          className={`attempt-chip attempt-chip-${attemptKind(c)}${c.id === selectedId ? " active" : ""}`}
          title={c.error || c.validation_result || undefined}
          onClick={() => onSelect(c.id)}
        >
          attempt {c.attempt} {attemptKind(c) === "ok" ? "✓" : attemptKind(c) === "error" ? "✕" : "·"}
        </button>
      ))}
    </div>
  );
}

function attemptKind(call: ModelCallDetail): "ok" | "error" | "neutral" {
  if (call.error) return "error";
  if (call.validation_result === "valid") return "ok";
  if (call.raw_response_text == null) return "error";
  return "neutral";
}

function AttemptEmpty({ call, what }: { call: ModelCallDetail | undefined; what: string }) {
  if (!call) return <div className="reasoning-empty">No model call attached to this node.</div>;
  return (
    <div className="reasoning-empty">
      Attempt {call.attempt} {what}.
      {call.error && <span className="error"> {call.error}</span>}
      {" "}Pick another attempt above to see one that did.
    </div>
  );
}

function Overview({ detail, onSelectNode, onCompareRun }: {
  detail: NodeDetail; onSelectNode?: (id: ID) => void; onCompareRun?: (runId: number) => void;
}) {
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
            <dd>{formatTimestamp(o.started_at)}</dd>
          </>
        )}
        {o.finished_at && (
          <>
            <dt>Finished</dt>
            <dd>{formatTimestamp(o.finished_at)}</dd>
          </>
        )}
        {duration(o.started_at, o.finished_at) && (
          <>
            <dt>Duration</dt>
            <dd>{duration(o.started_at, o.finished_at)}</dd>
          </>
        )}
        {/* The run id is returned on every node and was rendered nowhere,
            which left the Compare view (which asks for run ids by number)
            with no way to discover its own inputs. */}
        {detail.run && (
          <>
            <dt>Run</dt>
            <dd>
              #{detail.run.id} · {detail.run.kind}
              {detail.run.status && <> · <span className={`status-chip status-chip-${chipKind(detail.run.status)}`}>{detail.run.status}</span></>}
              {onCompareRun && (
                <> · <button className="connection-link" onClick={() => onCompareRun(detail.run!.id)}>Compare this run</button></>
              )}
            </dd>
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
      <EntityFacts detail={detail} />
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

/** The operational row behind the node, rendered per entity type.
 *
 * All of this existed one JOIN away and was reachable only by leaving the app
 * for raw Datasette: the six sub-scores behind the single number on a
 * threshold card, the item body the scorer actually read, what a duplicate
 * duplicated, how many times a delivery was attempted, why a mission exists.
 */
function EntityFacts({ detail }: { detail: NodeDetail }) {
  const row = detail.entity_row;
  const entityType = detail.overview.entity_type;
  if (!row || !entityType) return null;

  if (entityType === "scores") return <ScoreFacts row={row} weights={detail.score_weights} />;
  if (entityType === "candidate_items") return <ItemFacts row={row} truncated={detail.truncated} />;
  if (entityType === "notifications") {
    return (
      <FactList title="Delivery" pairs={[
        ["Channel", str(row.channel)],
        ["Attempts", str(row.attempts)],
        ["Sent", formatTimestamp(str(row.sent_at)) ?? ""],
        ["Result", row.ok ? "delivered" : "failed"],
      ]} />
    );
  }
  if (entityType === "search_missions") {
    return (
      <>
        <FactList title="Mission" pairs={[
          ["Interest", str(row.interest_key)],
          ["Status", str(row.status)],
          ["Attempts", str(row.attempts)],
          ["Items returned", str(row.items_returned)],
          ["Last error", str(row.last_error)],
        ]} />
        {str(row.rationale) && (
          <div className="overview-preview">
            <div className="overview-section-title">Why this mission exists</div>
            <p className="overview-summary">{str(row.rationale)}</p>
          </div>
        )}
        {str(row.prompt) && (
          <details className="entity-fold">
            <summary>Mission prompt</summary>
            <MonospaceViewer text={str(row.prompt)} large filename="mission-prompt.txt" />
          </details>
        )}
      </>
    );
  }
  if (entityType === "search_generations") {
    return (
      <FactList title="Generation" pairs={[
        ["Interest", str(row.interest_key)],
        ["Model", [str(row.provider), str(row.model)].filter(Boolean).join("/")],
        ["Missions", `${str(row.missions_returned)} of ${str(row.missions_requested)} requested`],
        ["Status", str(row.status)],
        ["Error", str(row.error)],
      ]} />
    );
  }
  return null;
}

/** The six dimensions behind the one number on a threshold card, each with the
 * weight that actually produced final_score (specificity is scored but
 * deliberately unweighted -- see discovery/models.py). */
function ScoreFacts({ row, weights }: { row: Record<string, unknown>; weights: Record<string, number> | null }) {
  const dimensions = ["personal_relevance", "novelty", "depth", "specificity", "importance", "surprise"];
  return (
    <div className="entity-facts">
      <div className="overview-section-title">Score breakdown</div>
      <ul className="score-bars">
        {dimensions.map((name) => {
          const value = Number(row[name]);
          if (!isFinite(value)) return null;
          const weight = weights?.[name];
          return (
            <li key={name}>
              <span className="score-bar-name">{name.replace(/_/g, " ")}</span>
              <span className="score-bar-track"><span className="score-bar-fill" style={{ width: `${Math.round(value * 100)}%` }} /></span>
              <span className="score-bar-value">
                {value.toFixed(2)}
                <span className="score-bar-weight">{weight != null ? ` ×${weight}` : " unweighted"}</span>
              </span>
            </li>
          );
        })}
      </ul>
      <FactList pairs={[
        ["Final score", str(row.final_score)],
        ["Confidence", str(row.confidence)],
        ["Prompt version", str(row.prompt_hash)],
      ]} />
      {str(row.reason) && <p className="overview-summary">{str(row.reason)}</p>}
      {str(row.why_better_than_generic) && (
        <p className="overview-summary"><em>Better than generic:</em> {str(row.why_better_than_generic)}</p>
      )}
    </div>
  );
}

/** The item body the scorer actually read -- previously the card showed only
 * its title, and the text (avg 760 chars, max 10,220) was invisible. */
function ItemFacts({ row, truncated }: { row: Record<string, unknown>; truncated: boolean }) {
  const duplicate = row.duplicate_of_item as { id: number; title: string; url: string } | undefined;
  return (
    <div className="entity-facts">
      <FactList title="Item" pairs={[
        ["Source", [str(row.source), str(row.type)].filter(Boolean).join(" · ")],
        ["Author", str(row.author)],
        ["Published", str(row.published_at)],
        ["From interest", str(row.origin_interest)],
        ["Prefilter", row.prefilter_ok === 0 ? `rejected -- ${str(row.prefilter_reason)}` : ""],
      ]} />
      {duplicate && (
        <div className="entity-duplicate">
          Duplicate of <a href={str(duplicate.url)} target="_blank" rel="noreferrer">{str(duplicate.title)}</a>
          {str(row.dup_reason) && <> ({str(row.dup_reason)})</>}
        </div>
      )}
      {str(row.text) && (
        <details className="entity-fold">
          <summary>Item text ({str(row.text).length.toLocaleString()} chars)</summary>
          <MonospaceViewer text={str(row.text)} large truncated={truncated} filename="item-text.txt" />
        </details>
      )}
    </div>
  );
}

function FactList({ title, pairs }: { title?: string; pairs: [string, string][] }) {
  const shown = pairs.filter(([, value]) => value !== "" && value !== "null" && value !== "undefined");
  if (shown.length === 0) return null;
  return (
    <>
      {title && <div className="overview-section-title">{title}</div>}
      <dl className="overview-facts">
        {shown.map(([label, value]) => (
          <Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </Fragment>
        ))}
      </dl>
    </>
  );
}

/** Row values arrive as raw SQLite cells; null/undefined render as "". */
function str(value: unknown): string {
  if (value == null) return "";
  return String(value);
}

/** One most-informative payload, previewed inline so a click on a node shows
 * its substance immediately instead of an empty pane and a hunt through tabs. */
function primaryPayload(detail: NodeDetail): { title: string; text: string; json: boolean; filename: string } | null {
  const call = bestCall(detail.model_calls);
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

// Pipeline-relevant relationships first, "matched" last -- on a candidate
// with many interest matches (common with ~40 active interests) the one
// link that actually explains what happened to this node (scored,
// cleared_threshold, rendered, sent...) was previously buried at the
// bottom of a pile of a dozen "matched" links, sorted by nothing but
// whatever order the API happened to return them in.
const RELATIONSHIP_PRIORITY = [
  "scored", "cleared_threshold", "rejected", "rendered", "sent", "normalized_to",
  "returned", "generated", "executed", "selected", "matched",
];
function relationshipRank(rel: string): number {
  const i = RELATIONSHIP_PRIORITY.indexOf(rel);
  return i === -1 ? RELATIONSHIP_PRIORITY.length : i;
}
const MATCH_PREVIEW_COUNT = 3;
const MATCH_COLLAPSE_THRESHOLD = 5;

function Connections({ title, edges, onSelectNode }: {
  title: string;
  edges: { id: ID; relationship: string; label: string; node_type: string }[];
  onSelectNode?: (id: ID) => void;
}) {
  const [showAllMatches, setShowAllMatches] = useState(false);
  if (edges.length === 0) return null;

  const nonMatched = edges
    .filter((e) => e.relationship !== "matched")
    .sort((a, b) => relationshipRank(a.relationship) - relationshipRank(b.relationship));
  const matched = edges
    .filter((e) => e.relationship === "matched")
    .sort((a, b) => (labelScore(b.label) ?? -Infinity) - (labelScore(a.label) ?? -Infinity));
  const collapse = matched.length > MATCH_COLLAPSE_THRESHOLD && !showAllMatches;
  const visibleMatched = collapse ? matched.slice(0, MATCH_PREVIEW_COUNT) : matched;
  const visible = [...nonMatched, ...visibleMatched];

  return (
    <div className="overview-connections">
      <div className="overview-section-title">{title}</div>
      <ul>
        {visible.map((e, i) => (
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
      {collapse && (
        <button className="connection-showall" onClick={() => setShowAllMatches(true)}>
          show all {matched.length} matches
        </button>
      )}
    </div>
  );
}

// pipeline.py/trace.py timestamps are ISO with an explicit +00:00 offset
// (always UTC -- see PROJECT_STATE.md's "All timestamps UTC" note); rendered
// as "2026-08-12 07:56:58 UTC" instead of the raw ISO string.
function formatTimestamp(iso: string | null): string | null {
  if (!iso) return iso;
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?\+00:00$/.exec(iso);
  return m ? `${m[1]} ${m[2]} UTC` : iso;
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
