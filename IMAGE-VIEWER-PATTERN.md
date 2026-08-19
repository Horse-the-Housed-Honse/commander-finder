# Pattern: Click-to-enlarge image + cycle through database entries

Extracted from `commander-finder/index.html` (vanilla HTML/CSS/JS, no build step,
no framework, no dependencies). This document is written for another Claude Code
instance that wants to reuse the same architecture in a different project.

---

## 1. The layer model

Three fixed-position layers, stacked by `z-index`. Each is a **single element that
exists in the HTML from page load** and is shown/hidden with one CSS class.

```
  z-index 200   LIGHTBOX          full-screen image, one <img> whose src is swapped
  z-index 100   MODAL / DRAWER    detail view for one DB entry, with ↑↓ navigation
  z-index   1   GRID / LIST       thumbnails; each carries its full-size URL inline
```

The key decision: **overlays are never created or destroyed at runtime.** They are
static markup toggled by `classList.add('hidden')` / `.remove('hidden')`.

Why this instead of `document.createElement` on demand:
- no re-parenting bugs, no memory churn, no "which overlay is mine" bookkeeping
- CSS transitions work, because the element is always in the DOM
- the ESC handler can query a single stable ID and know exactly what is open

Rejected alternative: the native `<dialog>` element. It is cleaner in principle,
but stacking two `<dialog>`s (modal + lightbox on top) fights the browser's own
top-layer ordering. Plain divs with explicit `z-index` are more predictable when
you need overlay-on-overlay.

---

## 2. The markup (put this once, near the top of `<body>`)

```html
<div class="lightbox hidden" id="lightbox" onclick="closeLightbox()">
  <button class="lightbox-close" onclick="closeLightbox()" title="Close (Esc)">✕</button>
  <img id="lightbox-img" src="" alt="">
</div>
```

Clicking the backdrop closes it because the handler is on the container itself.
The image is a child, so a click on the image *also* bubbles to the container and
closes it — which is the desired behaviour here (`cursor: zoom-out` on both).
If you want clicks on the image to NOT close, add `onclick="event.stopPropagation()"`
to the `<img>`.

```css
.lightbox {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,0.94);
  display: flex; align-items: center; justify-content: center;
  cursor: zoom-out;
}
.lightbox.hidden { display: none; }
.lightbox img {
  max-width: min(88vw, 488px);   /* second value = native asset width; don't upscale */
  max-height: 92vh;
  border-radius: 16px;
  box-shadow: 0 24px 70px rgba(0,0,0,0.85);
  cursor: zoom-out;
}
.lightbox-close {
  position: fixed; top: 1.2rem; right: 1.5rem;
  z-index: 201;                  /* must beat .lightbox itself */
}
```

`inset: 0` is shorthand for `top/right/bottom/left: 0`. Combined with
`position: fixed` it means "cover the whole viewport regardless of scroll".

`max-width: min(88vw, 488px)` is doing real work: it caps at the viewport on
mobile, but never blows a 488px-wide source image up into a blurry mess on a
4K monitor. Substitute your own asset's native width.

---

## 3. The open/close functions

```js
function openLightbox(event, src, alt) {
  event.stopPropagation();                       // ← see §4, this is critical
  const lb  = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  img.src = src;
  img.alt = alt || '';
  lb.classList.remove('hidden');
  document.body.style.overflow = 'hidden';       // scroll lock
}

function closeLightbox() {
  document.getElementById('lightbox').classList.add('hidden');
  document.getElementById('lightbox-img').src = '';   // free the decoded bitmap
  // Only restore page scroll if no modal is still open beneath us
  if (document.getElementById('group-modal').classList.contains('hidden')) {
    document.body.style.overflow = '';
  }
}
```

Two subtleties worth copying verbatim:

**`img.src = ''` on close.** Without it the browser holds the decoded bitmap for
every image you have ever enlarged. On a long browsing session that is real memory.

**The conditional scroll-unlock.** Both the modal and the lightbox set
`body.overflow = 'hidden'`. If the lightbox unconditionally cleared it on close,
closing the lightbox while the modal is still open would let the page behind the
modal scroll. The check makes the two layers cooperate. This is the single most
common bug when stacking overlays — a naive implementation clears a lock it did
not own.

---

## 4. Wiring thumbnails — and the click-conflict trap

The thumbnails sit inside a card that is *itself* clickable (it opens the modal):

