FROM python:3.11-slim

WORKDIR /app

# The server runs as this unprivileged user (see docker-entrypoint.sh)
RUN useradd --system --no-create-home --uid 10001 --user-group marrow

# Front-end (served from /app) and server + fetcher scripts
COPY index.html settings.html favicon.ico favicon.svg apple-touch-icon.png ./
COPY fonts/ ./fonts/
COPY news_server.py news_fetch.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# NEWS_DIR holds the generated edition.json and the settings.json saved from
# /settings (a volume in docker-compose), so rebuilding the image always ships
# the latest index.html and fonts without losing either.
ENV NEWS_DIR=/app/news
ENV NEWS_PORT=5551
ENV NEWS_HOST=0.0.0.0
ENV FETCH_TTL_SECONDS=3600
# Print log lines as they happen, so `docker logs` shows the setup code at once
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app/news && chown marrow:marrow /app/news

EXPOSE 5551

# Healthy once the server answers on its own port (python is the only HTTP
# client in the slim image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/refresh-status' % os.environ.get('NEWS_PORT', '5551'), timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "news_server.py"]
