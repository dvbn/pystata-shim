# Getting started — zero Docker assumed

This page walks you from "I have a Mac with Stata" to "an agent
running in a Linux container can run my `.do` files." If you've
never touched Docker, this is the right page.

If you've used Docker before and just want the API contract, jump
to [setup-macos.md](setup-macos.md) and [docker.md](docker.md).

If you'd rather have an LLM type these commands for you, paste
[llm-bootstrap.md](llm-bootstrap.md) into your favorite agent and
read along here as it works.

---

## What you should already have

- A Mac (Apple Silicon or Intel) running macOS Ventura or later.
- A licensed Stata install with PyStata available. StataNow is the
  default in the setup script; other macOS Stata installs usually only
  need a `PYSTATA_UTILITIES` override.
- About an hour the first time, mostly downloads (Docker is the
  big one).
- Python 3.10 or later in `python3` on your `PATH`. The shim's
  venv installs `pydantic>=2`, which pins the floor.

You do **not** need to know what a Dockerfile is, what an image
is, or what `host.docker.internal` is. This page explains each as
it comes up.

---

## What you'll have at the end

- A small **HTTP shim** that runs on your Mac while a container
  session is up. It exposes a Stata "run this code" service on
  `127.0.0.1:8787` only — nothing outside your Mac can reach it.
  You start it for a session with `host/scripts/shimctl.sh start`
  and stop it with `shimctl.sh stop` (or, in the example wrappers,
  the container's stop recipe handles that for you).
- A **shared-secret token** in `~/.pystata-shim/token` (mode `0600`)
  that authenticates calls to that service.
- A **Docker container** of your choice (we use a one-line example
  here; you'll usually have your own agent image) that can run a
  command like `stata-do /workspace/analysis.do` and have it
  execute on your Mac's Stata.

---

## Mental model in 90 seconds

You don't need to read this section to follow the steps below, but
it makes failures easier to debug.

A **container** is a small isolated Linux environment that runs on
top of macOS. It has its own filesystem, its own processes, and
its own network namespace. You start one with `docker run`. You
share files with it through a **bind mount**: a directory on your
Mac that the container sees at a different path. By convention,
agents read and write under `/workspace` inside the container,
which corresponds to some directory like `/Users/you/myproject` on
your Mac.

Why we need a shim: Stata is licensed per machine and per OS. Your
Stata sits on macOS at `/Applications/StataNow` (or `/Applications/Stata`).
The container is Linux, has no Stata, and cannot get one. So we
run Stata on the Mac and let the container talk to it over HTTP.

The HTTP service we run on the Mac is the **shim**. The container
reaches the Mac through a special hostname `host.docker.internal`
that Docker Desktop and OrbStack provide for exactly this case: a
container saying "talk to whatever is listening on the host's
loopback interface". We bind the shim to `127.0.0.1:8787` so it
can never accidentally be reached by another machine.

When the agent inside the container runs `stata-do file.do`, the
client inside the container takes the file path, rewrites
`/workspace/...` into the matching path on your Mac
(`/Users/you/myproject/...`), wraps it in `cd "..."; do "..."`,
sends it through the shim, the shim runs it through PyStata, and
returns whatever Stata printed plus any results you asked for.

That's the whole picture.

---

## Step 1 — Install Docker Desktop or OrbStack

You need exactly one of these. They both provide the Docker engine
and the `host.docker.internal` mapping.

### Docker Desktop (the most common choice)

1. Visit https://www.docker.com/products/docker-desktop/
2. Download the Mac installer for your chip (Apple Silicon or
   Intel — the page autodetects).
3. Open the `.dmg`, drag Docker into Applications, launch Docker.
4. The first launch shows a tutorial — you can skip it.
5. Wait for the Docker icon in the menu bar to stop animating.
   It should say "Docker Desktop is running".

Verify it works:

```bash
docker run --rm hello-world
```

You should see "Hello from Docker!" and a short message. If the
command hangs for more than a minute, Docker Desktop is not yet
ready — wait for the menu bar icon to settle.

> **Common first-time issue:** Docker Desktop refuses to start
> because virtualization is disabled in macOS Privacy & Security
> settings. The installer will tell you. Granting access requires
> a reboot.

### OrbStack (lighter, free for non-commercial)

1. Visit https://orbstack.dev/
2. Download, drag to Applications, launch.
3. OrbStack is significantly faster on Apple Silicon and uses less
   RAM than Docker Desktop, which matters because Stata can also
   want a lot of RAM.

Verify with `docker run --rm hello-world` (OrbStack ships a
compatible CLI).

### Why this matters for the shim

`host.docker.internal` is the magic hostname that tells a container
"connect to whatever is on the host's loopback interface." Docker
Desktop and OrbStack provide it automatically. **Colima** and
**Rancher Desktop** users need an extra `--add-host` flag on each
`docker run` (covered in [docker.md](docker.md)).

---

## Step 2 — Clone this repo

```bash
git clone <this-repo-url> ~/Code/pystata-shim
cd ~/Code/pystata-shim
```

(The path doesn't matter; pick anything you'll remember.)

---

## Step 3 — Prepare the host shim

This is one command, and it just gets the host runtime ready — it
does **not** start a daemon. You start the shim per session yourself
in the next step (or your container wrapper does it for you):

```bash
bash host/scripts/setup_host.sh
```

You should see a header showing where the shim home is, the bind
address, the edition, and the utilities path; five numbered steps;
and a final block of next-step commands ending in `bash
host/scripts/shimctl.sh start`.

This script:

1. Creates `~/.pystata-shim/` (your shim's home directory).
2. Copies the FastAPI shim files into that directory.
3. Creates a small Python virtualenv there with `fastapi`,
   `uvicorn`, and `pydantic` installed (only ~30 MB).
4. Generates a 64-hex-character shared secret (32 bytes) in
   `~/.pystata-shim/token` with mode `0600`.
5. Confirms the lifecycle script (`host/scripts/shimctl.sh`) and
   `start.sh` are present and prints what to run next.

There is no launchd plist installed by default. The shim is a
**container-session service**: it runs while a container needs it
and stops when the container is done. If you specifically want a
launchd-managed install, re-run with `INSTALL_LAUNCH_AGENT=1` —
even then, the agent ships without `RunAtLoad`, so it does not
start at login. See [setup-macos.md](setup-macos.md#opt-in-install-a-launch-agent).

> **If your Stata isn't StataNow:** stop, set the env var inline
> (no `export`) and re-run. The script is idempotent.
>
> ```bash
> PYSTATA_UTILITIES=/Applications/Stata/utilities \
>     bash host/scripts/setup_host.sh
> ```
>
> **If your Stata is BE or SE rather than MP:** stack the env var
> the same way:
>
> ```bash
> PYSTATA_EDITION=se PYSTATA_UTILITIES=/Applications/Stata/utilities \
>     bash host/scripts/setup_host.sh
> ```
>
> Either form works as `export VAR=value` first, then a bare
> `bash host/scripts/setup_host.sh`. Pick whichever you find
> easier to read.

---

## Step 4 — Start the shim and verify it is alive

Bring it up:

```bash
bash host/scripts/shimctl.sh start
```

The script forks the shim into the background, writes its PID to
`~/.pystata-shim/shim.pid`, and waits up to 15 seconds for the
HTTP listener to come up. On success it prints the URL and the PID.

Now check it:

```bash
bash host/scripts/healthcheck.sh
```

Expected output (the shape may vary while Stata initializes):

```json
{"ok": true, "pool_state": "ready", "queue_depth": 0}
```

If `pool_state` says `uninitialized`, run the authenticated
healthcheck to see the failure detail:

```bash
bash host/scripts/healthcheck.sh --auth
```

The most common reason this fails the first time:
`PYSTATA_UTILITIES` points at the wrong directory, or
`PYSTATA_EDITION` doesn't match your license. Fix the env var,
re-run `setup_host.sh`, then `shimctl.sh stop && shimctl.sh start`.

You can also check the shim's lifecycle state any time:

```bash
bash host/scripts/shimctl.sh status
```

The first whitespace-separated token of the output is one of
`running`, `stopped`, or `unknown`, so you can grep it from a
script.

---

## Step 5 — Run a canonical regression from a container

This is the smallest possible end-to-end check. From the repo
root:

```bash
bash examples/docker-run/run.sh
```

This pulls `python:3.12-slim` (a tiny base image), labels the
container with `com.pystata.shim.enabled=true`, bind-mounts the
current directory at `/workspace`, sets the four env vars the
client needs, and runs:

```python
import pystata_shim as s
res = s.do("sysuse auto, clear\nreg price mpg",
           capture=["e(N)", "e(r2)"], timeout_sec=120)
print(res)
```

If everything is working, you'll see something like:

```json
{
  "ok": true,
  "stata_rc": 0,
  "captured": { "e(N)": 74, "e(r2)": 0.219... },
  "duration_sec": 1.2
}
```

(The exact `e(r2)` should be near 0.22 — about 22% of variance in
auto prices explained by mileage. Stata builds may shift the last
few digits.)

That's the whole loop. Container ran a Python script. Python
script sent `.do` code to your Mac's Stata via HTTP. Stata ran the
regression. Captured `e(N)` and `e(r2)` came back to the
container. The example also calls `shimctl.sh stop-if-unused` on
exit, so when no other labeled container is running the shim
shuts down cleanly.

---

## Step 6 — Wire your real agent

The `examples/docker-run/run.sh` recipe is the template. Copy it
into your project, swap the image (`python:3.12-slim` → your agent
image), and either:

- mount this repo's `client/` into your container at
  `/opt/pystata-shim-client` and put it on `PATH`, or
- vendor the two files (`pystata_shim.py` and `stata-do`) into
  your image so the agent always has them.

Inside the container, your agent should follow the rules in
[agent-guidance.md](agent-guidance.md), in particular:

- Write `.do` files under `/workspace`.
- Run them with `stata-do /workspace/file.do --cwd /workspace`.
- Inside the `.do` file, use paths relative to `--cwd`, not
  absolute container paths.

---

## What if something goes wrong

The single most useful command:

```bash
tail -f ~/.pystata-shim/shim.stderr.log
```

That's where the shim writes its boot errors. If
`shimctl.sh start` fails, this is the first place to look.

Then [troubleshooting.md](troubleshooting.md) has a
symptom-by-symptom flow covering:

- "shim not running at all" (most common)
- `pool_state: uninitialized` (license / edition mismatch)
- Container can't reach `host.docker.internal`
- Container gets `401`
- Stata says "file not found" but the file is there
- Token rotation
- Mac sleep/wake
- Memory footprint

---

## Cleanup

If you decide this isn't for you and want to remove every trace:

```bash
bash host/scripts/shimctl.sh stop
# Only if you ever ran INSTALL_LAUNCH_AGENT=1:
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.pystata.shim.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.pystata.shim.plist
rm -rf ~/.pystata-shim
# If you ever ran sync_stata_mirrors.sh, also:
rm -rf /Users/Shared/stata-mirrors
```

That stops the shim, removes any launch agent you may have opted
into, the plist, the token, the venv, the audit log, and any
optional Stata mirror you may have created. Nothing else on your
system was touched.