```js
`<div class="card-grid-item" onclick="openModal('${escapedId}')">
   <img src="${thumbUrl}" data-large="${fullUrl}" alt="${entry.name}" loading="lazy"
        onclick="openLightbox(event, this.dataset.large, this.alt)">
   <div class="grid-name">${entry.name}</div>
 </div>`
```

Clicking the image fires `openLightbox`, then the event **bubbles** to the parent
div and fires `openModal` too — so you get a lightbox with a modal opening behind
it. `event.stopPropagation()` as the first line of `openLightbox` is what prevents
that. It is not decorative.

**`data-large` is the pattern for carrying the payload.** Rather than looking the
record back up by ID at click time, the full-size URL rides along on the element
in a `data-*` attribute and is read as `this.dataset.large`. It keeps the click
handler a pure function of the DOM node and means the handler needs zero knowledge
of your data layer.

**Two resolutions, always.** Thumbnail = small/normal asset, lightbox = large.
Loading full-res into a 195px grid slot wastes bandwidth and makes the grid crawl.
In this project the resolver handles missing sizes and multi-part records:

```js
function imgUri(card) {                                  // thumbnail
  if (card.image_uris?.normal) return card.image_uris.normal;
  if (card.card_faces) for (const f of card.card_faces)
    if (f.image_uris?.normal) return f.image_uris.normal;
  return null;                                           // caller renders a placeholder
}
function imgUriLarge(card) {                             // lightbox, with fallback
  if (card.image_uris?.large)  return card.image_uris.large;
  if (card.image_uris?.normal) return card.image_uris.normal;
  ...
}
```

Generalise by replacing these two functions with your own field lookups. Nothing
else in the pattern knows anything about the data source. Note the `?.` optional
chaining and the `${large||img}` fallback at the call site — the pattern degrades
to "enlarge the thumbnail" rather than breaking when no large asset exists.

Always emit an explicit placeholder branch when the resolver returns `null`, so
the grid keeps its geometry instead of collapsing:

```js
${img ? `<img …>` : `<div class="img-placeholder" style="width:${w}px">No image</div>`}
```

---

## 5. Cycling through entries

**State lives in a `Map`, not in the DOM.**

```js
let partnerIndex = new Map();                      // entryId -> current index
function getIndex(id)    { return partnerIndex.get(id) || 0; }
function setIndex(id, i) { partnerIndex.set(id, i); }
```

Reading "which item am I on" out of the DOM (a `data-idx` attribute, a class,
the visible text) means the DOM is your source of truth, and every re-render risks
losing it. A `Map` keyed by entry ID survives re-renders, and remembers position
per entry — reopen an entry and it is where you left it.

`step()` then performs a **surgical patch**, not a full re-render:

```js
function step(event, gid, dir) {
  event.stopPropagation();
  const group = groupMap.get(gid);
  if (!group) return;
  const cur  = getIndex(gid);
  const next = Math.max(0, Math.min(group.partners.length - 1, cur + dir));  // clamp
  if (next === cur) return;                                  // no-op at the ends
  setIndex(gid, next);

  const display = document.getElementById('pdisplay-' + CSS.escape(gid));
  if (!display) return;

  const navPanel = display.closest('.panel-partner');        // scope the query
  const btns = navPanel ? navPanel.querySelectorAll('.nav-btn') : [];
  if (btns[0]) btns[0].disabled = (next === 0);
  if (btns[1]) btns[1].disabled = (next === group.partners.length - 1);

  const label = navPanel ? navPanel.querySelector('.nav-label') : null;
  if (label) label.innerHTML = `<strong>${next+1}</strong> of <strong>${group.partners.length}</strong>`;

  display.style.opacity = '0';                               // cross-fade
  setTimeout(() => {
    display.innerHTML = partnerDisplayHtml(group, next);
    display.style.opacity = '1';
  }, 110);
}
```

Points to carry over:

| Technique | Why |
|---|---|
| `Math.max(0, Math.min(len-1, cur+dir))` | clamps instead of wrapping; ends feel like ends |
| `if (next === cur) return` | early-exit avoids a pointless fade at the boundary |
| `display.closest('.panel-partner')` | scopes the button lookup to *this* row — vital when several rows can be open |
| `CSS.escape(gid)` in the ID | IDs derived from data may contain `:` `/` `'`; `CSS.escape` makes them valid selectors |
| opacity 0 → swap → opacity 1 in a 110ms `setTimeout` | cross-fade with a CSS transition on `.partner-display`, no animation library |

**`step()` is called from three places** — the ↑↓ buttons, the arrow keys, and
(in the modal) the same buttons again. It works in all three because it addresses
elements by ID rather than by `event.target`, so it does not care who called it.
That is the reason the row drawer and the modal can share one navigation function.

