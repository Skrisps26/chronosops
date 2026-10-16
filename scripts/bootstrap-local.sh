#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate local database credentials" >&2
    exit 69
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
fi

secret_file=.secrets/db.env
if [[ ! -f $secret_file ]]; then
    umask 077
    mkdir -p .secrets
    {
        printf '%s\n' '# Generated locally by scripts/bootstrap-local.sh. Do not commit.'
        for name in \
            FALKORDB_ADMIN_PASSWORD \
            FALKORDB_READER_ACME_PASSWORD \
            FALKORDB_READER_GLOBEX_PASSWORD \
            FALKORDB_WRITER_ACME_PASSWORD \
            FALKORDB_WRITER_GLOBEX_PASSWORD; do
            printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
        done
    } >"$secret_file"
    chmod 600 "$secret_file"
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    compose=(podman compose)
elif command -v podman-compose >/dev/null 2>&1; then
    compose=(podman-compose)
elif command -v uvx >/dev/null 2>&1; then
    # A project-local ephemeral provider: no global alias or package install.
    compose=(uvx --from podman-compose==1.5.0 podman-compose)
else
    echo "Install Docker Compose, podman-compose, or uv to run this stack." >&2
    exit 69
fi

if (( $# == 0 )); then
    set -- up -d db
fi
exec "${compose[@]}" "$@"
