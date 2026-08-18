/** Bidirectional text helpers.
 *
 * 28% of the owner's conversations are Hebrew-titled, so this workspace's
 * normal case is an RTL run sitting inside otherwise-LTR chrome: an English
 * label, then a Hebrew quote, then an English date, on one line. Getting that
 * wrong is not cosmetic -- an un-isolated RTL run drags neighbouring
 * punctuation and digits to the wrong end, so the quotation mark, the comma
 * and the date all migrate, and the line reads as garbled rather than as
 * "Hebrew inside English".
 *
 * Two rules, applied everywhere:
 *
 *  1. Direction comes from the `lang` FIELD, not from the characters. PR H
 *     stores a `lang` on every evidence quote (the producer's, else
 *     `_guess_lang`), which makes it authoritative data rather than something
 *     each renderer re-derives differently. `dir="auto"` is used only as the
 *     fallback when a row carries no lang at all -- it infers direction from
 *     the first strong character, which is right often enough to be a safety
 *     net and wrong often enough not to be the primary mechanism (a Hebrew
 *     quote opening with a Latin product name infers LTR and mis-renders).
 *
 *  2. Every such run is `unicode-bidi: isolate` (see interests.css), so the
 *     bidi algorithm treats it as one neutral object and cannot reorder the
 *     surrounding LTR chrome around it.
 */
import type { ReactNode } from "react";

/** Scripts written right-to-left that could plausibly appear in this corpus.
 * Hebrew is the one that actually does; the rest cost nothing to support. */
const RTL_LANGS = new Set(["he", "iw", "ar", "fa", "ur", "yi", "ji", "dv", "ps"]);

/** Direction for a language tag. Returns "auto" when there is nothing to go
 * on, so the browser's own first-strong-character heuristic is the fallback
 * rather than a wrong hard-coded "ltr". */
export function dirFor(lang?: string | null): "rtl" | "ltr" | "auto" {
  if (!lang) return "auto";
  // Accept "he", "he-IL", "HE" alike.
  const primary = lang.toLowerCase().split(/[-_]/)[0];
  if (!primary) return "auto";
  return RTL_LANGS.has(primary) ? "rtl" : "ltr";
}

export function isRtl(lang?: string | null): boolean {
  return dirFor(lang) === "rtl";
}

interface QuoteProps {
  children: ReactNode;
  lang?: string | null;
  className?: string;
}

/** A verbatim quote from the owner's own conversation.
 *
 * Rendered as a real <q>: it is a quotation, the browser supplies
 * language-appropriate quotation marks, and screen readers announce it as
 * quoted speech. The lang attribute is set too, so assistive tech switches
 * voice rather than reading Hebrew with an English speech engine. */
export function Quote({ children, lang, className }: QuoteProps) {
  return (
    <q
      className={["bidi-quote", className].filter(Boolean).join(" ")}
      dir={dirFor(lang)}
      lang={lang || undefined}
    >
      {children}
    </q>
  );
}

interface BidiTextProps {
  children: ReactNode;
  lang?: string | null;
  className?: string;
  /** Render as a block instead of an inline span (titles in their own row). */
  block?: boolean;
}

/** Any other user-supplied string that might be RTL: a conversation title, an
 * interest title, a note. Same isolation, without the quotation marks. */
export function BidiText({ children, lang, className, block }: BidiTextProps) {
  const cls = ["bidi-text", block ? "bidi-block" : "", className].filter(Boolean).join(" ");
  const dir = dirFor(lang);
  if (block) {
    return <div className={cls} dir={dir} lang={lang || undefined}>{children}</div>;
  }
  return <span className={cls} dir={dir} lang={lang || undefined}>{children}</span>;
}

/** Heuristic for strings that arrive with NO lang field at all -- conversation
 * titles today, since PR H does not persist a per-title language. Used only to
 * pick a lang hint for BidiText; the isolation happens regardless.
 *
 * This is the one place characters are inspected, and it is explicitly the
 * fallback path, not the mechanism. If PR G/H start shipping a title language,
 * pass it through and this stops being consulted. */
const HEBREW = /[֐-׿יִ-ﭏ]/;
const ARABIC = /[؀-ۿݐ-ݿ]/;

export function guessLang(text: string): string | undefined {
  if (HEBREW.test(text)) return "he";
  if (ARABIC.test(text)) return "ar";
  return undefined;
}
