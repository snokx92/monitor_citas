#!/bin/bash
set -e

python -m pip install --upgrade pip
python -m playwright install --with-deps chromium
python monitor_citas_multiconsulados.py
