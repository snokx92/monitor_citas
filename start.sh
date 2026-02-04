#!/bin/bash
set -x

echo "[start.sh] Instalando Playwright (si hace falta)…"
python3 -m playwright install --with-deps chromium || echo "[start.sh] playwright install falló, pero continúo."

echo "[start.sh] Lanzando bot…"
exec python3 monitor_citas_multiconsulados.py
