# Troubleshooting

## "The shim is not running at all"

This is the most common first-time failure. Symptoms: `curl
http://127.0.0.1:8787/health` fails immediately with "connection
refused", and `~/.pystata-shim/shim.stderr.log` either does not
exist or has an error in it.

Diagnostic flow, in order:

```bash
# 1. Was setup_host.sh ever run?
ls -la ~/.pystata-shim/ ~/Library/LaunchAgents/com.pystata.shim.plist 2>&1

# 2. Is the launch agent loaded?
launchctl print "gui/$(id -u)/com.pystata.shim" 2>&1 | head -10

# 3. Why did it die?
tail -40 ~/.pystata-shim/shim.stderr.log 2>&1
```

What the outputs mean:

- step 1 fails → `setup_host.sh` was never run (or didn't
  finish). Re-run it from the repo: `bash host/scripts/setup_host.sh`.
- step 2 says "could not find service" → the plist exists but is
  not loaded. `launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist`
  then re-check.
- step 3 has a `pystata-shim:` line with a real error → fix that
  specific error (token-file mode, edition, utilities path),
  re-run `setup_host.sh` (idempotent), recheck.

If step 3 is empty but the agent is loaded and listening — check
that you're hitting the right port:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN 2>&1
```

If nothing answers on 8787 but the agent is up, you set
`PYSTATA_SHIM_PORT` to something else; check the plist.

## The launch agent installs but the shim never comes up

Symptoms:

- `curl http://127.0.0.1:8787/health` fails with "connection refused"
- `launchctl print gui/$(id -u)/com.pystata.shim` shows the agent
  alive but the uvicorn process is missing

Check:

- `~/.pystata-shim/shim.stderr.log` — the server logs a one-line
  error and exits if the token is missing, the token file has wrong
  permissions, or the non-loopback bind was attempted without the
  override.
- If you see "token file is mode XXX" — run
  `chmod 600 ~/.pystata-shim/token`.
- If you see "PYSTATA_SHIM_TOKEN must be set ... at least 32
  characters" — delete the file and re-run `setup_host.sh`.
- If you see "refusing to run with PYSTATA_SHIM_HOST=..." — change
  `PYSTATA_SHIM_HOST` back to `127.0.0.1` or accept the
  non-loopback risk by setting `PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1`.
- If you see `address already in use` — another process already owns
  port 8787. Identify it with
  `lsof -nP -iTCP:8787 -sTCP:LISTEN`. If it is an older shim launch
  agent, unload that old agent before re-running setup. For example:

  ```bash
  launchctl list | grep -i 'stata\|shim\|pystata'
  launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/<old-label>.plist
  bash host/scripts/setup_host.sh
  ```

## `/health` returns `"pool_state": "uninitialized"` forever

PyStata failed to initialize. Authenticate and re-probe:

```bash
python3 - <<'PY'
import pathlib
import urllib.request

token = pathlib.Path("~/.pystata-shim/token").expanduser().read_text().strip()
request = urllib.request.Request(
    "http://127.0.0.1:8787/health",
    headers={"X-Shim-Token": token},
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
PY
```

The authenticated payload has `error_code` and `init_error` with
the real failure string.

Common causes:

- Wrong `PYSTATA_UTILITIES` — set it to the `utilities` directory of
  your Stata install. Restart the launch agent after editing the
  plist.
- Missing Stata license (look for `license_error`).
- Python version inside the venv is too old for pydantic/FastAPI.

## Container: cannot resolve `host.docker.internal`

Your container runtime does not forward the host mapping. Fix with
`--add-host=host.docker.internal:host-gateway` on the docker run/
create command. Applies to Colima, Rancher Desktop, and Podman; not
needed on Docker Desktop or OrbStack.

## Container: connection refused or timed out

- Is the host launch agent running?
  `launchctl print gui/$(id -u)/com.pystata.shim`
- Is the container networked? `--network=none` cannot reach the host.
- Is the host binding loopback? Non-loopback binds take a separate
  env-var override.

## `POST /do` returns 401

- Check `PYSTATA_SHIM_TOKEN` inside the container matches
  `~/.pystata-shim/token` on the host byte-for-byte. Shell quoting
  and trailing newlines bite here.
- If you rotated the token on the host, update every container
  environment to use the new one.

## `POST /do` returns 422 with an "unsafe ..." validation error

The shim now rejects obvious host-escape directives by default
before Stata starts.

- `shell`, `!`, `xshell`, `python`, `unix`, `winexec`, `javacall`,
  and `mata` are blocked in submitted `do_code`, including their
  standard Stata abbreviations
