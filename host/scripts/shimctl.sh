#!/usr/bin/env bash
# shimctl - lifecycle script for the pystata-shim host runtime.
#
# Use this from a container wrapper so the shim runs only while a
# container session needs it, instead of as a login-time daemon.
#
# Subcommands:
#   start             - start the shim in the background if not already
#                       running. Waits up to 15s for /status to respond.
#                       Refuses to start when a foreign listener
#                       (a stale runtime, an old launchd shim) holds
#                       the port — see troubleshooting.md.
#   stop              - stop the shim if running. TERM, then KILL on
#                       grace timeout. Idempotent.
#   status            - report `running` or `stopped`, plus pid/url when
#                       known. Script-friendly: keyword is the first
#                       whitespace-separated token of stdout.
#   stop-if-unused    - stop the shim only when no Docker containers
#                       labeled $SHIM_LABEL are running. Conservative
#                       when Docker is unreachable: leaves shim alone.
#
# Environment:
#   PYSTATA_SHIM_HOME   default $HOME/.pystata-shim
#   PYSTATA_SHIM_HOST   default 127.0.0.1
#   PYSTATA_SHIM_PORT   default 8787
#   SHIM_LABEL          default com.pystata.shim.enabled=true. Must match
#                       the label used at `docker create`.
#
# Files (under $PYSTATA_SHIM_HOME):
#   start.sh                shim launcher (installed by setup_host.sh)
#   shim.pid                this script's PID file for the shim
#   shim.stdout.log         stdout from the shim (appended)
#   shim.stderr.log         stderr from the shim (appended)
#   .shimctl-start.lock/    transient lock dir held during a `start` call

set -euo pipefail

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
: "${SHIM_LABEL:=com.pystata.shim.enabled=true}"

START_SCRIPT="${PYSTATA_SHIM_HOME}/start.sh"
PID_FILE="${PYSTATA_SHIM_HOME}/shim.pid"
STDOUT_LOG="${PYSTATA_SHIM_HOME}/shim.stdout.log"
STDERR_LOG="${PYSTATA_SHIM_HOME}/shim.stderr.log"

# Script-level (not a `local`) so the EXIT trap can see this even
# after cmd_start has returned. Empty string is the "no lock held"
# sentinel; the cleanup function is idempotent against it.
SHIMCTL_LOCK_DIR=""

usage() {
    cat >&2 <<USAGE
usage: $(basename "$0") {start|stop|status|stop-if-unused}

  start            start the shim in the background
  stop             stop the shim if running
  status           print running/stopped state to stdout
  stop-if-unused   stop the shim only if no Docker containers labeled
                   ${SHIM_LABEL} are running
USAGE
    exit 2
}

pid_alive() {
    local pid="${1:-}"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
    [ -s "${PID_FILE}" ] || return 1
    local pid
    pid="$(head -1 "${PID_FILE}" | tr -d '[:space:]')"
    [ -n "$pid" ] || return 1
    printf '%s' "$pid"
}

shim_url() {
    printf 'http://%s:%s/status' "${PYSTATA_SHIM_HOST}" "${PYSTATA_SHIM_PORT}"
}

