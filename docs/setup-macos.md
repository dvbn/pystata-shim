# macOS host setup

This page walks through installing `pystata-shim` on a macOS machine
that already has Stata. Default installation is **lifecycle-driven**:
running `setup_host.sh` only prepares files; you (or a container
wrapper) explicitly start the shim with `shimctl.sh start` when you
need it and stop it with `shimctl.sh stop` (or `stop-if-unused`) when
you are done. There is no login-time daemon by default.

You end up with:

- a venv under `~/.pystata-shim/venv/` (only fastapi, uvicorn, pydantic)
- copies of `app.py`, `worker.py`, and `start.sh` under `~/.pystata-shim/`
- a 64-hex-character shared secret (32 bytes) in
  `~/.pystata-shim/token` with mode `0600`
- restricted `.do` validation by default, with an explicit trusted-mode
  opt-in if you want Stata host escapes such as `shell` or `python`

The shim is **not** running yet. Use the lifecycle script to bring it
up — either by hand, from a container wrapper, or from your project's
Justfile.

The shim never mutates your Stata install. It reads PyStata from the
utilities directory you point it at, nothing more.

## Requirements

- macOS (Apple Silicon or Intel)
- A licensed Stata install with PyStata available. StataNow is the
  default path in the setup script, but other macOS Stata installs can
  work when `PYSTATA_UTILITIES` points at their `utilities` directory.
- `python3` 3.10 or later.
- `openssl`, `rsync` (both ship with macOS by default).
- `curl` is preferred for `host/scripts/healthcheck.sh` and for
  `shimctl.sh status` / `start` (ships with macOS by default). When
  curl is missing, `shimctl.sh` falls back to a bash `/dev/tcp` HTTP
  probe that is a degree weaker but still distinguishes the shim
  from a stray TCP listener on the same port.
- `launchctl` (ships with macOS) — only required for the opt-in
  `INSTALL_LAUNCH_AGENT=1` mode.

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
about the license, you most likely need `PYSTATA_EDITION=se` or `be`.
Set it, re-run `setup_host.sh` (idempotent), and re-run `shimctl.sh
start` against the updated runtime.

## One-shot install

From a checkout of this repo:

```bash
# Optional: override defaults; the defaults match StataNow + mp.
export PYSTATA_EDITION=mp                           # mp | se | be
export PYSTATA_UTILITIES=/Applications/StataNow/utilities
export PYSTATA_SHIM_ALLOW_UNSAFE_DO=0               # 0 restricted, 1 trusted

bash host/scripts/setup_host.sh
```

What it does, in order (default mode, no LaunchAgent):

1. creates `~/.pystata-shim/` and `~/.pystata-shim/runs/`
2. installs `host/app.py`, `host/worker.py`, and `host/start.sh` into
   `~/.pystata-shim/`
3. creates a Python virtualenv at `~/.pystata-shim/venv/` and installs
   `fastapi`, `uvicorn[standard]`, and `pydantic`
4. writes a new 64-hex-character shared secret (32 bytes) to
   `~/.pystata-shim/token` with mode `0600`, unless a token already
   exists there (idempotent)
5. confirms `start.sh` and `host/scripts/shimctl.sh` are present and
   prints the next-step commands you need to start using the shim

The script does **not** start the shim. Bring it up with:

```bash
bash host/scripts/shimctl.sh start
bash host/scripts/healthcheck.sh --auth
```

Stop it with:

```bash
bash host/scripts/shimctl.sh stop
```

`shimctl.sh stop-if-unused` is the safer stop in a multi-container
workflow: it leaves the shim running if any other container labeled
`com.pystata.shim.enabled=true` is still up, and stops it otherwise.

## Opt-in: install a launch agent

If you want a `launchctl`-managed shim (instead of `shimctl`), re-run
setup with the opt-in flag:

```bash
INSTALL_LAUNCH_AGENT=1 bash host/scripts/setup_host.sh
```

This adds a sixth step that renders
`host/launchd/com.pystata.shim.plist.template` into
`~/Library/LaunchAgents/com.pystata.shim.plist` and loads it via
`launchctl bootstrap`. The plist template ships **without**
`RunAtLoad` and `KeepAlive`, so installing the agent does **not**
make the shim a login-time daemon — `launchctl bootstrap` only loads
the job into the user's GUI domain. To actually start it:

