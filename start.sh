#!/bin/bash
set -x

echo "[start.sh] Instalando Playwright…"
python3 -m playwright install --with-deps chromium || true

echo "[start.sh] Lanzando bot…"
python3 monitor_citas_multiconsulados.py

echo "[start.sh] El bot terminó con código $?"
sleep 300