# is_listening: 0 if /status responds with our shim's payload shape,
# nonzero otherwise. We deliberately verify a shim-specific field in
# the JSON body so that a foreign listener on the same port (a stale
# dev server, an unrelated service, an abandoned shim from a different
# PYSTATA_SHIM_HOME) does not produce a false positive.
is_listening() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 2 "$(shim_url)" 2>/dev/null \
            | grep -q '"pool_state"'
        return $?
    fi
    # No curl — speak HTTP/1.0 by hand over bash's /dev/tcp. We grep
    # for the shim's pool_state field so that an unrelated TCP
    # listener on the configured port does not look like the shim.
    # The 3<>... redirection on the brace block is local to the block,
    # so FD 3 is closed automatically when the block exits — no
    # explicit `exec 3<&-` is needed (and adding one would terminate
    # the function under `set -e` because FD 3 is no longer open in
    # the function's own scope).
    #
    # We do NOT `cat <&3` because a foreign server that accepts the
    # connection but never sends bytes would hang `cat` forever. Use
    # a `read -t 2`-driven loop bounded to a small response window:
    # we only need to see the JSON body's `pool_state` token.
    #
    # The outer subshell + 2>/dev/null is required on bash 3.2 (macOS
    # default) so that the redirection-setup error message ("bash:
    # /dev/tcp/127.0.0.1/8787: Connection refused") emitted at FD-open
    # time does not leak to stderr. Without this wrapping, a healthy
    # `shimctl start` polling loop would print up to ~30 spurious
    # "Connection refused" lines while waiting for the shim to bind.
    (
        {
            printf 'GET /status HTTP/1.0\r\nHost: %s:%s\r\nConnection: close\r\n\r\n' \
                "${PYSTATA_SHIM_HOST}" "${PYSTATA_SHIM_PORT}" >&3
            local _line=""
            local _budget=64
            while [ "${_budget}" -gt 0 ]; do
                _line=""
                if IFS= read -r -t 2 -u 3 _line; then
                    printf '%s\n' "${_line}"
                else
                    # `read` returns nonzero if EOF arrives before a
                    # newline, even when it did receive bytes. HTTP
                    # JSON bodies commonly have no trailing newline;
                    # emit that final partial line before stopping.
                    if [ -n "${_line}" ]; then
                        printf '%s\n' "${_line}"
                    fi
                    break
                fi
                _budget=$(( _budget - 1 ))
            done
        } 3<>"/dev/tcp/${PYSTATA_SHIM_HOST}/${PYSTATA_SHIM_PORT}"
    ) 2>/dev/null | grep -q '"pool_state"'
}

# port_taken: 0 if anything at all is listening on
# PYSTATA_SHIM_HOST:PORT (without verifying it is the shim), nonzero
# otherwise. Used by `cmd_start` to distinguish "shim is up" (good —
# is_listening returns 0) from "foreign service holds the port"
# (bad — is_listening returns nonzero but port_taken returns 0).
# The subshell opens and closes FD 3 entirely within itself.
port_taken() {
    (exec 3<>"/dev/tcp/${PYSTATA_SHIM_HOST}/${PYSTATA_SHIM_PORT}") 2>/dev/null
}

# spawn_shim: print the PID of a freshly-detached shim process.
#
# Real-host failure mode this guards against: macOS in some shells
# does not provide `setsid`, and `nohup &` alone left the shim
# vulnerable to SIGHUP when the calling shell's session exited.
# When `python3` is available (the public repo requires it for the
# venv anyway), we spawn through `subprocess.Popen` with
# `start_new_session=True`, which calls `setsid()` directly via the
# kernel API. Falls back to `nohup setsid` and then to `nohup` alone.
spawn_shim() {
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${START_SCRIPT}" "${STDOUT_LOG}" "${STDERR_LOG}" <<'PY'
import os
import subprocess
import sys

start_script, stdout_log, stderr_log = sys.argv[1:4]
with open(os.devnull, "rb") as devnull, \
     open(stdout_log, "ab") as so, \
     open(stderr_log, "ab") as se:
    p = subprocess.Popen(
        [start_script],
        stdin=devnull,
        stdout=so,
        stderr=se,
        start_new_session=True,
        close_fds=True,
    )
print(p.pid)
PY
        return $?
    fi
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "${START_SCRIPT}" \
            >>"${STDOUT_LOG}" \
            2>>"${STDERR_LOG}" </dev/null &
        local pid=$!
        disown 2>/dev/null || true
        printf '%s' "${pid}"
        return 0
    fi
    # Last-resort fallback for stripped environments without python3
    # AND without setsid. Documented in setup-macos.md as unreliable
    # on macOS — install python3 to use the preferred path.
    nohup "${START_SCRIPT}" \
        >>"${STDOUT_LOG}" \
        2>>"${STDERR_LOG}" </dev/null &
    local pid=$!
    disown 2>/dev/null || true
    printf '%s' "${pid}"
}

