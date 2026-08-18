import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { BidiText, Quote, dirFor, guessLang, isRtl } from "./Bidi";

/** 28% of this corpus is Hebrew-titled, so mixed-direction rendering is the
 * normal case. These tests pin the rule that matters: direction comes from the
 * data's `lang` field, never from sniffing the characters. */
describe("dirFor", () => {
  it("reads direction off the language tag", () => {
    expect(dirFor("he")).toBe("rtl");
    expect(dirFor("he-IL")).toBe("rtl");
    expect(dirFor("HE")).toBe("rtl");
    expect(dirFor("ar")).toBe("rtl");
    expect(dirFor("en")).toBe("ltr");
    expect(dirFor("en-GB")).toBe("ltr");
  });

  it("falls back to auto when there is no language to go on", () => {
    expect(dirFor(undefined)).toBe("auto");
    expect(dirFor("")).toBe("auto");
    expect(dirFor(null)).toBe("auto");
  });

  it("does NOT infer direction from the characters", () => {
    // A Hebrew quote tagged "en" still renders ltr: the field is authoritative,
    // and a renderer that second-guessed it would disagree with the store.
    expect(dirFor("en")).toBe("ltr");
    expect(isRtl("he")).toBe(true);
    expect(isRtl("en")).toBe(false);
  });
});

describe("Quote", () => {
  it("marks a Hebrew quote rtl and tags its language", () => {
    render(<Quote lang="he">יש דרך להוריד את צריכת הסוללה</Quote>);
    const q = screen.getByText("יש דרך להוריד את צריכת הסוללה");
    expect(q.tagName).toBe("Q");
    expect(q).toHaveAttribute("dir", "rtl");
    expect(q).toHaveAttribute("lang", "he");
  });

  it("marks an English quote ltr", () => {
    render(<Quote lang="en">Does Elden Ring multiplayer work</Quote>);
    expect(screen.getByText("Does Elden Ring multiplayer work")).toHaveAttribute("dir", "ltr");
  });

  it("falls back to auto for an untagged quote", () => {
    render(<Quote>untagged</Quote>);
    const q = screen.getByText("untagged");
    expect(q).toHaveAttribute("dir", "auto");
    expect(q).not.toHaveAttribute("lang");
  });
});

describe("BidiText", () => {
  it("isolates a title without adding quotation marks", () => {
    render(<BidiText lang="he">זיכרון עבודה ועייפות</BidiText>);
    const el = screen.getByText("זיכרון עבודה ועייפות");
    expect(el.tagName).toBe("SPAN");
    expect(el).toHaveAttribute("dir", "rtl");
    expect(el.className).toContain("bidi-text");
  });

  it("can render as a block", () => {
    render(<BidiText block lang="en">A title</BidiText>);
    expect(screen.getByText("A title").tagName).toBe("DIV");
  });
});

describe("guessLang", () => {
  it("is the LAST resort, for strings that carry no lang at all", () => {
    // Conversation titles have no stored language today, so this is the only
    // place characters are inspected.
    expect(guessLang("סוללה וביצועים בסטים דק")).toBe("he");
    expect(guessLang("Binding of Isaac Steam Deck")).toBeUndefined();
  });
});
