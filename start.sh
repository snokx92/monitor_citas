#!/bin/bash
set -e

# Instalar Chromium + dependencias del sistema para Linux
# (usa --with-deps; si tu versión de Playwright no lo soporta, ver fallback abajo)
python -m playwright install --with-deps chromium || \
(
  # Fallback para versiones que no soportan --with-deps
  python -m playwright install-deps chromium
  python -m playwright install chromium
)

# Arrancar el bot
python monitor_citas_huecos.py
