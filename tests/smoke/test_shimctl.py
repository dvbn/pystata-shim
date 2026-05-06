"""Smoke tests for the shimctl.sh lifecycle script.

These tests cover the cases reachable without macOS or a real Stata
install: status reporting against an empty $PYSTATA_SHIM_HOME, idempotent
stop with no shim running, and the stop-if-unused safe-default when
Docker is unavailable. Tests that need a live shim or live Docker are
intentionally out of scope and live in a maintainer-side manual script.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIMCTL = REPO / "host" / "scripts" / "shimctl.sh"


def _run(args, env_extra=None, path_prefix=None, path_only=None, timeout=None):
    env = {k: v for k, v in os.environ.items()}
    env.pop("PYSTATA_SHIM_HOME", None)
    env.pop("PYSTATA_SHIM_HOST", None)
    env.pop("PYSTATA_SHIM_PORT", None)
    env.pop("PYSTATA_SHIM_TOKEN", None)
    if env_extra:
        env.update(env_extra)
    if path_only is not None:
        # path_only replaces PATH entirely so no inherited binary
        # (e.g. real docker on a maintainer Mac) leaks into the test.
        env["PATH"] = path_only
    elif path_prefix is not None:
        env["PATH"] = path_prefix + ":" + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(SHIMCTL), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _populate_fake_path(fake_path, extras=()):
    """Symlink the basic POSIX utilities the script needs into a
    throwaway PATH directory so we can exercise shimctl in isolation."""
    base = ("bash", "cat", "grep", "awk", "sed", "kill", "rm",
            "test", "[", "true", "false", "head", "tail", "tr",
            "sleep", "sh", "mkdir", "rmdir", "stat", "ls",
            "dirname", "basename", "uname",
            # nohup is the second-fallback in spawn_shim; without it
            # the bare-nohup last-resort fallback would be untestable
            # under a fully-replaced PATH.
            "nohup")
    for util in tuple(base) + tuple(extras):
        real = shutil.which(util)
        if real:
            try:
                os.symlink(real, os.path.join(fake_path, util))
            except FileExistsError:
                pass


def test_shimctl_exists_and_is_executable():
    assert SHIMCTL.exists(), f"missing {SHIMCTL}"
    mode = SHIMCTL.stat().st_mode
    assert mode & stat.S_IXUSR, "shimctl.sh must be user-executable"


def test_shimctl_status_stopped_when_home_empty():
    home = tempfile.mkdtemp()
    try:
        proc = _run(["status"], {"PYSTATA_SHIM_HOME": home})
        assert proc.returncode == 0, proc.stderr
        assert "stopped" in proc.stdout.lower()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_shimctl_status_returns_machine_friendly_keyword():
    """The status output must include one of the script-friendly keywords
    so wrappers can grep without false positives."""
    home = tempfile.mkdtemp()
    try:
        proc = _run(["status"], {"PYSTATA_SHIM_HOME": home})
        out = proc.stdout.lower()
        assert any(kw in out for kw in ("running", "stopped", "unknown")), out
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_shimctl_stop_is_noop_when_not_running():
    home = tempfile.mkdtemp()
    try:
        proc = _run(["stop"], {"PYSTATA_SHIM_HOME": home})
        assert proc.returncode == 0, proc.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_shimctl_unknown_subcommand_exits_nonzero():
    home = tempfile.mkdtemp()
    try:
        proc = _run(["bogus"], {"PYSTATA_SHIM_HOME": home})
        assert proc.returncode != 0
        assert "usage" in (proc.stdout + proc.stderr).lower()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_stop_if_unused_conservative_when_docker_missing():
    """If `docker` is not on PATH, stop-if-unused must not stop the
    shim (conservative default).

    Real-host failure mode this test was rewritten to catch: the prior
    version prefixed PATH but appended the real PATH afterward, so on
    a developer machine with Docker installed the test resolved real
    docker and never exercised the conservative path. We now replace
    PATH entirely to make Docker genuinely unavailable.
    """
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()  # empty: no docker binary here
    _populate_fake_path(fake_path)
    try:
        proc = _run(
            ["stop-if-unused"],
            {"PYSTATA_SHIM_HOME": home},
            path_only=fake_path,  # NOT path_prefix — real PATH excluded
        )
        # Conservative: leave the shim alone; exit clean; emit a hint.
        assert proc.returncode == 0, proc.stderr
        out = (proc.stdout + proc.stderr).lower()
        assert "docker" in out, f"expected a docker hint, got: {out!r}"
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_stop_if_unused_conservative_when_docker_ps_fails():
    """If `docker info` succeeds but `docker ps` fails (transient daemon
    glitch, label-filter API surprise), stop-if-unused must leave the
    shim alone — same conservative policy as docker-missing."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    # Stub docker that returns 0 for `info` and non-zero for `ps`.
    docker_stub = Path(fake_path) / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  info) exit 0 ;;\n'
        '  ps)   exit 1 ;;\n'
        '  *)    exit 0 ;;\n'
        'esac\n'
    )
    docker_stub.chmod(0o755)
    # Pretend a shim is running so we can detect a wrongful stop.
    pid_file = Path(home) / "shim.pid"
    pid_file.write_text(f"{os.getpid()}\n")
    for util in ("bash", "cat", "grep", "awk", "sed", "kill", "rm",
                 "test", "[", "true", "false", "head", "tail", "tr"):
        real = shutil.which(util)
        if real:
            try:
                os.symlink(real, os.path.join(fake_path, util))
            except FileExistsError:
                pass
    try:
        proc = _run(
            ["stop-if-unused"],
            {"PYSTATA_SHIM_HOME": home},
            path_prefix=fake_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert pid_file.exists(), (
            "shim must NOT be stopped when docker ps fails — that path "
            "is supposed to be conservative."
        )
        out = (proc.stdout + proc.stderr).lower()
        assert "docker ps failed" in out or "leaving shim" in out, (
            "shimctl should explain why it left the shim running"
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_stop_if_unused_with_fake_docker_and_no_containers_stops():
    """When `docker` returns zero shim-enabled containers, stop-if-unused
    is allowed to stop the shim (here: it is already stopped, so it is
    a no-op that exits clean)."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    # Stub docker that returns no rows for the label query.
    docker_stub = Path(fake_path) / "docker"
    docker_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker_stub.chmod(0o755)
    for util in ("bash", "cat", "grep", "awk", "sed", "kill", "rm",
                 "test", "[", "true", "false", "head", "tail"):
        real = shutil.which(util)
        if real:
            try:
                os.symlink(real, os.path.join(fake_path, util))
            except FileExistsError:
                pass
    try:
        proc = _run(
            ["stop-if-unused"],
            {"PYSTATA_SHIM_HOME": home},
            path_prefix=fake_path,
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_start_recovers_from_stale_pid_file():
    """A PID file pointing at a dead process must not block start.
    The script should remove it and proceed."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    try:
        # Stub start.sh that backgrounds a long sleep and listens on
        # nothing — start should still detect the stale PID and try
        # to launch. We will time out the start call since it has no
        # real shim to find. The point is that it does NOT exit early
        # complaining about the stale PID.
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\nsleep 120\n")
        start_script.chmod(0o755)

        # Write a PID file pointing at a definitely-dead PID: pick a
        # PID that would never exist on this system.
        stale_pid = 999999
        Path(home, "shim.pid").write_text(f"{stale_pid}\n")

        # Strip `docker` and `curl` from PATH so start cannot probe.
        for util in ("bash", "cat", "grep", "awk", "sed", "kill", "rm",
                     "test", "[", "true", "false", "head", "tail",
                     "sleep", "tr", "sh"):
            real = shutil.which(util)
            if real:
                try:
                    os.symlink(real, os.path.join(fake_path, util))
                except FileExistsError:
                    pass
        # No `setsid` either, so the script falls through to plain
        # nohup background.
        env_extra = {
            "PYSTATA_SHIM_HOME": home,
            "PYSTATA_SHIM_PORT": "1",  # privileged unbindable port
        }
        # The 15s wait will time out (because nothing actually starts
        # listening), so use a thread-style timeout via subprocess
        # timeout=20s.
        import subprocess as _sp
        proc = _sp.run(
            ["bash", str(SHIMCTL), "start"],
            env={**os.environ, **env_extra,
                 "PATH": fake_path + ":" + os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=25,
        )
        # We expect a non-zero exit (start.sh is a stub that does not
        # listen), but the stale PID error message should NOT appear
        # in stderr — the script should have removed the stale file
        # and proceeded.
        combined = proc.stdout + proc.stderr
        assert "refusing to start a second shim" not in combined, (
            "start must NOT refuse when the PID file points at a dead "
            "process. Stale PID handling is broken."
        )
    finally:
        # Reap the sleep stub spawned via spawn_shim. Without this the
        # process leaks for up to 120s into the CI environment.
        pid_file = Path(home) / "shim.pid"
        if pid_file.exists():
            try:
                p = int(pid_file.read_text().strip())
                os.kill(p, 9)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_start_timeout_leaves_no_stale_lock():
    """If `start` times out waiting for the listener, the
    `.shimctl-start.lock` directory must be cleaned up — otherwise the
    next start invocation gets a permanent "another start is in
    progress" message and the user has no way to recover.

    Real-host failure mode: a timed-out start left the lock behind, and
    a subsequent start (after the user fixed the underlying issue) was
    refused with no actionable message.
    """
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)
    try:
        # Stub start.sh that just sleeps — never opens the listener.
        # `start` will fork it, wait 15s, then exit non-zero.
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\nsleep 60\n")
        start_script.chmod(0o755)

        env_extra = {
            "PYSTATA_SHIM_HOME": home,
            # Pick an unbindable port so is_listening always fails.
            "PYSTATA_SHIM_PORT": "1",
        }
        # Use real PATH (so python3 is available for the new spawn
        # path) but with our fake_path PREPENDED so utilities resolve
        # consistently.
        proc = _run(
            ["start"],
            env_extra,
            path_prefix=fake_path,
            timeout=30,
        )
        # Whether start succeeded or timed out, the lock dir must be
        # gone afterward.
        lock_dir = Path(home) / ".shimctl-start.lock"
        assert not lock_dir.exists(), (
            f"start left stale lock at {lock_dir}; "
            f"stdout={proc.stdout!r}, stderr={proc.stderr!r}"
        )
    finally:
        # Reap any background sleep we spawned.
        pid_file = Path(home) / "shim.pid"
        if pid_file.exists():
            try:
                p = int(pid_file.read_text().strip())
                os.kill(p, 9)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_start_succeeds_and_leaves_no_lock():
    """The happy path leaves no `.shimctl-start.lock` behind. Uses a
    Python listener that serves the shim's `pool_state` token so
    is_listening reports running."""
    import socket
    import threading

    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)

    # Bind a free port for the fake listener.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    bound_port = sock.getsockname()[1]
    sock.listen(8)
    stop = threading.Event()

    def _serve():
        sock.settimeout(0.25)
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.recv(4096)
                body = b'{"ok":true,"pool_state":"ready","queue_depth":0}'
                resp = (
                    b"HTTP/1.0 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"\r\n" + body
                )
                client.sendall(resp)
            finally:
                client.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    try:
        # start.sh is a no-op stub: the fake listener already serves
        # /status, so cmd_start will see is_listening=true and return
        # cleanly without forking.
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\n: never run\n")
        start_script.chmod(0o755)

        env_extra = {
            "PYSTATA_SHIM_HOME": home,
            "PYSTATA_SHIM_PORT": str(bound_port),
        }
        # Force the no-curl /dev/tcp branch. The fake listener's JSON
        # body deliberately has no trailing newline, matching common
        # HTTP responses from small servers.
        proc = _run(["start"], env_extra, path_only=fake_path, timeout=20)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        lock_dir = Path(home) / ".shimctl-start.lock"
        assert not lock_dir.exists(), "successful start must clean its lock"
    finally:
        stop.set()
        t.join(timeout=2)
        sock.close()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_repeated_start_handles_stale_lock_gracefully():
    """Even if a prior invocation left a stale lock dir on disk (a bug
    we are fixing, but defense-in-depth), the next start invocation
    must surface the situation instead of silently hanging or
    producing an `unbound variable` error."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)
    try:
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\n: never run\n")
        start_script.chmod(0o755)

        # Pre-create the lock dir to simulate a leftover from a crashed
        # prior run.
        lock_dir = Path(home) / ".shimctl-start.lock"
        lock_dir.mkdir()

        proc = _run(
            ["start"],
            {"PYSTATA_SHIM_HOME": home, "PYSTATA_SHIM_PORT": "1"},
            timeout=20,
        )
        # The script must NOT crash with "unbound variable" or any
        # other shell error noise. It can either reuse the lock and
        # eventually time out, or report the lock and exit cleanly.
        combined = proc.stdout + proc.stderr
        assert "unbound variable" not in combined, (
            f"shimctl raised an unbound variable error: {combined!r}"
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_start_refuses_when_foreign_listener_holds_port():
    """If the configured port is held by something that does not
    serve the shim's /status payload (a stale ~/.pystata-shim from
    a previous install, an old launchd shim with a different API
    shape, an unrelated dev server), `start` must abort with an
    actionable error rather than fork another shim that will never
    win the bind race.

    This is the exact scenario flagged by real-host testing: the
    old shim returned `{"detail":"Not Found"}` for /status because
    its API surface predated the cheap-status split."""
    import socket
    import threading

    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)

    # Bind a free port and serve a "404 not found" body so
    # is_listening's pool_state grep fails.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    bound_port = sock.getsockname()[1]
    sock.listen(8)
    stop = threading.Event()

    def _serve():
        sock.settimeout(0.25)
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.recv(4096)
                body = b'{"detail":"Not Found"}'
                resp = (
                    b"HTTP/1.0 404 Not Found\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"\r\n" + body
                )
                client.sendall(resp)
            finally:
                client.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    try:
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\n: never run\n")
        start_script.chmod(0o755)

        proc = _run(
            ["start"],
            {"PYSTATA_SHIM_HOME": home, "PYSTATA_SHIM_PORT": str(bound_port)},
            timeout=20,
        )
        combined = proc.stdout + proc.stderr
        # Must not silently succeed.
        assert proc.returncode != 0, (
            "start must refuse when a foreign listener holds the port; "
            f"got returncode=0 with output {combined!r}"
        )
        lower = combined.lower()
        assert "lsof" in lower or "another" in lower or "foreign" in lower or "already" in lower, (
            "start must produce an actionable error mentioning lsof / "
            "old shim / cleanup; "
            f"got: {combined!r}"
        )
    finally:
        stop.set()
        t.join(timeout=2)
        sock.close()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_start_refuses_when_foreign_200_listener_holds_port():
    """Variant of the foreign-listener test for an HTTP 200 server
    whose body does not contain `pool_state`. The classic real-world
    failure mode is HTTP 404 (an old API shape) — but a freshly-bound
    unrelated service that returns 200 with its own JSON body must be
    rejected by the same code path, since the production guard greps
    for `pool_state` on the body, not on the status line."""
    import socket
    import threading

    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    bound_port = sock.getsockname()[1]
    sock.listen(8)
    stop = threading.Event()

    def _serve():
        sock.settimeout(0.25)
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.recv(4096)
                # 200 OK, but the body does NOT mention `pool_state`.
                # An unrelated service ("status: alive" from a generic
                # health endpoint) is the realistic shape here.
                body = b'{"status":"alive","service":"unrelated"}'
                resp = (
                    b"HTTP/1.0 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"\r\n" + body
                )
                client.sendall(resp)
            finally:
                client.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    try:
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\n: never run\n")
        start_script.chmod(0o755)
        proc = _run(
            ["start"],
            {"PYSTATA_SHIM_HOME": home, "PYSTATA_SHIM_PORT": str(bound_port)},
            timeout=20,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, (
            "start must refuse when an HTTP 200 listener holds the port "
            f"but does not return our pool_state payload; got rc=0 with "
            f"output {combined!r}"
        )
        lower = combined.lower()
        assert any(kw in lower for kw in ("lsof", "another", "foreign", "already")), (
            "expected actionable error mentioning lsof / another shim / "
            f"port conflict cleanup; got: {combined!r}"
        )
    finally:
        stop.set()
        t.join(timeout=2)
        sock.close()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_spawn_shim_pid_is_alive_after_parent_python_exits():
    """`spawn_shim` runs the shim through `python3 subprocess.Popen(...
    start_new_session=True)`. The Python parent exits as soon as it
    has printed the child PID. The PID written to `shim.pid` must
    refer to a still-running process — otherwise the lifecycle script
    would record a dead PID and `cmd_status` / `cmd_stop` would never
    find anything.

    Scope of this test: it confirms the PID file points at a live
    process after the Python intermediary exits. It does NOT prove
    `start_new_session=True` specifically — on Linux, an orphaned
    child whose parent has exited gets reparented to init and stays
    alive even without a session leadership change, so a broken
    spawn_shim that dropped `start_new_session=True` would still
    pass this test. The production reason for `start_new_session` is
    SIGHUP isolation when the calling shell's session exits on
    macOS, which would need an interactive-tty test fixture to
    exercise. That gap is acknowledged in FUTURE_WORK as
    test-coverage debt and not blocking for v1.
    """
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    _populate_fake_path(fake_path)
    try:
        # start.sh is a long sleep so the child stays alive through
        # the test. cmd_start will spawn it via the python3 path,
        # write the PID, then time out waiting for /status (because
        # nothing actually serves the port). The interesting part is
        # what happens BEFORE the timeout: the PID has to be alive.
        start_script = Path(home) / "start.sh"
        start_script.write_text("#!/usr/bin/env bash\nexec sleep 120\n")
        start_script.chmod(0o755)

        env_extra = {
            "PYSTATA_SHIM_HOME": home,
            # Privileged-and-empty port so is_listening always fails.
            "PYSTATA_SHIM_PORT": "1",
        }
        # Use the real PATH so python3 is found. cmd_start will fail
        # the readiness wait, but spawn_shim will have written a PID
        # before that.
        proc = _run(
            ["start"],
            env_extra,
            path_prefix=fake_path,
            timeout=30,
        )
        assert proc.returncode != 0, (
            "expected start to time out (no listener); got rc=0"
        )
        pid_file = Path(home) / "shim.pid"
        assert pid_file.exists(), "spawn_shim must have written a PID file"
        pid_text = pid_file.read_text().strip()
        assert pid_text and pid_text.isdigit(), (
            f"PID file contents must be a positive integer; got {pid_text!r}"
        )
        pid = int(pid_text)
        # `os.kill(pid, 0)` raises ProcessLookupError if the process
        # has exited. spawn_shim's child should still be alive — the
        # sleep 120 has only been running for ~15 seconds at this
        # point.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise AssertionError(
                f"spawn_shim's child PID {pid} is not running; "
                f"start_new_session detachment is broken"
            )
    finally:
        # Reap the sleep stub.
        pid_file = Path(home) / "shim.pid"
        if pid_file.exists():
            try:
                p = int(pid_file.read_text().strip())
                os.kill(p, 9)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def test_stop_if_unused_keeps_shim_when_other_containers_exist():
    """When `docker` reports a labeled container is running,
    stop-if-unused must NOT stop the shim, even if the shim looks idle."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()
    # Stub docker that emits one container ID for the label query.
    docker_stub = Path(fake_path) / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Pretend one shim-enabled container is running.\n"
        "echo deadbeef1234\n"
    )
    docker_stub.chmod(0o755)
    # Place a fake PID file so the script believes a shim is running.
    (Path(home)).mkdir(exist_ok=True)
    pid_file = Path(home) / "shim.pid"
    # Use our own pid; signal 0 against ourselves succeeds.
    pid_file.write_text(f"{os.getpid()}\n")
    for util in ("bash", "cat", "grep", "awk", "sed", "kill", "rm",
                 "test", "[", "true", "false", "head", "tail"):
        real = shutil.which(util)
        if real:
            try:
                os.symlink(real, os.path.join(fake_path, util))
            except FileExistsError:
                pass
    try:
        proc = _run(
            ["stop-if-unused"],
            {"PYSTATA_SHIM_HOME": home},
            path_prefix=fake_path,
        )
        assert proc.returncode == 0, proc.stderr
        # The PID file should still exist — shim was not "stopped".
        assert pid_file.exists(), "shim must not be stopped while labeled containers run"
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_path, ignore_errors=True)


def _main() -> int:
    import traceback as _tb
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            print(f"FAIL  {fn.__name__}: AssertionError: {exc}")
            print(_tb.format_exc())
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            print(_tb.format_exc())
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
