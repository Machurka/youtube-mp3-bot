#!/usr/bin/env bash
# Launch script for Linux / macOS.
set -e
cd "$(dirname "$0")"

if [ -d "venv" ]; then
    source venv/bin/activate
fi

python3 main.py
