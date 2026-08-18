import { useEffect, useMemo, useRef, useState } from "react";
import { describeSize } from "./promptSections";

interface Props {
  text: string | null | undefined;
  json?: boolean; // syntax-highlight as JSON (text is pretty-printed if it parses)
  truncated?: boolean;
  filename?: string;
  /** Opt-in extras for very large payloads (prompts run 14kB average, 27kB
   * peak): a size line and in-text search. Off by default so the ordinary
   * tabs keep their compact toolbar. */
  large?: boolean;
}

/** Monospace viewer used by every Inspector tab: preserved whitespace, wrap
 * toggle, JSON syntax highlighting, copy button, full-screen mode, download
 * button, and a visible truncation warning -- all local UI state, no
 * network calls of its own. */
export function MonospaceViewer({ text, json, truncated, filename = "observatory-export.txt", large }: Props) {
  const [wrap, setWrap] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [query, setQuery] = useState("");
  const [activeHit, setActiveHit] = useState(0);

  const display = json ? prettyJson(text) : text ?? "";

  // Plain indexOf scanning, never a RegExp built from user input -- an
  // unescaped query would otherwise throw on a lone "(" or, worse, quietly
  // match something else.
  const hits = useMemo(() => findAll(display, query), [display, query]);
  useEffect(() => {
    setActiveHit(0);
  }, [query]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(display);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard API unavailable (older browser / no permission) -- silently no-op
    }
  }

  function download() {
    const blob = new Blob([display], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className={`monospace-viewer ${fullscreen ? "fullscreen" : ""}`}>
      <div className="viewer-toolbar">
        <label>
          <input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> wrap
        </label>
        <button onClick={copy}>{copied ? "Copied" : "Copy"}</button>
        <button onClick={download}>Download</button>
        <button onClick={() => setFullscreen((v) => !v)}>{fullscreen ? "Exit full screen" : "Full screen"}</button>
        {large && (
          <>
            <input
              className="viewer-search"
              type="search"
              value={query}
              placeholder="find in text"
              aria-label="Find in text"
              onChange={(e) => setQuery(e.target.value)}
            />
            {query !== "" && (
              <span className="viewer-search-count">
                {hits.length === 0 ? "no matches" : `${activeHit + 1} / ${hits.length}`}
                <button
                  aria-label="Previous match"
                  disabled={hits.length === 0}
                  onClick={() => setActiveHit((i) => (i - 1 + hits.length) % hits.length)}
                >‹</button>
                <button
                  aria-label="Next match"
                  disabled={hits.length === 0}
                  onClick={() => setActiveHit((i) => (i + 1) % hits.length)}
                >›</button>
              </span>
            )}
          </>
        )}
      </div>
      {large && <div className="viewer-size" data-testid="viewer-size">{describeSize(display)}</div>}
      {truncated && <div className="viewer-truncated-warning">⚠ truncated -- not the complete stored value</div>}
      <pre
        className={`viewer-content ${wrap ? "wrap" : "nowrap"} ${json ? "json" : ""}`}
        data-testid="monospace-content"
      >
        {json ? <JsonHighlight text={display} />
          : hits.length > 0 ? <SearchHighlight text={display} hits={hits} length={query.length} active={activeHit} />
          : display}
      </pre>
    </div>
  );
}

/** Every start offset of `needle` in `haystack`, case-insensitively. */
function findAll(haystack: string, needle: string): number[] {
  if (needle === "") return [];
  const hay = haystack.toLowerCase();
  const pin = needle.toLowerCase();
  const out: number[] = [];
  let from = 0;
  // Bounded so a one-character query on a 27kB prompt can't render tens of
  // thousands of spans and lock the panel up.
  while (out.length < 500) {
    const at = hay.indexOf(pin, from);
    if (at === -1) break;
    out.push(at);
    from = at + pin.length;
  }
  return out;
}

function SearchHighlight({ text, hits, length, active }: {
  text: string; hits: number[]; length: number; active: number;
}) {
  const activeRef = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [active, hits]);

  const parts: React.ReactNode[] = [];
  let last = 0;
  hits.forEach((at, i) => {
    if (at > last) parts.push(text.slice(last, at));
    parts.push(
      <span
        key={at}
        ref={i === active ? activeRef : undefined}
        className={`search-hit${i === active ? " search-hit-active" : ""}`}
      >
        {text.slice(at, at + length)}
      </span>,
    );
    last = at + length;
  });
  parts.push(text.slice(last));
  return <>{parts}</>;
}

function prettyJson(text: string | null | undefined): string {
  if (!text) return "";
  if (typeof text !== "string") return JSON.stringify(text, null, 2);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

const JSON_TOKEN_RE = /("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(\.\d+)?([eE][+-]?\d+)?)/g;

function JsonHighlight({ text }: { text: string }) {
  const parts: { text: string; cls: string }[] = [];
  let last = 0;
  for (const m of text.matchAll(JSON_TOKEN_RE)) {
    if (m.index! > last) parts.push({ text: text.slice(last, m.index), cls: "" });
    const token = m[0];
    let cls = "json-number";
    if (/^"/.test(token)) cls = token.endsWith(":") || /:\s*$/.test(token) ? "json-key" : "json-string";
    else if (/^(true|false)$/.test(token)) cls = "json-bool";
    else if (token === "null") cls = "json-null";
    parts.push({ text: token, cls });
    last = m.index! + token.length;
  }
  parts.push({ text: text.slice(last), cls: "" });
  return (
    <>
      {parts.map((p, i) => (p.cls ? <span key={i} className={p.cls}>{p.text}</span> : <span key={i}>{p.text}</span>))}
    </>
  );
}
