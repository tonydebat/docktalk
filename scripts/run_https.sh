#!/usr/bin/env bash
# scripts/run_https.sh
#
# Start DockTalk over HTTPS so it works from a phone on the same LAN.
#
# Microphone access (getUserMedia) and secure WebSocket (wss://) both require
# HTTPS when the page is served from a non-localhost address.  This script
# generates a self-signed certificate the first time it runs, then starts
# uvicorn with TLS enabled.
#
# Usage:
#   bash scripts/run_https.sh
#
# Then open:  https://<your-mac-LAN-ip>:8000
# The browser will warn about the self-signed cert — click "Advanced → Proceed".
# On iOS Safari you may need to install the cert: Settings → General →
# VPN & Device Management → the cert → Trust.
#
# Alternative (no cert install needed): use a cloudflare tunnel
#   brew install cloudflared
#   cloudflared tunnel --url http://localhost:8000
# That gives you a public https:// URL you can open directly on any device.

set -euo pipefail

CERT_DIR="$(dirname "$0")/../.certs"
CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"

mkdir -p "$CERT_DIR"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "Generating self-signed certificate (valid 825 days)…"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out    "$CERT_FILE" \
    -days   825 \
    -subj   "/CN=docktalk-local" \
    -addext "subjectAltName=IP:$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1),IP:127.0.0.1"
  echo "Certificate written to $CERT_DIR/"
fi

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")
echo ""
echo "Starting DockTalk on https://$LAN_IP:8000"
echo "Open that URL on your phone (accept the certificate warning)."
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UVICORN="${SCRIPT_DIR}/../venv/bin/uvicorn"
# Fall back to PATH if no local venv
if [[ ! -x "$UVICORN" ]]; then UVICORN="uvicorn"; fi

exec "$UVICORN" app.server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --reload \
  --ssl-keyfile  "$KEY_FILE" \
  --ssl-certfile "$CERT_FILE"


