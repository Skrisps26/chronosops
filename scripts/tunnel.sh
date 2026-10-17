#!/usr/bin/env bash
set -euo pipefail

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared is not installed on this machine."
    echo "To install cloudflared on Linux:"
    echo "  curl -L --output cloudflared.rpm https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm"
    echo "  sudo rpm -i cloudflared.rpm"
    echo ""
    echo "Alternatively, download binary from: https://github.com/cloudflare/cloudflared/releases"
    echo "Once installed, start the quick tunnel with: cloudflared tunnel --url http://localhost:8080"
    exit 1
fi

echo "=== Starting Cloudflare Quick Tunnel for ChronosOps on port 8080 ==="
echo "Note: Quick Tunnels do not support SSE. ChronosOps uses WebSockets."
exec cloudflared tunnel --url http://127.0.0.1:8080
