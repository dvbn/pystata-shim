# pystata-shim

Run Stata from a Linux Docker container while Stata stays installed on
your Mac. This is a first step toward MCP-style Stata tooling: it gives
scripts, notebooks, and coding agents a tool bridge to host-side Stata
without exposing your host shell.

`pystata-shim` is a small localhost HTTP service for researchers and
developers who use containerized coding agents, scripts, or notebooks
but need to execute Stata `.do` files on a licensed macOS Stata
installation. It is not an MCP server yet; this release builds the
host-side execution, security, path-mapping, and lifecycle layer that an
MCP adapter can wrap later. The current interface is intentionally
lower-level and works from ordinary containers, shell scripts, Python
code, notebooks, and agent runtimes that do not speak MCP.

The container sends a request to the Mac; the Mac runs Stata through
PyStata and returns stdout, return codes, and selected results.

## What It Does

- Runs Stata on the macOS host, never inside the container.
- Exposes a localhost-only HTTP shim at `127.0.0.1:8787`.
- Requires a shared-secret token for requests that run code or return
  detailed diagnostics.
- Serializes jobs so only one Stata session is active at a time.
- Translates container paths like `/workspace/analysis.do` to the
  matching host path before running Stata.
- Optionally mirrors Stata ado/docs files into a read-only directory
  for container-side inspection.

This repo does not include Stata, Stata binaries, ado files, manuals,
or a Stata license.

## Who It Is For

Use this if:

- You have Stata with PyStata installed on macOS.
- Your coding environment runs in a Linux container.
- You want the container to run `.do` files against your Mac's Stata.

This first version is macOS-focused. It is not a Windows installer, a
Python package, or a general workflow framework.

## Quick Start

From a checkout of this repo on your Mac:

```bash
bash host/scripts/setup_host.sh        # prepare host runtime (no daemon)
bash host/scripts/shimctl.sh start     # start the shim for this session
bash host/scripts/healthcheck.sh --auth
bash examples/docker-run/run.sh        # the example calls shimctl itself
bash host/scripts/shimctl.sh stop-if-unused
```

`setup_host.sh` only prepares files under `~/.pystata-shim/` and writes
a token. It does **not** install a login-time daemon. The shim runs only
while a container session needs it: `shimctl.sh start` brings it up,
`shimctl.sh stop-if-unused` brings it down once no labeled container is
left. `examples/docker-run/run.sh` already wraps both, so for the
canonical regression you only need `setup_host.sh` and the example.

If your Stata install is not at the default StataNow path, set
`PYSTATA_UTILITIES` before setup:

```bash
PYSTATA_UTILITIES=/Applications/Stata/utilities \
    bash host/scripts/setup_host.sh
```

If your license is SE or BE rather than MP, set `PYSTATA_EDITION`:

```bash
PYSTATA_EDITION=se PYSTATA_UTILITIES=/Applications/Stata/utilities \
    bash host/scripts/setup_host.sh
```

If you specifically want a launchd-managed shim (instead of `shimctl`),
re-run setup with `INSTALL_LAUNCH_AGENT=1`. The agent ships **without**
`RunAtLoad`/`KeepAlive`, so it still does not run at login — see
[docs/setup-macos.md](docs/setup-macos.md) for the persistent variant.

For a slower walkthrough, start with
[docs/getting-started.md](docs/getting-started.md).

## Using It From A Container

Mount your project at `/workspace`, mount this repo's `client/`
directory, label the container with `com.pystata.shim.enabled=true`,
and pass the token by environment variable name:

```bash
bash host/scripts/shimctl.sh start
export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"

docker run --rm -it \
  --label com.pystata.shim.enabled=true \
  -e PYSTATA_SHIM_TOKEN \
  -e PYSTATA_SHIM_HOST_WORKSPACE="$(pwd)" \
  -v "$(pwd):/workspace" \
  -v "$(pwd)/client:/opt/pystata-shim-client:ro" \
  --add-host=host.docker.internal:host-gateway \
  python:3.12-slim bash -lc '
    export PATH=/opt/pystata-shim-client:$PATH
    export PYTHONPATH=/opt/pystata-shim-client
    stata-do --health
  '

bash host/scripts/shimctl.sh stop-if-unused
```

