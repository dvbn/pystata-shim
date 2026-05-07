# Wiring a container to the shim

The container side is deliberately small. You need:

1. Python 3.10 or later in the container.
2. The shim env vars in the container. One is unconditionally
   required, one is required if you use the path-translating
   helpers, and two have defaults.
   - **always required:**
     - `PYSTATA_SHIM_TOKEN` — the same secret your host has
   - **required if you use `run_do_file()` or `stata-do`** (the
     path-translating helpers):
     - `PYSTATA_SHIM_HOST_WORKSPACE` — host path the workspace bind
       comes from
   - **defaulted; override only if you've changed them on the host:**
     - `PYSTATA_SHIM_BASE_URL` (default `http://host.docker.internal:8787`)
     - `PYSTATA_SHIM_CONTAINER_WORKSPACE` (default `/workspace`)
3. A bind mount from the host workspace directory to
   `$PYSTATA_SHIM_CONTAINER_WORKSPACE`.
4. The client files from this repo (`client/pystata_shim.py` and
   optionally `client/stata-do.py` with its sibling `client/stata-do`
   wrapper) mounted or copied into the container where the agent
   can find them. Put the `client/` directory on `PATH` so the
   `stata-do` CLI resolves as a bare command.

   For interactive login shells (`docker exec ... bash -l`), also
   bind-mount `client/profile.d/pystata-shim.sh` to
   `/etc/profile.d/pystata-shim.sh`. Without it, `/etc/profile`
   rewrites `PATH` from scratch on login and drops anything you
   passed via `docker create -e PATH=...`. The example Justfile
   already does this; mirror it in your own wrappers.

## Docker Desktop File Sharing

If your project workspace lives outside your `/Users/` tree (for
example `/opt/work`, `/Volumes/...`, or a custom bind-mount root),
add the directory to Docker Desktop's **Settings → Resources → File
Sharing** list before running any container. Docker Desktop refuses
to bind-mount directories that are not on that list. OrbStack and
Colima generally accept any path your user can read, with no extra
configuration.

## What is `host.docker.internal`

A container has its own network namespace. From inside it,
`localhost` and `127.0.0.1` mean *the container itself*, not your
Mac. To reach a service running on the Mac's loopback interface
you need a different name.

`host.docker.internal` is the conventional DNS name container
runtimes use to mean "the host this container is running on." It's
not a real Internet hostname — it only resolves inside containers.

| Runtime              | `host.docker.internal` works? | Notes                                                                 |
|----------------------|-------------------------------|-----------------------------------------------------------------------|
| Docker Desktop (Mac) | yes, default                  | nothing extra to do                                                   |
| OrbStack             | yes, default                  | nothing extra to do                                                   |
| Colima               | only if you ask               | add `--add-host=host.docker.internal:host-gateway` to your docker run |
| Rancher Desktop      | only if you ask               | same as Colima                                                        |
| Podman (rootless)    | only if you ask               | same as Colima; also `--network slirp4netns:allow_host_loopback=true` |

Test from inside the container:

```bash
python3 - <<'PY'
import urllib.request

with urllib.request.urlopen(
    "http://host.docker.internal:8787/health",
    timeout=15,
) as response:
    print(response.read().decode("utf-8"))
PY
```

You should see a JSON object. If you get DNS errors, your runtime
needs the `--add-host` flag above.

## Minimal docker run

The snippet below mounts `$(pwd)/client` assuming you are standing at
the repo root. Adjust the path if you run it from elsewhere.

The container is labeled `com.pystata.shim.enabled=true` so that
`shimctl.sh stop-if-unused` can identify it as a shim consumer when
deciding whether to stop the host shim. If you skip the label, the
container still works, but `stop-if-unused` cannot account for it
and will treat it as if no labeled container is running.


```bash
bash host/scripts/shimctl.sh start
export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"

docker run --rm -it \
  --label com.pystata.shim.enabled=true \
  -e PYSTATA_SHIM_TOKEN \
  -e PYSTATA_SHIM_HOST_WORKSPACE="$(pwd)" \
  -v "$(pwd):/workspace" \
  -v "$(pwd)/client:/opt/pystata-shim-client:ro" \
  -v "$(pwd)/client/profile.d/pystata-shim.sh:/etc/profile.d/pystata-shim.sh:ro" \
  --add-host=host.docker.internal:host-gateway \
  python:3.12-slim bash -lc '
    python3 -c "import pystata_shim as s; print(s.health_full())"
  '

bash host/scripts/shimctl.sh stop-if-unused
```

The bind-mounted profile snippet is the supported way to keep
`stata-do` on `PATH` and `pystata_shim` on `PYTHONPATH` across login
shells; you no longer need to export them by hand inside the
`bash -lc '...'` body.

`PYSTATA_SHIM_HOST_WORKSPACE` must be the absolute host path that
backs the `/workspace` bind. If you `cd /Users/you/proj && docker run
-v $(pwd):/workspace`, then `PYSTATA_SHIM_HOST_WORKSPACE` should be
`/Users/you/proj`.

See `examples/docker-run/` for a runnable shell script version,
`examples/docker-compose/` for a Compose file, and
`examples/justfile/` for a `just`-driven wrapper that handles
`shimctl start`/`stop-if-unused` for you.

## Optional: mounting the Stata mirrors

If you ran `sync_stata_mirrors.sh` on the host and want the agent to
be able to read Stata's built-in ado/docs:

```bash
docker run ... \
  -v /Users/Shared/stata-mirrors/ado:/stata/ado:ro \
  -v /Users/Shared/stata-mirrors/utilities-pystata:/stata/utilities/pystata:ro \
  -v /Users/Shared/stata-mirrors/docs:/stata/docs:ro \
  ...
```

Nothing in the shim depends on these. They are purely read-only
references for the agent.

## Shared-secret handling in the container

Three OK ways to get the token to the container, in order of
preference:

1. Read from the host token file into your shell env, then pass it to
   Docker by name only:
   `export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"`
   followed by `-e PYSTATA_SHIM_TOKEN`.
   The token sits in the container's environment, which is visible
   to any process inside it. Treat the container as inside the same
   trust boundary as the token.
2. Bind-mount the token file read-only at a known path, and let the
   container read it on demand. Safer across restarts because it is
   not captured in the container's command line.
3. Pass the token through a secrets manager (`docker secret`, a
   Kubernetes secret, or similar) if you are running in a fleet.

Do **not** bake the token into an image.
Also avoid `-e KEY=VALUE` when the value is sensitive; that form can
put the literal token in host-side process listings.

## Path translation contract

The shim runs on the host and receives Stata code that contains host
paths. The client helpers translate container paths to host paths for
you, but only under the following rules:

- `/workspace/...` (container) ⇔ `PYSTATA_SHIM_HOST_WORKSPACE/...` (host)
- A container-side path that does not live under
  `PYSTATA_SHIM_CONTAINER_WORKSPACE` raises an error. If you want to
  run a `.do` file somewhere else on the host, pass the host path
  directly in `do_code` rather than via `run_do_file(...)`.

See [architecture.md](./architecture.md) for the full contract and
the characters that are not safe inside a Stata quoted path.
