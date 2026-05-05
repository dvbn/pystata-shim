#!/usr/bin/env bash
# Install pystata-shim into $PYSTATA_SHIM_HOME on this macOS host.
#
# Default lifecycle: this script PREPARES the host runtime. It does NOT
# install or start a login-time daemon. After setup, start the shim
# per container session with:
#
#     host/scripts/shimctl.sh start
#
# Stop with:
#
#     host/scripts/shimctl.sh stop
#
# Or wire start/stop into your container wrapper (see examples/justfile).
#
# Steps performed by default:
#   1. create $PYSTATA_SHIM_HOME and runs/
#   2. copy host/app.py and host/worker.py into that directory
#   3. create a Python virtualenv and install fastapi/uvicorn/pydantic
#   4. generate a 64-hex-character shared secret (32 bytes) at
#      $PYSTATA_SHIM_HOME/token (mode 0600), unless one is already present
#   5. install host/start.sh into $PYSTATA_SHIM_HOME
#
# Opt-in extra step (set INSTALL_LAUNCH_AGENT=1):
#   6. render host/launchd/com.pystata.shim.plist.template into
#      ~/Library/LaunchAgents/com.pystata.shim.plist and load it via
#      launchctl. The plist template ships WITHOUT RunAtLoad/KeepAlive,
#      so installing the agent does NOT make the shim a login-time
#      daemon. Use launchctl kickstart to launch on demand.
#
# This script only touches files under your home directory, and (when
# opted in) ~/Library/LaunchAgents. It does not mutate any Stata install.
#
# Idempotent: re-running is safe. Re-running replaces app.py/worker.py
# with the current repo copies and re-installs the venv dependencies.
# It does NOT rotate an existing token. Delete $PYSTATA_SHIM_HOME/token
# first to force a new one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${HOST_DIR}/.." && pwd)"

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
: "${PYSTATA_SHIM_ALLOW_UNSAFE_DO:=0}"
: "${PYSTATA_EDITION:=mp}"
: "${PYSTATA_UTILITIES:=/Applications/StataNow/utilities}"
: "${PYSTATA_SHIM_POSTFLIGHT_TIMEOUT_SEC:=60}"
: "${INSTALL_LAUNCH_AGENT:=0}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
PLIST_LABEL="com.pystata.shim"
PLIST_TEMPLATE="${HOST_DIR}/launchd/${PLIST_LABEL}.plist.template"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
START_SCRIPT="${PYSTATA_SHIM_HOME}/start.sh"
SHIMCTL_SCRIPT="${HOST_DIR}/scripts/shimctl.sh"

if [ -z "${PYTHON_BIN}" ]; then
    echo "pystata-shim setup: python3 not found in PATH" >&2
    exit 1
fi

if [ "${INSTALL_LAUNCH_AGENT}" = "1" ] && ! command -v launchctl >/dev/null 2>&1; then
    echo "pystata-shim setup: launchctl not found — INSTALL_LAUNCH_AGENT=1 supports macOS only." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "pystata-shim setup: warning: curl not found." >&2
    echo "pystata-shim setup: setup can continue, but healthcheck.sh, shimctl.sh, and the printed quick-check commands prefer curl." >&2
fi

case "${PYSTATA_SHIM_ALLOW_UNSAFE_DO}" in
    0|1) ;;
    *)
        echo "pystata-shim setup: PYSTATA_SHIM_ALLOW_UNSAFE_DO must be 0 or 1." >&2
        exit 1
        ;;
esac

case "${INSTALL_LAUNCH_AGENT}" in
    0|1) ;;
    *)
        echo "pystata-shim setup: INSTALL_LAUNCH_AGENT must be 0 or 1." >&2
        exit 1
        ;;
esac

if [ ! -d "${PYSTATA_UTILITIES}" ]; then
    echo "pystata-shim setup: PYSTATA_UTILITIES (${PYSTATA_UTILITIES}) does not exist." >&2
    echo "pystata-shim setup: set PYSTATA_UTILITIES to your Stata install's utilities dir" >&2
    echo "pystata-shim setup: (contains the \`pystata\` package)." >&2
    exit 1
fi

# Warn if a legacy persistent LaunchAgent (RunAtLoad/KeepAlive) is loaded
# from a previous install. We do NOT silently remove it — the user might
# rely on it from another integration — but we tell them how to clean up.
if command -v launchctl >/dev/null 2>&1; then
    if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
        if [ -f "${PLIST_DEST}" ] && grep -q "<key>RunAtLoad</key>" "${PLIST_DEST}" 2>/dev/null \
            && grep -A1 "<key>RunAtLoad</key>" "${PLIST_DEST}" 2>/dev/null | grep -q "<true/>"; then
            echo "pystata-shim setup: NOTE: a previously-installed LaunchAgent at" >&2
            echo "  ${PLIST_DEST}" >&2
            echo "is configured with RunAtLoad. The current lifecycle model uses" >&2
            echo "shimctl.sh instead. To remove the persistent agent:" >&2
            echo "  launchctl bootout \"gui/\$(id -u)\" \"${PLIST_DEST}\"" >&2
            echo "  rm -f \"${PLIST_DEST}\"" >&2
            echo "" >&2
        fi
    fi
