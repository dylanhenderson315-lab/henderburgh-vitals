# DESIGN.md — HENDERBURGH

## Colors
- Background: #0f1115 (deep calm slate)
- Card: #161a20
- Accent (relaxing): #4ade80 (soft green) / #60a5fa (soft blue)
- Text: #e2e8f0 primary, #94a3b8 secondary
- Status "Relaxing": #22c55e

## Typography
- Headings: Inter / system-ui, 600-700 weight
- Body: Inter, 400-500
- Monospace for data: JetBrains Mono or system

## Spacing & Radius
- Generous padding (p-8, gap-6)
- Rounded-3xl on cards for softness
- Subtle shadows: shadow-xl / shadow-2xl with calm tones

## Motion Principles
- Springy, calm easing (never frantic)
- Data updates feel alive but not distracting
- Loading states are expressive but gentle (dot-matrix)

## Implementation Notes (adapted to current stack)
- All micro-interactions & transitions use Anime.js (CDN).
- No React/Framer/shadcn possible in this Jinja2 + Tailwind-CDN + vanilla JS setup. Effects are ported using Tailwind classes + Anime.js + custom CSS (glows, aurora, magnetic via JS, neon loaders via animated divs + anime).
- Bento grids, glowing cards, magnetic hover, smooth number anims, loading states implemented in templates.
- Consistent across home.html, home-assistant.html (lighting), dashboard.html (vitals), golf, clips, blog, xbox.

## Quick Wins Status
- [x] Dotmatrix-style loaders (NeonDrift equivalent via CSS+Anime)
- [x] Glowing cards + Anime hover/tap (Aceternity-inspired)
- [x] Anime.js number animations on vitals/gamerscore/etc.
- [x] Magnetic interactions on key cards/nav (Componentry-inspired, via anime on mousemove)
- [x] Subtle “relaxing” aurora/wavy background (Aceternity-inspired, CSS+Anime, status-driven)
- [x] Bento-style layouts on home overview
- [x] Full site-wide design system application (colors, spacing, cards, motion)
- All existing features preserved.
