import "@testing-library/jest-dom/vitest";

/** jsdom implements no `window.matchMedia` at all, and useIsMobile() -- now
 * consulted by the explorer, the connections view and the offer provenance so
 * each can render its phone shape -- calls it during the first render. Without
 * this every component that reaches it throws "matchMedia is not a function"
 * before it renders anything.
 *
 * The stub reports "not mobile", which is the shape these suites assert
 * against (the desktop table, the connections canvas, the expanded score
 * breakdown). A test that wants the phone shape stubs matchMedia itself --
 * theme.test.tsx already does exactly that for the colour-scheme query.
 */
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
