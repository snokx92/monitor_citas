#!/bin/bash
set -e

echo "[start.sh] Installing Playwright browsers (Chromium) + system deps…"
# Intento con --with-deps (nuevas versiones)
if python -m playwright install --with-deps chromium; then
  echo "[start.sh] Playwright Chromium installed with system deps (new flag)."
else
  echo "[start.sh] '--with-deps' not supported here; trying legacy sequence…"
  python -m playwright install-deps chromium || true
  python -m playwright install chromium
fi

echo "[start.sh] Launching bot…"
python monitor_citas_multiconsulados.py
