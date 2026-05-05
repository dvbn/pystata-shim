#!/usr/bin/env bash
# shimctl - lifecycle script for the pystata-shim host runtime.
#
# Use this from a container wrapper so the shim runs only while a
# container session needs it, instead of as a login-time daemon.
#
# Subcommands:
#   start             - start the shim in the background if not already
#                       running. Waits up to 15s for /status to respond.
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
#   start.sh            shim launcher (installed by setup_host.sh)
#   shim.pid            this script's PID file for the shim
#   shim.stdout.log     stdout from the shim
#   shim.stderr.log     stderr from the shim

set -euo pipefail

: "${PYSTATA_SHIM_HOME:=$HOME/.pystata-shim}"
: "${PYSTATA_SHIM_HOST:=127.0.0.1}"
: "${PYSTATA_SHIM_PORT:=8787}"
: "${SHIM_LABEL:=com.pystata.shim.enabled=true}"

START_SCRIPT="${PYSTATA_SHIM_HOME}/start.sh"
PID_FILE="${PYSTATA_SHIM_HOME}/shim.pid"
STDOUT_LOG="${PYSTATA_SHIM_HOME}/shim.stdout.log"
STDERR_LOG="${PYSTATA_SHIM_HOME}/shim.stderr.log"

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
    {
        printf 'GET /status HTTP/1.0\r\nHost: %s:%s\r\nConnection: close\r\n\r\n' \
            "${PYSTATA_SHIM_HOST}" "${PYSTATA_SHIM_PORT}" >&3
        cat <&3
    } 3<>"/dev/tcp/${PYSTATA_SHIM_HOST}/${PYSTATA_SHIM_PORT}" 2>/dev/null \
        | grep -q '"pool_state"'
    local rc=$?
    exec 3<&- 2>/dev/null || true
    return "$rc"
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
    # we use it as a lightweight cross-shell mutex. This serializes two
    # concurrent `start` invocations; the loser exits cleanly.
    local lock_dir="${PYSTATA_SHIM_HOME}/.shimctl-start.lock"
    if ! mkdir "${lock_dir}" 2>/dev/null; then
        echo "shimctl: another start is in progress; not starting another." >&2
        return 0
    fi
    # Always remove the lock dir on exit. Use a function so the trap
    # works under both `return` and `exit`.
    _release_start_lock() {
        rmdir "${lock_dir}" 2>/dev/null || true
    }
    trap '_release_start_lock' RETURN

    if is_listening; then
        echo "shimctl: shim already responding on $(shim_url); not starting another."
        return 0
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

    # nohup + setsid so the shim outlives the calling shell and a
    # detached terminal cannot SIGHUP it.
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "${START_SCRIPT}" \
            >"${STDOUT_LOG}" \
            2>"${STDERR_LOG}" &
    else
        # macOS does not ship setsid in older /usr/bin layouts, but the
        # default shell does background detachment well enough alone.
        nohup "${START_SCRIPT}" \
            >"${STDOUT_LOG}" \
            2>"${STDERR_LOG}" &
    fi
    local pid=$!
    disown 2>/dev/null || true
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
    # The handoff doc explicitly calls this out — we should never stop
    # the shim out from under a still-running container just because we
    # cannot ask Docker about it.
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
        status)             shift; cmd_status "$@" ;;
        stop-if-unused)    shift; cmd_stop_if_unused "$@" ;;
        ""|-h|--help|help) usage ;;
        *)
            echo "shimctl: unknown subcommand: $1" >&2
            usage
            ;;
    esac
}

main "$@"
