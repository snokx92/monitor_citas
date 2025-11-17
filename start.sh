#!/usr/bin/env bash
set -euo pipefail

echo "[start] Upgrading pip…"
python -m playwright install --with-deps

echo "[start] Installing Playwright + Chromium…"
python -m playwright install --with-deps chromium

echo "[start] Launching bot…"
python monitor_citas_multiconsulados.py
