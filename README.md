# Oura Vitals Dashboard

A beautiful, self-hosted personal dashboard for your Oura Ring data — inspired by shrey.com/vitals.

Built with **Python + FastAPI + HTMX + Tailwind CSS + Chart.js**. Runs instantly on localhost with zero build step.

## Features

- All major daily metrics from Oura API v2: Readiness, Sleep, Activity, HRV, Resting HR, Respiratory Rate, SpO₂, Skin Temperature, Stress, and more
- Clean cards with current values + trend arrows (vs yesterday)
- Interactive charts: HRV and Sleep trends over the last 7–14 days
- Dark mode by default + beautiful light mode toggle (persisted)
- Fast, responsive, premium feel with excellent typography and micro-interactions
- HTMX-powered refresh (no full page reloads for data updates)
- Zero-config local development

## Quick Start

### 1. Get your Oura token

1. Go to [https://cloud.ouraring.com/personal-access-tokens](https://cloud.ouraring.com/personal-access-tokens)
2. Create a new Personal Access Token (or use an existing one)
3. Copy it (you will only see it once)

> **Note**: Personal Access Tokens were deprecated for new creations in late 2025. If the page no longer offers them, create a free OAuth2 application at https://cloud.ouraring.com/oauth/applications and use the generated token for personal use, or reach out to Oura support.

### 2. Run the dashboard

```bash
# Using uv (recommended)
uv sync
uv run uvicorn main:app --reload

# Or with pip
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn main:app --reload
```

Open **http://localhost:8000**

### 3. Configure token

```bash
cp .env.example .env
# Edit .env and paste your token
```

Or set the environment variable directly:

```bash
export OURA_TOKEN=your_token_here
uvicorn main:app --reload
```

## Environment Variables

| Variable     | Description                        | Default |
|--------------|------------------------------------|---------|
| `OURA_TOKEN` | Your Oura Personal Access Token    | — (required) |
| `OURA_DAYS`  | Number of days to load for charts  | 14      |

## Tech Stack

- **Backend**: FastAPI + httpx (async Oura client with simple caching)
- **Frontend**: Tailwind (CDN) + HTMX + Chart.js (CDN) — no Node, no build
- **Templating**: Jinja2
- **Dev**: uvicorn --reload

## Project Structure

```
oura-dashboard/
├── main.py              # FastAPI app + Oura client + routes
├── templates/
│   └── dashboard.html   # The entire beautiful UI
├── pyproject.toml
├── .env.example
└── README.md
```

## Screenshots / Vibe

Premium dark UI with emerald accents (Oura brand), large readable numbers, tasteful charts, subtle glass cards, and buttery smooth HTMX updates.

## Troubleshooting

- **401 Unauthorized**: Your token is invalid or expired. Generate a new one.
- **No data showing**: Make sure your ring has synced recently in the Oura app. Some metrics (SpO₂, detailed HRV) only appear after certain syncs.
- **Rate limits**: The dashboard is efficient (a handful of calls on load + refresh). You shouldn't hit limits for personal use.

---

## Deploying as a Public Website (henderburgh.com example)

This dashboard is designed to be deployed publicly as a single-user site (your data only).

### Recommended Stack (Easy + Free Tier)

- **Railway** (best developer experience for Python + custom domains)
- **Cloudflare** for DNS + HTTPS (you already own henderburgh.com here)

### 1. Prepare for Production

Create a `.env.production` (never commit real secrets):

```env
PUBLIC_MODE=true
DISPLAY_NAME=Henderburgh
SITE_NAME=Henderburgh
SITE_URL=https://henderburgh.com
OURA_DAYS=14
CACHE_TTL_SECONDS=600          # 10 minutes — protects your Oura quota
```

### 2. Deploy on Railway (Recommended)

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub.
3. Railway auto-detects the `Dockerfile`.
4. In the project **Variables** tab, add:
   - `OURA_TOKEN` = your real token (mark as secret)
   - `PUBLIC_MODE` = `true`
   - `DISPLAY_NAME` = `Henderburgh`
   - `SITE_NAME` = `Henderburgh`
   - `SITE_URL` = `https://henderburgh.com`
5. Railway gives you a temporary `.railway.app` URL. Test it.

### 3. Connect Your Custom Domain (henderburgh.com)

In Railway:
- Go to your service → **Settings** → **Domains**
- Add `henderburgh.com` (and `www.henderburgh.com` if desired)

In Cloudflare (DNS):
- Add a **CNAME** record:
  - Name: `@` (or `www`)
  - Target: the Railway-provided hostname (e.g. `your-project.up.railway.app`)
  - Proxy status: **DNS only** (orange cloud off) *during setup*, then turn on after SSL issues resolve.

Railway will provision a certificate automatically.

### 4. Alternative Platforms

- **Render.com** — Also excellent, free tier available
- **Fly.io** — Great if you want global edge + cheap
- **Coolify** or self-hosted Docker

All of them support setting environment variables and custom domains the same way.

### Important Production Notes

- `PUBLIC_MODE=true` completely removes the setup screen and disables the manual refresh button (to protect your Oura API quota).
- Data is cached for ~10 minutes by default.
- Never put your Oura token in the code or Git history.

## License

MIT — personal use encouraged. Fork it, make it yours.

---

Made with ❤️ for Oura owners who love data.
