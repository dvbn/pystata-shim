"""In-container client for the pystata-shim host service.

Transport:
    Uses Python's stdlib HTTP stack so the client has no pip dependency
    and does not require `curl` in the container image.

Authentication:
    Every request that mutates state or reveals Stata output carries an
    `X-Shim-Token` header. The token must be provided via
    `PYSTATA_SHIM_TOKEN`.

Path mapping:
    `run_do_file()` accepts a container-side path under
    `PYSTATA_SHIM_CONTAINER_WORKSPACE` and translates it to the matching
    host-side path under `PYSTATA_SHIM_HOST_WORKSPACE` before building
    the `cd "<path>"; do "<path>"` snippet. There is no silent fallback
    for paths outside the workspace; the caller must pass a
    host-absolute path if they want to run something outside the bind.

Environment variables:
    PYSTATA_SHIM_BASE_URL               default http://host.docker.internal:8787
    PYSTATA_SHIM_TOKEN                  required for mutating calls
    PYSTATA_SHIM_CONTAINER_WORKSPACE    default /workspace
    PYSTATA_SHIM_HOST_WORKSPACE         required for path mapping

See ../docs/agent-guidance.md for usage patterns.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

BASE_URL = os.environ.get(
    "PYSTATA_SHIM_BASE_URL",
    "http://host.docker.internal:8787",
).rstrip("/")
DEFAULT_TIMEOUT_SEC = 600
HEALTH_TIMEOUT_SEC = 15.0
# Server-side queue can hold a request for up to QUEUE_TIMEOUT_SEC
# (20 min) before reaching the worker. The client must allow at
# least that long on top of the execution timeout, plus a small
# slack for the response trip.
QUEUE_TIMEOUT_PADDING_SEC = 1320  # 20 min queue + 2 min slack
SERVER_MAX_TIMEOUT_SEC = 3600


def _normalize_workspace(raw: str) -> Path:
    """Strip trailing slash and resolve symlinks once, for a stable prefix."""
    cleaned = raw.rstrip("/") or "/"
    return Path(cleaned).resolve(strict=False)


CONTAINER_WORKSPACE = _normalize_workspace(
    os.environ.get("PYSTATA_SHIM_CONTAINER_WORKSPACE", "/workspace"),
)
HOST_WORKSPACE = os.environ.get("PYSTATA_SHIM_HOST_WORKSPACE", "").rstrip("/")


class ShimError(RuntimeError):
    """Raised on any transport or HTTP-level failure against the shim."""


def _token() -> str:
    token = os.environ.get("PYSTATA_SHIM_TOKEN", "").strip()
    if not token:
        raise ShimError(
            "PYSTATA_SHIM_TOKEN is not set; request the shared secret from"
            " the host operator."
        )
    return token


def _url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return BASE_URL + path


def _request(
    path: str,
    *,
    method: str,
    body: dict[str, Any] | None,
    http_timeout: float,
    with_token: bool,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if with_token:
        headers["X-Shim-Token"] = _token()
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        _url(path),
        data=data,
        headers=headers,
        method=method,
    )
    # urlopen's timeout applies to socket connect/read operations. The
    # shim does not stream responses, so this is the practical wall-clock
    # guard for normal requests without needing an external curl process.
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            payload = resp.read()
            status_code = resp.getcode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ShimError(f"HTTP {exc.code} on {path}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise ShimError(f"transport error on {path}: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ShimError(f"timeout after {http_timeout:g}s on {path}") from exc
    except OSError as exc:
        raise ShimError(f"transport error on {path}: {exc}") from exc

    payload_text = payload.decode("utf-8", "replace")
    if status_code >= 400:
        raise ShimError(f"HTTP {status_code} on {path}: {payload_text[:500]}")
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ShimError(f"bad JSON on {path}: {payload_text[:500]!r}") from exc


def _get(path: str, *, http_timeout: float = 30.0, with_token: bool = True) -> dict[str, Any]:
    return _request(path, method="GET", body=None, http_timeout=http_timeout, with_token=with_token)


def _post(path: str, body: dict[str, Any], *, http_timeout: float = 3700.0) -> dict[str, Any]:
    return _request(path, method="POST", body=body, http_timeout=http_timeout, with_token=True)


# --- Path mapping --------------------------------------------------------

def _resolve_container_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _container_to_host(path: Path) -> Path:
    if not HOST_WORKSPACE:
        raise ShimError(
            "PYSTATA_SHIM_HOST_WORKSPACE is not set; set it to the host path"
            " that is bind-mounted to PYSTATA_SHIM_CONTAINER_WORKSPACE"
            f" (default {CONTAINER_WORKSPACE})."
        )
    try:
        relative = path.relative_to(CONTAINER_WORKSPACE)
    except ValueError:
        raise ShimError(
            f"path {path} is not under PYSTATA_SHIM_CONTAINER_WORKSPACE"
            f" ({CONTAINER_WORKSPACE}); if it is a host-absolute path that the"
            " host can see, embed it directly in do_code rather than going"
            " through run_do_file()."
        ) from None
    return (Path(HOST_WORKSPACE) / relative).resolve(strict=False)


def _stata_quote(path: Path) -> str:
    raw = str(path)
    if any(ch in raw for ch in ("\x00", "\r", "\n")):
        raise ShimError(
            f"path {raw!r} contains characters that are not safe in a quoted Stata path"
            " (NUL, CR, or LF)."
        )
    # Stata expands `name' (locals) and $name (globals) inside quoted paths.
    # We do not rewrite these — the caller is responsible for not using such
    # characters in path names that Stata will see. See docs/architecture.md.
    return raw.replace('"', '""')


# --- Public API ----------------------------------------------------------

def health() -> bool:
    """Return True if the shim is reachable and Stata initialized.

    Unauthenticated — safe to call from any container process.
    """
    try:
        body = _get("/health", http_timeout=HEALTH_TIMEOUT_SEC, with_token=False)
    except ShimError:
        return False
    return bool(body.get("ok") and body.get("pool_state") == "ready")


def health_full() -> dict[str, Any]:
    """Return the full diagnostic payload, authenticated."""
    return _get("/health", http_timeout=HEALTH_TIMEOUT_SEC, with_token=True)


def do(
    do_code: str,
    *,
    capture: Iterable[str] = (),
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    caller_tag: str = "",
) -> dict[str, Any]:
    """Run a do-code snippet. Blocks until the shim returns a result."""
    body = {
        "do_code": do_code,
        "capture": list(capture),
        "timeout_sec": timeout_sec,
        "caller_tag": caller_tag,
    }
    server_exec_budget = min(timeout_sec, SERVER_MAX_TIMEOUT_SEC)
    http_timeout = server_exec_budget + QUEUE_TIMEOUT_PADDING_SEC
    return _post("/do", body, http_timeout=http_timeout)


def run_do_file(
    do_file: str | os.PathLike[str],
    *,
    working_dir: str | os.PathLike[str] | None = None,
    capture: Iterable[str] = (),
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    caller_tag: str = "",
) -> dict[str, Any]:
    """Run a `.do` file under the container workspace on the host side.

    Translates the container path to the matching host path, then sends
    `cd "<host_working_dir>"; do "<host_do_file>"` as a `/do` body.
    """
    do_path = _resolve_container_path(do_file)
    work_path = _resolve_container_path(working_dir or do_path.parent)
    host_do = _container_to_host(do_path)
    host_work = _container_to_host(work_path)
    do_code = (
        f'cd "{_stata_quote(host_work)}"\n'
        f'do "{_stata_quote(host_do)}"\n'
    )
    return do(
        do_code,
        capture=capture,
        timeout_sec=timeout_sec,
        caller_tag=caller_tag,
    )


def job_status(job_id: str) -> dict[str, Any]:
    """Live status of a currently-running job. 404 once the job has exited."""
    return _get(f"/job/{job_id}", http_timeout=30.0, with_token=True)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Best-effort cancel. SIGTERM/SIGKILL against the worker process group."""
    return _post(f"/cancel/{job_id}", {}, http_timeout=30.0)
