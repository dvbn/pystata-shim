#!/usr/bin/env bash
# Bring up a python:3.12-slim container wired to the host pystata-shim.
#
# Assumes you have already run host/scripts/setup_host.sh on this Mac
# and that the shim launch agent is up.
#
# Token-handling note:
# The shared secret is RCE-equivalent for your Mac. We do NOT pass the
# token as `-e KEY=VALUE` because the value would land in `ps aux`
# output where any local process can read it. Instead we export the
# token into THIS shell's environment and pass it to docker by name
# only (`-e KEY`) — docker reads the value from our env and propagates
# it to the container without putting it in argv.

set -euo pipefail

TOKEN_FILE="${PYSTATA_SHIM_TOKEN_FILE:-$HOME/.pystata-shim/token}"
if [ ! -r "${TOKEN_FILE}" ]; then
    echo "Cannot read ${TOKEN_FILE}; run host/scripts/setup_host.sh first." >&2
    exit 1
fi
PYSTATA_SHIM_TOKEN="$(cat "${TOKEN_FILE}")"
export PYSTATA_SHIM_TOKEN

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$(cd "${SCRIPT_DIR}/../../client" && pwd)"
WORK_DIR="$(pwd)"

# `-e PYSTATA_SHIM_TOKEN` (no `=value`) tells docker to read the value
# from our exported env. The literal token never appears in argv.
docker run --rm \
    -e PYSTATA_SHIM_TOKEN \
    -e PYSTATA_SHIM_HOST_WORKSPACE="${WORK_DIR}" \
    -e PYSTATA_SHIM_CONTAINER_WORKSPACE="/workspace" \
    -e PYSTATA_SHIM_BASE_URL="http://host.docker.internal:8787" \
    -v "${WORK_DIR}:/workspace" \
    -v "${CLIENT_DIR}:/opt/pystata-shim-client:ro" \
    --add-host=host.docker.internal:host-gateway \
    python:3.12-slim bash -lc '
        set -euo pipefail
        export PATH=/opt/pystata-shim-client:$PATH
        export PYTHONPATH=/opt/pystata-shim-client

        echo "== health =="
        python3 -c "import pystata_shim as s; print(s.health_full())"

        echo ""
        echo "== canonical regression =="
        python3 -c "
import json
import pystata_shim as s
res = s.do(
    \"sysuse auto, clear\nreg price mpg\",
    capture=[\"e(N)\", \"e(r2)\"],
    timeout_sec=120,
    caller_tag=\"docker-run-example\",
)
print(json.dumps({k: res[k] for k in (\"ok\", \"stata_rc\", \"captured\", \"duration_sec\")}, indent=2))
"
    '
