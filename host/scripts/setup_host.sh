#!/usr/bin/env bash
# Install pystata-shim into $PYSTATA_SHIM_HOME on this macOS host.
#
# Steps:
#   1. create $PYSTATA_SHIM_HOME and runs/
#   2. copy host/app.py and host/worker.py into that directory
#   3. create a Python virtualenv and install fastapi/uvicorn/pydantic
#   4. generate a 64-hex-character shared secret (32 bytes) at
#      $PYSTATA_SHIM_HOME/token (mode 0600),
#      unless one is already present
#   5. install host/start.sh into $PYSTATA_SHIM_HOME
#   6. render host/launchd/com.pystata.shim.plist.template into
#      ~/Library/LaunchAgents/com.pystata.shim.plist and load it
#
# This script only touches files under your home directory, and under
# ~/Library/LaunchAgents. It does not mutate any Stata install.
#
# Idempotent: re-running is safe. Re-running replaces app.py/worker.py
# with the current repo copies, re-installs the venv dependencies, and
# reloads the launchd agent.
#
# It does NOT rotate an existing token. Delete $PYSTATA_SHIM_HOME/token
# first to force a new one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${HOST_DIR}/.." && pwd)"

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
: "${PYSTATA_EDITION:=mp}"
: "${PYSTATA_UTILITIES:=/Applications/StataNow/utilities}"
: "${PYSTATA_SHIM_POSTFLIGHT_TIMEOUT_SEC:=60}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
PLIST_LABEL="com.pystata.shim"
PLIST_TEMPLATE="${HOST_DIR}/launchd/${PLIST_LABEL}.plist.template"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
START_SCRIPT="${PYSTATA_SHIM_HOME}/start.sh"

if [ -z "${PYTHON_BIN}" ]; then
    echo "pystata-shim setup: python3 not found in PATH" >&2
    exit 1
fi

if ! command -v launchctl >/dev/null 2>&1; then
    echo "pystata-shim setup: launchctl not found — this script supports macOS only." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "pystata-shim setup: warning: curl not found." >&2
    echo "pystata-shim setup: setup can continue, but healthcheck.sh and the printed quick-check commands require curl." >&2
fi

if [ ! -d "${PYSTATA_UTILITIES}" ]; then
    echo "pystata-shim setup: PYSTATA_UTILITIES (${PYSTATA_UTILITIES}) does not exist." >&2
    echo "pystata-shim setup: set PYSTATA_UTILITIES to your Stata install's utilities dir" >&2
    echo "pystata-shim setup: (contains the \`pystata\` package)." >&2
    exit 1
fi

echo "=== pystata-shim setup ==="
echo "Shim home:       ${PYSTATA_SHIM_HOME}"
echo "Bind:            ${PYSTATA_SHIM_HOST}:${PYSTATA_SHIM_PORT}"
echo "Edition:         ${PYSTATA_EDITION}"
echo "Utilities:       ${PYSTATA_UTILITIES}"
echo "Python:          ${PYTHON_BIN}"
echo ""

echo "[1/6] Creating host directories..."
mkdir -p \
    "${PYSTATA_SHIM_HOME}" \
    "${PYSTATA_SHIM_HOME}/runs" \
    "${HOME}/Library/LaunchAgents"

echo "[2/6] Installing host shim files..."
install -m 0644 "${HOST_DIR}/app.py" "${PYSTATA_SHIM_HOME}/app.py"
install -m 0644 "${HOST_DIR}/worker.py" "${PYSTATA_SHIM_HOME}/worker.py"
install -m 0755 "${HOST_DIR}/start.sh" "${START_SCRIPT}"

echo "[3/6] Ensuring Python virtualenv..."
VENV_DIR="${PYSTATA_SHIM_HOME}/venv"
VENV_PY="${VENV_DIR}/bin/python"
if [ ! -x "${VENV_PY}" ]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
# Skip the pip install if the three required packages are already
# importable in the venv — keeps re-runs fast. Versions are pinned
# below tested upper bounds so a breaking upstream release cannot
# silently brick installs.
if "${VENV_PY}" -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
    echo "      (deps already present, skipping pip install)"
else
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet \
        "fastapi>=0.110,<0.130" \
        "uvicorn[standard]>=0.27,<0.40" \
        "pydantic>=2,<3"
fi

echo "[4/6] Ensuring shared-secret token..."
TOKEN_FILE="${PYSTATA_SHIM_HOME}/token"
generate_token() {
    umask 077
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32 > "${TOKEN_FILE}"
    else
        "${PYTHON_BIN}" -c "import secrets; print(secrets.token_hex(32))" > "${TOKEN_FILE}"
    fi
    chmod 600 "${TOKEN_FILE}"
}
if [ -s "${TOKEN_FILE}" ]; then
    # Validate the existing token meets current policy. The shim refuses
    # to start with a short or world-readable token anyway, but we'd
    # rather fail at setup time than at first request.
    EXISTING_LEN=$(wc -c < "${TOKEN_FILE}" | tr -d ' ')
    EXISTING_MODE=$(stat -f "%Lp" "${TOKEN_FILE}" 2>/dev/null || stat -c "%a" "${TOKEN_FILE}")
    if [ "${EXISTING_LEN}" -lt 32 ]; then
        echo "      existing ${TOKEN_FILE} is only ${EXISTING_LEN} bytes;" >&2
        echo "      shim requires >= 32. Refusing to keep it." >&2
        echo "      Delete the file manually if you want a fresh one." >&2
        exit 1
    fi
    if [ "${EXISTING_MODE}" != "600" ]; then
        echo "      existing ${TOKEN_FILE} has mode ${EXISTING_MODE} (expected 600);" >&2
        echo "      tightening to 600 in place." >&2
        chmod 600 "${TOKEN_FILE}"
    fi
    echo "      (token file already present, validated, keeping it)"
