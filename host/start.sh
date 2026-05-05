#!/usr/bin/env bash
# Launch the pystata-shim FastAPI service.
#
# Expected layout (populated by scripts/setup_host.sh):
#   $PYSTATA_SHIM_HOME/venv/bin/python
#   $PYSTATA_SHIM_HOME/app.py        (copy of this repo's host/app.py)
#   $PYSTATA_SHIM_HOME/worker.py     (copy of this repo's host/worker.py)
#   $PYSTATA_SHIM_HOME/token         (0600 shared-secret file)
#
# Respected environment variables:
#   PYSTATA_SHIM_HOME      (default: $HOME/.pystata-shim)
#   PYSTATA_SHIM_HOST      (default: 127.0.0.1)
#   PYSTATA_SHIM_PORT      (default: 8787)
#   PYSTATA_SHIM_TOKEN     (optional; falls back to $PYSTATA_SHIM_HOME/token)
#   PYSTATA_SHIM_ALLOW_NON_LOOPBACK  (set to 1 to allow non-loopback bind)
#   PYSTATA_EDITION        (default: mp)
#   PYSTATA_UTILITIES      (default: /Applications/StataNow/utilities)
set -euo pipefail

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
: "${PYSTATA_EDITION:=mp}"
: "${PYSTATA_UTILITIES:=/Applications/StataNow/utilities}"

export PYSTATA_SHIM_HOME PYSTATA_SHIM_HOST PYSTATA_SHIM_PORT PYSTATA_EDITION PYSTATA_UTILITIES

case "${PYSTATA_SHIM_HOST}" in
    127.0.0.1|localhost|::1) ;;
    *)
        if [ "${PYSTATA_SHIM_ALLOW_NON_LOOPBACK:-0}" != "1" ]; then
            echo "pystata-shim: refusing to bind ${PYSTATA_SHIM_HOST} (non-loopback)." >&2
            echo "pystata-shim: set PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1 if you really want this." >&2
            echo "pystata-shim: there is no TLS on this transport; do not expose it to other hosts." >&2
            exit 1
        fi
        echo "pystata-shim: WARNING binding non-loopback interface ${PYSTATA_SHIM_HOST}; no TLS." >&2
        ;;
esac

cd "${PYSTATA_SHIM_HOME}"

mkdir -p "${PYSTATA_SHIM_HOME}/runs"

PYTHON_BIN="${PYSTATA_SHIM_HOME}/venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
    echo "pystata-shim: python venv missing at ${PYTHON_BIN}" >&2
    echo "pystata-shim: run host/scripts/setup_host.sh first" >&2
    exit 1
fi

exec "${PYTHON_BIN}" -m uvicorn app:app \
    --host "${PYSTATA_SHIM_HOST}" \
    --port "${PYSTATA_SHIM_PORT}" \
    --log-level info
