# Daily Marrow

A self-hosted daily news digest served as a newspaper-style web app.

## What it is

- Fetches RSS/Atom feeds from public sources (ABC, Guardian AU, BBC, Reuters, etc.)
- Renders a newspaper layout with sections, picture cards, and a reader modal
- Dark/light theme toggle (localStorage)
- Mobile-responsive (3-column grid → 2 → 1)
- Pulls article content via a `/read` extractor endpoint (strips paywalls, ads, nav)
- Auto-fetches on startup if edition is missing or stale

## Tech

- **Frontend**: Single `index.html` (vanilla JS, no framework)
- **Backend**: Python HTTP server (`news_server.py`) with RSS fetcher (`news_fetch.py`)
- **Data**: `edition.json` generated from RSS feeds (auto-updated)
- **Hosting**: Docker container or any static HTTP server

## Layout

- **Desktop**: 3-column picture cards, 3-column newsprint text
- **Tablet** (≤900px): 2-column cards, 2-column newsprint
- **Mobile** (≤640px): 1-column, horizontal card layout, clipped blurbs

## Reader modal

Click any card → opens a modal with the extracted article text + a "Read the original" link at the bottom. Closes via X button, backdrop click, or Escape key.

## Quick start with Docker

```bash
# Build and run
docker compose up -d

# Or build manually
docker build -t daily-marrow .
docker run -d -p 8091:8091 daily-marrow
```

Access at `http://localhost:8091`

## Docker options

| Env Var | Default | Description |
|---------|---------|-------------|
| `NEWS_PORT` | `8091` | HTTP port the server listens on |
| `NEWS_HOST` | `0.0.0.0` | Bind address |
| `FETCH_TTL_SECONDS` | `3600` | Auto-fetch interval (1 hour) |

## Local setup (no Docker)

1. Clone the repo
2. Install Python 3.11+
3. Run the server: `python3 news_server.py`
4. The server will auto-fetch on startup and serve on port 8091

## Configuration

Edit `news_fetch.py` to add/remove RSS feeds:

```python
SECTIONS = [
    ("world", "World", "news", [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 36),
        # Add your own feeds here
    ], 11),
]
```

## License

MIT — use it however you want.
