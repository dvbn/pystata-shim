# LLM bootstrap playbook

This doc is written for an **AI coding agent** (Claude Code,
ChatGPT with code execution, Codex, Cursor, Aider, etc.) to set up
`pystata-shim` end-to-end on a user's Mac.

If you are a **human user**: open this in your favorite agent and
say something like:

> "I want to set up pystata-shim on this Mac so my containerized
> agent can run Stata. Follow `docs/llm-bootstrap.md` from the
> pystata-shim repo. Walk through every step, run the verification
> commands at each gate, ask me before any destructive or
> permission-prompting action, and stop if a verification fails."

If you are an **LLM**: read the rest of this file as your
playbook. Each section has a goal, the commands to run, the
verification step, and the failure paths. Do not skip verification
gates. Ask the user before any step marked **CONFIRM FIRST**.

---

## Operating principles

1. **Never** run a command marked `CONFIRM FIRST` without
   explicitly asking the user. Examples: installing system
   software, modifying `~/Library/LaunchAgents`, rotating the
   token, removing files, running `docker run`.
2. Run each verification gate before moving on. If a gate fails,
   stop, surface the error to the user, and propose the most
   targeted fix.
3. The shim must end up bound to `127.0.0.1:8787`. Never bind a
   non-loopback interface unless the user has read
   `docs/security.md` and explicitly opted in.
4. The token in `~/.pystata-shim/token` is a host-RCE credential.
   Do not echo, log, or paste it into chat. Reference it in
   commands as `"$(cat ~/.pystata-shim/token)"` rather than
   pasting its literal value, and never run `cat token` as a
   standalone command — the value will land in your transcript.
5. State, ahead of any destructive command, exactly what the
   command will change. If the user says "go", run it. If they
   don't reply, don't run it.

---

## Gate 0 — Detect the environment

Run these and read the output:

```bash
sw_vers                                # macOS version
uname -m                               # arm64 or x86_64
which python3                          # must exist
python3 --version                      # need 3.10+
ls -la /Applications | grep -i 'Stata\|StataNow'
which docker || which orb || true      # at least one Docker engine
docker --version 2>/dev/null || true
docker info 2>/dev/null | head -30 || true
```

Decide:

- **macOS**: Ventura or later required. If older, stop and tell
  the user.
- **Architecture**: arm64 (Apple Silicon) and x86_64 (Intel) both
  supported.
- **Python 3.10+**: required for the host venv (pydantic v2
  floor). If `python3 --version` is older, stop and tell the user
  to install Python 3.10+ before Gate 1 — `brew install python@3.12`
  is the most common fix.
- **Stata location**: locate the `utilities/` directory. Common
  paths:
  - `/Applications/StataNow/utilities`
  - `/Applications/Stata/utilities`
  - `/Applications/Stata18/utilities`, `/Applications/Stata17/utilities`
- **Stata edition**: the user knows whether they have MP, SE, or
  BE. If they don't, ask. The license string in Stata's
  splash screen says.
- **Docker engine**: if absent, you need to install one in Gate 1.

---

## Gate 1 — Docker engine present (CONFIRM FIRST)

If `docker run --rm hello-world` succeeds, skip to Gate 2.

If not, **CONFIRM FIRST**: tell the user you're about to install a
container engine and let them choose between Docker Desktop and
OrbStack. Then guide them to install one of:

