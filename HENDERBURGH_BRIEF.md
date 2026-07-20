# HENDERBURGH OS — Master Design & Engineering Brief

You are an elite UI/UX designer + senior frontend engineer specializing in
high-craft personal operating systems, quantified-self dashboards, and immersive
web experiences. This file is the canonical brief: paste it (or point an agent at
it) before any significant design or frontend work on henderburgh.com.

## ⚠️ The stack — read first, everything depends on it

**FastAPI + Jinja2 server-rendered templates. Tailwind via CDN. Vanilla JS.
Chart.js, anime.js, native Web Animations API. NO React. NO Next.js. NO build
step. NO npm/bundler.** Deployed on Railway (Docker), media on Cloudflare R2,
proxied by Cloudflare (100 MB upload cap — big media goes direct-to-R2 presigned).

This corrects earlier briefs that assumed a React stack. Any instruction
elsewhere that says "React", "Next.js", or "install X" resolves as follows:

| Asked for | Reality here | Use instead |
|---|---|---|
| Framer Motion / Motion library | React-only; cannot run | Native WAAPI + `static/motion.js`; Motion One (motion.dev, vanilla ~5kb CDN) if springs/scroll-linked needed |
| 21st.dev Magic MCP components | React components; no MCP installed | Treat 21st.dev as **visual reference only**; re-implement as HTML + Tailwind + vanilla JS (the pointer-tracked spotlight cards on home.html are the worked example) |
| Higgsfield MCP media generation | Connector not installed (verified) | If/when the owner supplies the MCP URL/key it can be wired; until then, static assets or CSS/canvas generative accents |
| UI/UX Pro Max skill | Not an installed skill | The equivalent intelligence lives in this repo: `static/tokens.css` (the decided system) + the research digests below |

## Primary goal

Elevate Henderburgh from a clean functional dashboard into a premium, immersive,
studio-quality **Personal OS** that still feels fast, personal, and data-true. A
living digital twin — precise, calm, slightly futuristic, highly crafted — never
a generic AI-portfolio or over-designed agency site.

## Page hierarchy (owner's words, decided)

- **`/` (home) = the front door.** The showstopper; what gets shown to people.
  Theatre is allowed here — but it's loaded ~10x/day, so every flourish must be
  gorgeous once and instant on repeat (session-gate entrances; see home.html).
- **`/home-assistant` (lighting) = the workhorse.** Real control surface for real
  lights: one-handed, phone, often a dark room. Sub-100ms tactile feedback,
  honest pending/error states, thumb-arc ergonomics, 44px targets. Game Room is
  the most-used room. Almost all theatrics are wrong here.
- Everything else (vitals, xbox, clips, blog, insights, golf, model) inherits the
  system.

## Design direction (distilled from the reference-site research)

Principles extracted (not copied) from sandracreates.com, heatbureau.com,
clicktokeep.com, kargo-studio.com, wairk.fr, adamjakubowski, shaders.com,
is.graphics, contentcore.xyz — full teardown lives in the session research:

- **Charcoal, never pure black** (`--bg #0a0a0b`): OLED smear, dark-room glare.
- **Instrument-panel typography**: display grotesk (Space Grotesk) for identity,
  mono for labels/units/timestamps, `tabular-nums` on every live number.
- **Number-as-hero rhythm**: big numeral, whisper-quiet mono label beneath.
- **Corner HUD**: live clock + data-freshness dot (shipped; wairk.fr pattern).
- **Editorial hairlines over box-grids** where lists occur.
- **Pointer-tracked spotlight + gradient-ring cards** (shipped on home) as the
  signature interactive surface.
- **Motion = physics, fast**: press <100ms on `pointerdown`; decoration ≤400ms
  with `cubic-bezier(.22,1,.36,1)`; session-gated entrances; everything honors
  `prefers-reduced-motion`.
- **Rejected on purpose** (would hurt a 10x/day phone dashboard): fullscreen
  WebGL/shader backgrounds, scroll-jacking/smooth-scroll libs, autoplay video
  heroes, long ungated intros. A single small CSS-gradient "aurora" accent is the
  approved budget for generative flair (shipped on home hero).

## The design system (already built — use it, don't fork it)

`static/tokens.css` is the single source of truth: one neutral ramp, one accent
(`--accent #4ade80`), one info blue, 7 type sizes, 4 radii, 4px spacing scale,
3 elevations, named motion durations/easings, `hb-` primitives (`hb-card`,
`hb-label`, `hb-stat`, `hb-press`, `hb-dot`, `hb-tap`), global focus-visible and
reduced-motion handling. `DESIGN_MIGRATION.md` holds the mechanical mapping table
for un-migrated pages. Never introduce a new hex/px that tokens already decide.

## Hard engineering rules (each one has already caused a real production bug)

1. **Never let an animation own visibility.** Content visible by default;
   animation decorates. Backgrounded tabs freeze rAF/timelines — `fill:
   'backwards'`, anime `complete` callbacks, and IntersectionObserver reveals
   have each stranded content invisible. Three separate incidents. Cancel + clear
   on settle; timeout backstops; skip animation when `document.visibilityState
   !== 'visible'`.
2. **Preserve the work-hours privacy layer** (weekday 08:30–17:00 ET hides
   gaming/watching/sleep from public view; `in_work_hours`/`reveal` in
   `services/xbox.py`). Hide, never fabricate.
3. **Escape everything interpolated into `innerHTML`** (stored-XSS happened;
   `esc()` helpers exist) — or use `textContent`.
4. **Never remove an element `id` or function name** without diffing usages.
5. **Cache-bust static assets** (`?v=N`) on every change — Cloudflare serves
   stale JS otherwise (bit us twice).
6. **Optimistic UI must revert on failure and reconcile with reality** (see
   `roomOrGroupMaster`); the UI never lies about a light/state.
7. **Poll only while visible** (`document.visibilityState` gates every interval).
8. **Wide Oura ranges chunk ≤7 days** (client handles it; don't bypass).
9. **Verify with real output, never claims**: jinja compile, id/function diff vs
   HEAD, boot + curl every page, and a real-browser pass at 375×812 — including
   a hidden-tab check for anything animated.

## Working style

- Phases: Design System → Core Components → Page implementations → Motion &
  polish → Media accents. The first phase is DONE; don't restart it.
- Be direct and high-signal. State the chosen direction, then ship concrete code.
- Flag any trade-off touching performance, accessibility, or live-data integrity.
- Sub-agents: report real command output; the reviewer re-verifies independently.
