#!/usr/bin/env bash
# Starts the service on $PORT (default 8080). The upstream comes from
# $FX_UPSTREAM_BASE (default https://api.frankfurter.dev) and is read per
# request, so the process can be pointed anywhere without a rebuild.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import uvicorn" 2>/dev/null; then
  echo "setting up .venv ..." >&2
  "${PYTHON:-python3}" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
