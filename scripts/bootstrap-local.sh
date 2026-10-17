#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate local credentials" >&2
    exit 69
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
fi

umask 077
mkdir -p .secrets
chmod 700 .secrets

secret_file=.secrets/db.env
if [[ ! -f $secret_file ]]; then
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
fi
chmod 600 "$secret_file"

alert_token_file=.secrets/alert-token
if [[ -e $alert_token_file ]]; then
    if [[ ! -f $alert_token_file || ! -s $alert_token_file ]]; then
        echo "alert token file must be a nonempty regular file" >&2
        exit 64
    fi
else
    alert_token_tmp=$(mktemp "${alert_token_file}.XXXXXX")
    openssl rand -hex 32 >"$alert_token_tmp"
    chmod 600 "$alert_token_tmp"
    mv "$alert_token_tmp" "$alert_token_file"
fi
chmod 600 "$alert_token_file"

proxy_auth_file=.secrets/proxy.htpasswd
proxy_credentials_file=.secrets/proxy-basic-auth.env
proxy_token_file=.secrets/proxy-token

if [[ -e $proxy_auth_file || -e $proxy_credentials_file || -e $proxy_token_file ]]; then
    if [[ ! -f $proxy_auth_file || ! -f $proxy_credentials_file || ! -f $proxy_token_file \
        || ! -s $proxy_auth_file || ! -s $proxy_credentials_file || ! -s $proxy_token_file ]]; then
        echo "proxy credential files are incomplete; remove all three to regenerate them" >&2
        exit 64
    fi
else
    acme_password=$(openssl rand -hex 24)
    globex_password=$(openssl rand -hex 24)
    proxy_token=$(openssl rand -hex 32)
    proxy_auth_tmp=$(mktemp "${proxy_auth_file}.XXXXXX")
    proxy_credentials_tmp=$(mktemp "${proxy_credentials_file}.XXXXXX")
    proxy_token_tmp=$(mktemp "${proxy_token_file}.XXXXXX")

    if command -v htpasswd >/dev/null 2>&1; then
        printf '%s\n' "$acme_password" | htpasswd -B -C 10 -i -c "$proxy_auth_tmp" acme >/dev/null
        printf '%s\n' "$globex_password" | htpasswd -B -C 10 -i "$proxy_auth_tmp" globex >/dev/null
    else
        {
            printf 'acme:%s\n' "$(printf '%s\n' "$acme_password" | openssl passwd -apr1 -stdin)"
            printf 'globex:%s\n' "$(printf '%s\n' "$globex_password" | openssl passwd -apr1 -stdin)"
        } >"$proxy_auth_tmp"
    fi

    {
        printf '%s\n' '# Generated locally by scripts/bootstrap-local.sh. Distribute out of band.'
        printf 'ACME_BASIC_AUTH_USERNAME=acme\n'
        printf 'ACME_BASIC_AUTH_PASSWORD=%s\n' "$acme_password"
        printf 'GLOBEX_BASIC_AUTH_USERNAME=globex\n'
        printf 'GLOBEX_BASIC_AUTH_PASSWORD=%s\n' "$globex_password"
    } >"$proxy_credentials_tmp"
    printf '%s\n' "$proxy_token" >"$proxy_token_tmp"

    chmod 600 "$proxy_auth_tmp" "$proxy_credentials_tmp" "$proxy_token_tmp"
    mv "$proxy_auth_tmp" "$proxy_auth_file"
    mv "$proxy_credentials_tmp" "$proxy_credentials_file"
    mv "$proxy_token_tmp" "$proxy_token_file"
fi
chmod 600 "$proxy_auth_file" "$proxy_credentials_file" "$proxy_token_file"

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
