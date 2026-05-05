"""Smoke tests for server boot-time guards.

The server refuses to start when the token is unset, too short, or
stored in a world/group-readable file; and refuses non-loopback binds
unless explicitly allowed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST_DIR = REPO / "host"


def _run_server_boot(env: dict) -> subprocess.CompletedProcess:
    """Import app.py in a subprocess so its sys.exit calls don't poison us."""
    base_env = os.environ.copy()
    # Strip anything that would leak the host's real config.
    for k in list(base_env):
        if k.startswith("PYSTATA_"):
            base_env.pop(k, None)
    base_env.update(env)
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(HOST_DIR)!r})
        import importlib.util
        spec = importlib.util.spec_from_file_location("app", {str(HOST_DIR / 'app.py')!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=base_env,
        capture_output=True,
        text=True,
    )


def test_refuses_when_token_missing():
    home = tempfile.mkdtemp()
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
    })
    assert proc.returncode != 0
    assert "PYSTATA_SHIM_TOKEN" in proc.stderr


def test_refuses_when_token_too_short():
    home = tempfile.mkdtemp()
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
        "PYSTATA_SHIM_TOKEN": "short",
    })
    assert proc.returncode != 0
    assert "32 characters" in proc.stderr or "at least" in proc.stderr


def test_refuses_when_token_file_is_group_readable():
    home = tempfile.mkdtemp()
    token_file = Path(home) / "token"
    token_file.write_text("T" * 64)
    token_file.chmod(0o644)
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
    })
    assert proc.returncode != 0
    assert "chmod 600" in proc.stderr


def test_accepts_good_token_file():
    home = tempfile.mkdtemp()
    token_file = Path(home) / "token"
    token_file.write_text("T" * 64)
    token_file.chmod(0o600)
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
    })
    assert proc.returncode == 0, proc.stderr


def test_refuses_non_loopback_bind_by_default():
    home = tempfile.mkdtemp()
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
        "PYSTATA_SHIM_TOKEN": "T" * 64,
        "PYSTATA_SHIM_HOST": "0.0.0.0",
    })
    assert proc.returncode != 0
    assert "non-loopback" in proc.stderr or "refusing" in proc.stderr


def test_allows_non_loopback_bind_with_opt_in():
    home = tempfile.mkdtemp()
    proc = _run_server_boot({
        "PYSTATA_SHIM_HOME": home,
        "PYSTATA_SHIM_TOKEN": "T" * 64,
        "PYSTATA_SHIM_HOST": "0.0.0.0",
        "PYSTATA_SHIM_ALLOW_NON_LOOPBACK": "1",
    })
    assert proc.returncode == 0, proc.stderr


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
