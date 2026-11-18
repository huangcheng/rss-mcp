FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 rss \
    && mkdir -p /data && chown rss:rss /data

USER rss
ENV RSS_MCP_DB=/data/rss.db
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["rss-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]