# Diagrams

Five Mermaid diagrams that answer a single question each. GitHub
and most modern markdown viewers render them inline; if your
viewer doesn't, paste the source block at https://mermaid.live/.

If you only have one minute, read **Diagram 1**. It's the whole
picture.

1. [What sits where, who talks to whom](#1-architecture-overview)
2. [What happens when an agent calls `stata-do`](#2-request-lifecycle)
3. [How a container path becomes a host path](#3-path-mapping)
4. [How the shim's `pool_state` transitions](#4-state-machine)
5. [How the shared-secret token flows end-to-end](#5-auth-flow)

---

## 1. Architecture overview

Question: where do the pieces live, and what crosses the
boundary?

```mermaid
flowchart LR
    subgraph CONTAINER["Linux Docker container"]
        A["AI agent<br/>Claude Code / Codex / etc."]
        CLI["stata-do CLI"]
        CLIENT["pystata_shim.py<br/>(stdlib HTTP client)"]
        WS["/workspace<br/>(bind-mounted)"]

        A -->|invokes| CLI
        A -->|or imports| CLIENT
        CLI -->|argparse + run_do_file| CLIENT
        A -.->|"reads/writes do/dta/output files"| WS
    end

    subgraph HOST["macOS host"]
        SHIMCTL["host/scripts/shimctl.sh<br/>start / stop / stop-if-unused"]
        APP["host/app.py<br/>FastAPI<br/>127.0.0.1:8787"]
        WORKER["host/worker.py<br/>(spawned per job)"]
        STATA["Stata<br/>via PyStata"]
        TOKEN["~/.pystata-shim/token<br/>(0600)"]
        PIDFILE["~/.pystata-shim/shim.pid"]
        AUDIT["~/.pystata-shim/audit.jsonl"]
        HOSTWS["/Users/you/project<br/>(your project dir)"]

        SHIMCTL -->|starts in background| APP
        SHIMCTL -.->|writes/reads| PIDFILE
        APP -->|reads at boot| TOKEN
        APP -->|spawn subprocess| WORKER
        WORKER -->|import pystata| STATA
        APP -->|append| AUDIT
        STATA -.->|"reads/writes dta and output files"| HOSTWS
    end

    CLIENT ==>|"HTTP POST /do<br/>X-Shim-Token header<br/>via host.docker.internal:8787"| APP

    WS <-->|"same bytes via bind mount"| HOSTWS

    classDef boundary fill:#fef9c3,stroke:#a16207,stroke-width:2px
    classDef host fill:#dcfce7,stroke:#15803d
    classDef container fill:#dbeafe,stroke:#1d4ed8
    class APP,WORKER,STATA,SHIMCTL,TOKEN,PIDFILE,AUDIT,HOSTWS host
    class A,CLI,CLIENT,WS container
```

The container session and the shim run together: a wrapper recipe
(see `examples/justfile/Justfile` or `examples/docker-run/run.sh`)
calls `shimctl start` before the container starts and
`shimctl stop-if-unused` after it exits, so the shim is up only
while a labeled container needs it.

Reading rules:

- **Solid arrows** are control flow (function calls, signals,
  HTTP requests).
- **Dotted arrows** are data flow on shared storage (the bind
  mount, output files).
- **Double arrow** (`==>`) is the network boundary. It's the
  only edge that crosses from container to host. Everything else
  is in-process or on shared disk.
- The container holds **no Stata license** and never imports
  PyStata. The host runs Stata exactly once per job and never
  exposes anything beyond `127.0.0.1:8787`.

---

## 2. Request lifecycle

Question: what happens, in order, when an agent runs
`stata-do my_analysis.do`?

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent in container
    participant Client as pystata_shim.py
    participant HTTP as urllib.request
    participant App as host/app.py (FastAPI)
    participant Slot as Single-worker slot
    participant Worker as worker.py (fresh subprocess)
    participant Stata as PyStata → Stata
    participant Audit as audit.jsonl

    Agent->>Client: stata-do /workspace/x.do --cwd /workspace
    Client->>Client: translate container path<br/>→ host path
    Client->>Client: assemble cd + do snippet
    Client->>HTTP: POST /do<br/>+ X-Shim-Token header
    HTTP->>App: HTTP request via<br/>host.docker.internal:8787

    App->>App: secrets.compare_digest(token)
    alt token bad
        App-->>HTTP: 401 Unauthorized
        HTTP-->>Client: ShimError
        Client-->>Agent: exit 2
    end

    App->>App: pydantic validates body<br/>(do_code ≤ 1 MiB, etc.)
    App->>Slot: acquire (waits up to 20 min)
    App->>App: register job_id in _current
    App->>Worker: subprocess.Popen(...)<br/>(scrubs token from env)

    Worker->>Stata: pystata.config.init(edition)
    Worker->>Stata: stata.run(prologue + do_code)
    Stata-->>Worker: stdout, stderr, captures
    Worker->>Worker: write .response.json
    Worker-->>App: exit 0

    App->>App: read .response.json
    App->>Audit: append one JSONL line
    App->>Slot: release
    App-->>HTTP: 200 OK + JSON envelope
    HTTP-->>Client: JSON
    Client->>Client: parse, raise on ok=false
    Client-->>Agent: stdout / exit code = stata_rc
```

What can go wrong at each step:

| step | failure | shim returns                 |
|------|---------|------------------------------|
| 5    | bad token | HTTP 401, `ShimError`        |
| 6    | bad body  | HTTP 422, FastAPI default    |
| 7    | slot held >20 min | `error_code: "queue_timeout"` |
| 9    | timeout fires | SIGTERM→SIGKILL, `error_code: "timeout"` |
| 9    | `/cancel` arrives | `error_code: "job_interrupted"` |
| 10   | license init failed | `error_code: "license_error"` |
| 14   | worker exits != 0 | `error_code: "stata_crash"`  |
| 14   | 3rd crash in 10 min | trips breaker → `shim_degraded` |

---

## 3. Path mapping

Question: I write `.do` paths inside the container; the host runs
Stata. How do those paths line up?

```mermaid
flowchart TB
    subgraph C["Container (Linux)"]
        direction LR
        CWS["PYSTATA_SHIM_CONTAINER_WORKSPACE<br/>/workspace"]
        CFILE["/workspace/analysis/main.do"]
    end

    subgraph H["Host (macOS)"]
        direction LR
        HWS["PYSTATA_SHIM_HOST_WORKSPACE<br/>/Users/you/proj"]
        HFILE["/Users/you/proj/analysis/main.do"]
    end

    CFILE -->|client _resolve_container_path| REL["relative path<br/>analysis/main.do"]
    REL -->|client _container_to_host| HFILE

    CWS <-.same bytes via bind mount.-> HWS
    CFILE <-.same bytes via bind mount.-> HFILE

    HFILE -->|client builds| DOCODE["cd &quot;/Users/you/proj/analysis&quot;<br/>do &quot;/Users/you/proj/analysis/main.do&quot;"]
    DOCODE -->|sent in /do body| RUN["host Stata runs<br/>against host paths"]

    classDef container fill:#dbeafe,stroke:#1d4ed8
    classDef host fill:#dcfce7,stroke:#15803d
    classDef derived fill:#fef9c3,stroke:#a16207
    class CWS,CFILE container
    class HWS,HFILE,RUN host
    class REL,DOCODE derived
```

Three rules this enforces:

1. The client only translates paths under
   `PYSTATA_SHIM_CONTAINER_WORKSPACE`. Anything else raises
   `ShimError` — there is no silent fallback to `/tmp` or `/Users`.
2. The container and host workspace paths must be **the same
   bytes through a single bind mount**. Overlapping or nested
   binds are unsupported.
3. The `cd` and `do` lines that reach Stata always use the host
   path. Inside `.do` code, write outputs with paths *relative to
   the working directory*, never absolute container paths like
   `/workspace/output/...`.

---

## 4. State machine

Question: what does `pool_state` mean, and how does it change?

```mermaid
stateDiagram-v2
    [*] --> uninitialized: server boots,<br/>Stata init deferred
    uninitialized --> ready: probe succeeds<br/>(PyStata.init OK)
    uninitialized --> uninitialized: probe fails<br/>(license/edition)
    uninitialized --> degraded: 3 worker crashes<br/>in 10 min

    ready --> busy: /do request<br/>acquires slot
    busy --> ready: worker exits<br/>(success or per-job error)
    busy --> degraded: 3rd crash trips breaker

    ready --> degraded: 3rd crash trips breaker
    degraded --> ready: cooldown<br/>(default 5 min)<br/>expires + probe OK
    degraded --> degraded: cooldown<br/>still active

    note right of busy
        /health returns:
        ok: true
        pool_state: "busy"
        + breaker_tripped if both
    end note

    note right of degraded
        /do returns immediately:
        error_code: "shim_degraded"
        no worker spawned
    end note
```

What unauthenticated `/health` shows in each state:

| state          | unauth `/health`                                            |
|----------------|-------------------------------------------------------------|
| uninitialized  | `{"ok": false, "pool_state": "uninitialized", "queue_depth": N}` |
| ready          | `{"ok": true,  "pool_state": "ready",         "queue_depth": N}` |
| busy           | `{"ok": true,  "pool_state": "busy",          "queue_depth": N}` |
| degraded       | `{"ok": false, "pool_state": "degraded",      "queue_depth": N}` |

Authenticated `/health` adds `edition`, `stata_version_number` when
known, `error_code`, `init_error`, and `current_job_id` (when busy).

Thresholds are env-tunable:
`PYSTATA_SHIM_DEGRADE_CRASHES` (3),
`PYSTATA_SHIM_DEGRADE_WINDOW_SEC` (600),
`PYSTATA_SHIM_DEGRADE_COOLDOWN_SEC` (300).

---

## 5. Auth flow

Question: how does the shared-secret token get from generation
to the comparison on every request?

```mermaid
flowchart TB
    SETUP["bash setup_host.sh"]
    GEN["openssl rand -hex 32<br/>(or python secrets.token_hex)"]
    TOKENFILE["~/.pystata-shim/token<br/>chmod 600"]
    APPBOOT["app.py boot:<br/>_load_token()"]
    EXPECTED["_EXPECTED_TOKEN<br/>(in-memory)"]

    SETUP --> GEN
    GEN --> TOKENFILE
    TOKENFILE -.read once at boot.-> APPBOOT
    APPBOOT -->|"refuse if<br/>< 32 chars OR<br/>any group/other bit"| EXPECTED

    USER["user copies token<br/>into container env"]
    CENV["PYSTATA_SHIM_TOKEN<br/>(in container env)"]
    CALL["pystata_shim._token()"]
    HEADER["X-Shim-Token: &lt;value&gt;"]

    TOKENFILE -.cat'd at runtime.-> USER
    USER --> CENV
    CENV --> CALL
    CALL --> HEADER
    HEADER -->|HTTP request| APPCHECK

    APPCHECK["app.py _check_token():<br/>secrets.compare_digest(<br/>header, _EXPECTED_TOKEN)"]
    EXPECTED --> APPCHECK
    APPCHECK -->|mismatch or absent| FAIL["HTTP 401"]
    APPCHECK -->|match| PASS["proceed to handler"]

    WORKER["spawn worker.py"]
    PASS --> WORKER
    WORKER -.PYSTATA_SHIM_TOKEN scrubbed from env.-> WSTATA["worker has no token<br/>in its environment"]

    classDef gen fill:#fef3c7,stroke:#a16207
    classDef trust fill:#fee2e2,stroke:#991b1b
    classDef pass fill:#dcfce7,stroke:#15803d
    class GEN,TOKENFILE,SETUP gen
    class EXPECTED,APPCHECK,USER,CENV trust
    class PASS,WSTATA pass
    class FAIL trust
```

Boot-time refusals enforced in `_load_token()`:

- **token unset** → `sys.exit(2)`, server never binds the port.
- **token < 32 chars** → `sys.exit(2)`.
- **token file has any group/other permission bit** →
  `sys.exit(2)` with a `chmod 600` hint.

Per-request:

- comparison is `secrets.compare_digest` (constant-time).
- mismatch returns 401 before any handler logic runs.
- successful auth proceeds to the typed pydantic body parser.

Per-spawn:

- the worker subprocess inherits *every* env var **except**
  `PYSTATA_SHIM_TOKEN`. If an operator explicitly enables
  `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1` and a `.do` file runs `shell env`,
  the token still is not present in that child process environment.

---

## Where these diagrams come from in the code

| diagram | source files                                              |
|---------|-----------------------------------------------------------|
| 1       | `host/app.py`, `host/worker.py`, `client/pystata_shim.py` |
| 2       | `host/app.py` (`StataSupervisor._invoke_worker`, `_finalize`) |
| 3       | `client/pystata_shim.py` (`_resolve_container_path`, `_container_to_host`, `_stata_quote`, `run_do_file`) |
| 4       | `host/app.py` (`StataSupervisor.status`, `_record_crash`, `_is_degraded`) |
| 5       | `host/app.py` (`_load_token`, `_check_token`), `host/scripts/setup_host.sh` |