fi

if [ "${INSTALL_LAUNCH_AGENT}" = "1" ]; then
    TOTAL_STEPS=6
else
    TOTAL_STEPS=5
fi

echo "=== pystata-shim setup ==="
echo "Shim home:       ${PYSTATA_SHIM_HOME}"
echo "Bind:            ${PYSTATA_SHIM_HOST}:${PYSTATA_SHIM_PORT}"
echo "Do validation:   $([ "${PYSTATA_SHIM_ALLOW_UNSAFE_DO}" = "1" ] && echo "disabled (trusted mode)" || echo "restricted")"
echo "Edition:         ${PYSTATA_EDITION}"
echo "Utilities:       ${PYSTATA_UTILITIES}"
echo "Python:          ${PYTHON_BIN}"
echo "LaunchAgent:     $([ "${INSTALL_LAUNCH_AGENT}" = "1" ] && echo "installed (opt-in)" || echo "not installed (default)")"
echo ""

echo "[1/${TOTAL_STEPS}] Creating host directories..."
mkdir -p \
    "${PYSTATA_SHIM_HOME}" \
    "${PYSTATA_SHIM_HOME}/runs"
if [ "${INSTALL_LAUNCH_AGENT}" = "1" ]; then
    mkdir -p "${HOME}/Library/LaunchAgents"
fi

echo "[2/${TOTAL_STEPS}] Installing host shim files..."
install -m 0644 "${HOST_DIR}/app.py" "${PYSTATA_SHIM_HOME}/app.py"
install -m 0644 "${HOST_DIR}/worker.py" "${PYSTATA_SHIM_HOME}/worker.py"
install -m 0755 "${HOST_DIR}/start.sh" "${START_SCRIPT}"

echo "[3/${TOTAL_STEPS}] Ensuring Python virtualenv..."
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

echo "[4/${TOTAL_STEPS}] Ensuring shared-secret token..."
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

echo "[5/${TOTAL_STEPS}] Confirming start.sh and shimctl.sh are present..."
if [ ! -x "${START_SCRIPT}" ]; then
    echo "pystata-shim setup: ${START_SCRIPT} missing or not executable" >&2
    exit 1
fi
if [ ! -x "${SHIMCTL_SCRIPT}" ]; then
    echo "pystata-shim setup: ${SHIMCTL_SCRIPT} missing or not executable" >&2
    echo "pystata-shim setup: this is the lifecycle script; the repo install is incomplete." >&2
    exit 1
fi

if [ "${INSTALL_LAUNCH_AGENT}" = "1" ]; then
    echo "[6/${TOTAL_STEPS}] Rendering launchd plist (opt-in)..."
    "${PYTHON_BIN}" - \
        "${PLIST_TEMPLATE}" \
        "${PLIST_DEST}" \
        "${START_SCRIPT}" \
        "${PYSTATA_SHIM_HOME}" \
        "${PYSTATA_SHIM_HOST}" \
        "${PYSTATA_SHIM_PORT}" \
        "${PYSTATA_SHIM_ALLOW_UNSAFE_DO}" \
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
    allow_unsafe_do,
    edition,
    utilities,
) = sys.argv[1:11]
subs = {
    "__START_SCRIPT__": start_script,
    "__PYSTATA_SHIM_HOME__": shim_home,
    "__PYSTATA_SHIM_HOST__": shim_host,
    "__PYSTATA_SHIM_PORT__": shim_port,
    "__PYSTATA_SHIM_ALLOW_UNSAFE_DO__": allow_unsafe_do,
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

    echo "      (Re)loading launch agent..."
    launchctl bootout "gui/$(id -u)" "${PLIST_DEST}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
    # Note: with RunAtLoad/KeepAlive removed from the template, the agent
    # is loaded but not running. The user (or shimctl.sh) drives it.
fi

echo ""
echo "Setup complete."
echo "Token file:    ${TOKEN_FILE}"
if [ "${INSTALL_LAUNCH_AGENT}" = "1" ]; then
    echo "LaunchAgent:   ${PLIST_DEST} (loaded, not running)"
fi
echo "Shim stdout:   ${PYSTATA_SHIM_HOME}/shim.stdout.log"
echo "Shim stderr:   ${PYSTATA_SHIM_HOME}/shim.stderr.log"
echo ""
echo "Start the shim for a container session:"
echo "  bash ${SHIMCTL_SCRIPT} start"
echo ""
echo "Stop the shim:"
echo "  bash ${SHIMCTL_SCRIPT} stop"
echo ""
echo "Stop the shim only when no labeled containers are running:"
echo "  bash ${SHIMCTL_SCRIPT} stop-if-unused"
echo ""
echo "Wire a container with the lifecycle label so stop-if-unused can find it:"
echo "  --label com.pystata.shim.enabled=true"
echo "See examples/justfile/Justfile for a full create/start/stop wrapper pattern."
echo ""
echo "Authenticated health check (after starting):"
echo "  bash ${REPO_DIR}/host/scripts/healthcheck.sh --auth"
