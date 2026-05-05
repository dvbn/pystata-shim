# macOS host setup

This page walks through installing `pystata-shim` on a macOS machine
that already has Stata. You end up with:

- a launch agent running as your user account
- a FastAPI HTTP shim on `127.0.0.1:8787`
- a 64-hex-character shared secret (32 bytes) in
  `~/.pystata-shim/token`
- a venv under `~/.pystata-shim/venv/` (only fastapi, uvicorn, pydantic)
- restricted `.do` validation by default, with an explicit trusted-mode
  opt-in if you want Stata host escapes such as `shell` or `python`

The shim never mutates your Stata install. It reads PyStata from the
utilities directory you point it at, nothing more.

## Requirements

- macOS (Apple Silicon or Intel)
- A licensed Stata install with PyStata available. StataNow is the
  default path in the setup script, but other macOS Stata installs can
  work when `PYSTATA_UTILITIES` points at their `utilities` directory.
- `python3` 3.10 or later.
- `openssl`, `rsync` (both ship with macOS by default).
- `curl` for the optional helper healthcheck commands (ships with
  macOS by default).
- `launchctl` (ships with macOS).

## Where PyStata lives on your machine

The worker adds `PYSTATA_UTILITIES` to `sys.path` so it can `import
pystata`. Common values (with the matching `PYSTATA_EDITION` for the
license your install actually has):

| Stata install                | `PYSTATA_UTILITIES`                         | Typical `PYSTATA_EDITION` |
|------------------------------|---------------------------------------------|---------------------------|
| StataNow (default)           | `/Applications/StataNow/utilities`          | `mp`                      |
| Stata 18 MP                  | `/Applications/Stata/utilities`             | `mp`                      |
| Stata 18 SE                  | `/Applications/Stata/utilities`             | `se`                      |
| Stata 18 BE                  | `/Applications/Stata/utilities`             | `be`                      |
| Stata 17                     | `/Applications/Stata17/utilities`           | `mp` / `se` / `be`        |
| non-default install location | `<install-root>/utilities`                  | as licensed               |

If your install is elsewhere, set `PYSTATA_UTILITIES` in your shell
before running `setup_host.sh`. The setup script verifies that the
directory exists before it creates anything.

The default `PYSTATA_EDITION` is `mp`. If the shim comes up and
`/health` reports `pool_state: uninitialized` with an `init_error`
about the license, you most likely need `PYSTATA_EDITION=se` or
`be`. Set it, re-run `setup_host.sh` (idempotent), and the launch
agent reloads against the new edition.

## One-shot install

From a checkout of this repo:

```bash
# Optional: override defaults; the defaults match StataNow + mp.
export PYSTATA_EDITION=mp                           # mp | se | be
export PYSTATA_UTILITIES=/Applications/StataNow/utilities
export PYSTATA_SHIM_ALLOW_UNSAFE_DO=0               # 0 restricted, 1 trusted

bash host/scripts/setup_host.sh
```

What it does, in order:

1. creates `~/.pystata-shim/` and `~/.pystata-shim/runs/`
2. installs `host/app.py`, `host/worker.py`, and `host/start.sh` into
   `~/.pystata-shim/`
3. creates a Python virtualenv at `~/.pystata-shim/venv/` and installs
   `fastapi`, `uvicorn[standard]`, and `pydantic`
4. writes a new 64-hex-character shared secret (32 bytes) to
   `~/.pystata-shim/token` with mode `0600`, unless a token already
   exists there (idempotent)
5. renders `host/launchd/com.pystata.shim.plist.template` into
   `~/Library/LaunchAgents/com.pystata.shim.plist` with your paths
6. loads the launch agent with `launchctl bootstrap`, starts it, and
   waits for authenticated `/health` to succeed

At the end, setup should print `authenticated health ok`. If the
launch agent accepted the job but the shim did not become healthy,
the script exits non-zero and prints the stderr log tail plus any
process listening on the configured port.

You can re-check the unauthenticated health payload with:

```bash
bash host/scripts/healthcheck.sh
```

For the detailed, authenticated payload:

