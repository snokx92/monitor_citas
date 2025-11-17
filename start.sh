#!/usr/bin/env bash

set -euo pipefail

echo "[start] Upgrading pip…"
python -m pip install --upgrade pip

echo "[start] Installing Playwright + system deps…"
python -m playwright install --with-deps

echo "[start] Launching bot…"
python monitor_citas_multiconsulados.py
