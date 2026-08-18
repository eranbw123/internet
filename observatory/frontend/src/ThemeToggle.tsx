import { useResolvedTheme, useTheme } from "./theme";

const FACE: Record<string, { glyph: string; label: string }> = {
  system: { glyph: "◐", label: "System" },
  light: { glyph: "☀", label: "Light" },
  dark: { glyph: "☾", label: "Dark" },
};

/** The theme control: one button cycling system -> light -> dark, in the app
 * header. A cycle rather than a switch because there are three states, and
 * labelled rather than icon-only because "half moon" does not read as "follow
 * the OS" to anyone.
 *
 * The accessible name says both what is selected AND what that currently
 * resolves to, since "System" alone tells a screen-reader user nothing about
 * what is on screen. */
export function ThemeToggle() {
  const { theme, cycle } = useTheme();
  const resolved = useResolvedTheme(theme);
  const face = FACE[theme];

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={cycle}
      aria-label={`Theme: ${face.label}${theme === "system" ? ` (currently ${resolved})` : ""}. Activate to change.`}
      data-theme-choice={theme}
      data-theme-resolved={resolved}
      title="Cycle theme: system, light, dark"
    >
      <span aria-hidden="true">{face.glyph}</span>
      <span className="theme-toggle-label">{face.label}</span>
    </button>
  );
}
