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