```bash
launchctl kickstart -k "gui/$(id -u)/com.pystata.shim"
```

If you do want the shim to start at login automatically, edit the
installed plist and add the keys yourself. The template's header
comment shows the exact XML to add.

If you previously ran an older version of this repo that installed a
persistent (`RunAtLoad`+`KeepAlive`) launch agent, the new
`setup_host.sh` will print a one-time warning when it detects that
agent and tell you exactly how to remove it. It will not silently
delete user-installed services.

## Restricted Mode Vs Trusted Mode

By default, the shim runs in restricted mode. It rejects Stata code
that uses direct host escapes (`shell`, `!`, `xshell`, `python`,
`unix`, `winexec`, `javacall`, `mata`) or dynamic constructs the shim
cannot inspect safely before execution.

If you fully trust the container and want the older, less restrictive
behavior, re-run setup with:

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

If you also installed the LaunchAgent in opt-in mode, `launchctl
kickstart -k "gui/$(id -u)/com.pystata.shim"` reloads it against the
new value. Otherwise, just `shimctl.sh stop && shimctl.sh start`.

## Verifying end-to-end from your Mac

```bash
bash host/scripts/shimctl.sh start

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

## Managing the shim with shimctl

```bash
# Status (script-friendly: first whitespace-separated token is the
# state — running, stopped, or unknown)
bash host/scripts/shimctl.sh status

# Tail the logs (one is stdout, one is stderr)
tail -f ~/.pystata-shim/shim.stdout.log
tail -f ~/.pystata-shim/shim.stderr.log

# Restart after editing the runtime
bash host/scripts/shimctl.sh stop
bash host/scripts/shimctl.sh start

# Conservative stop in a multi-container workflow:
bash host/scripts/shimctl.sh stop-if-unused
```

The logs grow unbounded. If you run the shim long-term, rotate them
with `newsyslog` or truncate on a schedule.

## Managing the launch agent (opt-in only)

If you installed the agent with `INSTALL_LAUNCH_AGENT=1`, the label is
`com.pystata.shim`. Standard launchctl commands apply:

```bash
# Inspect
launchctl print "gui/$(id -u)/com.pystata.shim"

# Run it now (the default plist does not RunAtLoad)
launchctl kickstart -k "gui/$(id -u)/com.pystata.shim"

# Reload after editing the plist or rotating the token
launchctl bootout   "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist

# Unload completely
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist
```

## Rotating the shared secret

```bash
bash host/scripts/shimctl.sh stop
rm ~/.pystata-shim/token
bash host/scripts/setup_host.sh          # writes a fresh token
bash host/scripts/shimctl.sh start
```

Then update `PYSTATA_SHIM_TOKEN` on every container that talks to the
shim. `docker exec` does **not** change the env vars of an already-
running container — re-create the container, or `docker stop` and
`docker run` it with the new token in your shell's environment.
Changing the token invalidates previous sessions on the next request,
not mid-request.

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
bash host/scripts/shimctl.sh stop

# Remove launchd agents under both historical label names. The
# `com.pystata.shim` agent is what `INSTALL_LAUNCH_AGENT=1` installs
# for this repo; `com.openstata.pystata-shim` covers older renamed or
# embedded installs that may still be present on the same Mac. Either
# command is a no-op if nothing is loaded.
launchctl bootout "gui/$(id -u)" \
    ~/Library/LaunchAgents/com.pystata.shim.plist 2>/dev/null || true
launchctl bootout "gui/$(id -u)" \
    ~/Library/LaunchAgents/com.openstata.pystata-shim.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.pystata.shim.plist
rm -f ~/Library/LaunchAgents/com.openstata.pystata-shim.plist

rm -rf ~/.pystata-shim
# If you ran sync_stata_mirrors.sh and want the mirrors gone too:
rm -rf /Users/Shared/stata-mirrors
```

If something is still listening on `127.0.0.1:8787` after this,
identify it before killing it:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

No other files on your system are touched.
