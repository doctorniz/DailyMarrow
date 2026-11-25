<div align="center">

# Daily Marrow

**A self-hosted daily news digest, set like a printed newspaper.**

Daily Marrow pulls about thirty RSS, Atom and YouTube feeds, folds them into a single edition,
and serves it as a calm, newspaper-style front page with a built-in reader.
Pure Python standard library on the back end, one HTML file on the front. No accounts, no tracking, no AI.

[![Docker Build Test](https://github.com/doctorniz/DailyMarrow/actions/workflows/docker-build.yml/badge.svg)](https://github.com/doctorniz/DailyMarrow/actions/workflows/docker-build.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-2ea44f)
![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)

<img src="docs/desktop-light.png" alt="Daily Marrow front page in light mode" width="100%">

<sub>Rendered from a sample edition with placeholder headlines and images.</sub>

</div>

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Choosing your feeds](#choosing-your-feeds)
- [HTTP endpoints](#http-endpoints)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Testing](#testing)
- [License](#license)

## Features

- **Newspaper layout.** A lead story with a halftone-screened picture, picture cards for each desk, and multi-column newsprint for the rest.
- **Sectioned edition.** World, Australia, Europe, Middle East, Asia, Tech, Health and Sport out of the box, with a sticky section bar that tracks where you are.
- **Fair mixing.** Stories are deduplicated across the whole edition and interleaved by source, so no single outlet dominates the top of a section.
- **Built-in reader.** Click a story to read the extracted article text in a modal, with ads, navigation and paywall chrome stripped out, and a link back to the original.
- **Fresh on its own.** Fetches on startup when the edition is missing or stale, and a **Refresh** button re-prints it on demand (rate limited, so it is safe to expose).
- **Light and dark themes.** Follows the system setting, with a toggle that is remembered per browser.
- **Responsive.** Three columns on desktop, two on tablets (≤ 900 px) and one on phones (≤ 640 px).
- **Zero dependencies.** Runs on a stock Python 3.11 install or as a small Docker container.

## Screenshots

<table>
  <tr>
    <td width="72%"><img src="docs/desktop-dark.png" alt="Daily Marrow in dark mode"></td>
    <td width="28%"><img src="docs/mobile-light.png" alt="Daily Marrow on a phone"></td>
  </tr>
  <tr>
    <td align="center"><sub>Dark theme</sub></td>
    <td align="center"><sub>Phone layout</sub></td>
  </tr>
</table>

## Quick start

### With Docker (recommended)

```bash
git clone https://github.com/doctorniz/DailyMarrow.git
cd DailyMarrow
docker compose up -d
```

Open <http://localhost:8091>. The first edition is fetched in the background on startup and usually lands within a minute; reload the page once it is ready.

The compose file keeps `edition.json` in a named volume (`news-data`), so rebuilding the image picks up front-end changes without losing the current edition.

To build and run the image by hand:

```bash
docker build -t daily-marrow .
docker run -d --name daily-marrow -p 8091:8091 -v news-data:/app/news daily-marrow
```

### Without Docker

Requires Python 3.11 or newer. There is nothing to install.

```bash
git clone https://github.com/doctorniz/DailyMarrow.git
cd DailyMarrow
python3 news_server.py
```

The server listens on <http://localhost:8091> and writes editions to `~/.hermes/news/` unless you set `NEWS_DIR`. You can also print an edition without starting the server:

```bash
NEWS_DIR=./news python3 news_fetch.py
```

> `edition.json` is generated, not committed. Until the first fetch finishes, the page shows *"No edition yet"*.

## Configuration

Everything is configured through environment variables.

| Variable | Default | Description |
|---|---|---|
| `NEWS_PORT` | `8091` | Port the server listens on. |
| `NEWS_HOST` | `0.0.0.0` | Address the server binds to. |
| `NEWS_DIR` | `/app/news` in Docker, `~/.hermes/news` otherwise | Where `edition.json` is written and served from. |
| `NEWS_STATIC_DIR` | The folder holding `news_server.py` if it contains `index.html`, else `NEWS_DIR` | Where `index.html`, `fonts/` and the favicons are served from. |
| `NEWS_FETCHER` | `news_fetch.py` next to `news_server.py` | Fetcher script that refreshes run. |
| `FETCH_TTL_SECONDS` | `3600` | On startup, fetch a new edition if the current one is older than this. |
| `NEWS_REFRESH_COOLDOWN` | `900` | Minimum seconds between refreshes, however they are triggered. |

## Choosing your feeds

Feeds live in the `SECTIONS` list at the top of [`news_fetch.py`](news_fetch.py). Each section has an id, a title, a kind, a list of feeds and a cap on how many stories it shows. Each feed has a label, a URL and a maximum age in hours.

```python
SECTIONS = [
    ("world", "World", "news", [
        # (source label,   feed URL,                                       max age in hours)
        ("BBC World",      "https://feeds.bbci.co.uk/news/world/rss.xml", 36),
        ("Guardian World", "https://www.theguardian.com/world/rss",       36),
    ], 11),   # show at most 11 stories
]
```

The first section with stories becomes the lead. Any RSS 2.0, RSS 1.0 (RDF) or Atom feed works, including YouTube channel feeds (`https://www.youtube.com/feeds/videos.xml?channel_id=…`). Sections of kind `curated` are collected into `edition.json` but not shown on the page.

## HTTP endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | The front page. |
| `GET` | `/edition.json` | The current edition: sections, stories and any feed errors. |
| `GET` | `/read?url=<link>` | Extracted article text for the reader modal. Only links that appear in the current edition are accepted. |
| `POST` | `/refresh` | Starts a new fetch in the background. Returns `{"started": …, "running": …}`, plus `retry_in` while the cooldown is active. |
| `GET` | `/refresh-status` | Whether a fetch is running, when the last one started and its exit code. |
| `GET` | `/fonts/*`, `/favicon.*` | Static assets. |

Nothing else is served, so the server source and `.git` folder are never exposed.

## How it works

```
 ┌──────────────┐   RSS / Atom / YouTube   ┌───────────────┐   edition.json   ┌──────────────┐
 │   33 feeds   │ ───────────────────────▶ │ news_fetch.py │ ───────────────▶ │ news_server  │ ──▶ browser
 └──────────────┘    fetched in parallel   └───────────────┘   (NEWS_DIR)     │   :8091      │
                                                    ▲                         └──────┬───────┘
                                                    └──── startup if stale / POST /refresh
```

1. **Fetch.** `news_fetch.py` pulls every feed in parallel, drops stories older than each feed's max age, deduplicates by link, interleaves sources and writes `edition.json` atomically.
2. **Serve.** `news_server.py` serves the page and the edition, and runs the fetcher in a subprocess on startup or on refresh.
3. **Read.** The `/read` extractor is built to be public-facing: it only fetches links already in the edition, over HTTPS, to hosts that resolve to public IPs, re-checks every redirect, caps response size, rate limits each client and rebuilds the article from plain text and images so no third-party script reaches the browser.

## Project layout

```
.
├── index.html            # the whole front end (vanilla JS and CSS)
├── news_fetch.py         # feed fetcher, writes edition.json
├── news_server.py        # HTTP server, reader extractor, refresh endpoints
├── fonts/                # self-hosted web fonts
├── Dockerfile
├── docker-compose.yml
├── test-docker.sh        # local smoke test of the Docker image
├── docs/                 # README screenshots
└── .github/workflows/    # CI: Docker build and smoke test
```

## Testing

Every push and pull request to `main` builds the Docker image and smoke-tests the running container in GitHub Actions. To run the same checks locally:

```bash
./test-docker.sh
```

## License

MIT. Use it however you want.
