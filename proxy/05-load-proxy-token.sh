#!/bin/sh
set -eu

if [ -n "${CHRONOSOPS_PROXY_TOKEN_FILE:-}" ]; then
    if [ ! -r "$CHRONOSOPS_PROXY_TOKEN_FILE" ]; then
        echo "configured proxy token file is unreadable" >&2
        exit 64
    fi
    CHRONOSOPS_PROXY_TOKEN=$(cat "$CHRONOSOPS_PROXY_TOKEN_FILE")
    export CHRONOSOPS_PROXY_TOKEN
fi

if [ -z "${CHRONOSOPS_PROXY_TOKEN:-}" ]; then
    echo "CHRONOSOPS_PROXY_TOKEN or CHRONOSOPS_PROXY_TOKEN_FILE is required" >&2
    exit 64
fi

case "$CHRONOSOPS_PROXY_TOKEN" in
    *[!A-Za-z0-9._~-]*)
        echo "CHRONOSOPS_PROXY_TOKEN contains unsafe characters" >&2
        exit 64
        ;;
esac

# Bootstrap stores the host file mode 600. Root stages a private runtime copy
# readable by Nginx workers without relaxing the host secret's permissions.
if [ ! -r /etc/nginx/.htpasswd ]; then
    echo "mounted Basic Auth password file is unreadable" >&2
    exit 64
fi
mkdir -p /run/chronosops
cp /etc/nginx/.htpasswd /run/chronosops/htpasswd
chown -R nginx:nginx /run/chronosops
chmod 700 /run/chronosops
chmod 400 /run/chronosops/htpasswd
