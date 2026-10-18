# Official Node 22 runtime pinned by manifest digest for the installed MCP server.
FROM docker.io/library/node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS mcp-deps
WORKDIR /mcp
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM mcp-deps AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY ui/ ./
RUN npm run build

# Official Python 3.12 runtime pinned by manifest digest.
FROM docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS python-base

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libstdc++6 libgomp1 git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.11.31

FROM python-base AS python-deps
# Compile hnswlib against the container ABI, never reuse its host-built wheel.
ENV UV_CACHE_DIR=/root/.cache/uv/chronosops-debian13-py312-portable \
    HNSWLIB_NO_NATIVE=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN UV_CACHE_DIR=/root/.cache/uv uv sync --frozen --no-install-project --no-install-package hnswlib \
    && uv sync --frozen --no-install-project

FROM python-base
COPY --from=mcp-deps /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY --from=python-deps /app/.venv /app/.venv
COPY --from=mcp-deps /mcp /app/mcp
COPY . .
COPY --from=ui-build /ui/dist /app/ui/dist

RUN groupadd --gid 10001 chronosops \
    && useradd --uid 10001 --gid chronosops --create-home --shell /usr/sbin/nologin chronosops \
    && chown -R chronosops:chronosops /app

ENV PATH="/app/.venv/bin:/app/mcp/node_modules/.bin:${PATH}" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER chronosops

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
