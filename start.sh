#!/bin/bash

# Instalar navegadores y deps; si falla algo raro, seguimos
python -m playwright install --with-deps chromium || true

# Ejecutar el bot
python monitor_citas_multiconsulados.py
