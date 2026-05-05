# Security

`pystata-shim` is a development-time tool. It is not a sandbox, not
a multi-tenant service, and not hardened for public exposure. Read
this page before you deploy it.

## Trust model in one paragraph

Anyone who holds `PYSTATA_SHIM_TOKEN` can submit Stata code to run on
your Mac as your user account. By default the shim rejects direct
host-escape directives such as `shell`, `!`, `xshell`, `python`,
`unix`, `winexec`, `javacall`, and `mata`, including their standard
Stata abbreviations; rejects macro-expanded command names it cannot
inspect safely; and rejects dynamic `do` / `run` / `include`
directives it cannot inspect literally. That materially reduces the
most obvious container-to-host escape path, but it is still
**not a sandbox**:
Stata can read and write files your user can access, and a trusted
operator can opt back into the old behavior with
`PYSTATA_SHIM_ALLOW_UNSAFE_DO=1`. **Treat the shared secret like an
SSH key to your Mac.**

The shim is designed to be:

- **loopback-only by default.** Binding to a non-loopback interface
  requires `PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1`, and even then there
  is no TLS.
- **single-trust.** There is exactly one shared secret and one trust
  level. Every authenticated caller can run, cancel, and inspect
  every job.
- **local-developer-shaped.** No rate limiting, no role-based access
  control, no audit-log integrity protection beyond file permissions.

## Attack surface

1. **Anyone with localhost reachability** can call `GET /health`
   without a token. The unauthenticated payload is deliberately
   three fields (`ok`, `pool_state`, `queue_depth`) with no leaks.
2. **Anyone with the token** can:
   - run validated Stata `.do` code (`POST /do`)
   - read detailed health (`GET /health` with the header)
   - cancel any job, including jobs submitted by other callers
     (`POST /cancel/{job_id}`)
   - inspect job status while it runs (`GET /job/{job_id}`)
3. **Anyone with the same macOS user account** has the same
   capabilities directly. The shim does not try to defend against
   a compromised user.

## What the shim enforces

- `X-Shim-Token` header required on `POST /do`, `GET /job/{id}`, and
  `POST /cancel/{id}`.
- Token comparison uses `secrets.compare_digest` (constant-time).
- Minimum token length of 32 characters. The setup script generates
  a 64-hex-character token (`openssl rand -hex 32`), and the server
  refuses to start with anything shorter than 32 characters.
- The token file (`$PYSTATA_SHIM_HOME/token`) must be mode `0600`.
  Any group or other bits and the server refuses to start.
- `PYSTATA_SHIM_TOKEN` is stripped from the worker subprocess
  environment before `Popen`.