Keyboard binding, scoped to the focused row (`tabindex="0"` on the row makes it
focusable):

```js
function handleKey(event, gid) {
  const el = document.getElementById('group-' + CSS.escape(gid));
  if (!el) return;
  const isOpen = el.classList.contains('open');
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleGroup(event, gid); }
  else if (isOpen && event.key === 'ArrowDown')   { event.preventDefault(); step(event, gid,  1); }
  else if (isOpen && event.key === 'ArrowUp')     { event.preventDefault(); step(event, gid, -1); }
  else if (isOpen && event.key === 'Escape')      { event.preventDefault(); toggleGroup(event, gid); }
}
```

`event.preventDefault()` on the arrows stops the page from scrolling underneath.

---

## 6. The ESC priority stack

One global listener, not one per overlay. It closes the **topmost** open layer:

```js
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!document.getElementById('lightbox').classList.contains('hidden')) {
      closeLightbox();                       // topmost first
    } else if (!document.getElementById('group-modal').classList.contains('hidden')) {
      closeGroupModal();
    }
  }
});
```

Order in this `if/else` chain must mirror `z-index` order, top layer first. With
per-overlay listeners, one ESC press would close both layers at once. Add new
layers to the top of the chain as you add them.

---

## 7. Known gap, if you want lightbox-internal cycling

In `commander-finder` the lightbox shows **one static image**; the ↑↓ cycling
happens in the modal *behind* it. If the goal is to page through records while
staying zoomed in, extend it like this — the state Map already supports it:

```js
let lightboxList = [];      // array of {src, alt}
let lightboxPos  = 0;

function openLightboxAt(event, list, pos) {
  event.stopPropagation();
  lightboxList = list; lightboxPos = pos;
  paintLightbox();
  document.getElementById('lightbox').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function paintLightbox() {
  const img = document.getElementById('lightbox-img');
  img.src = lightboxList[lightboxPos].src;
  img.alt = lightboxList[lightboxPos].alt;
}
function stepLightbox(dir) {
  const next = Math.max(0, Math.min(lightboxList.length - 1, lightboxPos + dir));
  if (next === lightboxPos) return;
  lightboxPos = next;
  paintLightbox();
}
```

Then extend the global key handler — **while the lightbox is open, arrows belong
to it, not to the row underneath**:

```js
document.addEventListener('keydown', e => {
  const lbOpen = !document.getElementById('lightbox').classList.contains('hidden');
  if (lbOpen && (e.key === 'ArrowRight' || e.key === 'ArrowDown')) { e.preventDefault(); stepLightbox( 1); }
  if (lbOpen && (e.key === 'ArrowLeft'  || e.key === 'ArrowUp'))   { e.preventDefault(); stepLightbox(-1); }
  ...
});
```

Preload the neighbours so paging feels instant:

```js
function preload(i) {
  if (lightboxList[i]) { const p = new Image(); p.src = lightboxList[i].src; }
}
// after each stepLightbox: preload(lightboxPos+1); preload(lightboxPos-1);
```

---

## 8. Porting checklist

```
[ ] Copy the .lightbox / .lightbox.hidden / .lightbox img / .lightbox-close CSS
[ ] Paste the #lightbox div once, near the top of <body>
[ ] Copy openLightbox() and closeLightbox() verbatim
[ ] Replace imgUri() / imgUriLarge() with your own field lookups (2 fns, nothing else)
[ ] On every thumbnail: data-large="…" + onclick="openLightbox(event,this.dataset.large,this.alt)"
[ ] Verify event.stopPropagation() is present if thumbnails sit inside clickable parents
[ ] Add the index Map + getIndex/setIndex if you need cycling
[ ] Add ONE global ESC listener ordered top layer first
[ ] Wrap any data-derived DOM id in CSS.escape() before querying it
[ ] Add loading="lazy" to grid thumbnails
```

## 9. Assumptions this pattern makes

- **No build step, no framework.** HTML is built by string templates and handlers
  are inline `onclick=` attributes. If porting into React/Vue, keep the layer model,
  the two-resolution rule, the state-in-a-Map rule and the ESC stack; replace the
  string templates with components and the inline handlers with props.
- **Data is already in memory** as an array/Map of records. There is no pagination
  or fetch inside the lightbox.
- **String interpolation into HTML.** Values come from a trusted API here. If your
  records can contain user-supplied text, HTML-escape before interpolating —
  `data-large="${url}"` and `alt="${name}"` are injection points otherwise.
