/** Every timestamp the Observatory renders, in the owner's own clock.
 *
 * The engine stores UTC and the UI used to print it verbatim, so a discovery
 * collected at 17:23 local read "2026-08-18T14:23:10+00:00" -- three hours
 * wrong, in a tool whose whole job is telling you when things happened.
 *
 * Two rules this module exists to enforce.
 *
 * 1. ONE place. Formatting scattered across components is how half a UI ends
 *    up in a different zone from the other half; there were already two
 *    private formatters disagreeing with each other before this existed.
 *
 * 2. An IANA ZONE, never a fixed offset. Israel is UTC+3 in summer (IDT) and
 *    UTC+2 in winter (IST), so a hardcoded +3 is correct today and silently
 *    an hour wrong from late October. `Asia/Jerusalem` makes the runtime
 *    apply the rules, including whatever the Knesset does to them next.
 *
 * Parsing: every column measured in the production database carries an offset
 * ("+00:00" or "Z"), so `new Date` is unambiguous. A naive string would be
 * parsed as LOCAL time by the runtime and be silently hours off, so one is
 * treated as the UTC the engine writes everywhere else, rather than hoped
 * about.
 */

export const TIME_ZONE = "Asia/Jerusalem";

/** Date-only, e.g. "2026-08-04": a day, not an instant. Shifting one by a
 * timezone is how a quote from the 4th starts displaying as the 3rd. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
/** Has an explicit offset or a Z. Anything else is naive. */
const HAS_OFFSET = /(?:Z|[+-]\d{2}:?\d{2})$/i;

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  // A naive timestamp is the engine's UTC without its suffix -- say so
  // explicitly rather than letting the runtime read it as local time.
  const normalised = DATE_ONLY.test(raw) || HAS_OFFSET.test(raw) ? raw : `${raw}Z`;
  const d = new Date(normalised);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatter(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-GB", { timeZone: TIME_ZONE, ...options });
}

const DAY_FMT = formatter({ year: "numeric", month: "short", day: "numeric" });
const CLOCK_FMT = formatter({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const INSTANT_FMT = formatter({
  year: "numeric", month: "short", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

/** "18 Aug 2026, 17:23:10" -- a full instant in Israel time. */
export function formatInstant(value: string | null | undefined): string {
  const d = parse(value);
  return d ? INSTANT_FMT.format(d) : (value ?? "");
}

/** "17:23:10" -- the clock alone, for places already grouped by day. */
export function formatClock(value: string | null | undefined): string {
  const d = parse(value);
  return d ? CLOCK_FMT.format(d) : (value ?? "");
}

/** "4 Aug 2026" -- a calendar day. */
export function formatDay(value: string | null | undefined): string {
  const d = parse(value);
  if (!d) return value ?? "";
  // A date-only value names a day in nobody's timezone; formatting it in
  // Jerusalem would still be right today but is one DST rule away from
  // rendering the previous day, so it is read back in UTC as it was written.
  if (DATE_ONLY.test(String(value).trim())) {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC", year: "numeric", month: "short", day: "numeric",
    }).format(d);
  }
  return DAY_FMT.format(d);
}

/** The exact stored instant, for a `title=`. The friendly text is what you
 * read; this is what you check when the friendly text looks wrong. */
export function exactTitle(value: string | null | undefined): string | undefined {
  const d = parse(value);
  if (!d) return undefined;
  return `${d.toISOString()} (UTC) · shown in ${TIME_ZONE}`;
}
