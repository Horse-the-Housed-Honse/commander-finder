# CLAUDE.md — Commander Partner Finder

## Project type
Static frontend app. No backend, no build step, no npm. Vanilla HTML/CSS/JS, deployable to GitHub Pages by pushing files directly.

**File structure:** Claude Code can decide whether to keep everything in one `index.html` or split into `index.html` + `style.css` + `main.js`. Either works for GitHub Pages. No bundler either way.

## Non-negotiable rules

1. **No build step.** No webpack, vite, parcel, etc. If splitting files, use plain `<link>` and `<script src>` tags with relative paths.

2. **All data comes from Scryfall's live API** (`https://api.scryfall.com`). Never hardcode card lists, names, or IDs.

3. **Partner subtype rules are strict — do not relax them:**
   - Generic Partner pairs **only** with other Generic Partner cards
   - `partner—friends forever` → only FF × FF
   - `partner—character select` → only CS × CS
   - `partner—father & son` → only F&S × F&S
   - `partner—survivors` → only S × S
   - Named subtypes (`partner—X`) never cross-pair with Generic Partner or each other
   - The generic partner filter must exclude any card whose oracle text contains `partner—`

4. **Exactness check is mandatory.** A pairing only appears if `union(colorId(A), colorId(B))` equals the target identity exactly. Never show subsets or supersets.

5. **Explain the "why" before structural changes.** Horse is a beginner coder. If a change affects architecture or data flow, describe the reasoning first and confirm before proceeding.

## Repo setup (first session)
- Repo name suggestion: `commander-finder` under account `Horse-the-Housed-Honse`
- Starting file: `commander-partner-finder.html` → rename to `index.html`
- GitHub Pages: Settings → Pages → Deploy from branch → main / root
- No CI, no actions needed — just push the file

## Key functions (read before editing)
- `fetchAll(query)` — paginates Scryfall, returns full card array
- `isExactTarget(set)` — true only if set matches TARGET exactly (size + contents)
- `addPair(anchor, partner, type, tagClass, tagLabel)` — adds to groupMap, deduplicates by card ID
- `pairNamedGroup(group, type, tagClass, tagLabel)` — same-subtype-only pairing helper
- `renderAll()` — full re-render from filter state; call after any data/state change
- `step(event, gid, dir)` — advances partner index within an open group row

## When adding a new partner mechanic type
1. Add oracle text detection in `startSearch()` (after existing subtype filters)
2. Add pairing call (`pairNamedGroup` for same-subtype-only, custom loop otherwise)
3. Add CSS `.tag-X` class (follow existing pattern)
4. Add to `typeOrder` array
5. Add to `sectionMeta` object
6. Add `<option>` to the filter dropdown

## CSS variable convention
- `--gold`, `--gold-light` — primary accent color
- `--dark`, `--surface`, `--surface2`, `--surface3` — background layers (darkest to lightest)
- `--border`, `--border-hover` — gold-tinted border colors
- `--accent1-rgb`, `--accent2-rgb` — set dynamically from selected colors for background gradient

## Scryfall color identity convention
Always stored internally as **lowercase** (`w u b r g`). Scryfall returns `color_identity` as uppercase (`["W","U","R"]`) — convert on ingest with `.map(c => c.toLowerCase())`.
