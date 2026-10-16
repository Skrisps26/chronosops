# Official Node 22 runtime pinned by manifest digest for the installed MCP server.
FROM docker.io/library/node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS mcp-deps
WORKDIR /mcp
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

# Official Python 3.12 runtime pinned by manifest digest.
FROM docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libstdc++6 git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.11.31

COPY --from=mcp-deps /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY --from=mcp-deps /mcp /app/mcp
COPY . .

RUN groupadd --gid 10001 chronosops \
    && useradd --uid 10001 --gid chronosops --create-home --shell /usr/sbin/nologin chronosops \
    && chown -R chronosops:chronosops /app

ENV PATH="/app/.venv/bin:/app/mcp/node_modules/.bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER chronosops

# Module 0 deliberately has no web-server substitute: the image executes the
# real dependency probe until a later module introduces the application API.
CMD ["python", "-m", "scripts.probe"]
