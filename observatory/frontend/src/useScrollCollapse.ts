import { useEffect, useState } from "react";

/** True while the reader is moving DOWN a list, false again the moment they
 * move back up or reach the top.
 *
 * This is the whole mechanism behind the owner's top complaint: "when you
 * scroll down interests, you don't see the main table columns names, but you
 * do see the interests offers and the activation suggestions on the top, and
 * it takes too much UI property on iPhone". Measured on an iPhone 15 (393 x
 * 852) before this hook existed, the permanently-pinned chrome on the
 * interests list was the 59px app header, a 110px sticky control block and
 * the 50px tab bar -- 219px, 26% of the display, held forever while you read
 * a list. Scrolling down now sheds the app header and the control block's
 * second row, leaving 106px; scrolling up brings them straight back, which is
 * the pattern every iOS list app uses and therefore the one that needs no
 * explaining.
 *
 * Why a document-level capture listener rather than a ref per scroller: the
 * phone shell has one scroll container per surface (`.ws-body`, `.explorer`,
 * `.compare-view`, the editor sheet) and they mount and unmount as the tab bar
 * switches. Scroll events do not bubble, but they DO reach a capturing
 * listener on `document`, so one listener sees every scroller including ones
 * that did not exist when the effect ran. `resetKey` re-arms it when the
 * surface changes, so arriving on a new tab never starts you off collapsed.
 *
 * The 6px dead zone stops the collapse flickering on the sub-pixel scroll
 * deltas iOS emits during rubber-banding, and `top <= 8` forces the expanded
 * state at the very top of a list so the header cannot get stuck hidden.
 */
export function useScrollCollapse(enabled: boolean, resetKey: string): boolean {
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    setCollapsed(false);
    if (!enabled) return;

    let lastTop = 0;
    let lastTarget: EventTarget | null = null;
    // Collapsing removes ~50px of document from ABOVE the reading position
    // (the control block's second row lives inside the scroller). Chrome's
    // scroll anchoring then corrects scrollTop to keep the visible content
    // still, which arrives here as a 50px scroll UP and expands everything
    // again -- the header visibly collapsed and instantly came back. The
    // scroller sets `overflow-anchor: none` so the correction is small, and
    // this lockout ignores whatever is left of it: for 350ms after a state
    // change, scroll events only re-baseline, they cannot flip the state.
    let lockedUntil = 0;

    const onScroll = (event: Event) => {
      const target = event.target;
      const top =
        target === document || target === window
          ? window.scrollY
          : (target as HTMLElement).scrollTop;
      if (typeof top !== "number") return;
      // A different scroller took over (tab switch, a modal opening): adopt its
      // position rather than diffing against the previous element's.
      if (target !== lastTarget) {
        lastTarget = target;
        lastTop = top;
        return;
      }
      const now = Date.now();
      if (now < lockedUntil) {
        lastTop = top;
        return;
      }
      const delta = top - lastTop;
      const change = (next: boolean) => {
        lastTop = top;
        setCollapsed((prev) => {
          if (prev !== next) lockedUntil = Date.now() + 350;
          return next;
        });
      };
      if (top <= 8) change(false);
      else if (delta > 6) change(true);
      else if (delta < -6) change(false);
    };

    document.addEventListener("scroll", onScroll, true);
    return () => document.removeEventListener("scroll", onScroll, true);
  }, [enabled, resetKey]);

  return collapsed;
}
