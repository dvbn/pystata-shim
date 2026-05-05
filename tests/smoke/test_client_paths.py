"""Smoke tests for client-side path mapping and Stata quoting.

Do not hit the network; they poke the helper functions directly.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import os
import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLIENT_DIR = REPO / "client"


def _load_client():
    if str(CLIENT_DIR) not in sys.path:
        sys.path.insert(0, str(CLIENT_DIR))
    sys.modules.pop("pystata_shim", None)
    import pystata_shim  # noqa: E402  -- module name overrides on reload
    return importlib.reload(pystata_shim)


def test_token_required_raises():
    os.environ.pop("PYSTATA_SHIM_TOKEN", None)
    c = _load_client()
    try:
        c._token()
    except c.ShimError:
        return
    raise AssertionError("expected ShimError when token unset")


def test_container_to_host_requires_workspace_env():
    os.environ.pop("PYSTATA_SHIM_HOST_WORKSPACE", None)
    c = _load_client()
    try:
        c._container_to_host(Path("/workspace/a"))
    except c.ShimError:
        return
    raise AssertionError("expected ShimError without HOST_WORKSPACE set")


def test_container_to_host_translates_under_workspace():
    os.environ["PYSTATA_SHIM_HOST_WORKSPACE"] = "/Users/example/proj"
    os.environ["PYSTATA_SHIM_CONTAINER_WORKSPACE"] = "/workspace"
    c = _load_client()
    result = c._container_to_host(Path("/workspace/sub/file.do"))
    assert str(result) == "/Users/example/proj/sub/file.do"


def test_container_to_host_rejects_outside_workspace():
    os.environ["PYSTATA_SHIM_HOST_WORKSPACE"] = "/Users/example/proj"
    os.environ["PYSTATA_SHIM_CONTAINER_WORKSPACE"] = "/workspace"
    c = _load_client()
    try:
        c._container_to_host(Path("/tmp/not-under-workspace"))
    except c.ShimError:
        return
    raise AssertionError("expected ShimError for out-of-workspace path")


def test_stata_quote_doubles_embedded_quotes():
    c = _load_client()
    result = c._stata_quote(Path('/a/b "c" d'))
    assert result == '/a/b ""c"" d'


def test_stata_quote_rejects_control_chars():
    c = _load_client()
    for bad in ("\x00", "\r", "\n"):
        try:
            c._stata_quote(Path(f"/a/{bad}/b"))
        except c.ShimError:
            continue
        raise AssertionError(f"expected ShimError for char {bad!r}")


def test_stdlib_transport_sends_json_and_token_header():
    os.environ["PYSTATA_SHIM_TOKEN"] = "T" * 64
    os.environ["PYSTATA_SHIM_BASE_URL"] = "http://shim.test"
    c = _load_client()
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

        def getcode(self):
            return 200

    original = c.urllib.request.urlopen

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeResponse()

    c.urllib.request.urlopen = fake_urlopen
    try:
        result = c._post("/do", {"do_code": "display 1"}, http_timeout=12.5)
    finally:
        c.urllib.request.urlopen = original

    assert result == {"ok": True}
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.full_url == "http://shim.test/do"
    assert req.get_method() == "POST"
    assert timeout == 12.5
    assert req.get_header("X-shim-token") == "T" * 64
    assert req.get_header("Content-type") == "application/json"
    assert req.data == b'{"do_code": "display 1"}'


def test_stdlib_transport_reports_http_errors_without_network():
    os.environ["PYSTATA_SHIM_TOKEN"] = "T" * 64
    os.environ["PYSTATA_SHIM_BASE_URL"] = "http://shim.test"
    c = _load_client()
    original = c.urllib.request.urlopen

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"invalid or missing X-Shim-Token"}'),
        )

    c.urllib.request.urlopen = fake_urlopen
    try:
        try:
            c._get("/health", http_timeout=2, with_token=True)
        except c.ShimError as exc:
            assert "HTTP 401" in str(exc)
            assert "invalid or missing" in str(exc)
            return
        raise AssertionError("expected ShimError for HTTPError")
    finally:
        c.urllib.request.urlopen = original


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
