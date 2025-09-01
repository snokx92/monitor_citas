#!/bin/bash
set -e

# Instalar Chromium y dependencias de sistema para Playwright
python -m playwright install --with-deps chromium || (
  # fallback si el flag --with-deps falla en el entorno
  python -m playwright install-deps chromium || true
  python -m playwright install chromium
)

# Ejecuta el bot
python monitor_citas_multiconsulados.py
