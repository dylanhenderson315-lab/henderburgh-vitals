# Design System Migration — Handoff Brief

**Paste this whole file as the prompt.** It is self-contained on purpose: assume the
agent has no memory of prior conversations.

---

## The task

Migrate the remaining pages of Henderburgh OS onto the design system defined in
`static/tokens.css`. This is deliberately **mechanical** work — the design decisions
are already made and encoded in that file. Do not invent new colours, sizes, radii,
or components. Your job is to replace drifted one-off values with the decided ones.

Repo: `/Users/dylanhenderson/oura-dashboard` · Live: https://henderburgh.com

## Stack constraints — violating these breaks the site

- Python **FastAPI + Jinja2** server-rendered templates. **No React. No build step.
  No bundler. No npm.**
- **Tailwind via CDN**, vanilla JS, Chart.js, anime.js, native Web Animations API.
- Anything you add must be plain HTML/CSS/vanilla-JS, or a single small
  dependency-free CDN script.
- **Do NOT suggest or install Framer Motion, 21st.dev, shadcn, or any React
  library.** They cannot run here.

## Scope

**Already migrated — use as reference, do not redo:**
- `static/tokens.css` — the system itself
- `templates/home.html` — the front door (reference implementation)
- `templates/home-assistant.html` — tokens linked; page body NOT yet migrated

**To migrate (in priority order):**
1. `templates/home-assistant.html` — **the workhorse.** Used many times a day,
   one-handed, on a phone, often in a dark room. Highest care.
2. `templates/dashboard.html` — the `/vitals` page. Densest page, ~2000 lines.
3. `templates/xbox.html`
4. `templates/clips.html`
5. `templates/blog.html`
6. `templates/insights.html`, `templates/golf.html`, `templates/model.html`

## What to change

For each template:

1. **Link the tokens** in `<head>` if absent:
   `<link rel="stylesheet" href="/static/tokens.css?v=1">`
2. **Replace raw values with tokens** (see mapping below).
3. **Retire tiny text.** Any `text-[8px]`/`text-[9px]`/`text-[10px]` used for *prose*
   becomes `var(--t-xs)` (12px) minimum. 10px (`--t-label`) is allowed **only** for
   mono uppercase labels, never sentences.
4. **Fix contrast.** `text-zinc-500` (~3.0:1, fails WCAG AA) must become `--text-2`
   for anything a user needs to read. `--text-3` is only for ≥12px incidental text.
5. **Enforce touch targets.** Any interactive control under 44px gets `.hb-tap` or
   explicit `min-height: var(--tap-min)`. The lighting page has 26px-tall toggles —
   fix those.
6. **Fix the font-stack typo.** Several templates declare `system_ui` (underscore),
   an invalid keyword that silently drops the system-font fallback. Use
   `var(--font-sans)` / `var(--font-display)`.

### Mapping

| Found in templates | Replace with |
|---|---|
| `#0f1115`, `#0a0a0b`, `#111113` (page bg) | `var(--bg)` |
| `#161a20`, `#18181b` (card bg) | `var(--surface)` |
| `#27272a` (hover/raised) | `var(--surface-2)` |
| `rgba(255,255,255,0.08)`, `#3f3f46` (borders) | `var(--line)` |
| `#52525b` (stronger border) | `var(--line-strong)` |
| `#e2e8f0`, `#f4f4f5`, `#fafafa` | `var(--text)` |
| `#94a3b8`, `#a1a1aa`, `text-zinc-400` | `var(--text-2)` |
| `#71717a`, `text-zinc-500` | `var(--text-3)` *(only if ≥12px)* |
| `#4ade80`, `#10b981`, `#22c55e` | `var(--accent)` |
| `#60a5fa`, `#3b82f6` | `var(--info)` |
| `#f59e0b`, `#fcd34d` | `var(--warn)` |
| `border-radius: 2–10px` | `var(--r-sm)` |
| `border-radius: 12–14px` | `var(--r-md)` |
| `border-radius: 16px, 1.5rem, rounded-2xl/3xl` | `var(--r-lg)` |
| `999px`, `9999px`, `rounded-full` | `var(--r-full)` |
| ad-hoc `box-shadow` | `var(--shadow-sm|md|lg)` |
| `transition: … 0.2s ease` | `var(--d-fast)` + `var(--ease-out)` |

