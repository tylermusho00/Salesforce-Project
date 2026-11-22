#!/bin/bash
# Lead Router: double-click on Mac (or run `bash start_mac.command` on Mac/Linux).
# First run installs everything into a private .venv folder (about 1-2 minutes). Needs Python 3.11+.
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Python 3.11+ is required: https://www.python.org/downloads/"; read -p "Press Enter to close"; exit 1; fi
if [ ! -f .venv/installed.ok ]; then
  echo "First run: installing (about 1-2 minutes)..."
  "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip \
    && .venv/bin/python -m pip install --quiet -r requirements.txt && touch .venv/installed.ok \
    || { echo "Install failed."; read -p "Press Enter to close"; exit 1; }
fi
.venv/bin/python run_dashboard.py
