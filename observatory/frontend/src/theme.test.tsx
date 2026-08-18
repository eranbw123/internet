import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeToggle } from "./ThemeToggle";
import { applyTheme, isTheme, nextTheme, readTheme, resolvedTheme, THEME_KEY } from "./theme";

/** jsdom has no real matchMedia, and the OS preference is exactly what the
 * three-state cascade hinges on, so every test states which OS it is on. */
function setOsPrefersDark(dark: boolean) {
  const listeners = new Set<() => void>();
  const mq = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue(mq));
  return { mq, listeners };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  setOsPrefersDark(false);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("theme state", () => {
  it("defaults to system when nothing is stored", () => {
    expect(readTheme()).toBe("system");
  });

  it("reads back a stored choice", () => {
    localStorage.setItem(THEME_KEY, "dark");
    expect(readTheme()).toBe("dark");
  });

  it("falls back to system on a junk stored value rather than trusting it", () => {
    localStorage.setItem(THEME_KEY, "solarized");
    expect(readTheme()).toBe("system");
  });

  it("survives localStorage throwing, which it does in a partitioned context", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(readTheme()).toBe("system");
    spy.mockRestore();
  });

  it("cycles system -> light -> dark -> system", () => {
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("system");
  });

  it("only accepts the three real themes", () => {
    expect(["light", "dark", "system"].every(isTheme)).toBe(true);
    expect(isTheme("auto")).toBe(false);
    expect(isTheme(null)).toBe(false);
  });
});

describe("applyTheme", () => {
  it("sets data-theme for an explicit choice", () => {
    applyTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    applyTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("REMOVES the attribute for system, instead of writing data-theme=\"system\"", () => {
    // Writing it would leave `:root:not([data-theme="light"])` matching inside
    // the media query, pinning a light-OS user to whatever the query said.
    applyTheme("dark");
    applyTheme("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});

describe("resolvedTheme", () => {
  it("passes an explicit choice straight through, whatever the OS says", () => {
    setOsPrefersDark(true);
    expect(resolvedTheme("light")).toBe("light");
    setOsPrefersDark(false);
    expect(resolvedTheme("dark")).toBe("dark");
  });

  it("follows the OS when the choice is system", () => {
    setOsPrefersDark(true);
    expect(resolvedTheme("system")).toBe("dark");
    setOsPrefersDark(false);
    expect(resolvedTheme("system")).toBe("light");
  });
});

describe("ThemeToggle", () => {
  it("starts on system and applies no attribute", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("button")).toHaveAttribute("data-theme-choice", "system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("drives the document attribute and persists the choice as it cycles", () => {
    render(<ThemeToggle />);
    const button = screen.getByRole("button");

    fireEvent.click(button); // system -> light
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");

    fireEvent.click(button); // light -> dark
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");

    fireEvent.click(button); // dark -> system
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem(THEME_KEY)).toBe("system");
  });

  it("restores the stored choice on the next mount", () => {
    localStorage.setItem(THEME_KEY, "dark");
    render(<ThemeToggle />);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(screen.getByRole("button")).toHaveAttribute("data-theme-choice", "dark");
  });

  it("names the effective theme, not just the selection, while on system", () => {
    setOsPrefersDark(true);
    render(<ThemeToggle />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe(
      "Theme: System (currently dark). Activate to change.",
    );
  });

  it("drops the parenthetical once the choice is explicit", () => {
    localStorage.setItem(THEME_KEY, "light");
    render(<ThemeToggle />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe(
      "Theme: Light. Activate to change.",
    );
  });

  it("keeps up when the OS flips underneath a system selection", () => {
    const { mq, listeners } = setOsPrefersDark(false);
    render(<ThemeToggle />);
    expect(screen.getByRole("button")).toHaveAttribute("data-theme-resolved", "light");

    act(() => {
      mq.matches = true;
      listeners.forEach((fn) => fn());
    });
    expect(screen.getByRole("button")).toHaveAttribute("data-theme-resolved", "dark");
  });

  it("stops listening to the OS once the user has chosen explicitly", () => {
    const { mq, listeners } = setOsPrefersDark(false);
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button")); // -> light

    act(() => {
      mq.matches = true;
      listeners.forEach((fn) => fn());
    });
    expect(screen.getByRole("button")).toHaveAttribute("data-theme-resolved", "light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});
