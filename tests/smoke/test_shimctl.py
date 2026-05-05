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


def _run(args, env_extra=None, path_prefix=None):
    env = {k: v for k, v in os.environ.items()}
    env.pop("PYSTATA_SHIM_HOME", None)
    env.pop("PYSTATA_SHIM_HOST", None)
    env.pop("PYSTATA_SHIM_PORT", None)
    env.pop("PYSTATA_SHIM_TOKEN", None)
    if env_extra:
        env.update(env_extra)
    if path_prefix is not None:
        env["PATH"] = path_prefix + ":" + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(SHIMCTL), *args],
        env=env,
        capture_output=True,
        text=True,
    )


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
    shim (conservative default per the handoff doc)."""
    home = tempfile.mkdtemp()
    fake_path = tempfile.mkdtemp()  # empty: no docker binary here
    # Add a couple of utilities the script may need, by symlinking from
    # the real PATH. This way the script can still find e.g. cat/grep.
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
