# examples/codex

Wiring a Codex-style CLI agent inside a container that talks to the
host `pystata-shim`.

Codex is one of several agents this shim has been used with; nothing
in the shim is Codex-specific. Follow the generic recipe in
[`../docker-run/`](../docker-run/) or
[`../docker-compose/`](../docker-compose/) and inject the four
`PYSTATA_SHIM_*` environment variables into the Codex container.

## Minimum wiring

```bash
export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"

docker run --rm -it \
    -e PYSTATA_SHIM_TOKEN \
    -e PYSTATA_SHIM_HOST_WORKSPACE="$(pwd)" \
    -e PYSTATA_SHIM_CONTAINER_WORKSPACE="/workspace" \
    -e PYSTATA_SHIM_BASE_URL="http://host.docker.internal:8787" \
    -v "$(pwd):/workspace" \
    -v "$(pwd)/client:/opt/pystata-shim-client:ro" \
    --add-host=host.docker.internal:host-gateway \
    <your-codex-image>
```

The `export ...` step keeps the token out of the `docker run` argv.

Inside the container, point Codex at `stata-do`:

```bash
export PATH=/opt/pystata-shim-client:$PATH
export PYTHONPATH=/opt/pystata-shim-client
stata-do /workspace/analysis/main.do --cwd /workspace --tag codex
```

## Agent-facing prompt hints

See [`../../docs/agent-guidance.md`](../../docs/agent-guidance.md)
for the rules an agent needs to follow when producing Stata outputs.
The important ones, in order:

1. Write `.do` files under `/workspace`.
2. Use `stata-do /workspace/file.do --cwd /workspace`.
3. Write outputs with paths relative to `--cwd`, not absolute
   container paths.
4. Check `result["ok"]` and `result["stata_rc"]` before claiming
   success.