- `do_code` is capped at 1 MiB.
- By default, submitted `do_code` is statically screened before Stata
  starts:
  - direct host-escape directives such as `shell`, `!`, `xshell`,
    `python`, `unix`, `winexec`, `javacall`, and `mata` are rejected,
    including their standard Stata abbreviations
  - macro-expanded command names such as `$cmd ...` or `` `cmd' ... ``
    are rejected because the shim cannot inspect what command they
    resolve to
  - literal `do` / `run` / `include` targets are recursively
    inspected before execution
  - dynamic or non-literal `do` / `run` / `include` directives are
    rejected because the shim cannot inspect them safely
  - hidden Unicode control / format characters are rejected
  - nested `do` / `run` / `include` inspection is bounded to
    16 levels and 1 MiB per inspected file
  - trusted operators can disable this screen with
    `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1`
- `capture` lists are capped at 128 entries and each name must match
  `^[A-Za-z_][A-Za-z0-9_:.()]{0,127}$`.
- `caller_tag` is printable ASCII, ≤128 chars, before it enters the
  JSONL audit log.
- Worker subprocesses are spawned in their own process group so
  `SIGTERM`/`SIGKILL` on cancel/timeout reaches Stata and any child
  processes it spawned.
- Bind is refused on non-loopback addresses unless explicitly
  allowed (no TLS on this transport).

## Trusted Mode

The default mode is intentionally conservative. For fully trusted
setups, you can opt into the older, less restrictive behavior:

```bash
PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 bash host/scripts/setup_host.sh
bash host/scripts/shimctl.sh stop
bash host/scripts/shimctl.sh start
```

The setup script records the mode for the next start; `shimctl.sh
stop && start` (or, in the opt-in launchd path, `launchctl kickstart
-k`) reloads the shim against the new value. In trusted mode, the
static `.do` validator is disabled, so Stata host escapes such as
`shell`, `!`, `xshell`, `python`, `unix`, `winexec`, `javacall`, and
`mata` are allowed. Those commands run on the Mac as your user
account.

Return to restricted mode with:

```bash
PYSTATA_SHIM_ALLOW_UNSAFE_DO=0 bash host/scripts/setup_host.sh
bash host/scripts/shimctl.sh stop
bash host/scripts/shimctl.sh start
```

## Where the token is exposed even when used correctly

Once the token is set on a running container, it lives in that
container's process environment. That means anything with read
access to the host's Docker daemon (or to the running container's
namespace) can extract it:

- `docker inspect <container>` shows env vars to anyone in the
  `docker` group on the host.
- `/proc/<pid>/environ` (Linux) is readable by anyone with the
  matching uid; on Docker Desktop / OrbStack the container's pid
  namespace is firewalled from the host, but other processes in
  the *same* container see each other's env.
- The `examples/docker-run/run.sh` recipe is careful to pass
  `-e PYSTATA_SHIM_TOKEN` (no `=value`) so the literal token does
  **not** appear in `ps aux` on the host. Avoid the `-e KEY=VALUE`
  form whenever the value is sensitive.

These are not bugs in the shim — they are properties of the host
sharing a secret with a container at all. They matter to your
threat model: if anyone but you can `docker exec` into the
container or read its environ, they have RCE on your Mac.

## What the shim does NOT do

- Sandbox `.do` code. Even with direct host-escape directives blocked
  by default, Stata can still `copy`, `erase`, `cd`, open files, and
  write anywhere your user can write. If you enable
  `PYSTATA_SHIM_ALLOW_UNSAFE_DO=1`, direct host escapes such as
  `shell`, `!`, `xshell`, `python`, and `mata` return too.
- Filter or redact Stata `stdout`. Whatever `.do` code prints is
  returned in the response and mirrored to the per-job directory.
  Treat both as sensitive.
- Encrypt the audit log or verify its integrity. The log is a
  user-writable file.
- Rate-limit requests.
- Authenticate the container to the host beyond "presents the
  token". If an attacker can bind `host.docker.internal:8787` from
  inside another container on the same daemon, they can reach the
  shim too.

## Audit log contents

`$PYSTATA_SHIM_HOME/audit.jsonl` is the source of truth for "what
ran". Each line has:

- `ts_utc` — UTC timestamp of the log write
- `job_id`
- `endpoint` (`"/do"`)
- `caller_tag` — verbatim from the request body
- `do_code_sha256` — SHA-256 of the submitted `do_code`
- `duration_sec`
- `stata_rc`, `error_code`, `status`

Full `stdout`/`stderr` of the run are NOT in the audit log; they are
captured in `$PYSTATA_SHIM_HOME/runs/<job_id>/` and retained for
`PYSTATA_SHIM_RUNS_RETENTION_DAYS` (default 7 days). Pruning runs
at server startup.

## Threat-model checklist

- [ ] Token is at least 32 characters; recommended generation is
      `openssl rand -hex 32` (64 hex chars / 32 bytes)
- [ ] Token file is mode `0600`
- [ ] The shim binds loopback only, or you have explicitly accepted
      the non-loopback risk
- [ ] The container that holds the token is itself trusted
- [ ] `PYSTATA_SHIM_ALLOW_UNSAFE_DO` is left unset unless you have a
      fully trusted workflow and explicitly want host escapes,
      macro-selected commands, or dynamic nested do-files back
- [ ] You treat `$PYSTATA_SHIM_HOME/` as sensitive (no world-readable
      contents)
- [ ] You have a plan for rotating the token if it leaks (see
      [setup-macos.md](./setup-macos.md#rotating-the-shared-secret))

If any box is not ticked, the deployment is not safe.

## Reporting issues

Follow the repository's [SECURITY.md](../SECURITY.md). Do not file
live tokens, exploit payloads, or host-specific paths in a public
issue; use private vulnerability reporting if the platform supports
it.
