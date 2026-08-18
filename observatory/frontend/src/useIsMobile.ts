import { useEffect, useState } from "react";

/** The one place the phone-layout breakpoint is defined.
 *
 * It used to live twice -- a `MOBILE_BREAKPOINT = 480` constant in App.tsx and
 * a hand-synced `@media (max-width: 480px)` block in styles.css -- which is a
 * standing invitation for the JS and the CSS to disagree about what "mobile"
 * means. Driving both from one matchMedia query removes that class of bug.
 *
 * 768, not 480: at 480 every device between 481px and 768px (large phones,
 * landscape phones, small tablets) got the full three-pane desktop layout with
 * a fixed 380px inspector pane, which does not fit.
 *
 * The second clause is for a phone held sideways. An iPhone 15 in landscape is
 * 852 x 393, which sails past a width-only test and got the desktop layout in
 * a window 393px tall -- three panes and a 380px inspector inside it. Short
 * AND narrow is the shape that identifies it: 852x393 and 932x430 (Pro Max)
 * match, an iPad in landscape (1024x768) does not, and neither does any
 * ordinary desktop window. `pointer: coarse` would say it more directly but is
 * not reliably reported under headless emulation, so it cannot be tested.
 *
 * styles.css and interests.css repeat this query verbatim; test_observatory.py
 * asserts the three copies are identical, because a JS/CSS disagreement about
 * what "mobile" means renders half of each layout.
 */
export const MOBILE_MEDIA_QUERY =
  "(max-width: 768px), (max-height: 500px) and (max-width: 950px)";

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(() => window.matchMedia(MOBILE_MEDIA_QUERY).matches);
  useEffect(() => {
    const mql = window.matchMedia(MOBILE_MEDIA_QUERY);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mql.addEventListener("change", onChange);
    // Re-sync on mount in case the viewport changed between first render and
    // the listener being attached (rotation during load).
    setIsMobile(mql.matches);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return isMobile;
}
