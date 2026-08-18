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
 */
export const MOBILE_MEDIA_QUERY = "(max-width: 768px)";

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