### Primitives available
`.hb-card`, `.hb-label`, `.hb-stat` + `.hb-stat-unit`, `.hb-press`,
`.hb-dot` (+ `-live` / `-stale` / `-off`), `.hb-tap`. Read `static/tokens.css` first.

## Page-specific requirements

**`/home-assistant` (the workhorse) — extra care:**
- Controls must sit in the **thumb arc** (lower ~60% of a 375×812 screen). The most
  used room is **Game Room**.
- Toggle feedback must fire on `pointerdown` and complete in **under 100ms**
  (`var(--d-press)`). It must never wait on the network — the page already does
  optimistic UI; preserve that.
- Must be usable in a dark room: no blinding whites, "on" vs "off" distinguishable by
  **fill and brightness**, not hue alone.
- **Do not restyle away any state.** Pending and error states must stay obvious.
- Known gap worth fixing: `roomOrGroupMaster` (~line 4086) `await`s `Promise.all`
  with **no `.catch()`/revert**, relying only on an external 350ms reconcile.

**`/vitals` (dashboard.html):** heaviest page. Do not touch Chart.js config or the
HTMX swap logic. Style only.

## Absolute rules

1. **Never let an animation own visibility.** This bug has bitten this codebase
   twice: elements set to `opacity: 0` awaiting a JS callback (IntersectionObserver
   or an anime.js `complete`) stayed permanently blank when the tab was backgrounded,
   because browsers pause rAF. Content must be visible by default and animation may
   only decorate. Always provide a non-JS fallback path.
2. **Preserve the work-hours privacy layer.** Weekday 08:30–17:00 gaming/watching/
   sleep is hidden from public view (see `in_work_hours` / `reveal` in
   `services/xbox.py`). Never expose redacted data, and never fabricate a value to
   fill a gap — hiding is fine, lying is not.
3. **Escape everything interpolated into `innerHTML`.** There was a stored-XSS bug
   here. Reuse the `esc()`/`dxEsc()` helper pattern.
4. **Do not remove any existing element `id` or JS function name.** Other scripts
   depend on them.
5. **Do not commit or push.** Leave changes in the working tree and report.

## Verification — required, per file

Run these and paste the **real output** (do not paraphrase):

```bash
cd /Users/dylanhenderson/oura-dashboard

# 1. Template compiles
.venv/bin/python -c "from jinja2 import Environment, FileSystemLoader; \
  Environment(loader=FileSystemLoader('templates')).get_template('TEMPLATE.html'); print('OK')"

# 2. Nothing lost — must print nothing
diff <(git show HEAD:templates/TEMPLATE.html | grep -oE 'id=\"[^\"]+\"' | sort -u) \
     <(grep -oE 'id=\"[^\"]+\"' templates/TEMPLATE.html | sort -u) | grep '^<'
diff <(git show HEAD:templates/TEMPLATE.html | grep -oE 'function [A-Za-z0-9_]+\(' | sort -u) \
     <(grep -oE 'function [A-Za-z0-9_]+\(' templates/TEMPLATE.html | sort -u) | grep '^<'

# 3. App boots and every page still 200
OURA_TOKEN="$OURA_TOKEN" .venv/bin/python -m uvicorn main:app --port 8093 &
sleep 6
for p in / /vitals /home-assistant /xbox /clips /blog /insights; do
  echo "$p -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 25 http://127.0.0.1:8093$p)"
done
```

Then **load the page in a real browser at 375×812**, screenshot it, scroll it, and
confirm: no element stuck invisible, no horizontal overflow, no clipped text, every
control ≥44px.

Report what you changed, the verification output, and anything you think is wrong
with this brief. If you could not verify something, **say so plainly** rather than
claiming success.
