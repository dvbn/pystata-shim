# examples/docker-run

A minimal `docker run` recipe for wiring a container to the host
`pystata-shim`.

```bash
./run.sh
```

This script:

1. reads the shared secret from `~/.pystata-shim/token`
2. bind-mounts the current directory to `/workspace` inside the
   container
3. sets the four `PYSTATA_SHIM_*` environment variables
4. mounts `../../client/` into the container at
   `/opt/pystata-shim-client/` so you have `pystata_shim.py` and
   `stata-do.py` available
5. runs a tiny health check + a canonical Stata regression

You can use this recipe as a template; adapt it to your image and
your agent.
