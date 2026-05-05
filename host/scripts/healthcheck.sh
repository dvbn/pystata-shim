#!/usr/bin/env bash
# Poll pystata-shim's /health endpoint. Prints the JSON and exits 0 on
# an "ok" response, non-zero otherwise.
#
# By default calls the unauthenticated, minimal /health payload. Pass
# --auth to read the token from $PYSTATA_SHIM_HOME/token (or
# $PYSTATA_SHIM_TOKEN) and include diagnostic detail.
#
# Usage:
#   healthcheck.sh
#   healthcheck.sh --auth
#   PYSTATA_SHIM_PORT=9787 healthcheck.sh
set -euo pipefail

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
URL="http://${PYSTATA_SHIM_HOST}:${PYSTATA_SHIM_PORT}/health"

HEADER_FILE=""
cleanup() {
    if [ -n "${HEADER_FILE}" ]; then
        rm -f "${HEADER_FILE}"
    fi
}
trap cleanup EXIT

if [ "${1:-}" = "--auth" ]; then
    TOKEN="${PYSTATA_SHIM_TOKEN:-}"
    if [ -z "${TOKEN}" ] && [ -f "${PYSTATA_SHIM_HOME}/token" ]; then
        TOKEN="$(cat "${PYSTATA_SHIM_HOME}/token")"
    fi
    if [ -z "${TOKEN}" ]; then
        echo "healthcheck: no token available (set PYSTATA_SHIM_TOKEN or populate ${PYSTATA_SHIM_HOME}/token)" >&2
        exit 2
    fi
    TMP_ROOT="${TMPDIR:-/tmp}"
    TMP_ROOT="${TMP_ROOT%/}"
    HEADER_FILE="$(mktemp "${TMP_ROOT}/pystata-shim-header.XXXXXX")"
    printf 'X-Shim-Token: %s\n' "${TOKEN}" > "${HEADER_FILE}"
fi

if [ -n "${HEADER_FILE}" ]; then
    RAW="$(curl -sS --max-time 20 -H @"${HEADER_FILE}" "${URL}")" || {
        echo "healthcheck: curl failed — is the shim running at ${URL}?" >&2
        exit 1
    }
else
    RAW="$(curl -sS --max-time 20 "${URL}")" || {
        echo "healthcheck: curl failed — is the shim running at ${URL}?" >&2
        exit 1
    }
fi
if [ -z "${RAW}" ]; then
    echo "healthcheck: empty response from ${URL}" >&2
    exit 1
fi
echo "${RAW}"

python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print('healthcheck: response is not JSON', file=sys.stderr)
    sys.exit(1)
if not data.get('ok'):
    sys.exit(1)
" "${RAW}"
