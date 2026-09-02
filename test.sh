#!/usr/bin/env bash
# Runs the tests. They need no network: every upstream call is intercepted
# before it leaves the process, so $FX_UPSTREAM_BASE may point at a closed port.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import pytest, respx" 2>/dev/null; then
  echo "setting up .venv ..." >&2
  "${PYTHON:-python3}" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec .venv/bin/python -m pytest -q "$@"
