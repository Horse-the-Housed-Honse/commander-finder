# Commander Partner Finder — SPEC.md

## What This Is

A static single-page app that lets a user pick any MTG color identity and see every legal commander pairing — solo, partner, partner-with, background, friends forever, character select, father & son, survivors, and doctor's companion — that resolves to **exactly** that identity. Card images load on demand from Scryfall. Deployable to GitHub Pages or Netlify with no backend.

---

## File Structure Decision

**Claude Code should decide** whether to keep this as a single `index.html` or split into `index.html` + `style.css` + `main.js`. Both are valid for GitHub Pages — a split structure works fine as long as paths are relative. The tradeoff:

- **Single file** → simpler to deploy and share, harder to navigate as the codebase grows
- **Split files** → better for long-term maintenance, easier to diff CSS vs JS changes, no bundler needed

Either way: **no build step, no npm, no bundler.** Vanilla HTML/CSS/JS only. GitHub Pages serves static files directly.

**Current state of the codebase:** the working app lives in `commander-partner-finder.html` (rename to `index.html` when setting up the repo). All CSS is in a `<style>` block and all JS is in a `<script>` block within that file. Claude Code can refactor the structure as it sees fit at the start of the first session.

---

## Repo Setup (Starting Fresh)

No GitHub repo exists yet. Steps to create one and deploy to GitHub Pages:

1. Create a new repo at github.com (name suggestion: `commander-finder`)
2. Add `index.html` (and optionally `style.css` / `main.js` if splitting)
3. Go to repo **Settings → Pages → Source → Deploy from branch → main / root**
4. Live at `https://horse-the-housed-honse.github.io/commander-finder/`

For local development, just open `index.html` in a browser directly — no dev server needed since all fetches go to Scryfall's external API (no same-origin issues).

---

## Architecture

**Data source: Scryfall API (live, client-side)**
All data is fetched at runtime from `https://api.scryfall.com`. Nothing is cached or bundled. The app auto-updates when new sets drop or cards change legality — no code changes needed.

**Two screens, no router**
- **Screen 1 (`#identity-screen`):** Color identity picker. User selects colors via pip toggles or named preset buttons (guilds, shards, wedges, nephilim, WUBRG). Clicking "Find Commanders" triggers the search.
- **Screen 2 (`#results-screen`):** Results grouped by anchor card. Each group row expands to show card images with ↑/↓ keyboard navigation to cycle through valid partners. A back button returns to the picker without a page reload.

---

## Key Data Flows

