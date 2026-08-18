import { describe, expect, it } from "vitest";
import { describeSize, splitPromptSections } from "./promptSections";

// Shaped like a real scoring prompt: instructions, then the bulk <interests>
// block, then the item actually being judged.
const PROMPT = [
  "You are scoring one item.",
  "",
  "<interests>",
  "  narcolepsy-eds: sleep research",
  "  llm-agents: agent papers",
  "</interests>",
  "",
  "<item>",
  "  title: A Breakthrough",
  "</item>",
  "",
  "Return JSON with six dimensions.",
].join("\n");

describe("splitPromptSections", () => {
  it("returns nothing for empty text", () => {
    expect(splitPromptSections("")).toEqual([]);
  });

  it("splits top-level blocks out from the prose around them", () => {
    const sections = splitPromptSections(PROMPT);
    expect(sections.map((s) => s.tag)).toEqual([null, "interests", "item", null]);
  });

  it("folds bulk context by default and leaves the item open", () => {
    const byTag = Object.fromEntries(splitPromptSections(PROMPT).filter((s) => s.tag).map((s) => [s.tag, s]));
    expect(byTag.interests.defaultOpen).toBe(false);
    expect(byTag.item.defaultOpen).toBe(true);
  });

  it("keeps each block's own tags in its text so nothing is lost", () => {
    const joined = splitPromptSections(PROMPT).map((s) => s.text).join("\n");
    // Every original line survives somewhere in the split.
    for (const line of PROMPT.split("\n").filter((l) => l.trim() !== "")) {
      expect(joined).toContain(line);
    }
  });

  it("falls back to one flat section when nothing parses as a block", () => {
    const flat = splitPromptSections("just a prompt\nwith no blocks");
    expect(flat).toHaveLength(1);
    expect(flat[0].tag).toBeNull();
    expect(flat[0].text).toBe("just a prompt\nwith no blocks");
  });

  it("treats an unterminated block as prose rather than folding the tail away", () => {
    const sections = splitPromptSections("<item>\nnever closed\n");
    expect(sections.every((s) => s.defaultOpen)).toBe(true);
    expect(sections.map((s) => s.text).join("")).toContain("never closed");
  });
});

describe("describeSize", () => {
  it("labels the token figure as an estimate", () => {
    // usage_json is NULL on 97.7% of live calls, so this is chars/4 and must
    // never be presented as a real count.
    expect(describeSize("x".repeat(8000))).toContain("(est.)");
    expect(describeSize("x".repeat(8000))).toContain("~2.0k tokens");
  });

  it("counts chars and lines", () => {
    const out = describeSize("ab\ncd");
    expect(out).toContain("5 chars");
    expect(out).toContain("2 lines");
  });
});
