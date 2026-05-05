"""Smoke tests for the cheap /status endpoint and the new
container-lifecycle contract: starting the HTTP shim must not initialize
PyStata until an explicit health probe or a real Stata job."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST_DIR = REPO / "host"

TOKEN = "T" * 64


def _load_app_module(extra_env: dict[str, str] | None = None):
    os.environ["PYSTATA_SHIM_TOKEN"] = TOKEN
    os.environ["PYSTATA_SHIM_HOME"] = tempfile.mkdtemp()
    os.environ.pop("PYSTATA_SHIM_HOST", None)
    os.environ.pop("PYSTATA_SHIM_ALLOW_NON_LOOPBACK", None)
    os.environ.pop("PYSTATA_SHIM_ALLOW_UNSAFE_DO", None)
    os.environ.pop("PYSTATA_SHIM_WARMUP", None)
    os.environ.pop("PYSTATA_SHIM_SKIP_WARMUP", None)
    if extra_env:
        os.environ.update(extra_env)
    if str(HOST_DIR) not in sys.path:
        sys.path.insert(0, str(HOST_DIR))
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", HOST_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_status_endpoint_exists_and_returns_minimal_payload():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/status")
    assert r.status_code == 200, r.text
    data = r.json()
    # Must always contain pool_state and ok; the rest is optional detail.
    assert "ok" in data
    assert "pool_state" in data
    # Unauth payload must not leak detail.
    assert "edition" not in data
    assert "init_error" not in data
    assert "stata_version_number" not in data


def test_status_endpoint_does_not_probe_pystata():
    """Calling /status must not run a PyStata probe — that is the entire
    point of splitting it from /health for container-lifecycle use."""
    mod = _load_app_module()
    from fastapi.testclient import TestClient

    probes_seen = {"count": 0}

    original_probe = mod.SUPERVISOR._probe_once

    def counting_probe():
        probes_seen["count"] += 1
        return original_probe()

    mod.SUPERVISOR._probe_once = counting_probe  # type: ignore[method-assign]

    client = TestClient(mod.app)
    for _ in range(3):
        r = client.get("/status")
        assert r.status_code == 200
    assert probes_seen["count"] == 0, "/status must not probe PyStata"


def test_default_startup_does_not_probe_pystata():
    """Default container-lifecycle contract: importing app.py and starting
    the FastAPI lifespan must not kick off a Stata probe. The warm-up
    runs only when PYSTATA_SHIM_WARMUP=1 is set explicitly."""
    mod = _load_app_module()
    from fastapi.testclient import TestClient

    probes_seen = {"count": 0}
    original_probe = mod.SUPERVISOR._probe_once

    def counting_probe():
        probes_seen["count"] += 1
        return original_probe()

    mod.SUPERVISOR._probe_once = counting_probe  # type: ignore[method-assign]

    # TestClient runs the FastAPI startup hook on entry. The default
    # contract is no warm-up, so even after startup runs and a small
    # settle window passes, no probe should have fired.
    with TestClient(mod.app):
        # let any wrongly-scheduled background thread settle
        import time as _t
        _t.sleep(0.2)

    assert probes_seen["count"] == 0, (
        "default startup must not probe PyStata; "
        f"saw {probes_seen['count']} probes"
    )


def test_explicit_warmup_opt_in_probes_in_background():
    """When the operator opts in with PYSTATA_SHIM_WARMUP=1, the warm-up
    thread must run (off the listen path), so a probe call eventually
    happens."""
    mod = _load_app_module({"PYSTATA_SHIM_WARMUP": "1"})
    from fastapi.testclient import TestClient

    probes_seen = {"count": 0}
    original_probe = mod.SUPERVISOR._probe_once

    def counting_probe():
        probes_seen["count"] += 1
        return original_probe()

    mod.SUPERVISOR._probe_once = counting_probe  # type: ignore[method-assign]

    with TestClient(mod.app):
        import time as _t
        # Background thread should fire within a generous window.
        deadline = _t.time() + 3.0
        while _t.time() < deadline and probes_seen["count"] == 0:
            _t.sleep(0.05)

    assert probes_seen["count"] >= 1, "PYSTATA_SHIM_WARMUP=1 must trigger a probe"


def test_status_does_not_require_token():
    """For the lifecycle script to poll /status without the token, /status
    is unauth-readable like /health (with stripped payload)."""
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/status")
    assert r.status_code == 200


def test_auth_status_includes_detail():
    """When the token is presented, /status reveals the same detail keys
    /health does, so operators can introspect without paying for a probe."""
    mod = _load_app_module()
    import time as _t
    with mod.SUPERVISOR._state_lock:
        mod.SUPERVISOR._probe_cache = {
            "ok": True,
            "pool_state": "ready",
            "edition": "mp",
            "stata_version_number": 19.5,
        }
        mod.SUPERVISOR._probe_cache_at = _t.time()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)

    auth = client.get("/status", headers={"x-shim-token": TOKEN}).json()
    unauth = client.get("/status").json()

    assert auth["stata_version_number"] == 19.5
    assert "stata_version_number" not in unauth


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
