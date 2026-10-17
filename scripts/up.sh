#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

echo "=== Starting ChronosOps Local Infrastructure ==="
./scripts/bootstrap-local.sh up -d db

echo "=== Checking database readiness ==="
until uv run python -c "from seed.load import get_config; from falkordb import FalkorDB; c=get_config(); FalkorDB(host=c['FALKORDB_HOST'], port=int(c['FALKORDB_PORT']), username=c['FALKORDB_ADMIN_USERNAME'], password=c['FALKORDB_ADMIN_PASSWORD']).select_graph('ops_topology').query('RETURN 1')" >/dev/null 2>&1; do
    echo "Waiting for FalkorDB to accept connections..."
    sleep 2
done

echo "FalkorDB is online and healthy at 127.0.0.1:6379."
echo "To start the API server locally: uv run uvicorn api.main:app --host 127.0.0.1 --port 8000"
echo "To run full seed:               ./scripts/seed.sh"
echo "To run interactive demo:        ./scripts/demo.sh --mode simulation"