# _release_start_lock: idempotent cleanup hook for the start lock.
# Runs from both the RETURN trap (normal cmd_start flow) and the EXIT
# trap (set -e exits, signal-driven exits). Clears the script-level
# variable after rmdir so a subsequent call is a no-op.
_release_start_lock() {
    if [ -n "${SHIMCTL_LOCK_DIR}" ] && [ -d "${SHIMCTL_LOCK_DIR}" ]; then
        rmdir "${SHIMCTL_LOCK_DIR}" 2>/dev/null || true
    fi
    SHIMCTL_LOCK_DIR=""
}

cmd_status() {
    local pid=""
    if pid="$(read_pid 2>/dev/null)"; then
        if pid_alive "$pid"; then
            echo "running pid=${pid} url=$(shim_url)"
            return 0
        fi
    fi
    if is_listening; then
        echo "running url=$(shim_url)"
        return 0
    fi
    echo "stopped"
}

cmd_start() {
    if [ ! -x "${START_SCRIPT}" ]; then
        echo "shimctl: ${START_SCRIPT} missing or not executable;" >&2
        echo "shimctl: run host/scripts/setup_host.sh first." >&2
        exit 1
    fi

    mkdir -p "${PYSTATA_SHIM_HOME}"

    # mkdir is atomic on POSIX filesystems (APFS, HFS+, ext4, ...) so
    # we use it as a lightweight cross-shell mutex. This serializes
    # two concurrent `start` invocations; the loser exits cleanly.
    local candidate_lock="${PYSTATA_SHIM_HOME}/.shimctl-start.lock"
    if ! mkdir "${candidate_lock}" 2>/dev/null; then
        # Lock already exists. We deliberately do not own it, so leave
        # SHIMCTL_LOCK_DIR empty — the cleanup hook must not remove a
        # lock another process is holding.
        echo "shimctl: another start is in progress (or a prior start crashed)." >&2
        echo "shimctl:   if no other shimctl is running, remove the stale lock with:" >&2
        echo "shimctl:     rmdir ${candidate_lock}" >&2
        return 0
    fi
    SHIMCTL_LOCK_DIR="${candidate_lock}"
    # Cleanup runs on RETURN (normal flow) AND EXIT (set -e propagating
    # an `exit 1` from inside the function, signal-driven exits).
    # Both targets share one handler.
    trap '_release_start_lock' RETURN EXIT

    if is_listening; then
        echo "shimctl: shim already responding on $(shim_url); not starting another."
        return 0
    fi

    # If something is already on the port but does NOT speak our
    # /status payload, abort with an actionable error. Forking another
    # shim would just lose the bind race and produce a confusing
    # failure later. This is the real-host scenario where a stale
    # ~/.pystata-shim install was still serving its old API on 8787
    # and `/status` returned `{"detail":"Not Found"}`.
    if port_taken; then
        echo "shimctl: something is already listening on $(shim_url)" >&2
        echo "shimctl:   but it does not return our /status pool_state payload." >&2
        echo "shimctl:   This usually means a stale ~/.pystata-shim or an old" >&2
        echo "shimctl:   launchd agent is still bound to the port." >&2
        echo "shimctl:" >&2
        echo "shimctl: investigate with:" >&2
        echo "shimctl:   lsof -nP -iTCP:${PYSTATA_SHIM_PORT} -sTCP:LISTEN" >&2
        echo "shimctl:" >&2
        echo "shimctl: clean up old launch agents (both legacy labels) with:" >&2
        echo "shimctl:   launchctl bootout \"gui/\$(id -u)\" \\" >&2
        echo "shimctl:     \"\$HOME/Library/LaunchAgents/com.openstata.pystata-shim.plist\" 2>/dev/null || true" >&2
        echo "shimctl:   launchctl bootout \"gui/\$(id -u)\" \\" >&2
        echo "shimctl:     \"\$HOME/Library/LaunchAgents/com.pystata.shim.plist\" 2>/dev/null || true" >&2
        echo "shimctl:" >&2
        echo "shimctl: if a stale ~/.pystata-shim process is the holder," >&2
        echo "shimctl:   kill that process or run \`shimctl stop\` from its install" >&2
        echo "shimctl:   tree before retrying \`shimctl start\` here." >&2
        exit 1
    fi

    if [ -e "${PID_FILE}" ]; then
        local prev
        prev="$(read_pid 2>/dev/null || true)"
        if [ -n "${prev}" ] && pid_alive "${prev}"; then
            echo "shimctl: pid ${prev} alive but $(shim_url) not responding;" >&2
            echo "shimctl: refusing to start a second shim. Run 'stop' first." >&2
            exit 1
        fi
        rm -f "${PID_FILE}"
    fi

    local pid
    pid="$(spawn_shim)"
    if [ -z "${pid}" ]; then
        echo "shimctl: spawn_shim produced no PID; check ${STDERR_LOG}" >&2
        exit 1
    fi
    echo "${pid}" > "${PID_FILE}"

    # SECONDS is a bash special; the shebang above is bash so this is
    # safe. If sourced from dash, this loop short-circuits — do not
    # do that.
    local deadline=$(( SECONDS + 15 ))
    while [ $SECONDS -lt $deadline ]; do
        if is_listening; then
            echo "shimctl: started pid=${pid} url=$(shim_url)"
            return 0
        fi
        sleep 0.5
    done

    echo "shimctl: timed out waiting for $(shim_url);" >&2
    echo "shimctl: see ${STDERR_LOG}" >&2
    exit 1
}

