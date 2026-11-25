FROM python:3.11-slim

WORKDIR /app

# Copy news site files
COPY news/ ./news/

# Copy server and fetcher scripts
COPY news_server.py ./news_server.py
COPY news_fetch.py ./news_fetch.py

# Set environment variables
ENV NEWS_DIR=/app/news
ENV NEWS_PORT=8091
ENV NEWS_HOST=0.0.0.0
ENV FETCH_TTL_SECONDS=3600

# Expose port
EXPOSE 8091

# Run the server
CMD ["python3", "news_server.py"]
