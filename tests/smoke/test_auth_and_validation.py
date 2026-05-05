"""Smoke tests for auth behavior and pydantic validators.

Uses FastAPI's TestClient so the tests do not need a running server.
Does not require PyStata.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST_DIR = REPO / "host"

TOKEN = "T" * 64


def _load_app_module(extra_env: dict[str, str] | None = None):
    # Each test-run gets its own home + fresh module import so the module
    # global _EXPECTED_TOKEN reflects the current env.
    os.environ["PYSTATA_SHIM_TOKEN"] = TOKEN
    os.environ["PYSTATA_SHIM_HOME"] = tempfile.mkdtemp()
    os.environ["PYSTATA_SHIM_SKIP_WARMUP"] = "1"
    os.environ.pop("PYSTATA_SHIM_HOST", None)
    os.environ.pop("PYSTATA_SHIM_ALLOW_NON_LOOPBACK", None)
    os.environ.pop("PYSTATA_SHIM_ALLOW_UNSAFE_DO", None)
    if extra_env:
        os.environ.update(extra_env)
    if str(HOST_DIR) not in sys.path:
        sys.path.insert(0, str(HOST_DIR))
    # Drop any prior import so we re-execute module-level guards.
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", HOST_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unauth_health_payload_is_minimal():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) <= {"ok", "pool_state", "queue_depth"}
    assert "edition" not in data
    assert "init_error" not in data
    assert "version" not in data
    assert "stata_version_number" not in data


def test_auth_health_payload_has_detail():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/health", headers={"x-shim-token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    # Unauth keys still present:
    for k in ("ok", "pool_state", "queue_depth"):
        assert k in data
    # At least one detail key is present (exact set varies by probe state):
    assert any(k in data for k in ("edition", "init_error", "error_code"))


def test_auth_health_includes_stata_version_from_probe_cache():
    mod = _load_app_module()
    with mod.SUPERVISOR._state_lock:
        mod.SUPERVISOR._probe_cache = {
            "ok": True,
            "pool_state": "ready",
            "edition": "mp",
            "stata_version_number": 19.5,
        }
        mod.SUPERVISOR._probe_cache_at = time.time()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)

    auth = client.get("/health", headers={"x-shim-token": TOKEN}).json()
    unauth = client.get("/health").json()

    assert auth["stata_version_number"] == 19.5
    assert "stata_version_number" not in unauth


def test_worker_error_payload_is_not_success():
    mod = _load_app_module()
    original_popen = mod.subprocess.Popen

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
            Path(cmd[3]).write_text(
                '{"worker_error":"AttributeError: missing API"}',
                encoding="utf-8",
            )

        def communicate(self, timeout=None):
            return "", ""

    mod.subprocess.Popen = FakePopen
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = mod.SUPERVISOR._invoke_worker(
                job_id="fake",
                request={"op": "probe", "job_dir": tmp},
                timeout_sec=1,
                manage_slot=False,
            )
    finally:
        mod.subprocess.Popen = original_popen

    assert result["kind"] == "crash"
    assert "missing API" in result["detail"]


def test_do_requires_token():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post("/do", json={"do_code": "display 1"})
    assert r.status_code == 401


def test_do_rejects_wrong_token():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post(
        "/do",
        json={"do_code": "display 1"},
        headers={"x-shim-token": "X" * 64},
    )
    assert r.status_code == 401


def test_job_status_requires_token():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/job/whatever")
    assert r.status_code == 401


def test_job_status_404_when_not_running():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/job/nonexistent", headers={"x-shim-token": TOKEN})
    assert r.status_code == 404


def test_cancel_requires_token():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post("/cancel/whatever")
    assert r.status_code == 401


def test_cancel_returns_envelope_for_missing_job():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post("/cancel/nonexistent", headers={"x-shim-token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["cancelled"] is False
    assert "no matching job" in data["note"]


def test_do_code_too_big_is_rejected():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    payload = {"do_code": "x" * (2 * 1024 * 1024)}
    r = client.post("/do", json=payload, headers={"x-shim-token": TOKEN})
    assert r.status_code == 422


def test_shell_directive_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="shell ls")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for shell")


def test_bang_shell_directive_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="quietly ! ls")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for ! shell escape")


def test_xshell_directive_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="xshell vi myfile.do")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for xshell")


def test_double_bang_xshell_directive_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="!! vi myfile.do")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for !! xshell escape")


def test_quietly_prefix_abbreviation_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="quie shell ls")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for quie shell")


def test_shell_command_abbreviation_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="sh ls")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for sh shell abbreviation")


def test_python_command_abbreviation_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code='py: print("hello")')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for py python abbreviation")


def test_python_directive_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code='python: print("hello")')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for python block")


def test_literal_do_file_is_inspected_recursively():
    tmp = Path(tempfile.mkdtemp())
    child = tmp / "child.do"
    child.write_text("shell ls\n", encoding="utf-8")
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code=f'cd "{tmp}"\ndo "{child}"\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        assert str(child) in str(exc)
        return
    raise AssertionError("expected recursive validation failure for child do-file")


def test_dynamic_do_file_reference_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code='local f "child.do"\ndo "`f\'"\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe indirect file execution" in str(exc)
        return
    raise AssertionError("expected validation error for dynamic do reference")


def test_macro_expanded_command_name_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code='global g shell\n$g ls\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe dynamic command dispatch" in str(exc)
        return
    raise AssertionError("expected validation error for macro-expanded command")


def test_macro_expanded_bang_command_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="local bang \"!\"\n`bang' ls\n")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe dynamic command dispatch" in str(exc)
        return
    raise AssertionError("expected validation error for macro-expanded ! command")


def test_if_prefix_host_escape_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="if 1 quietly ! ls\n")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for if ... ! command")


def test_if_prefix_literal_do_file_is_inspected_recursively():
    tmp = Path(tempfile.mkdtemp())
    child = tmp / "child.do"
    child.write_text("shell ls\n", encoding="utf-8")
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code=f'if 1 do "{child}"\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        assert str(child) in str(exc)
        return
    raise AssertionError("expected recursive validation failure for if ... do child")


def test_quietly_prefix_abbreviation_does_not_bypass_recursive_validation():
    tmp = Path(tempfile.mkdtemp())
    child = tmp / "child.do"
    child.write_text("shell ls\n", encoding="utf-8")
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code=f'quie do "{child}"\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        assert str(child) in str(exc)
        return
    raise AssertionError("expected recursive validation failure for quie do child")


def test_mata_stata_bridge_is_rejected_by_default():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code='mata: stata("shell ls")\n')
    except Exception as exc:  # noqa: BLE001
        assert "unsafe host-escape directive" in str(exc)
        return
    raise AssertionError("expected validation error for mata: stata(...)")


def test_hidden_zero_width_character_is_rejected():
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code="\u200bshell ls\n")
    except Exception as exc:  # noqa: BLE001
        assert "unsafe non-printing character" in str(exc)
        return
    raise AssertionError("expected validation error for zero-width-space prefix")


def test_nested_do_file_over_size_limit_is_rejected():
    tmp = Path(tempfile.mkdtemp())
    child = tmp / "child.do"
    child.write_text("display 1\n" + ("x" * (2 * 1024 * 1024)), encoding="utf-8")
    mod = _load_app_module()
    try:
        mod.DoRequest(do_code=f'do "{child}"\n')
    except Exception as exc:  # noqa: BLE001
        assert "exceeds 1048576 bytes" in str(exc)
        return
    raise AssertionError("expected validation error for oversized nested do-file")


def test_comments_and_strings_do_not_trigger_unsafe_do_guard():
    mod = _load_app_module()
    req = mod.DoRequest(
        do_code='* shell ls is forbidden\n'
        'display "python: still just a string"\n'
        'display 1\n'
    )
    assert "display 1" in req.do_code


def test_restricted_mode_allows_representative_safe_stata_code():
    mod = _load_app_module()
    req = mod.DoRequest(
        do_code=(
            "clear all\n"
            "set obs 3\n"
            "generate x = _n\n"
            "summarize x\n"
            "if r(N) == 3 {\n"
            '    display "safe code path"\n'
            "}\n"
        )
    )
    assert "summarize x" in req.do_code


def test_allow_unsafe_do_opt_in_bypasses_guard():
    mod = _load_app_module({"PYSTATA_SHIM_ALLOW_UNSAFE_DO": "1"})
    req = mod.DoRequest(do_code="shell ls")
    assert req.do_code == "shell ls"


def test_bad_capture_name_is_rejected():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post(
        "/do",
        json={"do_code": "display 1", "capture": ["bad name"]},
        headers={"x-shim-token": TOKEN},
    )
    assert r.status_code == 422


def test_caller_tag_control_chars_rejected():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post(
        "/do",
        json={"do_code": "display 1", "caller_tag": "foo\x00bar"},
        headers={"x-shim-token": TOKEN},
    )
    assert r.status_code == 422


def test_timeout_out_of_range_rejected():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.post(
        "/do",
        json={"do_code": "display 1", "timeout_sec": 9999},
        headers={"x-shim-token": TOKEN},
    )
    assert r.status_code == 422


def test_openapi_title_is_public_name():
    mod = _load_app_module()
    from fastapi.testclient import TestClient
    client = TestClient(mod.app)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    body = r.json()
    assert body["info"]["title"] == "pystata-shim"
    # Belt-and-suspenders: no legacy/private-flavored vocabulary leaks
    # into any endpoint description, summary, schema name, or tag.
    raw = r.text.lower()
    for needle in ("private_shim_name", "caller_role", "private-package", "com.private"):
        assert needle not in raw, f"leaked vocabulary in OpenAPI: {needle!r}"


def test_public_text_has_no_internal_review_identifiers():
    text_suffixes = {".md", ".py", ".sh", ".txt", ".yml", ".yaml"}
    text_names = {"README", "LICENSE", "SECURITY"}
    needle = "C" + "R-"
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in text_suffixes and path.name not in text_names:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        assert needle not in raw, f"internal review identifier leaked in {path}"


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
