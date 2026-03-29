#!/usr/bin/env bash
set -euo pipefail

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt >/dev/null
python3 -m app.cli
