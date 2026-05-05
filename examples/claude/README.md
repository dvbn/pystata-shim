# examples/claude

Wiring a Claude Code CLI agent inside a container that talks to the
host `pystata-shim`.

Claude Code is one of several agents this shim has been used with;
nothing in the shim is Claude-specific. Follow the generic recipe in
[`../docker-run/`](../docker-run/) or
[`../docker-compose/`](../docker-compose/) and inject the four
`PYSTATA_SHIM_*` environment variables into the Claude container.

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
    <your-claude-code-image>
```

The `export ...` step keeps the token out of the `docker run` argv.

Inside the container, point the agent at `stata-do`:

```bash
export PATH=/opt/pystata-shim-client:$PATH
export PYTHONPATH=/opt/pystata-shim-client
stata-do /workspace/analysis/main.do --cwd /workspace --tag claude
```

## Agent-facing prompt hints

See [`../../docs/agent-guidance.md`](../../docs/agent-guidance.md)
for the rules an agent needs to follow when producing Stata outputs.
In particular: write `.do` files under `/workspace`, run them with
`stata-do /workspace/file.do --cwd /workspace`, and inside the
`.do` file write outputs using paths relative to the working
directory — not absolute container paths.
