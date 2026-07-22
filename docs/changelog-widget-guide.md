# Changelog / "What's New" widget — implementation guide

A short guide for adding a SaaS-style changelog notification to any site: a small
entry point (e.g. a bell icon) shows an unread indicator when there are updates
the visitor hasn't seen; clicking it opens a panel listing recent changes and
clears the indicator. No backend required — everything below is static-site
friendly (a JSON file + vanilla JS), so it works regardless of your stack.

## 1. The pattern

- **Entry point**: a small button/icon in your header or nav (bell, gift box,
  megaphone — pick one that fits your brand). Sits quietly until there's
  something new.
- **Unread indicator**: a small dot/badge on that button, shown only when the
  visitor hasn't seen the latest entry yet.
- **Panel**: clicking the entry point opens a dropdown or modal listing recent
  entries, newest first — a date/title and one or two lines of plain-language
  description each. Not a full commit log; pick the changes visitors actually
  care about.
- **Seen state**: clears the indicator as soon as the panel is opened, and
  persists that across visits (so it doesn't reappear until the *next* new
  entry ships).

## 2. Data model

A single JSON file, newest entry first:

```json
[
  { "id": "2026-07-22-topbar", "date": "2026-07-22", "title": "New top bar", "body": "Added community navigation and site links." },
  { "id": "2026-07-19-docker", "date": "2026-07-19", "title": "Docker deploy", "body": "One-command deploy via Docker Compose + Cloudflare Tunnel." }
]
```

`id` just needs to be unique and sortable (a date-prefixed slug works well).
Keep entries short — this is for visitors, not a commit history.

## 3. Seen-state logic

Store the `id` of the newest entry the visitor has seen in `localStorage`.
Unread = the newest entry's `id` differs from (or sorts after) the stored one.

```js
const SEEN_KEY = "changelog:lastSeenId";

function hasUnread(entries) {
  if (!entries.length) return false;
  const lastSeen = localStorage.getItem(SEEN_KEY);
  return entries[0].id !== lastSeen;
}

function markSeen(entries) {
  if (entries.length) localStorage.setItem(SEEN_KEY, entries[0].id);
}
```

## 4. Minimal vanilla implementation

Framework-agnostic — adapt the markup/classes to your own site's styling.

```html
<button id="whats-new-button" aria-label="What's new" aria-haspopup="dialog">
  🔔<span id="whats-new-dot" hidden></span>
</button>

<div id="whats-new-panel" role="dialog" aria-label="What's new" hidden>
  <div id="whats-new-list"></div>
</div>

<style>
  #whats-new-button { position: relative; }
  #whats-new-dot {
    position: absolute; top: 2px; right: 2px;
    width: 8px; height: 8px; border-radius: 50%;
    background: #e0473e; /* pick a color that reads as "new" against your header */
  }
  #whats-new-panel {
    position: absolute; /* anchor near the button in your own layout */
    max-width: 320px; padding: 12px 14px; border-radius: 12px;
  }
  #whats-new-panel .entry { padding: 8px 0; border-top: 1px solid rgba(0,0,0,0.08); }
  #whats-new-panel .entry:first-child { border-top: none; }
  #whats-new-panel .entry time { font-size: 0.75rem; opacity: 0.6; }
</style>

<script>
(async function () {
  const SEEN_KEY = "changelog:lastSeenId";
  const button = document.getElementById("whats-new-button");
  const dot = document.getElementById("whats-new-dot");
  const panel = document.getElementById("whats-new-panel");
  const list = document.getElementById("whats-new-list");

  const entries = await fetch("/changelog.json").then((r) => r.json());

  function render() {
    list.innerHTML = entries.map((e) => `
      <div class="entry">
        <time>${e.date}</time>
        <strong>${e.title}</strong>
        <p>${e.body}</p>
      </div>
    `).join("");
  }

  function updateDot() {
    const lastSeen = localStorage.getItem(SEEN_KEY);
    dot.hidden = !entries.length || entries[0].id === lastSeen;
  }

  button.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      render();
      if (entries.length) localStorage.setItem(SEEN_KEY, entries[0].id);
      updateDot();
    }
  });

  document.addEventListener("click", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== button) {
      panel.hidden = true;
    }
  });

  updateDot();
})();
</script>
```

## 5. Integration notes

- Serve `changelog.json` from wherever's convenient (static file, same origin
  as your page — no CORS complications).
- Keep entries editable by anyone on the team without a deploy if possible —
  a plain JSON file in the repo, edited via a normal PR, is usually enough;
  no CMS needed for a handful of entries a month.
- If your header already has other icon buttons (GitHub link, help, etc.),
  match their existing size/spacing/hover style rather than introducing a new
  visual language for just this one button.
- Don't overthink entry granularity: one entry per user-visible change is
  plenty; don't log internal refactors or dependency bumps here.

## 6. Accessibility

- The entry-point button needs a real accessible name (`aria-label="What's new"`
  or equivalent) — an emoji/icon alone isn't enough.
- The panel should be reachable and closeable via keyboard (Escape to close,
  focus should land inside it when opened) if you're rolling your own rather
  than using a dialog/popover primitive your framework already provides.
- Don't rely on color alone for the unread indicator if you can help it — a
  dot is fine paired with the button's accessible name, but avoid making the
  *only* signal a color change with no text equivalent for screen readers.
