import { describe, expect, it } from "vitest";
import { readBootstrap } from "./deepLink";

function docWithBootstrap(json: string | null): Document {
  const doc = document.implementation.createHTMLDocument("test");
  if (json !== null) {
    const script = doc.createElement("script");
    script.id = "observatory-bootstrap";
    script.type = "application/json";
    script.textContent = json;
    doc.body.appendChild(script);
  }
  return doc;
}

describe("readBootstrap", () => {
  it("returns focus:null when there is no bootstrap element (plain /observatory/)", () => {
    expect(readBootstrap(docWithBootstrap(null))).toEqual({ focus: null });
  });

  it("returns focus:null for the plain index_view payload", () => {
    expect(readBootstrap(docWithBootstrap('{"focus": null}'))).toEqual({ focus: null });
  });

  it("parses a score deep-link focus payload (trace/score/<id>)", () => {
    const bootstrap = readBootstrap(
      docWithBootstrap('{"focus": {"kind": "score", "node_id": 42, "run_id": 7, "score_id": 99}}'),
    );
    expect(bootstrap.focus).toEqual({ kind: "score", node_id: 42, run_id: 7, score_id: 99 });
  });

  it("falls back to focus:null on malformed JSON instead of throwing", () => {
    expect(readBootstrap(docWithBootstrap("{not json"))).toEqual({ focus: null });
  });
});
