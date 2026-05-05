#!/usr/bin/env bash
# Bring up a python:3.12-slim container wired to the host pystata-shim.
#
# Assumes you have already run host/scripts/setup_host.sh on this Mac.
#
# This example uses the host shim's container-session lifecycle:
# - we start the shim before the container with shimctl.sh start
# - we stop the shim after the container exits (only if no other labeled
#   containers are running) with shimctl.sh stop-if-unused
# The container carries the `com.pystata.shim.enabled=true` label so
# stop-if-unused can find it.
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
SHIMCTL="$(cd "${SCRIPT_DIR}/../../host/scripts" && pwd)/shimctl.sh"
WORK_DIR="$(pwd)"

# Start the host shim if it is not already running.
bash "${SHIMCTL}" start

# Stop-if-unused on exit (success or failure). Will leave the shim
# running if any other com.pystata.shim.enabled=true container exists.
trap 'bash "${SHIMCTL}" stop-if-unused || true' EXIT

# `-e PYSTATA_SHIM_TOKEN` (no `=value`) tells docker to read the value
# from our exported env. The literal token never appears in argv.
docker run --rm \
    --label com.pystata.shim.enabled=true \
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
