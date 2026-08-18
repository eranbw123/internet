# Literal → token migration (the PR-K follow-up)

PR K shipped the token system, the three-state theme, the toggle, and one
migrated proof-of-concept surface. It **deliberately did not** rewrite the
remaining colour literals: that rewrite touches nearly every line of
`styles.css` and `graph/GraphCanvas.tsx`, which is exactly what the concurrent
`observatory/pr8-compare-polish` session is rewriting. Doing both at once
produces a conflict in every hunk.

This file is the executable plan for that follow-up, so it can be done
mechanically once the frontend redesign lands.

## Where things stand

Measured on this branch (`interests/pr-k-dark-mode`, off `c7e812d`):

| | occurrences | distinct values | lines |
|---|---|---|---|
| `styles.css` before PR K | 100 | 53 | — |
| `styles.css` after PR K | **78** | 42 | 66 |
| `graph/GraphCanvas.tsx` | **10** | 8 | 5 |
| **remaining total** | **88** | | **71** |

PR K removed 22 of them: the ten `#98a2b0` muted-text greys (an AA fix, see
below) and the twelve in the migrated MonospaceViewer surface.

Re-measure at any time with:

```bash
# every colour literal still outside the token file
rg -n '#[0-9a-fA-F]{3,8}\b|rgba?\(' observatory/frontend/src/styles.css \
   observatory/frontend/src/graph/GraphCanvas.tsx
```

## What is visibly wrong until this lands

Dark mode is usable today — `color-scheme: dark` themes the native controls,
and the explorer, inspector, header and the migrated viewer all read correctly
— but two areas are still painted from literals and look broken on a dark page.
Both are in the graph pane, and both are the first rows of the table below:

1. **`.graph-toolbar`** — `background: #fff` with token-driven ink, so the
   toolbar is a white bar with white button labels (`styles.css` lines 72, 76).
2. **The React Flow minimap** — renders on the library's own
   `--xy-minimap-*` defaults (white), plus `GraphCanvas.tsx`'s hardcoded
   `maskColor` / `nodeStrokeColor`.

Fix those two first; they are most of the visible win.

## Mapping table

Every remaining literal, with the token that replaces it. Values are grouped by
the role they play, not by hue — that is the whole point of the exercise.

### `styles.css` — surfaces

| literal | × | lines | token |
|---|---|---|---|
| `#fff` | 10 | 72, 79, 104, 126, 159, 202, 253, 365, 372, 386 | `var(--surface-raised)` (cards, chips, menus, drawer, sheet) |
| `rgba(255,255,255,.92)` | 1 | 76 | `var(--surface-raised)` |
| `rgba(255,255,255,.95)` | 1 | 119 | `var(--surface-raised)` |
| `#eef1f5` | 4 | 189, 242, 251, 282 | `var(--neutral-bg)` (chip + score-bar track → `var(--track)` on 189) |
| `#f5f8ff` | 1 | 55 | `var(--hover)` |
| `#f6f8fb` | 2 | 219, 234 | `var(--surface)` |
| `#f4f7fb` | 2 | 210, 236 | `var(--accent-soft)` |
| `#eef4ff` | 1 | 163 | `var(--accent-soft)` |
| `#e8f0fb` | 1 | 254 | `var(--accent-soft)` |

### `styles.css` — ink

| literal | × | lines | token |
|---|---|---|---|
| `#556` | 6 | 109, 120, 150, 202, 251, 282 | `var(--fg-muted)` |
| `#667` | 4 | 56, 210, 241, 262 | `var(--fg-muted)` |
| `#778` | 3 | 80, 151, 278 | `var(--fg-faint)` |
| `#889` | 2 | 98, 184 | `var(--fg-faint)` |
| `#66707d` | 2 | 179, 189 | `var(--fg-muted)` |
| `#a8b3c2` | 1 | 95 | `var(--lane-title)` |

### `styles.css` — status / severity

| literal | × | lines | token |
|---|---|---|---|
| `#fdecea` | 4 | 122, 206, 270, 285 | `var(--error-bg)` |
| `#f2b8b5` | 2 | 122, 270 | `var(--error)` (border) |
| `#fff6f5` | 2 | 131, 143 | `var(--error-bg)` |
| `#e3f4e8` | 2 | 205, 284 | `var(--ok-bg)` |
| `#fdf3dc` | 2 | 215, 286 | `var(--active-bg)` |
| `#9a6b00` | 1 | 286 | `var(--active-text)` |
| `#8a6100` | 1 | 215 | `var(--warn-text)` |

### `styles.css` — grouped / derived nodes

| literal | × | lines | token |
|---|---|---|---|
| `#f6f4ff` | 2 | 115, 133 | `var(--group-bg)` |
| `#6b5bd2` | 2 | 115, 135 | `var(--group-border)` / `var(--group-label)` on 135 |
| `#4a3fa8` | 1 | 136 | `var(--group-fg)` |

