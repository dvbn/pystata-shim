# pystata-shim

Run Stata from a Linux Docker container while Stata stays installed on
your Mac. There are probably better/simpler ways to achieve this but it works well for me. 

`pystata-shim` is a small localhost service for researchers and
developers who use containerized coding agents, scripts, or notebooks
but need to execute Stata `.do` files on a licensed macOS Stata
installation. It is designed for workflows such as replication work,
automated empirical checks, and agent-assisted Stata development where
the container should be able to run Stata code without having direct
access to the host shell.

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
bash host/scripts/setup_host.sh
bash host/scripts/healthcheck.sh
bash examples/docker-run/run.sh
```

The setup script installs the host shim under `~/.pystata-shim/`,
creates a launch agent, generates a token, and waits for authenticated
health to succeed.

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

For a slower walkthrough, start with
[docs/getting-started.md](docs/getting-started.md).

## Using It From A Container

Mount your project at `/workspace`, mount this repo's `client/`
directory, and pass the token by environment variable name:

```bash
export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"

docker run --rm -it \
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
```

Then run a `.do` file:

```bash
stata-do /workspace/analysis/main.do --cwd /workspace
```

For container details, see [docs/docker.md](docs/docker.md). For
agent-specific rules, see [docs/agent-guidance.md](docs/agent-guidance.md).

## LLM-Assisted Setup

If you want an LLM or coding agent to perform the setup for you, use
[docs/llm-bootstrap.md](docs/llm-bootstrap.md). It is written as an
operator checklist with verification gates and rollback notes.

There are also short wiring examples for:

- [Claude Code](examples/claude/README.md)
- [Codex-style CLI agents](examples/codex/README.md)

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