- macro-expanded command names such as `$cmd ...` or `` `cmd' ... ``
  are rejected because the shim cannot inspect what command they
  resolve to
- literal nested `do` / `run` / `include` files are inspected
  recursively
- dynamic nested `do` / `run` / `include` directives are rejected
  because the shim cannot inspect them safely
- hidden Unicode control / format characters are rejected
- nested file inspection stops at 16 levels and 1 MiB per file

What to do:

- If you meant to run Stata analysis, remove the host-escape command.
- If you meant to run a `.do` file from the container workspace, keep
  using `stata-do /workspace/file.do --cwd /workspace`; that path is
  compatible with the new validation.
- If you intentionally want the old unsafe behavior for a fully
  trusted setup, the host operator must explicitly opt in with
  `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1`.

## `error_code: "timeout"`

The worker exceeded `timeout_sec`. Either raise the request
`timeout_sec` (max 3600) or break the job into smaller pieces. The
cap is a hard ceiling; requests above 3600 are clamped.

## `error_code: "shim_degraded"`

Three worker crashes inside a 10-minute window tripped the circuit
breaker; the shim is cooling down for 5 minutes. You cannot bypass
this without waiting. If it happens repeatedly, `shim.stderr.log`
has the underlying crash reasons — usually a broken PyStata install
or an out-of-memory Stata job.

Thresholds are env-tunable:

| env var                              | default |
|--------------------------------------|---------|
| `PYSTATA_SHIM_DEGRADE_CRASHES`       | `3`     |
| `PYSTATA_SHIM_DEGRADE_WINDOW_SEC`    | `600`   |
| `PYSTATA_SHIM_DEGRADE_COOLDOWN_SEC`  | `300`   |

## `error_code: "queue_timeout"`

The single-worker slot was busy for 20 minutes. That usually means
an earlier job is stuck. Cancel it:

```bash
HEADER_FILE="$(mktemp)"
trap 'rm -f "${HEADER_FILE}"' EXIT
printf 'X-Shim-Token: %s\n' "$(cat ~/.pystata-shim/token)" > "${HEADER_FILE}"
curl -sS -X POST -H @"${HEADER_FILE}" \
    http://127.0.0.1:8787/cancel/<job_id>
```

You can see the current job id in the authenticated `/health`
payload's `current_job_id` field.

## Stata says "file not found" but the file is right there

Most common cause: the `.do` file uses `/workspace/...` paths while
Stata is running on the host. `/workspace` does not exist on the
host. Rewrite the `.do` file to use paths relative to `--cwd`, or
write a host-absolute path directly.

Second most common: the container path maps to a different host path
than `PYSTATA_SHIM_HOST_WORKSPACE` claims. Verify with:

```bash
# Inside the container:
echo "container workspace: $PYSTATA_SHIM_CONTAINER_WORKSPACE"
echo "host workspace:      $PYSTATA_SHIM_HOST_WORKSPACE"
```

and from the host:

```bash
ls -d "$PYSTATA_SHIM_HOST_WORKSPACE"
```

They must point at the same directory bytes-for-bytes.

## `stata-do` prints nothing on success

That is expected for a `.do` file that produces no output. Pass
`--json` to see the full response envelope including `stata_rc` and
`duration_sec`.

## Log files are growing without bound

The launch agent's `StandardOutPath` and `StandardErrorPath` never
rotate on their own. For long-lived installs, either truncate the
logs periodically or add a `newsyslog` entry. Per-job directories
under `$PYSTATA_SHIM_HOME/runs/` are pruned at server startup on a
7-day retention (configurable via `PYSTATA_SHIM_RUNS_RETENTION_DAYS`).

## I changed the token but old calls still work

The token is read once at server startup. Restart the launch agent
after rotating the token:

```bash
launchctl kickstart -k "gui/$(id -u)/com.pystata.shim"
```

## Mac went to sleep mid-job; client timed out

The shim is a regular user-space process and pauses with the OS
when the Mac sleeps. There is no auto-resume across sleep. After
wake:

- a job that was running before sleep is still wedged in the
  worker; the parent shim's timeout fires when the wallclock
  catches up
- the supervisor returns `error_code: "timeout"` to the client
- subsequent `/health` should return `ok: true` again within a
  few seconds; if not, kick the agent:
  `launchctl kickstart -k "gui/$(id -u)/com.pystata.shim"`

For long jobs, either disable display sleep
(System Settings → Displays → "Prevent automatic sleeping when
the display is off"), or split the job into chunks short enough
to finish between sleeps.

## How much RAM will Stata use?

Same as Stata always uses. The shim itself takes about 60 MiB of
RAM idle (uvicorn + a thin worker subprocess). The actual Stata
workload — loading datasets, running estimates — is whatever your
`.do` files demand. Rules of thumb:

- A clean Stata MP process at idle: ~250 MiB.
- A 10-million-row dataset in a regression: ~2–4 GiB depending on
  variable types.
- A `bsample`/`bootstrap` loop in MP: scales with `nthreads`.

If your Mac has 8 GiB total RAM, expect serious paging on jobs
that load large datasets. There is nothing in the shim that can
help; this is a Stata-vs-RAM problem.

## Docker Desktop says "mount denied" on a path outside /Users/

Docker Desktop has an explicit allow-list of host directories it
will bind into containers. By default it includes `/Users`,
`/Volumes`, `/private`, and `/tmp`. If your project lives at, say,
`/opt/work/...` or a custom mount point, Docker will refuse to
bind it.

Fix:

1. Quit any running container that's trying to use that mount.
2. Open Docker Desktop.
3. Go to **Settings → Resources → File Sharing** (the Settings
   panel is the gear icon in the menu bar).
4. Click the `+` button and add the parent directory.
5. Click "Apply & Restart".
6. Re-run your `docker run` (or `setup_host.sh` if it was the
   shim setup that hit this).

OrbStack and Colima do not have this allow-list — any path your
user can read is bind-mountable.

## I put `client/` on PATH but `stata-do` says permission denied

The wrapper script needs the executable bit. After cloning:

```bash
chmod +x client/stata-do
chmod +x client/stata-do.py
```

`git` preserves these bits across pulls, so you only need to do
this once.

## Stata says something about `` `...' `` or `$...` in my path

Stata expands local-macro (`` `name' ``) and global-macro (`$name`)
references inside quoted strings, including in `cd` and `do`. The
shim does not rewrite these. Pick directory names that don't contain
`` ` `` or `$` followed by letters.
