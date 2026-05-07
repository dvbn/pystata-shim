# Agent guidance

This file is written for whoever is dropping an agent (Codex, Claude
Code, a custom harness, etc.) into a container that talks to
`pystata-shim`. It is a checklist for making the agent produce Stata
output that actually lands on your disk.

If you operate an agent that reads `AGENTS.md` files at session start,
also point it at the in-container breadcrumb at
`/opt/pystata-shim-client/AGENTS.md` (mounted from `client/AGENTS.md`
in this repo). It is a quick-reference twin of this file scoped to
what an agent inside the container actually sees, including the
warning against stale `openstata_pystata` modules.

## Core rules

1. **Write `.do` files under `$PYSTATA_SHIM_CONTAINER_WORKSPACE`**
   (default `/workspace`). The host shim reads files from the host
   filesystem; `/tmp`, `/home/coder`, or any other container-only path
   cannot be reached by Stata running on the host.

2. **Run with `stata-do <file.do> --cwd <workspace-dir>`**. The wrapper
   resolves the container path to the matching host path, builds a
   `cd "..."; do "..."` snippet, and sends it through the shim.

3. **Inside a `.do` file, write outputs with paths relative to the
   working directory** passed as `--cwd`, not absolute container
   paths. The `cd` happens on the host, where `/workspace` may not
   even exist.

   Correct:
   ```stata
   clear all
   sysuse auto, clear
   capture mkdir "output"
   export delimited using "output/auto.csv", replace
   save "output/auto.dta", replace
   ```

   Then `output/auto.csv` appears at
   `$PYSTATA_SHIM_CONTAINER_WORKSPACE/output/auto.csv` inside the
   container, which is the same file as
   `$PYSTATA_SHIM_HOST_WORKSPACE/output/auto.csv` on the host.

4. **Do not use `/workspace/...` inside `.do` code.** On the host that
   path may not exist, and the shim deliberately does not rewrite
   paths that appear inside `do_code`.

5. **Do not write to `/tmp` or `/home/coder` expecting persistence.**
   Those are container-local and invisible to the host Stata process.

## `stata-do` — quick reference

```bash
stata-do /workspace/path/to/file.do --cwd /workspace
stata-do /workspace/file.do --timeout 1800 --tag my-agent
stata-do /workspace/file.do --capture 'e(b)' --capture 'e(N)' --json
stata-do --health
```

- Exit code `0` = Stata ran and returned `rc == 0`.
- Exit code `2` = shim-level error (auth, transport, timeout, crash,
  bad args, unauthenticated health).
- Any other non-zero code = Stata's own return code.

Shell-quote capture names containing parentheses:
`--capture 'e(b)'`, not `--capture e(b)`.

## Using the Python client directly

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

if not result["ok"]:
    raise SystemExit(f"error: {result['error_code']}: {result['error_detail']}")
print("N =", result["captured"]["e(N)"])
```

```python
# Run a .do file that lives in the container workspace.
result = shim.run_do_file(
    "/workspace/analysis/main.do",
    timeout_sec=1800,
    caller_tag="my-agent",
)
```

## When the shim returns `ok: false`

Check `error_code`:

| code              | what to do                                             |
|-------------------|--------------------------------------------------------|
| `timeout`         | retry with a larger `timeout_sec` or break the job up  |
| `job_interrupted` | someone cancelled it; decide whether to re-run         |
| `license_error`   | Stata's license check failed on the host; escalate to the host operator |
| `stata_crash`     | retry once; repeated failures trip the circuit breaker |
| `queue_timeout`   | the single worker slot is busy; retry later            |
| `shim_degraded`   | the breaker is open; wait it out (default 5 min)       |

## What NOT to do

- Do not ask the shim to run `.do` files you have not written to disk
  first. `do_code` snippets larger than 1 MiB are rejected.
- Do not pass secrets in `caller_tag`. It is logged in the clear.
- Do not rely on `/job/{job_id}` for history. It only reflects live
  jobs. Once a job exits, that endpoint returns `404`.
- Do not assume the shim is reachable. Containers launched with
  `--network=none` cannot reach `host.docker.internal:8787`. Catch
  the connection error and fail cleanly.
- Do not rely on host-escape directives inside `.do` code. By
  default the shim rejects `shell`, `!`, `xshell`, `python`, `unix`,
  `winexec`, `javacall`, and `mata`, including their standard
  abbreviations; rejects macro-expanded command names it cannot
  inspect; and rejects dynamic `do` / `run` / `include` directives it
  cannot inspect.
  If the host operator explicitly enabled trusted mode with
  `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 bash host/scripts/setup_host.sh`,
  `shell` and other host escapes run on the Mac as the host user, not
  inside the container.

## How to confirm Stata actually ran

Single response field: `stata_rc`. Zero means Stata processed the
code without a `r(<nonzero>);` error. Capture specific results with
the `capture` argument and compare them to your expected values. Do
not infer success from `stdout` alone — Stata can print informational
text even on failure.
