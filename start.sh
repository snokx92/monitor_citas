#!/bin/bash
set -e

echo "[start] Instalando Playwright + Chromium y dependencias del sistema…"
# Intento 1: todo en un paso (con --with-deps)
python -m playwright install --with-deps chromium || (
  # Fallback por si el entorno no acepta --with-deps de una
  echo "[start] Fallback: instalando deps del sistema y luego chromium…"
  python -m playwright install-deps chromium || true
  python -m playwright install chromium
)

echo "[start] Lanzando monitor…"
# Ejecuta tu script (asegúrate que el nombre coincide)
python monitor_citas_multiconsulados.py
