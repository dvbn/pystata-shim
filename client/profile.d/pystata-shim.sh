# /etc/profile.d snippet that puts the pystata-shim client on PATH and
# PYTHONPATH for login shells inside containers that mount this file at
# /etc/profile.d/pystata-shim.sh.
#
# Why this exists: a non-login docker exec inherits PATH/PYTHONPATH from
# the docker create env (-e PATH=..., -e PYTHONPATH=...). A login shell
# (`bash -l`, `bash -lc ...`) re-sources /etc/profile, which on Debian
# and Ubuntu rewrites PATH from scratch and drops anything we set via
# `docker create -e PATH=...`. /etc/profile then sources every file
# under /etc/profile.d, which is where this snippet runs and re-adds
# /opt/pystata-shim-client.
#
# The container wrapper (the example Justfile, the docker-run script,
# or your own equivalent) bind-mounts this file read-only. It is
# safe to source repeatedly; the case-statement guards against
# duplicating the entry on PATH or PYTHONPATH.

case ":$PATH:" in
    *":/opt/pystata-shim-client:"*) ;;
    *) PATH="/opt/pystata-shim-client:$PATH" ;;
esac
case ":${PYTHONPATH:-}:" in
    *":/opt/pystata-shim-client:"*) ;;
    *) PYTHONPATH="/opt/pystata-shim-client${PYTHONPATH:+:$PYTHONPATH}" ;;
esac
export PATH PYTHONPATH
