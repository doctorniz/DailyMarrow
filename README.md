# Daily Marrow

A self-hosted daily news digest served as a newspaper-style web app.

## What it is

- Fetches RSS/Atom feeds from public sources (ABC, Guardian AU, BBC, Reuters, etc.)
- Renders a newspaper layout with sections, picture cards, and a reader modal
- Dark/light theme toggle (localStorage)
- Mobile-responsive (3-column grid → 2 → 1)
- Pulls article content via a `/read` extractor endpoint (strips paywalls, ads, nav)

## Tech

- **Frontend**: Single `index.html` (vanilla JS, no framework)
- **Backend**: Node.js server (`news_server.py` is the Python fetcher, served by a simple HTTP server)
- **Data**: `edition.json` generated from RSS feeds
- **Hosting**: Self-hosted on a local server (Proxmox LXC, nginx reverse proxy)

## Layout

- **Desktop**: 3-column picture cards, 3-column newsprint text
- **Tablet** (≤900px): 2-column cards, 2-column newsprint
- **Mobile** (≤640px): 1-column, horizontal card layout, clipped blurbs

## Reader modal

Click any card → opens a modal with the extracted article text + a "Read the original" link at the bottom. Closes via X button, backdrop click, or Escape key.

## Setup

1. Clone the repo
2. Run `news_fetch.py` to generate `edition.json` (or set up a cron job)
3. Serve `index.html` + `edition.json` + `news_server.py` via any static HTTP server
4. Point nginx (or your reverse proxy) at the directory

## License

MIT — use it however you want.
