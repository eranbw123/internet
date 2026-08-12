import { useState } from "react";

interface Props {
  text: string | null | undefined;
  json?: boolean; // syntax-highlight as JSON (text is pretty-printed if it parses)
  truncated?: boolean;
  filename?: string;
}

/** Monospace viewer used by every Inspector tab: preserved whitespace, wrap
 * toggle, JSON syntax highlighting, copy button, full-screen mode, download
 * button, and a visible truncation warning -- all local UI state, no
 * network calls of its own. */
export function MonospaceViewer({ text, json, truncated, filename = "observatory-export.txt" }: Props) {
  const [wrap, setWrap] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [copied, setCopied] = useState(false);

  const display = json ? prettyJson(text) : text ?? "";

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
      </div>
      {truncated && <div className="viewer-truncated-warning">⚠ truncated -- not the complete stored value</div>}
      <pre
        className={`viewer-content ${wrap ? "wrap" : "nowrap"} ${json ? "json" : ""}`}
        data-testid="monospace-content"
      >
        {json ? <JsonHighlight text={display} /> : display}
      </pre>
    </div>
  );
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