```bash
bash host/scripts/healthcheck.sh --auth
```

## Restricted Mode Vs Trusted Mode

By default, the shim runs in restricted mode. It rejects Stata code
that uses direct host escapes (`shell`, `!`, `xshell`, `python`,
`unix`, `winexec`, `javacall`, `mata`) or dynamic constructs the shim
cannot inspect safely before execution.

If you fully trust the container and want the older, less restrictive
behavior, install or re-render the LaunchAgent with:

```bash
PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 bash host/scripts/setup_host.sh
```

That disables the static `.do` validator. Stata host escapes then run
on the Mac as your user account, not inside the container. The shared
token should be treated as equivalent to shell access to your Mac.

To return to restricted mode:

```bash
PYSTATA_SHIM_ALLOW_UNSAFE_DO=0 bash host/scripts/setup_host.sh
```

## Verifying end-to-end from your Mac

```bash
HEADER_FILE="$(mktemp)"
trap 'rm -f "${HEADER_FILE}"' EXIT
printf 'X-Shim-Token: %s\n' "$(cat ~/.pystata-shim/token)" > "${HEADER_FILE}"
curl -sS \
    -H @"${HEADER_FILE}" \
    -H 'Content-Type: application/json' \
    --data-binary '{"do_code": "display 1+1", "timeout_sec": 30}' \
    http://127.0.0.1:8787/do
```

You should get a JSON blob with `"ok": true`, `"stata_rc": 0`, and
`"stdout"` containing `"2"`. The temporary header file keeps the
literal token out of the `curl` process arguments.

## Managing the launch agent

The label is `com.pystata.shim`. Standard launchctl commands apply:

```bash
# Inspect
launchctl print "gui/$(id -u)/com.pystata.shim"

# Tail the logs (one is stdout, one is stderr)
tail -f ~/.pystata-shim/shim.stdout.log
tail -f ~/.pystata-shim/shim.stderr.log

# Reload after editing the plist or rotating the token
launchctl bootout   "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
launchctl kickstart -k "gui/$(id -u)/com.pystata.shim"

# Stop it
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
```

The logs grow unbounded. If you run the shim long-term, rotate them
with `newsyslog` or truncate on a schedule.

## Rotating the shared secret

```bash
rm ~/.pystata-shim/token
bash host/scripts/setup_host.sh          # writes a fresh token, reloads the agent
```

Then update `PYSTATA_SHIM_TOKEN` on every container that talks to the
shim. Changing the token invalidates previous sessions on the next
request, not mid-request.

## Memory footprint

The shim itself is tiny (~60 MiB resident: uvicorn + the worker
subprocess shell). The Stata workload is whatever your `.do`
files demand — a 10-million-row regression in MP can easily use
2–4 GiB. Stata, not the shim, owns the RAM. Plan capacity around
your largest expected workload, not around the shim.

## Making the client wrapper executable

The container client ships two files:

- `client/stata-do.py` — argparse CLI (Python)
- `client/stata-do` — `/bin/sh` wrapper that just `exec`s the
  Python file, so `stata-do` resolves on `PATH` without the `.py`

On a fresh clone, the executable bit might not be set if your
checkout went through a tarball or a tool that doesn't preserve
modes. Verify and fix:

```bash
ls -l client/stata-do client/stata-do.py
chmod +x client/stata-do client/stata-do.py
```

## Optional: Stata ado/docs mirrors

If you want a containerized agent to be able to read Stata's built-in
`.ado` files or PDF manuals, run:

```bash
bash host/scripts/sync_stata_mirrors.sh
```

This populates `/Users/Shared/stata-mirrors/{ado,utilities-pystata,docs}/`
from your local Stata install. Nothing in `pystata-shim`'s execution
path depends on those mirrors — they exist solely so the container can
bind-mount them read-only and browse the files.

## Uninstall

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
rm ~/Library/LaunchAgents/com.pystata.shim.plist
rm -rf ~/.pystata-shim
# If you ran sync_stata_mirrors.sh and want the mirrors gone too:
rm -rf /Users/Shared/stata-mirrors
```

No other files on your system are touched.
