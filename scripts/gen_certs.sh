#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="$ROOT/certs"
mkdir -p "$CERT_DIR"

if [[ ! -f "$CERT_DIR/server.key" || ! -f "$CERT_DIR/server.crt" ]]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -subj "/CN=localhost"
  echo "已生成自签证书: $CERT_DIR/server.crt"
fi

if [[ ! -f "$CERT_DIR/.htpasswd" ]]; then
  if command -v htpasswd >/dev/null 2>&1; then
    htpasswd -bc "$CERT_DIR/.htpasswd" admin changeme
  else
    HASH="$(openssl passwd -apr1 changeme)"
    printf 'admin:%s\n' "$HASH" > "$CERT_DIR/.htpasswd"
  fi
  echo "已生成 Basic Auth: 用户名 admin / 密码 changeme（请上线前修改）"
fi
