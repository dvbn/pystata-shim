# Architecture

`pystata-shim` is a small HTTP service that lets a **Linux Docker
container** execute Stata `.do` code against a **locally installed
Stata on a macOS host** through PyStata.

The container never runs Stata. The host never gives the container
direct process access. A localhost-only HTTP boundary is the only
coupling.

For a visual walkthrough, see [diagrams.md](diagrams.md) — five
Mermaid diagrams covering architecture, request lifecycle, path
mapping, the state machine, and token flow.

## Who this is for

You already have:

- a licensed, working Stata install on a macOS machine
- Stata's PyStata utilities available on that machine
- a Docker container (Docker Desktop or OrbStack) where your agent
  or tooling runs
- a workspace directory that is bind-mounted from host into the
  container

You want an agent inside the container to be able to write a `.do`
file and run it against your real Stata, without putting Stata inside
the container. This repo gives you the scripts and a small HTTP shim
to wire that up.

This repo does **not** ship Stata, does not distribute Stata content,
does not replace Stata licensing, and does not aim to be a general
analysis workflow system. See [security.md](./security.md) for the
trust model and [setup-macos.md](./setup-macos.md) for the host
install path.

## Shape

```text
Container (Linux)                        Host (macOS)
┌──────────────────────────────┐         ┌──────────────────────────────────┐
│ client/pystata_shim.py       │  HTTP   │ host/app.py (FastAPI parent)     │
│ client/stata-do.py           │ ─────▶  │  └─ host/worker.py (subprocess)  │
│ (uses shared-secret token)   │         │       └─ PyStata → Stata         │
│                              │         │                                  │
│ /workspace (bind-mounted)    │ ◀──RW── │ <HOST_WORKSPACE> on host disk    │
│                              │         │                                  │
│ /stata/... (optional RO)     │ ◀──RO── │ <MIRROR_ROOT> on host disk       │
└──────────────────────────────┘         └──────────────────────────────────┘
```

## Core invariants

1. **Host runs Stata; the container never runs Stata directly.**
   Stata sits behind a localhost HTTP boundary. The container holds
   no Stata license and does not execute Stata code in-process.
2. **Mutating endpoints require a shared-secret token.**
   Every `POST` endpoint, and `GET /job/{job_id}`, check an
   `X-Shim-Token` header against a server-side secret using
   `secrets.compare_digest`. The unauthenticated `GET /health`
   payload is deliberately thin (see Security, below).
3. **Jobs are serialized.** PyStata embeds Stata in a single Python
   process. The shim enforces a one-job-at-a-time slot so two
   requests never race the same Stata session.
4. **Every job runs in a dedicated worker subprocess.** A wedged or
   crashing Stata job terminates the worker, not the API server.
   Timeouts escalate `SIGTERM` → `SIGKILL` against the worker
   process group after a grace period.
5. **Explicit host/container workspace mapping.** Path translation
   is a documented contract — four environment variables the client
   and host must agree on — not implicit magic.
6. **Read-only ado/docs mirrors are optional.** If you want the
   container to read Stata's built-in source or manuals, a separate
   mirror script copies those into a user-chosen directory you bind
   into the container. Execution does not depend on the mirrors.
7. **The repo does not distribute proprietary Stata content.** Setup
   scripts read your local Stata install and copy its readable files
   into a directory you own. No Stata binaries, no `.ado` files, no
   manuals are shipped from this repo.

## Transport

- Host FastAPI server listens only on `127.0.0.1:8787` by default.
  Non-loopback binds are refused unless
  `PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1` is set (and even then, there
  is no TLS — see security.md).
- Container reaches the host via `host.docker.internal:8787`. On
  Docker Desktop and OrbStack this resolves out of the box. On
  Colima and Rancher Desktop, add
  `--add-host=host.docker.internal:host-gateway` when you create
  the container.
- The in-container client uses Python's stdlib `urllib.request`.
  Reason: the client works in slim Python images without `apt-get`,
  `curl`, or any pip-installable dependency. Request timeouts are
  passed to the stdlib socket layer.

## Endpoints

| Method | Path              | Auth | Purpose                                    |
|--------|-------------------|------|--------------------------------------------|
| GET    | `/health`         | no\* | Probe Stata init + pool state              |
| POST   | `/do`             | yes  | Run a `.do` code snippet                   |
| GET    | `/job/{job_id}`   | yes  | Live status of a currently-running job     |
| POST   | `/cancel/{job_id}`| yes  | Terminate a running job                    |

\* The unauthenticated `GET /health` payload is minimal: `ok`,
`pool_state`, `queue_depth`. To see diagnostic detail
(`edition`, `stata_version_number`, `error_code`, `init_error`,
`current_job_id`), call
`GET /health` with the same `X-Shim-Token` header used for mutating
requests.