- **Docker Desktop** (https://www.docker.com/products/docker-desktop/)
  — most common, bigger footprint.
- **OrbStack** (https://orbstack.dev/) — lighter on Apple Silicon,
  free for non-commercial.

You cannot install these from a CLI; the user must download and
double-click the `.dmg`. Wait for them to confirm the menu-bar
icon shows "running."

Verification gate:

```bash
docker run --rm hello-world
```

Expected: prints "Hello from Docker!" and exits 0.

---

## Gate 2 — Repo cloned

```bash
test -d ~/Code/pystata-shim || git clone <repo-url> ~/Code/pystata-shim
cd ~/Code/pystata-shim
ls host client docs examples tests
```

If the user already has the repo somewhere else, use that path.
The path doesn't matter; record it as `REPO_PATH_VERIFIED` (the
absolute directory you `cd`'d into and where `host/scripts/setup_host.sh`
exists). Later gates reference this name.

Verification: `host/scripts/setup_host.sh` exists in
`REPO_PATH_VERIFIED`.

---

## Gate 3 — Stata + PyStata reachable

If Gate 0 found no Stata install at all (no `Stata*` directory in
`/Applications`), **STOP HERE**. Tell the user: "I cannot find a
Stata install on this Mac. pystata-shim is a bridge to your local
Stata; without it, there is nothing to install. Please install or
locate your Stata before continuing." Do not proceed to Gate 4.

Otherwise, take the path you found in Gate 0 (do not fall back to
the default) and confirm the `pystata` package is present:

```bash
# Use the path you found in Gate 0:
ls -la "<the-utilities-dir-from-Gate-0>/pystata" | head
```

Examples of what `<the-utilities-dir-from-Gate-0>` might be:
`/Applications/StataNow/utilities`,
`/Applications/Stata/utilities`,
`/Applications/Stata18/utilities`,
`/Applications/Stata17/utilities`.

You should see `__init__.py` (or compiled `.pyc`) inside that
directory. If a Stata install exists but has no `pystata`
package, it predates PyStata. Stop and tell the user.

Remember the directory you confirmed here — call it
`PYSTATA_UTILITIES_VERIFIED`. You will pass that exact path to
Gate 4.

---

## Gate 4 — Run the host setup script (CONFIRM FIRST)

**CONFIRM FIRST.** Tell the user, before running this:

> "I'm going to install the pystata-shim launch agent. This will
> create `~/.pystata-shim/` (about 60 MB), generate a
> 64-hex-character shared secret (32 bytes) in
> `~/.pystata-shim/token` (mode 0600), and
> install a launchd plist at
> `~/Library/LaunchAgents/com.pystata.shim.plist` that runs as
> your user. The script is idempotent. Proceed?"

If they say yes, run it with the values you established earlier:

```bash
cd <REPO_PATH_VERIFIED>          # from Gate 2; e.g. ~/Code/pystata-shim
PYSTATA_UTILITIES="<PYSTATA_UTILITIES_VERIFIED>" \
PYSTATA_EDITION="<EDITION_THE_USER_TOLD_YOU>" \
bash host/scripts/setup_host.sh
```

If you do not have an explicit edition from the user, ask:
"Which edition does your license cover — MP, SE, or BE?" Default
to `mp` only if they confirm they don't know and want you to try.

Verification gate:

```bash
test -s ~/.pystata-shim/token && stat -f "%Lp %N" ~/.pystata-shim/token
test -f ~/Library/LaunchAgents/com.pystata.shim.plist
launchctl print "gui/$(id -u)/com.pystata.shim" | head -20
```

Expected:

- token file exists and shows `600 /Users/<you>/.pystata-shim/token`
- the plist file exists
- `launchctl print` shows `state = running`

---

## Gate 5 — Health check

```bash
bash ~/Code/pystata-shim/host/scripts/healthcheck.sh
```

Expected: a JSON object with `"ok": true` and
`"pool_state": "ready"`.

If `pool_state` is `uninitialized`, get the detail:

```bash
bash ~/Code/pystata-shim/host/scripts/healthcheck.sh --auth
```

The `init_error` field will tell you exactly what's wrong. Most
common:

- `PYSTATA_EDITION` mismatch — user has BE/SE, default is MP.
- `PYSTATA_UTILITIES` wrong path.
- `pystata` import failed because the Stata install is too old.

Fix, re-run Gate 4, re-check.

---

## Gate 6 — End-to-end smoke

This step pulls the `python:3.12-slim` image (~50 MB) on first run.
The client uses Python stdlib HTTP, so the example does not install
Debian packages inside the container. On a slow connection it can
take 2–3 minutes before any output appears.
Tell the user the wait is expected. Do not declare it "hung" until
> 5 minutes of silence.

```bash
bash ~/Code/pystata-shim/examples/docker-run/run.sh
```

Expected: a JSON object with `"ok": true`, `"stata_rc": 0`, and
`"captured": {"e(N)": 74, "e(r2)": <a number near 0.22>}`.

If you see `"ok": false`:

- `error_code: "license_error"` → fix `PYSTATA_EDITION`.
- `error_code: "shim_degraded"` → wait 5 minutes and retry; the
  breaker has tripped from earlier failures.
- transport error (`Connection refused`, timeout, or DNS failure) →
  Gate 5 isn't actually green. Re-check the launch agent and
  `host.docker.internal` mapping.

---

## Gate 7 — Wire the user's real agent (CONFIRM FIRST)

**CONFIRM FIRST.** Before running any `docker run`, ask the user:

- "Which container image does your agent run in?"
  (You'll substitute it for `<your-agent-image>` below.)
- "What absolute path on your Mac is your project workspace?"
  (You'll use that for `PYSTATA_SHIM_HOST_WORKSPACE`. Don't
  default to `$(pwd)` unless you've actually `cd`'d there.)

When you build the `docker run` command, first load the token into
the shell environment with
`export PYSTATA_SHIM_TOKEN="$(cat ~/.pystata-shim/token)"`, then
pass it to Docker as `-e PYSTATA_SHIM_TOKEN` with no `=value`.
Never paste the literal token into a command line. The token must
reach the container via the env var, but it should not appear in
plaintext in your transcript or in host-side process listings.

The `--add-host=host.docker.internal:host-gateway` flag is
harmless on Docker Desktop and OrbStack (where the mapping
already exists) and required on Colima, Rancher Desktop, and
Podman. Include it unconditionally.

Three common agent shapes follow.

### 7a. The user's agent is Claude Code (CLI)

Claude Code already runs in a Docker container in many setups. Add
the four env vars to the container `claude` runs in:

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
    <your-claude-image>
```

Inside that container:

```bash
export PATH=/opt/pystata-shim-client:$PATH
stata-do /workspace/your_file.do --cwd /workspace
```

### 7b. The user's agent is Codex CLI

Same shape, swap the image. Same four env vars. Same
`PATH=/opt/pystata-shim-client:$PATH`.

### 7c. The user is calling the shim from a Python script in any container

The container must have these env vars set (same four as 7a/7b):
`PYSTATA_SHIM_TOKEN`, `PYSTATA_SHIM_HOST_WORKSPACE`,
`PYSTATA_SHIM_CONTAINER_WORKSPACE` (default `/workspace`),
`PYSTATA_SHIM_BASE_URL` (default
`http://host.docker.internal:8787`). Without
`PYSTATA_SHIM_HOST_WORKSPACE`, `run_do_file()` will raise
`ShimError`.

```python
import os, sys
sys.path.insert(0, "/opt/pystata-shim-client")
import pystata_shim as s

assert s.health(), "shim not ready"
result = s.run_do_file("/workspace/analysis.do", caller_tag="my-agent")
print(result["stata_rc"], result["captured"])
```

---

## Common deflections

These are the questions / failure modes you'll see often.

| Symptom                                                                 | First check                                                                                                       | Fix                                                                                                       |
|-------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| `setup_host.sh` exits with "PYSTATA_UTILITIES does not exist"           | Run `ls /Applications/Stata*/utilities/pystata`                                                                   | Set `PYSTATA_UTILITIES` to the directory that does exist.                                                  |
| `/health` returns `pool_state: uninitialized`                            | Authenticated `/health` to read `init_error`                                                                       | Usually edition mismatch; set `PYSTATA_EDITION=se` or `be`.                                               |
| Container cannot resolve `host.docker.internal`                         | Is the user on Colima / Rancher Desktop / Podman?                                                                  | Add `--add-host=host.docker.internal:host-gateway` to the docker run.                                     |
| Container connection is refused or times out                            | Is the launch agent running on the host? `launchctl print "gui/$(id -u)/com.pystata.shim"`                          | If not, re-run `setup_host.sh`.                                                                            |
| `POST /do` returns `401`                                                | Is `PYSTATA_SHIM_TOKEN` inside the container the same as `cat ~/.pystata-shim/token` on the host?                    | Re-export the env var; trailing newlines from `cat` are a common gotcha.                                  |
| Stata says "file not found" but file exists                             | Did the `.do` file use `/workspace/...` paths?                                                                     | Inside `.do` code, use paths relative to `--cwd`, not absolute container paths.                            |
| `error_code: "shim_degraded"`                                           | `tail ~/.pystata-shim/shim.stderr.log`                                                                              | Wait 5 minutes; investigate the underlying crash.                                                          |
| Mac went to sleep, jobs hang                                            | The shim is paused with the OS                                                                                     | Wake the Mac; jobs resume. Long jobs across sleep aren't supported.                                        |
| Docker Desktop "mount denied" on a path outside /Users/                 | File Sharing list                                                                                                  | Settings → Resources → File Sharing → add the path. Then restart Docker Desktop and the container.         |

---

## When NOT to bootstrap

If any of these are true, surface them and stop:

- The user is on Windows. v1 is macOS-only.
- The user is asking you to install on a remote / shared / public-
  facing host. There is no TLS; the shared secret is RCE-equivalent.
  Refer them to `docs/security.md`.
- The user wants to expose the shim to other devices on their LAN.
  Same reason; refer them to `docs/security.md` and require
  `PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1` and an explicit user
  acknowledgement.

(The "no Stata install present" stop is enforced earlier in
Gate 3.)

---

## After a successful bootstrap

Tell the user, in order:

1. The shim is running at `127.0.0.1:8787` and starts at login.
2. Their token is at `~/.pystata-shim/token` (mode `0600`).
3. Their audit log is `~/.pystata-shim/audit.jsonl` and grows
   slowly (one line per finished `.do` request).
4. To rotate the token, `rm ~/.pystata-shim/token && bash host/scripts/setup_host.sh`,
   then update the container env.
5. To uninstall, see the "Cleanup" section in
   `docs/getting-started.md`.

---

## Failure-mode reporting template

If a gate fails and you can't fix it from the user's responses,
report this to them:

> Gate <N> failed at the verification step. The command was:
> `<exact command>`. The output was: `<paste output verbatim>`.
> The most likely cause is `<your hypothesis>`. The next thing to
> try is `<your suggestion>`. Should I try it, or do you want to
> investigate manually?

Don't apply destructive changes (deleting the home dir, force-
unloading the launch agent, deleting the token) without the user
explicitly confirming.