Then run a `.do` file:

```bash
stata-do /workspace/analysis/main.do --cwd /workspace
```

If you already drive containers with `just`, copy
[`examples/justfile/Justfile`](examples/justfile/Justfile) into your
project and you get `just create`/`start`/`stop`/`shell` recipes that
handle shim lifecycle for you.

For container details, see [docs/docker.md](docs/docker.md). For
agent-specific rules, see [docs/agent-guidance.md](docs/agent-guidance.md).

## LLM-Assisted Setup

If you want an LLM or coding agent to perform the setup for you, use
[docs/llm-bootstrap.md](docs/llm-bootstrap.md). It is written as an
operator checklist with verification gates and rollback notes.

There are also short wiring examples for:

- [Claude Code](examples/claude/README.md)
- [Codex-style CLI agents](examples/codex/README.md)

## MCP And Future Development

This repo is deliberately shaped as a foundation for an MCP server, but
keeps the first release protocol-agnostic. The host shim handles the
parts an MCP server would still need: finding the macOS Stata install,
keeping the host token private, translating container paths, serializing
jobs, enforcing timeouts and cancels, blocking unsafe host escapes by
default, and managing the shim lifecycle around Docker containers.

An MCP server would be a natural next layer on top of this shim, not a
replacement for it. That adapter can expose the same capabilities as
typed MCP tools while reusing the tested host service and client logic.

Likely future work includes:

- an MCP adapter exposing tools such as `stata_health`,
  `stata_run_code`, `stata_run_do_file`, `stata_job_status`, and
  `stata_cancel_job`;
- packaging improvements for easier install, upgrade, and uninstall on
  macOS;
- richer job history, logs, and diagnostics for long-running empirical
  workflows;
- optional lifecycle helpers for users who stop containers outside the
  provided wrappers;
- more examples for common agent, notebook, and CI workflows;
- modernization of FastAPI startup handling to use lifespan events.

## Security Model

The shared token is powerful: anyone who has it can ask the shim to
run Stata code as your Mac user. The shim reduces accidental exposure
by binding to loopback, requiring a token for mutating endpoints, and
rejecting common host-escape directives by default, but it is not a
complete sandbox.

If you fully trust the container and want the less restrictive mode
where Stata `shell`, `python`, `mata`, and similar host escapes are
allowed, run:

```bash
PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 bash host/scripts/setup_host.sh
```

Read [docs/security.md](docs/security.md) before using this with
untrusted code or untrusted containers.

## Repository Layout

```text
host/                       macOS shim service and setup scripts
client/                     container-side Python client and stata-do CLI
docs/                       setup, architecture, Docker, and security docs
examples/docker-run/        runnable docker run smoke example
examples/docker-compose/    compose equivalent of the docker-run example
examples/justfile/          Justfile wrapper recipes (per-session lifecycle)
examples/claude/            Claude Code container wiring notes
examples/codex/             Codex-style container wiring notes
tests/smoke/                Stata-free smoke tests for the shim layer
```

Important docs:

- [docs/getting-started.md](docs/getting-started.md): first-time setup
- [docs/setup-macos.md](docs/setup-macos.md): host install reference
- [docs/docker.md](docs/docker.md): container wiring
- [docs/architecture.md](docs/architecture.md): service design and API
- [docs/troubleshooting.md](docs/troubleshooting.md): common failures

## Development Checks

Smoke tests do not require Stata:

```bash
python3 -m venv /tmp/pystata-shim-smoke-venv
/tmp/pystata-shim-smoke-venv/bin/python -m pip install -r requirements-smoke.txt
PYTHONDONTWRITEBYTECODE=1 /tmp/pystata-shim-smoke-venv/bin/python -m pytest tests/smoke -q
```

## License

MIT. See [LICENSE](LICENSE).