### `/do` body

```json
{
  "do_code": "sysuse auto, clear\nreg price mpg",
  "capture": ["e(b)", "e(V)", "e(N)", "e(r2)"],
  "timeout_sec": 600,
  "caller_tag": "my-agent"
}
```

- `do_code` is required. Hard-capped server-side at 1 MiB.
- Unless `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1` is set on the host, the shim
  statically validates `do_code` before Stata starts:
  - direct host-escape directives such as `shell`, `!`, `xshell`,
    `python`, `unix`, `winexec`, `javacall`, and `mata` are rejected,
    including their standard Stata abbreviations
  - macro-expanded command names are rejected because the shim cannot
    inspect what command they resolve to
  - literal `do` / `run` / `include` targets are recursively
    inspected
  - dynamic or non-literal `do` / `run` / `include` directives are
    rejected because the shim cannot inspect them safely
  - hidden Unicode control / format characters are rejected
  - nested file inspection is capped at 16 levels and 1 MiB per file
  To persist trusted mode under launchd, run
  `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 bash host/scripts/setup_host.sh`.
- `capture` is optional. Up to 128 entries. Each name must match
  `^[A-Za-z_][A-Za-z0-9_:.()]{0,127}$`. Locals and pattern
  fallbacks are out of scope in v1 — pass globals, scalars, and
  named matrices.
- `timeout_sec` is optional. Clamped to `[1, 3600]`. Default 600.
- `caller_tag` is an opaque string (≤ 128 chars, no control chars)
  that the shim logs for audit. The shim validates only length and
  printable-ASCII safety, not meaning or allowed values.

### Running a file through the client helper

Call `POST /do` with a body like:

```json
{
  "do_code": "cd \"/Users/<you>/pkg\"\ndo \"/Users/<you>/pkg/main.do\"\n",
  "timeout_sec": 1800,
  "caller_tag": "my-agent"
}
```

and the client helper `run_do_file(do_file_path)` composes exactly
that pattern for you. In the default restricted mode, the shim also
opens the referenced literal `main.do`, validates its contents, and
recursively validates any further literal `do` / `run` / `include`
files it references before Stata starts. If the file path is dynamic
or the nested file cannot be inspected, the request is rejected with
`422`.

### Response shapes

All endpoints return JSON with `Content-Type: application/json`.

Successful `/do`:

```json
{
  "ok": true,
  "job_id": "20260424T150142-1a2b3c4d",
  "status": "done",
  "stata_rc": 0,
  "stata_error": null,
  "stdout": "...",
  "stderr": "",
  "captured": { "e(N)": 74, "e(r2)": 0.2195 },
  "duration_sec": 1.23,
  "job_dir": "/Users/<you>/.pystata-shim/runs/20260424T150142-1a2b3c4d"
}
```

Failed `/do` (same envelope):

```json
{
  "ok": false,
  "job_id": "20260424T150142-1a2b3c4d",
  "status": "error",
  "error_code": "timeout",
  "error_detail": "request exceeded timeout_sec=30",
  "duration_sec": 30.01
}
```

`error_code` is one of:

| code             | meaning                                                             |
|------------------|---------------------------------------------------------------------|
| `timeout`        | worker exceeded the clamped `timeout_sec`                           |
| `job_interrupted`| cancelled by `POST /cancel/{job_id}`                                |
| `license_error`  | PyStata reported a license or init failure                          |
| `stata_crash`    | worker exited abnormally without a license error                    |
| `queue_timeout`  | request waited too long for the single-worker slot                  |
| `shim_degraded`  | the circuit breaker is open; no worker was spawned                  |

Live `/job/{job_id}`:

```json
{
  "ok": true,
  "job_id": "20260424T150142-1a2b3c4d",
  "status": "running",
  "started_at": "2026-04-24T15:01:42Z",
  "pid": 54321,
  "cancel_requested": false
}
```

`GET /job/{job_id}` returns `404` once the job has exited. History
is in the audit log, not this endpoint.

`POST /cancel/{job_id}`:

```json
{ "ok": true, "job_id": "...", "cancelled": true, "note": "termination signal sent" }
```

Cancel is best-effort. The shim sends `SIGTERM` to the worker's
process group, waits up to ~10 s, then `SIGKILL`. This kills the
worker process **and every subprocess it started**. Cancelled jobs
cannot be resumed.

Pydantic validation failures (oversize `do_code`, bad `capture` name,
unsafe host-escape directives, uninspectable nested `do` / `run` /
`include`, out-of-range `timeout_sec`, etc.) return FastAPI's default
`422` with a `detail` array, not the `ok/error_code` envelope.

## Audit log

