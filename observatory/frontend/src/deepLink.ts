// Reads the bootstrap payload plugin.py's _shell_html() embeds server-side
// as <script id="observatory-bootstrap" type="application/json">...</script>
// -- see plugin.py:index_view (always {"focus": null}) and
// plugin.py:trace_score_view (the /observatory/trace/score/<id> deep link,
// {"focus": {"kind": "score", "node_id", "run_id", "score_id"}}).
import type { Bootstrap } from "./types";

export function readBootstrap(doc: Document = document): Bootstrap {
  const el = doc.getElementById("observatory-bootstrap");
  if (!el || !el.textContent) return { focus: null };
  try {
    const parsed = JSON.parse(el.textContent);
    if (parsed && typeof parsed === "object" && "focus" in parsed) return parsed as Bootstrap;
  } catch {
    // malformed bootstrap payload -- fall through to the safe default
  }
  return { focus: null };
}

/** The app's own state, reflected into location.hash.
 *
 * Deep-linking was one-way and score-only: /observatory/trace/score/<id> could
 * get you in, but the app wrote nothing back to the URL, so a reload lost the
 * seed and the selection and nothing could link to an item, mission,
 * generation or run. The hash needs no server route -- everything under
 * /observatory/ already serves the same shell.
 *
 * Shape: #/e/<entity_type>/<entity_id>/n/<node_id> or #/r/<run_id>/n/<node_id>,
 * with the /n/ part omitted when no node is selected.
 */
export interface HashState {
  seed: { entity_type?: string; entity_id?: string; run_id?: number } | null;
  nodeId: string | null;
}

export function formatHash(state: HashState): string {
  if (!state.seed) return "";
  const parts: string[] = [];
  if (state.seed.run_id != null) {
    parts.push("r", String(state.seed.run_id));
  } else if (state.seed.entity_type && state.seed.entity_id != null) {
    parts.push("e", encodeURIComponent(state.seed.entity_type), encodeURIComponent(String(state.seed.entity_id)));
  } else {
    return "";
  }
  if (state.nodeId != null) parts.push("n", encodeURIComponent(String(state.nodeId)));
  return `#/${parts.join("/")}`;
}

export function parseHash(hash: string): HashState {
  const empty: HashState = { seed: null, nodeId: null };
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts.length === 0) return empty;

  let seed: HashState["seed"] = null;
  let rest: string[] = [];
  if (parts[0] === "r" && parts[1]) {
    const runId = Number(parts[1]);
    if (!Number.isFinite(runId)) return empty;
    seed = { run_id: runId };
    rest = parts.slice(2);
  } else if (parts[0] === "e" && parts[1] && parts[2]) {
    seed = { entity_type: decodeURIComponent(parts[1]), entity_id: decodeURIComponent(parts[2]) };
    rest = parts.slice(3);
  } else {
    return empty;
  }
  const nodeId = rest[0] === "n" && rest[1] ? decodeURIComponent(rest[1]) : null;
  return { seed, nodeId };
}
