import { useCallback, useEffect, useState } from "react";

/** Three states, not two. "system" is a real, persisted choice -- it means
 * "follow the OS", and it is the default -- so the toggle cycles through
 * three positions rather than flipping a boolean. tokens.css implements the
 * matching cascade: bare :root is light, a guarded prefers-color-scheme block
 * handles "system on a dark OS", and :root[data-theme="dark"] handles the
 * explicit pick. */
export type Theme = "light" | "dark" | "system";

/** Same localStorage namespace as the existing `observatory-inspector-width`. */
export const THEME_KEY = "observatory-theme";

const ORDER: readonly Theme[] = ["system", "light", "dark"];

export function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

/** The stored choice, or "system" when there is nothing (or nonsense) stored.
 * localStorage access is guarded: it throws outright in a partitioned or
 * cookie-blocked context, and a theme preference is never worth a blank page. */
export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

/** Writes the DOM side of the choice, and nothing else. "system" REMOVES the
 * attribute rather than setting data-theme="system": the media query has to be
 * the thing that decides, and `:root:not([data-theme="light"])` inside it
 * would otherwise still match and pin the page dark on a light OS. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Private/partitioned context -- the theme still applies for this page
    // load, it just won't survive a reload. Not worth surfacing.
  }
}

/** system -> light -> dark -> system. */
export function nextTheme(theme: Theme): Theme {
  return ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
}

/** What the page is ACTUALLY showing right now, which is not the same question
 * as which theme is selected: "system" resolves through the OS. Used for the
 * toggle's aria-label so a screen reader gets the effective state. */
export function resolvedTheme(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** resolvedTheme(), but live: while the choice is "system" the answer changes
 * under us when the OS flips, and a label reading "dark" over a light page is
 * exactly the kind of wrong that makes a toggle untrustworthy. */
export function useResolvedTheme(theme: Theme): "light" | "dark" {
  const [resolved, setResolved] = useState(() => resolvedTheme(theme));

  useEffect(() => {
    setResolved(resolvedTheme(theme));
    if (theme !== "system" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setResolved(mq.matches ? "dark" : "light");
    // addEventListener over the deprecated addListener, but not every jsdom /
    // older WebKit build has it on MediaQueryList.
    if (typeof mq.addEventListener !== "function") return;
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  return resolved;
}

export function useTheme(): { theme: Theme; setTheme: (t: Theme) => void; cycle: () => void } {
  const [theme, setThemeState] = useState<Theme>(readTheme);

  // Applied on mount too, not just on change: index.html's pre-mount script
  // covers the first paint (that's what stops the flash), and this keeps the
  // DOM honest if anything else touched the attribute in between.
  useEffect(() => {
    applyTheme(theme);
    storeTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => setThemeState(next), []);
  const cycle = useCallback(() => setThemeState(nextTheme), []);
  return { theme, setTheme, cycle };
}
