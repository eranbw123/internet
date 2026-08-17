import { beforeEach, describe, expect, it } from "vitest";
import { initialFocusMode } from "./useGraphData";

// Focus mode defaults ON for phones (a full run is unreadable at phone width)
// but an explicit choice wins and persists, so the toggle isn't re-guessed on
// every load or reset by every newly opened discovery.
describe("initialFocusMode", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("defaults on for mobile and off for desktop when nothing is stored", () => {
    expect(initialFocusMode(true)).toBe(true);
    expect(initialFocusMode(false)).toBe(false);
  });

  it("lets a stored choice override the mobile default", () => {
    localStorage.setItem("observatory-focus-mode", "false");
    expect(initialFocusMode(true)).toBe(false);
  });

  it("lets a stored choice override the desktop default", () => {
    localStorage.setItem("observatory-focus-mode", "true");
    expect(initialFocusMode(false)).toBe(true);
  });

  it("ignores a junk stored value and falls back to the viewport default", () => {
    localStorage.setItem("observatory-focus-mode", "yes-please");
    expect(initialFocusMode(true)).toBe(true);
    expect(initialFocusMode(false)).toBe(false);
  });
});
