#!/usr/bin/env bash
# build.sh - Render build script for BI Chatbot
# Using pymssql (no ODBC driver required)

set -euo pipefail

echo "[build] Installing Python dependencies"
pip install -r requirements.txt

echo "[build] Build complete - ready for deployment"
