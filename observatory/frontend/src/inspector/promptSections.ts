/** Splitting a scoring prompt into foldable sections.
 *
 * Scoring prompts run 14kB on average and 27kB at the top end, most of it one
 * block: the ~40 interest definitions, which are identical across every item
 * scored in a run and are almost never what the reader came to look at. The
 * prompt is XML-ish (`<interests>...</interests>`, `<past_verdicts>`,
 * `<item>`, then trailing instructions), so it folds along those blocks --
 * which turns "scroll through 27kB" into "read the item and the instructions,
 * open the rest if you want it".
 *
 * Deliberately not a parser: it recognises top-level `<tag>`/`</tag>` pairs at
 * the start of a line and treats everything else as prose between them. Any
 * text that doesn't split this way falls back to a single flat section, so a
 * prompt shape this doesn't recognise still renders in full.
 */

export interface PromptSection {
  /** `<interests>` -> "interests"; prose between blocks -> null. */
  tag: string | null;
  text: string;
  /** Sections big enough to be worth hiding start folded. */
  defaultOpen: boolean;
}

/** Blocks that are bulk context rather than the thing being judged. */
const COLLAPSED_BY_DEFAULT = new Set(["interests", "past_verdicts", "feedback", "examples"]);

const OPEN_TAG = /^<([a-z_][a-z0-9_]*)>\s*$/i;

export function splitPromptSections(text: string): PromptSection[] {
  if (!text) return [];
  const lines = text.split("\n");
  const sections: PromptSection[] = [];
  let buffer: string[] = [];
  let openTag: string | null = null;

  const flushProse = () => {
    if (buffer.length === 0) return;
    const prose = buffer.join("\n");
    // Whitespace-only gaps between blocks aren't worth their own section.
    if (prose.trim() !== "") sections.push({ tag: null, text: prose, defaultOpen: true });
    buffer = [];
  };

  for (const line of lines) {
    if (openTag === null) {
      const match = OPEN_TAG.exec(line);
      if (match) {
        flushProse();
        openTag = match[1];
        buffer = [line];
        continue;
      }
      buffer.push(line);
      continue;
    }
    buffer.push(line);
    if (line.trim() === `</${openTag}>`) {
      sections.push({
        tag: openTag,
        text: buffer.join("\n"),
        defaultOpen: !COLLAPSED_BY_DEFAULT.has(openTag.toLowerCase()),
      });
      buffer = [];
      openTag = null;
    }
  }

  // An unterminated block is prose, not a section -- better to show it than to
  // silently fold away the tail of a prompt.
  if (openTag !== null) {
    sections.push({ tag: null, text: buffer.join("\n"), defaultOpen: true });
  } else {
    flushProse();
  }

  // Nothing recognisable: one flat section, so the caller can render as before.
  if (sections.length <= 1) return [{ tag: null, text, defaultOpen: true }];
  return sections;
}

/** "27,412 chars · ~6.9k tokens (est.) · 312 lines".
 *
 * The token figure is a chars/4 estimate and is labelled as one: usage_json is
 * NULL on 97.7% of stored calls (the browser-driven providers don't report it),
 * so a real count is unavailable far more often than not. */
export function describeSize(text: string): string {
  const chars = text.length;
  const lines = text === "" ? 0 : text.split("\n").length;
  const tokens = Math.round(chars / 4);
  const tokenText = tokens >= 1000 ? `~${(tokens / 1000).toFixed(1)}k` : `~${tokens}`;
  return `${chars.toLocaleString()} chars · ${tokenText} tokens (est.) · ${lines.toLocaleString()} lines`;
}
