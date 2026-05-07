# pystata-shim client (in-container)

If an agent or script finds this file under `/opt/pystata-shim-client`
(or wherever the host wrapper mounted the client directory), this is
the canonical pystata-shim Python client. Use it. Do **not** import any
older `openstata_pystata` module that may also be on the system; those
predate the auth flow and will fail with HTTP 401.

## Quick check

```bash
stata-do --health
```

If `stata-do` is not on `PATH`, the client directory is mounted but not
exported. Either run from a login shell (the example wrappers ship a
`/etc/profile.d/pystata-shim.sh` snippet that fixes PATH), or invoke
the wrapper directly:

```bash
python3 /opt/pystata-shim-client/stata-do.py --health
```

## Run a `.do` file

The `.do` file must live under `$PYSTATA_SHIM_CONTAINER_WORKSPACE`
(default `/workspace`). The wrapper translates the container path to
the matching host path so Stata, running on the host, can read it.

```bash
stata-do /workspace/analysis/main.do --cwd /workspace
stata-do /workspace/analysis/main.do --capture 'e(N)' --capture 'e(r2)' --json
```

Quote `e(...)`-style capture names; an unquoted `e(N)` is a function
definition to most shells.

## Python API

```python
import pystata_shim as shim

if not shim.health():
    raise SystemExit("shim not ready")

result = shim.do(
    "sysuse auto, clear\nreg price mpg",
    capture=["e(N)", "e(r2)"],
    timeout_sec=120,
    caller_tag="my-agent",
)
print("rc =", result["stata_rc"])

# Or run a .do file under the container workspace:
result = shim.run_do_file(
    "/workspace/analysis/main.do",
    timeout_sec=1800,
    caller_tag="my-agent",
)
```

## Required environment

The host wrapper (justfile / docker-run / compose) sets these. Confirm
with `env | grep PYSTATA_SHIM` if calls fail:

- `PYSTATA_SHIM_TOKEN` — the host shared secret. Without it `/do` and
  authenticated `/health` 401.
- `PYSTATA_SHIM_HOST_WORKSPACE` — host path that backs `/workspace`.
  Required for `run_do_file()` and `stata-do`'s path translation.
- `PYSTATA_SHIM_BASE_URL` — defaults to
  `http://host.docker.internal:8787`.
- `PYSTATA_SHIM_CONTAINER_WORKSPACE` — defaults to `/workspace`.

## See also

The full agent rules and failure-mode catalog live in
`docs/agent-guidance.md` of the pystata-shim repo. This file is a
pointer for whoever lands in the container first.
