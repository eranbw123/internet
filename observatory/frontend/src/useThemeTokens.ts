/** Resolve CSS custom properties to concrete colour strings, and re-resolve
 * them whenever the theme changes.
 *
 * Shared by the interests connections graph and the trace explorer's canvas,
 * which is why it sits at the src root rather than inside either one.
 *
 * CSS handles theming for every DOM element by itself; React Flow does not.
 * Its edge strokes, markers and minimap take colours as JS props, so those
 * few values have to be READ out of the cascade rather than declared in it.
 * The design's own note about this (section 8.2/8.4) is exactly this
 * approach: read the tokens via getComputedStyle once per theme change.
 *
 * "Theme change" has three triggers, and missing any one of them leaves the
 * graph painted in the previous theme's colours:
 *   - the OS switching, when the user is on "system" (matchMedia);
 *   - PR K's toggle writing data-theme on <html> (MutationObserver);
 *   - first paint (the initial read).
 */
import { useEffect, useState } from "react";

export type TokenMap<K extends string> = Record<K, string>;

function read<K extends string>(names: readonly K[]): TokenMap<K> {
  const style = getComputedStyle(document.documentElement);
  const out = {} as TokenMap<K>;
  for (const name of names) {
    out[name] = style.getPropertyValue(name).trim();
  }
  return out;
}

export function useThemeTokens<K extends string>(names: readonly K[]): TokenMap<K> {
  const [tokens, setTokens] = useState<TokenMap<K>>(() =>
    (typeof window === "undefined" ? ({} as TokenMap<K>) : read(names)));

  useEffect(() => {
    const refresh = () => setTokens(read(names));
    refresh();

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    // Safari < 14 has no addEventListener on MediaQueryList; the repo targets
    // a desktop dev tool, but the guard costs one line and avoids a hard
    // throw on an older engine.
    if (media.addEventListener) media.addEventListener("change", refresh);
    else media.addListener?.(refresh);

    const observer = new MutationObserver(refresh);
    observer.observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme", "class"],
    });

    return () => {
      if (media.removeEventListener) media.removeEventListener("change", refresh);
      else media.removeListener?.(refresh);
      observer.disconnect();
    };
    // `names` is a module-level constant at every call site; re-running on a
    // fresh array identity each render would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return tokens;
}