### `styles.css` — lines, graph chrome, effects

| literal | × | lines | token |
|---|---|---|---|
| `#eee` | 1 | 54 | `var(--border-subtle)` |
| `#dfe5ec` | 1 | 89 | `var(--lane-line)` |
| `#ccc` | 1 | 376 | `var(--border-strong)` |
| `#8b98a9` | 1 | 176 | `var(--edge)` |
| `#c3ccd8` | 1 | 174 | `var(--handle)` |
| `rgba(255,255,255,.85)` | 1 | 178 | `var(--edge-label-bg)` |
| `#c9d4e8`, `#cfe0c9` | 2 | 111 | `var(--lane-3)`, `var(--lane-5)` (legend swatch gradient) |
| `rgba(28,37,48,.025)` | 1 | 88 | `var(--lane-band)` |
| `rgba(47,111,235,.03)` | 1 | 92 | `var(--lane-band-odd)` |
| `rgba(47,111,235,.3)` | 2 | 129, 164 | `var(--accent-ring)` |
| `rgba(47, 111, 235, .25)` | 1 | 43 | `var(--accent-ring)` |
| `rgba(28,37,48,.06)` | 1 | 160 | `var(--shadow)` |
| `rgba(28,37,48,.08)` | 1 | 126 | `var(--shadow)` |
| `rgba(28,37,48,.12)` | 1 | 105 | `var(--shadow)` |
| `rgba(0,0,0,.12)` | 1 | 389 | `var(--shadow-strong)` |
| `rgba(0,0,0,.3)` | 1 | 368 | `var(--scrim)` |

### `graph/GraphCanvas.tsx`

React Flow accepts any CSS colour string, so these become token reads. The six
`LANE_TINT` pastels are the categorical chart palette and already have tokens.

| line | literal | token |
|---|---|---|
| 123 | `#e8c9c9`, `#d9c9e8`, `#c9d4e8` | `--lane-1`, `--lane-2`, `--lane-3` |
| 124 | `#c9e0e8`, `#cfe0c9`, `#e8ddc9` | `--lane-4`, `--lane-5`, `--lane-6` |
| 446 | `#8b98a9` (edge marker) | `--edge` |
| 461 | `#b9c6d8` (minimap node fallback) | `--minimap-node` |
| 463 | `#8b98a9` (minimap node stroke) | `--edge` |
| 464 | `rgba(28, 37, 48, 0.08)` (minimap mask) | `--minimap-mask` |

Two of these are *props*, not CSS, so they need the computed value rather than
a `var()` string:

```ts
const token = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
```

Re-read it when the theme changes — `useTheme()` from `../theme` already
re-renders on every change, so depending on its `theme` value is enough. The
`LANE_TINT` map itself can move to plain `var(--lane-N)` strings, since those
are consumed as CSS.

Also worth doing in the same pass: React Flow's own defaults, which are
literals inside the vendored stylesheet. One additive block covers the minimap
without patching the library:

```css
.react-flow {
  --xy-minimap-background-color: var(--surface);
  --xy-minimap-mask-background-color: var(--minimap-mask);
  --xy-controls-button-background-color: var(--surface-raised);
  --xy-controls-button-color: var(--fg);
  --xy-attribution-background-color: transparent;
}
```

## Order of work

1. `.graph-toolbar` + the React Flow variable block (the two visible defects).
2. The `styles.css` table above, group by group — surfaces, ink, status,
   group, chrome. One commit per group keeps the review readable.
3. `GraphCanvas.tsx`'s ten literals.
4. Delete this file.

## The guard to add when it is done

Once the count reaches zero, lock it in — `test_observatory.py`'s
`ObservatoryThemeTokenTests` is the natural home, next to
`test_the_monospace_viewer_surface_has_no_colour_literals_left`, which already
does this for the one migrated surface:

```python
def test_no_colour_literals_survive_outside_tokens_css(self):
    for path in (self.STYLES_PATH, GRAPH_CANVAS_PATH):
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(re.findall(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', fh.read()), [])
```

## Already done in PR K (do not redo)

- The ten `#98a2b0` muted-text greys → `var(--fg-faint)`. This was one of the
  three WCAG AA failures the **light** theme was already shipping (2.58:1 on
  white); the other two, `--ok` `#2f9e44` (3.45:1) and the `--active` amber
  `#f0a500` (2.08:1), were token values and were fixed in `tokens.css`.
- `var(--ink, #223)` on `.interest-count b` → `var(--fg)`. `--ink` was never
  defined anywhere, so that always fell through to a literal.
- The whole MonospaceViewer surface (`.monospace-viewer`, `.viewer-*`,
  `.json-*`, `.search-hit*`), which is the proof-of-concept.