else
    generate_token
    echo "      wrote ${TOKEN_FILE} (mode 600)"
fi

echo "[5/6] Rendering launchd plist..."
# Render through Python to avoid sed-delimiter clashes if any placeholder
# value ever contains the delimiter character. Values arrive as argv so
# they are not interpolated into Python source.
"${PYTHON_BIN}" - \
    "${PLIST_TEMPLATE}" \
    "${PLIST_DEST}" \
    "${START_SCRIPT}" \
    "${PYSTATA_SHIM_HOME}" \
    "${PYSTATA_SHIM_HOST}" \
    "${PYSTATA_SHIM_PORT}" \
    "${PYSTATA_EDITION}" \
    "${PYSTATA_UTILITIES}" <<'PY_RENDER'
import sys
(
    template_path,
    dest_path,
    start_script,
    shim_home,
    shim_host,
    shim_port,
    edition,
    utilities,
) = sys.argv[1:10]
subs = {
    "__START_SCRIPT__": start_script,
    "__PYSTATA_SHIM_HOME__": shim_home,
    "__PYSTATA_SHIM_HOST__": shim_host,
    "__PYSTATA_SHIM_PORT__": shim_port,
    "__PYSTATA_EDITION__": edition,
    "__PYSTATA_UTILITIES__": utilities,
}
with open(template_path, "r", encoding="utf-8") as f:
    rendered = f.read()
for key, val in subs.items():
    rendered = rendered.replace(key, val)
with open(dest_path, "w", encoding="utf-8") as f:
    f.write(rendered)
PY_RENDER

echo "[6/6] (Re)loading launch agent..."
launchctl bootout "gui/$(id -u)" "${PLIST_DEST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}"

echo "      waiting for authenticated /health..."
HEALTH_URL="http://${PYSTATA_SHIM_HOST}:${PYSTATA_SHIM_PORT}/health"
if ! "${PYTHON_BIN}" - "${HEALTH_URL}" "${TOKEN_FILE}" "${PYSTATA_SHIM_POSTFLIGHT_TIMEOUT_SEC}" <<'PY_POSTFLIGHT'
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
token_file = pathlib.Path(sys.argv[2])
timeout_sec = float(sys.argv[3])
token = token_file.read_text(encoding="utf-8").strip()
deadline = time.monotonic() + timeout_sec
last_error = "no response"

while time.monotonic() < deadline:
    remaining = max(1.0, deadline - time.monotonic())
    request = urllib.request.Request(url, headers={"X-Shim-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=min(5.0, remaining)) as response:
            raw = response.read().decode("utf-8", "replace")
        data = json.loads(raw)
        has_auth_detail = any(
            key in data
            for key in (
                "edition",
                "stata_version_number",
                "error_code",
                "init_error",
                "current_job_id",
            )
        )
        if data.get("ok") is True and has_auth_detail:
            summary = {
                key: data[key]
                for key in (
                    "ok",
                    "pool_state",
                    "queue_depth",
                    "edition",
                    "stata_version_number",
                )
                if key in data
            }
            print("      authenticated health ok: " + json.dumps(summary, sort_keys=True))
            sys.exit(0)
        if has_auth_detail:
            last_error = "health not ok: " + raw[:500]
        else:
            last_error = "service responded without authenticated detail: " + raw[:500]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        last_error = f"HTTP {exc.code}: {detail[:500]}"
    except Exception as exc:  # noqa: BLE001
        last_error = f"{type(exc).__name__}: {exc}"
    time.sleep(1.0)

print(
    f"postflight health failed after {timeout_sec:g}s: {last_error}",
    file=sys.stderr,
)
sys.exit(1)
PY_POSTFLIGHT
then
    echo "pystata-shim setup: launch agent did not pass authenticated health." >&2
    STDERR_LOG="${PYSTATA_SHIM_HOME}/shim.stderr.log"
    if [ -s "${STDERR_LOG}" ]; then
        echo "" >&2
        echo "Last lines from ${STDERR_LOG}:" >&2
        tail -80 "${STDERR_LOG}" >&2 || true
    fi
    if command -v lsof >/dev/null 2>&1; then
        echo "" >&2
        echo "Listeners on TCP port ${PYSTATA_SHIM_PORT}:" >&2
        lsof -nP -iTCP:"${PYSTATA_SHIM_PORT}" -sTCP:LISTEN >&2 || \
            echo "  (no listener reported by lsof)" >&2
    fi
    echo "" >&2
    echo "Fix the error above and re-run host/scripts/setup_host.sh." >&2
    exit 1
fi

echo ""
echo "Setup complete."
echo "Token file:    ${TOKEN_FILE}"
echo "LaunchAgent:   ${PLIST_DEST}"
echo "Shim stdout:   ${PYSTATA_SHIM_HOME}/shim.stdout.log"
echo "Shim stderr:   ${PYSTATA_SHIM_HOME}/shim.stderr.log"
echo ""
echo "Quick check:"
echo "  bash ${REPO_DIR}/host/scripts/healthcheck.sh"
echo ""
echo "Authenticated check:"
echo "  bash ${REPO_DIR}/host/scripts/healthcheck.sh --auth"