### 1. Color Selection → Search
- User picks colors → `getSelectedColors()` returns e.g. `['W','U','R']`
- `startSearch()` converts to a lowercase Scryfall identity string (`'wur'`)
- Four sequential Scryfall fetches:
  1. `is:commander legal:commander id=<target>` → solo commanders
  2. `is:commander legal:commander id<=<target> is:partner` → all partnerable commanders within the target identity
  3. `t:background legal:commander id<=<target>` → Background enchantments
  4. `is:commander legal:commander t:"time lord" t:doctor` → Time Lord Doctors (for Doctor's Companion)
- `fetchAll()` handles Scryfall pagination automatically

### 2. Pairing Logic
After fetching, cards are categorized by oracle text and paired:

| Type | Oracle text detected | Pairing rule |
|---|---|---|
| Generic Partner | `partner (you can have two commanders` (no dash) | Any generic partner × any generic partner |
| Partner With | `partner with [name]` | Fixed named pair only |
| Choose a Background | `choose a background` | Commander × any Background |
| Friends Forever | `partner—friends forever` | Only FF × FF |
| Character Select | `partner—character select` | Only CS × CS |
| Father & Son | `partner—father & son` | Only F&S × F&S |
| Survivors | `partner—survivors` | Only S × S |
| Doctor's Companion | `doctor's companion` | Companion × any Time Lord Doctor within target identity |
| Solo | (no partner mechanic) | `id=<target>` exactly |

**Critical rule:** Named partner subtypes (`partner—X`) can ONLY pair within their own subtype. They do NOT cross-pair with generic Partner or each other. The generic partner filter explicitly excludes any card whose oracle text contains `partner—`.

**Exactness:** A pairing is valid only if `union(colorId(a), colorId(b))` equals the target identity exactly — not a subset, not a superset. `isExactTarget()` enforces this.

### 3. Grouping
Valid pairs are stored in `groupMap: Map<cardId, { anchor, partners[], type, tagClass, tagLabel }>`. Each anchor card gets one entry; all valid partners for that anchor are listed under it. This is what enables the "one row per anchor, cycle through partners" UX.

### 4. Rendering
- `renderAll()` reads filter state and renders sections by `typeOrder`
- Each group renders as a collapsed `.group-item` row
- Click to expand → injects drawer HTML with anchor image (fixed left panel) and partner cycling panel (right)
- `step(event, gid, dir)` swaps partner display with a 110ms opacity fade
- Keyboard: `↑`/`↓` to step, `Enter`/`Space` to toggle open/close, `Escape` to close

---

## State

All state is in-memory JS variables. Nothing is persisted to localStorage or any backend.

| Variable | Type | Purpose |
|---|---|---|
| `TARGET` | `Set<string>` | Currently searched color identity (lowercase) |
| `groupMap` | `Map<cardId, group>` | All computed pairings, grouped by anchor |
| `soloCards` | `Card[]` | Solo commanders with exact target identity |
| `partnerIndex` | `Map<cardId, number>` | Which partner is currently shown per group |
| `activeGroupId` | `string \| null` | Currently open group row |

---

## Color System

`COLOR_DATA` maps `W/U/B/R/G` to background color, foreground color, and RGB values used for dynamic CSS accent gradients. Colors are always stored internally as **lowercase** (`w/u/b/r/g`) to match Scryfall's API. UI display uses uppercase.

`PRESETS` is a flat array of all named identities (10 guilds + 5 shards + 5 wedges + 5 nephilim + WUBRG) used to populate the preset buttons and to generate human-readable names for the results header.

---

## Scryfall API Notes

- Base URL: `https://api.scryfall.com/cards/search`
- Pagination: response includes `has_more` and `next_page`. `fetchAll()` loops until exhausted.
- `not_found` error code (0 results) is treated as an empty array, not an error.
- Image URIs: `card.image_uris.normal` for single-faced cards; `card.card_faces[0].image_uris.normal` for DFC/split cards.
- Color identity: `card.color_identity` is an array of uppercase color letters, e.g. `["W", "U"]`.
- Oracle text: `card.oracle_text` for single-faced; concatenate `card.card_faces[].oracle_text` for multi-faced.
- Scryfall asks that automated clients add a delay between requests and identify themselves — not an issue here since the user triggers fetches manually and there are only 4 requests per search.

---

## Known Limitations / Backlog

- **Partner With resolution:** Named partners are resolved by matching oracle text against the `partnerables` pool. If the named partner falls outside the target identity (and thus wasn't fetched), it won't appear. This is correct behavior.
- **No URL state:** Selecting Jeskai and sharing the URL doesn't preserve the selection. A `?colors=WUR` query param approach would fix this.
- **No sorting:** Results sort alphabetically by anchor name (Scryfall default). EDHREC rank sort would be useful.
- **Mobile layout:** The two-panel drawer (anchor left, partner right) stacks awkwardly below ~600px. Needs a responsive rework.
- **Colorless commanders:** ✅ Done — grey C pip added, mutually exclusive with colored pips.
- **Variable color identity commanders:** Some cards (e.g. Faceless One, Clara Oswald) let the player choose their color identity at deck-building time. These could theoretically fill any missing color in a pairing. Currently the app shows them only at their printed identity. A future enhancement could flag these cards specially and let users explore what additional identities become reachable by including them.

### Cross-tool: shared card-image viewer module

**Status:** deferred by decision, not blocked. Revisit when a third tool needs it.

**Idea:** the click-to-enlarge image viewer (lightbox + entry cycling) is currently
embedded in `index.html`. Other MTG tools in this family — e.g. a Card Kingdom
sell-list tool — will want the same behaviour. Rather than maintaining N copies,
extract it once into a shared `card-viewer.js` that any tool loads with a plain
`<script src>` (no build step, consistent with rule 1).

**Scope of what gets shared:** the *viewer code* only. The images themselves already
work this way — they live on Scryfall's servers and every tool fetches them by URL,
so there is nothing to centralize there. A shared module would own:
- `imgUri(card)` / `imgUriLarge(card)` — Scryfall image-URL resolution, including
  `card_faces` fallback for double-faced cards and the normal→large degradation
- `openLightbox()` / `closeLightbox()` — overlay show/hide, scroll lock, ESC handling
- optionally the index-Map cycling state (`getIndex`/`setIndex`/`step`)

**Deliberate decision (2026-08-19):** copy-paste into the second tool instead of
extracting now. Extracting a shared module pays off only once the same code is being
maintained in three or more places and duplicate bug-fixes are actually being felt.
Premature extraction adds hosting/versioning complexity for no benefit while there
is one consumer. **Trigger to revisit: the third copy-paste.**

**Open questions when it is time:**
- Where does the shared file live? Same GitHub Pages origin as the tools, its own
  repo, or a `mtg-tools/shared/` directory served alongside?
- Cross-origin `<script src>` from GitHub Pages works, but pins every tool to that
  file's availability. A copied-and-pinned version may still be preferable.
- Versioning: how does one tool avoid breaking when the shared file changes?

**Reference:** `IMAGE-VIEWER-PATTERN.md` in this repo documents the full pattern —
layer model, the three non-obvious gotchas (`stopPropagation`, conditional
scroll-unlock, state-in-a-Map), and a porting checklist. That doc is the interim
"build once" artifact: prose instead of a module.

### Lightbox-internal entry cycling

The lightbox currently shows a single static image; the ↑↓ cycling happens in the
modal *behind* it. Paging between entries while staying zoomed in would need
`lightboxList` / `lightboxPos` state plus arrow-key routing that gives arrows to the
lightbox while it is open. Sketched in §7 of `IMAGE-VIEWER-PATTERN.md` (~25 lines).
