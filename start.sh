#!/bin/bash
set -e

echo "[start] Upgrading pip/setuptools/wheel…"
python -m pip install --upgrade pip setuptools wheel

echo "[start] Installing requirements…"
pip install -r requirements.txt

echo "[start] Installing Playwright Chromium + system deps…"
# Intento 1: todo junto
python -m playwright install --with-deps chromium || (
  echo "[start] Fallback: install-deps + chromium separately…"
  python -m playwright install-deps chromium || true
  python -m playwright install chromium
)

echo "[start] Launching bot…"
python monitor_citas_multiconsulados.py
