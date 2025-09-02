#!/usr/bin/env bash
set -euo pipefail

echo "[start.sh] Python: $(python -V)"
echo "[start.sh] PWD: $(pwd)"
echo "[start.sh] Installing Python deps…"
pip install --upgrade pip
pip install -r requirements.txt

# Opcional: fuerza ruta de navegadores dentro del contenedor (evita reinstalar en cada boot)
export PLAYWRIGHT_BROWSERS_PATH=/workspace/.cache/ms-playwright
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" || true

# Instalar navegadores y dependencias del sistema para Chromium
echo "[start.sh] Installing Playwright browsers (chromium) + deps…"
python -m playwright install --with-deps chromium

# Salida de diagnóstico básica (útil en logs de Railway)
echo "[start.sh] Env summary:"
echo "  TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-not-set}"
echo "  PROXY_HOST=${PROXY_HOST:-not-set}"
echo "  PROXY_PORT=${PROXY_PORT:-not-set}"
echo "  PROXY_USER=${PROXY_USER:-not-set}"
echo "  PROXY_PASS=${PROXY_PASS:-not-set}"
echo "  SHOW_PUBLIC_IP=${SHOW_PUBLIC_IP:-not-set}"
echo "  CHECK_MIN_SEC=${CHECK_MIN_SEC:-default-300}"
echo "  CHECK_MAX_SEC=${CHECK_MAX_SEC:-default-420}"
echo "  NAV_TIMEOUT_MS=${NAV_TIMEOUT_MS:-default-20000}"
echo "  SEL_TIMEOUT_MS=${SEL_TIMEOUT_MS:-default-8000}"

echo "[start.sh] Launching bot…"
exec python monitor_citas_multiconsulados.py
