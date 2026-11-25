FROM python:3.11-slim

WORKDIR /app

# Front-end (served from /app) and server + fetcher scripts
COPY index.html favicon.ico favicon.svg apple-touch-icon.png ./
COPY fonts/ ./fonts/
COPY news_server.py news_fetch.py ./

# NEWS_DIR only holds the generated edition.json (a volume in docker-compose),
# so rebuilding the image always ships the latest index.html and fonts.
ENV NEWS_DIR=/app/news
ENV NEWS_PORT=8091
ENV NEWS_HOST=0.0.0.0
ENV FETCH_TTL_SECONDS=3600

RUN mkdir -p /app/news

EXPOSE 8091

CMD ["python3", "news_server.py"]
