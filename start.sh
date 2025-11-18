#!/bin/bash
set -e

# Instalar navegadores para Playwright (Chromium headless)
python -m playwright install chromium

# Ejecutar el bot
python monitor_citas_multiconsulados.py
