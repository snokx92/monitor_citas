#!/bin/bash
set -e

# Instalar navegadores y dependencias de sistema para Playwright (Chromium headless)
python -m playwright install --with-deps chromium

# Ejecutar el bot
python monitor_citas_multiconsulados.py
