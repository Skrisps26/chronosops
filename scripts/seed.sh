#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

echo "=== Step 1: Generating synthetic topology and evidence corpus ==="
uv run python seed/generate.py

echo "=== Step 2: Loading operational topology into ops_topology graph ==="
uv run python seed/load.py --wipe

echo "=== Step 3: Ingesting company documents and structured tables into company_brain ==="
if [[ -n "${AWS_PROFILE:-}" || -n "${AWS_ACCESS_KEY_ID:-}" ]]; then
    uv run python seed/ingest.py --wipe
else
    echo "NOTICE: AWS credentials not set in environment. Running ingestion with sai profile if configured."
    AWS_PROFILE=sai AWS_REGION=us-east-1 uv run python seed/ingest.py --wipe || {
        echo "WARNING: Ingestion skipped or failed due to missing AWS credentials. Re-run with valid AWS_PROFILE to seed Company Brain."
    }
fi

echo "=== Seeding complete. All graphs initialized! ==="
