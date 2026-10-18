#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if [[ $# -lt 2 || $1 != "--mode" ]]; then
    echo "usage: $0 --mode simulation|real [--wait-seconds SECONDS]" >&2
    exit 2
fi

case "$2" in
    simulation|real) ;;
    *)
        echo "invalid --mode: $2 (expected simulation or real)" >&2
        exit 2
        ;;
esac

exec uv run python scripts/demo.py "$@"