cmd_stop() {
    local pid=""
    pid="$(read_pid 2>/dev/null || true)"
    if [ -n "${pid}" ] && pid_alive "${pid}"; then
        kill -TERM "${pid}" 2>/dev/null || true
        local deadline=$(( SECONDS + 8 ))
        while [ $SECONDS -lt $deadline ] && pid_alive "${pid}"; do
            sleep 0.25
        done
        if pid_alive "${pid}"; then
            kill -KILL "${pid}" 2>/dev/null || true
        fi
    fi
    rm -f "${PID_FILE}"
    echo "stopped"
}

cmd_stop_if_unused() {
    # Conservative when Docker is unavailable: leave the shim alone.
    # Never stop the shim out from under a still-running container just
    # because we cannot ask Docker about it.
    if ! command -v docker >/dev/null 2>&1; then
        echo "shimctl: docker not on PATH; leaving shim untouched (conservative default)." >&2
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "shimctl: docker daemon not reachable; leaving shim untouched." >&2
        return 0
    fi
    # Symmetric conservative policy: if `docker ps` itself fails for
    # any reason (transient daemon glitch, label-filter API surprise),
    # we leave the shim alone instead of risking a false stop. Only
    # an explicit zero-row success advances to cmd_stop.
    local running
    if ! running="$(docker ps --filter "label=${SHIM_LABEL}" --format '{{.ID}}' 2>/dev/null)"; then
        echo "shimctl: docker ps failed; leaving shim untouched (conservative)." >&2
        return 0
    fi
    if [ -n "${running}" ]; then
        echo "shimctl: shim still in use by other containers; leaving it running."
        return 0
    fi
    cmd_stop
}

main() {
    case "${1:-}" in
        start)             shift; cmd_start "$@" ;;
        stop)              shift; cmd_stop  "$@" ;;
        status)            shift; cmd_status "$@" ;;
        stop-if-unused)    shift; cmd_stop_if_unused "$@" ;;
        ""|-h|--help|help) usage ;;
        *)
            echo "shimctl: unknown subcommand: $1" >&2
            usage
            ;;
    esac
}

main "$@"
