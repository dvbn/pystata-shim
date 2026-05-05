#!/usr/bin/env bash
# Mirror readable parts of your Stata install into a user-owned directory.
#
# This is OPTIONAL. Nothing in pystata-shim's execution path depends on
# these mirrors. They exist so a containerized agent can READ Stata's
# built-in `.ado`, PyStata bridge code, and PDF manuals (inspection only,
# never execution).
#
# Why mirror rather than bind-mount `/Applications/StataNow/...` directly?
# Docker Desktop on macOS frequently refuses direct bind mounts from
# `/Applications`. Copying the readable trees into a user-owned directory
# sidesteps that.
#
# You do NOT need to run this script to use pystata-shim. Skip it if you
# only want the container to run `.do` files through the shim.
#
# Respected environment variables:
#   PYSTATA_SHIM_MIRROR_ROOT  (default: /Users/Shared/stata-mirrors)
#   PYSTATA_SOURCE_ROOT       (default: /Applications/StataNow)
#
# Safety:
#   - The mirror root is created if absent, with mode 0755.
#   - rsync is called with -a --delete to keep the mirror in sync.
#   - No file from this repo is copied into the mirror. The mirror only
#     contains your Stata install's readable files.
#
# Licensing:
#   Stata's built-in `.ado` files, help files, and manuals are owned by
#   StataCorp and covered by your local license. Mirroring them into a
#   directory on your own machine, for your own agent to read, is
#   equivalent to letting a local process read them directly. Do not
#   publish or redistribute the mirror.

set -euo pipefail

: "${PYSTATA_SHIM_MIRROR_ROOT:=/Users/Shared/stata-mirrors}"
: "${PYSTATA_SOURCE_ROOT:=/Applications/StataNow}"

echo "=== Stata mirror sync ==="
echo "Source root:    ${PYSTATA_SOURCE_ROOT}"
echo "Mirror root:    ${PYSTATA_SHIM_MIRROR_ROOT}"
echo ""

if [ ! -d "${PYSTATA_SOURCE_ROOT}" ]; then
    echo "sync_stata_mirrors: PYSTATA_SOURCE_ROOT not found at ${PYSTATA_SOURCE_ROOT}" >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "sync_stata_mirrors: rsync not found in PATH" >&2
    exit 1
fi

mkdir -p \
    "${PYSTATA_SHIM_MIRROR_ROOT}" \
    "${PYSTATA_SHIM_MIRROR_ROOT}/ado" \
    "${PYSTATA_SHIM_MIRROR_ROOT}/utilities-pystata" \
    "${PYSTATA_SHIM_MIRROR_ROOT}/docs"

mirror_one() {
    local src="$1"
    local dst="$2"
    local label="$3"
    if [ -d "${src}" ]; then
        echo "  mirror ${label}: ${src} -> ${dst}"
        rsync -a --delete "${src}/" "${dst}/"
    else
        echo "  skip   ${label}: ${src} not present"
    fi
}

mirror_one "${PYSTATA_SOURCE_ROOT}/ado" \
           "${PYSTATA_SHIM_MIRROR_ROOT}/ado" \
           "ado"
mirror_one "${PYSTATA_SOURCE_ROOT}/utilities/pystata" \
           "${PYSTATA_SHIM_MIRROR_ROOT}/utilities-pystata" \
           "utilities/pystata"
mirror_one "${PYSTATA_SOURCE_ROOT}/docs" \
           "${PYSTATA_SHIM_MIRROR_ROOT}/docs" \
           "docs"

echo ""
echo "Mirror ready. Bind the subdirectories you need into the container,"
echo "read-only. Suggested container paths:"
echo "  ${PYSTATA_SHIM_MIRROR_ROOT}/ado                -> /stata/ado"
echo "  ${PYSTATA_SHIM_MIRROR_ROOT}/utilities-pystata  -> /stata/utilities/pystata"
echo "  ${PYSTATA_SHIM_MIRROR_ROOT}/docs               -> /stata/docs"
