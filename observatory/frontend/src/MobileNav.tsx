/** The phone's primary navigation: a bottom tab bar.
 *
 * Why a bottom bar rather than the header buttons it replaces. On a 393px
 * iPhone the header had to carry a hamburger, "Interests", "Compare" and the
 * theme toggle in one 44px strip; measured, every one of them was under the
 * 44x44 tap floor (the hamburger was 32x26) and the offers inbox -- the thing
 * the owner actually wants to reach from the sofa -- was three taps away
 * (Interests -> "Suggested for you" tab -> scroll). A bottom bar puts all five
 * destinations in the thumb arc at 44px+ each, and makes "Suggested" one tap
 * with its pending count visible from anywhere in the app.
 *
 * Icons are inline SVG using `currentColor` only: no colour literal is
 * introduced anywhere (test_observatory.py's guard scans .tsx too), and the
 * selected/unselected states come from the token palette in styles.css.
 */
export type MobileSurface = "explore" | "interests" | "offers" | "connections" | "compare";

function Icon({ name }: { name: MobileSurface }) {
  const common = {
    width: 22, height: 22, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", strokeWidth: 1.7,
    strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (name === "explore") {
    return (
      <svg {...common}>
        <circle cx="11" cy="11" r="6.5" /><path d="M16 16l4.5 4.5" />
      </svg>
    );
  }
  if (name === "interests") {
    return (
      <svg {...common}>
        <path d="M4 6h16M4 12h16M4 18h10" />
      </svg>
    );
  }
  if (name === "offers") {
    return (
      <svg {...common}>
        <path d="M12 3l2.4 5.3 5.6.7-4.1 3.9 1.1 5.6L12 15.8 6.9 18.5 8 12.9 4 9l5.6-.7z" />
      </svg>
    );
  }
  if (name === "connections") {
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="2.6" /><circle cx="18" cy="9" r="2.6" /><circle cx="9" cy="18" r="2.6" />
        <path d="M8.3 7.1l7.2 1.4M7.3 8.4l1.2 7.1" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="3.5" y="5" width="7" height="14" rx="1.5" />
      <rect x="13.5" y="5" width="7" height="14" rx="1.5" />
    </svg>
  );
}

const ITEMS: { id: MobileSurface; label: string }[] = [
  { id: "explore", label: "Explore" },
  { id: "interests", label: "Interests" },
  { id: "offers", label: "Suggested" },
  { id: "connections", label: "Links" },
  { id: "compare", label: "Compare" },
];

export function MobileNav({
  surface, onSelect, offerCount,
}: {
  surface: MobileSurface;
  onSelect: (s: MobileSurface) => void;
  offerCount: number | null;
}) {
  return (
    <nav className="mobile-nav" aria-label="Main" data-testid="mobile-nav">
      {ITEMS.map((item) => {
        const selected = surface === item.id;
        return (
          <button
            key={item.id}
            type="button"
            className={`mobile-nav-item ${selected ? "is-selected" : ""}`}
            aria-current={selected ? "page" : undefined}
            data-surface={item.id}
            onClick={() => onSelect(item.id)}
          >
            <span className="mobile-nav-icon">
              <Icon name={item.id} />
              {item.id === "offers" && offerCount ? (
                <span className="mobile-nav-badge" aria-hidden="true">{offerCount > 99 ? "99+" : offerCount}</span>
              ) : null}
            </span>
            <span className="mobile-nav-label">{item.label}</span>
            {item.id === "offers" && offerCount ? (
              <span className="sr-only">{offerCount} waiting</span>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}
