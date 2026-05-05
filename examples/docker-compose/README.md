# examples/docker-compose

Compose file for bringing up a single service wired to the host
`pystata-shim`. Equivalent to `examples/docker-run/` but expressed
declaratively so it is easier to copy into a real project.

```bash
# Provide the token at apply time. Do not bake it into the Compose file.
cd examples/docker-compose
export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"
export PYSTATA_SHIM_HOST_WORKSPACE="$(cd ../.. && pwd)"

docker compose up --abort-on-container-exit
```

The token stays in your shell environment rather than in the Compose
file itself. The Compose file uses name-only environment passthrough
for `PYSTATA_SHIM_TOKEN`; avoid `docker compose config` while the
token is exported, because that diagnostic command can render
environment values to stdout.

The service:

- mounts the current working directory at `/workspace`
- mounts this repo's `client/` at `/opt/pystata-shim-client`
- resolves `host.docker.internal` via `host-gateway`
- runs a tiny health probe and a canonical regression, then exits

If you are running on Docker Desktop or OrbStack, you can remove the
`extra_hosts` entry — the mapping resolves by default on those
runtimes.
