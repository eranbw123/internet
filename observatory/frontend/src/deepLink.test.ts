import { describe, expect, it } from "vitest";
import { formatHash, parseHash } from "./deepLink";

// Deep-linking used to be one-way and score-only: /trace/score/<id> could get
// you in, but the app wrote nothing back to the URL, so a reload lost both the
// seed and the selection and nothing could link to an item, mission,
// generation or run.
describe("hash round-trip", () => {
  const cases = [
    { seed: { entity_type: "candidate_items", entity_id: "2114" }, nodeId: "25686" },
    { seed: { entity_type: "candidate_items", entity_id: "2114" }, nodeId: null },
    { seed: { run_id: 187 }, nodeId: "25686" },
    { seed: { run_id: 187 }, nodeId: null },
  ];

  for (const state of cases) {
    it(`round-trips ${JSON.stringify(state)}`, () => {
      expect(parseHash(formatHash(state))).toEqual(state);
    });
  }

  it("produces an empty hash for no seed", () => {
    expect(formatHash({ seed: null, nodeId: null })).toBe("");
  });

  it("reads an empty or junk hash as no state, never as a bad seed", () => {
    for (const hash of ["", "#", "#/", "#/nonsense", "#/e/only-one-part", "#/r/notanumber"]) {
      expect(parseHash(hash)).toEqual({ seed: null, nodeId: null });
    }
  });

  it("escapes entity types and ids rather than breaking on separators", () => {
    const state = { seed: { entity_type: "candidate_items", entity_id: "a/b c" }, nodeId: null };
    expect(parseHash(formatHash(state))).toEqual(state);
  });
});

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
