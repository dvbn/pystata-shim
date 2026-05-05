# Justfile container-session lifecycle example

A generic [`just`](https://github.com/casey/just) recipe set you can copy
into your own project to wire a Docker container to the host
`pystata-shim`. The shim runs only while a container session needs it;
when the last labeled container stops, the shim stops too.

## When to use this

Use this example if:

- you already use `just` (or are happy to install it) for project chores
- you want a small, repo-local wrapper around `docker create` / `docker
  start` / `docker stop` that handles host-shim lifecycle without
  thinking about it

If you are new to Docker entirely, start with
[`examples/docker-run/`](../docker-run/), which is one shell script and no
extra tools.

## Setup

1. Run `host/scripts/setup_host.sh` once to install the host runtime.
   You do **not** need `INSTALL_LAUNCH_AGENT=1` — this Justfile starts
   the shim explicitly via `shimctl.sh`.
2. Copy [`Justfile`](./Justfile) into the root of your own project.
3. Edit the four variables at the top:
   - `image` — the Docker image you want to run inside.
   - `container_prefix` — prefix for the names of created containers.
   - `shimctl` — path to `host/scripts/shimctl.sh` in your pystata-shim
     checkout. The default assumes a sibling clone at `../pystata-shim`.
   - `shim_url` — defaults to the macOS-friendly
     `http://host.docker.internal:8787`. On Linux change to the host
     gateway address (e.g. `http://172.17.0.1:8787`).
4. Before running `just create` for the first time, export the
   shared-secret token into your shell:

   ```bash
   export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"
   ```

   The `create` recipe passes the value to docker by environment
   variable name (`-e PYSTATA_SHIM_TOKEN`, no `=value`), so the literal
   token never lands in `ps aux` argv. If `PYSTATA_SHIM_TOKEN` is not
   set, `create` fails fast with the same instruction.

## Recipes

```text
just create <name> [<path>]   # create a labeled container
just start <name>              # start container, start shim if needed
just stop <name>               # stop container, stop shim if last one
just shell <name> [<cmd>...]   # exec into the container
just status                    # report shim status
just destroy <name>            # remove the container, then stop-if-unused
```

The container is labeled `com.pystata.shim.enabled=true` so
`shimctl.sh stop-if-unused` can identify it. Plain `docker stop
<name>` bypasses this Justfile and will leave the shim running. Either
use `just stop`, or run `bash <shimctl> stop-if-unused` yourself
afterward.

## What the recipes do

`just create` creates the container with:

- `--label com.pystata.shim.enabled=true` (so stop-if-unused can find it)
- `-e PYSTATA_SHIM_BASE_URL=...` (where the container looks for the shim)
- `-e PYSTATA_SHIM_CONTAINER_WORKSPACE=/workspace`
- `-e PYSTATA_SHIM_HOST_WORKSPACE="$project_dir"` (host path the
  shim should map back to)
- `-v "$project_dir:/workspace:rw"`
- `--add-host=host.docker.internal:host-gateway`

`just start` starts the container, then asks `shimctl` to start the
shim if the container is labeled. `just stop` stops the container and
asks `shimctl stop-if-unused`, which is conservative when Docker is
unreachable and a no-op when other labeled containers are still up.

## Adapting to a project that already uses `just`

If your project already has a `Justfile`, do not replace it. Instead,
copy the recipes you need into your existing file (`create`, `start`,
`stop`, `shell`) and add the four variables at the top. The recipes are
self-contained; they do not depend on each other.
