#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

export AWS_PROFILE="${AWS_PROFILE:-sai}"
compose=(./scripts/bootstrap-local.sh -p chronosops-demo -f docker-compose.yml -f docker-compose.demo.yml)
echo "Starting the persistent local demo database (existing chronosops volumes are untouched)."
"${compose[@]}" up -d db

echo "Building the current browser application."
npm ci --prefix ui --ignore-scripts
npm run build --prefix ui

export FALKORDB_HOST=127.0.0.1 FALKORDB_PORT=6380 REAL_RECOVERY_ENABLED=false
export CHRONOSOPS_SOURCE_LOCK_PATH="$repo_root/.secrets/demo-locks/company.lock"
uv run --frozen --no-env-file python -m seed.load

if ! uv run --frozen --no-env-file python -c '
from falkordb import FalkorDB
from seed.load import get_config
c=get_config()
g=FalkorDB(host=c["FALKORDB_HOST"],port=int(c["FALKORDB_PORT"]),
    username=c["FALKORDB_ADMIN_USERNAME"],password=c["FALKORDB_ADMIN_PASSWORD"]).select_graph("company_brain")
try:
    rows=g.ro_query("MATCH (m:Meta {id: '\''company_brain_meta'\''}) RETURN m.ready,m.schema_ready LIMIT 1",timeout=3000).result_set
except Exception:
    rows=[]
raise SystemExit(0 if rows == [[True,True]] else 1)
'; then
    echo "Publishing the company corpus through the official SDK and Bedrock."
    uv run --frozen --no-env-file python -m seed.ingest
fi

echo "Building the application container."
podman build --security-opt label=disable \
    --volume "$HOME/.cache/uv:/root/.cache/uv:rw" \
    -t localhost/chronosops-test_tests:latest .
"${compose[@]}" up -d app proxy

echo "Checking application readiness."
curl --fail --silent --show-error --retry 30 --retry-connrefused --retry-all-errors \
    --retry-delay 1 --max-time 5 http://127.0.0.1:8080/readyz
printf '\nDemo: http://localhost:8080\n'
echo "Sign in with the preconfigured acme or globex identity; credentials stay in .secrets/proxy-basic-auth.env."
echo "Mode: graph-backed simulation. Real AWS recovery is disabled; live Slack and monitoring require their configured prerequisites."