Every finished `/do` request is appended as one line of JSON to
`$PYSTATA_SHIM_HOME/audit.jsonl`. The shim is the only writer.
Fields: `ts_utc`, `job_id`, `endpoint`, `caller_tag`, `do_code_sha256`,
`duration_sec`, `stata_rc`, `error_code`, `status`. Full `stdout` is
not duplicated into the audit log; it lives only in the per-job
directory under `runs/<job_id>/`.

## Job lifecycle

1. Client sends a request with `X-Shim-Token`.
2. Shim validates the token (constant-time compare). If missing or
   wrong, the server returns `401` and does not touch Stata.
3. Shim generates a `job_id`, increments `queue_depth`, and waits for
   the single-worker slot (bounded by a queue timeout).
4. Shim spawns a fresh `worker.py` subprocess with its own process
   group, writes a small JSON request file, and waits up to
   `timeout_sec`. `PYSTATA_SHIM_TOKEN` is scrubbed from the worker
   environment before spawn.
5. Worker imports `pystata`, runs the `.do` code inside a fresh job
   directory, writes a JSON response, and exits.
6. Shim reads the response, appends a record to its JSONL audit log,
   and returns JSON to the client.
7. On `timeout_sec` expiry or `POST /cancel/{job_id}`: shim sends
   `SIGTERM` to the worker process group, waits a grace period, then
   `SIGKILL` if the worker has not exited. This kills the worker and
   every process it spawned.

## Degraded mode

Repeated worker crashes inside a sliding window flip the shim into a
`degraded` state for a cooldown period. While degraded, mutating
requests return `error_code: "shim_degraded"` immediately without
spawning a worker. Health reflects the degraded state explicitly.

Thresholds are configurable:

| env var                            | default | meaning                                         |
|------------------------------------|---------|-------------------------------------------------|
| `PYSTATA_SHIM_DEGRADE_CRASHES`     | `3`     | crashes within the window that trip the breaker |
| `PYSTATA_SHIM_DEGRADE_WINDOW_SEC`  | `600`   | sliding window for counting crashes             |
| `PYSTATA_SHIM_DEGRADE_COOLDOWN_SEC`| `300`   | how long the degraded state lasts after trip    |

## Path mapping contract

The container and the host see the same bytes through a bind-mount,
but the two operating systems disagree on the path string. The shim
runs `cd "<host_path>"` before running your `.do` file, so inputs
must be expressed in host-path terms.

The four env vars that make this explicit:

| env var                                | side       | required | purpose                                            |
|----------------------------------------|------------|----------|----------------------------------------------------|
| `PYSTATA_SHIM_BASE_URL`                | container  | no       | default `http://host.docker.internal:8787`         |
| `PYSTATA_SHIM_TOKEN`                   | container  | yes      | shared secret; same string as on the host          |
| `PYSTATA_SHIM_CONTAINER_WORKSPACE`     | container  | no       | default `/workspace`; root of the container bind   |
| `PYSTATA_SHIM_HOST_WORKSPACE`          | container  | yes\*    | host path the container workspace is bound from    |

\* Required only if you use `run_do_file` or any helper that
translates a container-local path into a host path. For raw `/do`
calls with host-absolute paths already in the `.do` code, it is
optional.

Rules:

- one container workspace root maps to exactly one host workspace
  root — overlapping or nested bind mounts are not supported
- paths passed to `run_do_file` must resolve under
  `PYSTATA_SHIM_CONTAINER_WORKSPACE`, or be host-absolute paths that
  the caller knows already exist on the host
- path characters are limited to the safe subset that Stata's quoted
  `cd`/`do` syntax handles — no `NUL`, no newline, no carriage
  return; embedded `"` is doubled before being emitted
- Stata expands local-macro (`` `name' ``) and global-macro (`$name`)
  references inside quoted paths; the shim does **not** rewrite these.
  If a path name contains `` ` `` or `$`, Stata may mutate it. Pick
  directory and file names that avoid those characters
- symlinks are resolved non-strictly (they may point at missing
  targets); the caller is responsible for pointing them at real
  files before calling

## Security

See [security.md](./security.md) for the full threat model. The
one-line version:

> Any caller that holds `PYSTATA_SHIM_TOKEN` can submit Stata code to
> run as your macOS user. The shim blocks direct host escapes by
> default, but it is still not a sandbox. Treat the token like an SSH
> key to your Mac.

## What is deliberately out of scope in v1

- Caching `.do` results.
- Role-based access control. There is exactly one trust level: the
  caller who presents the token.
- Windows host support.
- pip-publishing / distribution packaging.
- Batch-job or workflow orchestration.
- Rate limiting. This is a local-developer tool on a loopback
  socket; rate limiting is left to the operator's environment.
- Any framing as a "research platform" or "workflow system".
