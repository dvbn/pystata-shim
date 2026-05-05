"""FastAPI shim that runs Stata through PyStata in a killable worker.

The FastAPI server stays in the parent process. Every Stata request
runs in a dedicated worker subprocess so that timeouts and cancels can
terminate a wedged Stata job without taking down the API server itself.

This file is the public `pystata-shim` host service. It is intentionally
small: auth, serialization, a worker lifecycle, and a JSONL audit log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator


# --- Configuration -------------------------------------------------------

SHIM_HOME = Path(os.environ.get("PYSTATA_SHIM_HOME", Path.home() / ".pystata-shim"))
SHIM_HOME.mkdir(parents=True, exist_ok=True)

JSONL_PATH = SHIM_HOME / "audit.jsonl"
RUNS_DIR = SHIM_HOME / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = SHIM_HOME / "token"
WORKER_PATH = Path(__file__).with_name("worker.py")

EDITION = os.environ.get("PYSTATA_EDITION", "mp")

# Limits and thresholds.
MAX_TIMEOUT_SEC = 3600
QUEUE_TIMEOUT_SEC = 20 * 60
TERMINATE_GRACE_SEC = 10.0
HEALTH_PROBE_TIMEOUT_SEC = 20
HEALTH_CACHE_SEC = 30.0
MAX_DO_CODE_BYTES = 1 * 1024 * 1024
MAX_CAPTURE_ITEMS = 128
MAX_CALLER_TAG_LEN = 128
MAX_VALIDATED_INCLUDE_DEPTH = 16
RUNS_RETENTION_DAYS = int(os.environ.get("PYSTATA_SHIM_RUNS_RETENTION_DAYS", "7"))
MIN_TOKEN_LEN = 32
ALLOW_UNSAFE_DO = os.environ.get("PYSTATA_SHIM_ALLOW_UNSAFE_DO", "0") == "1"

# Degraded-mode thresholds (env-tunable).
DEGRADED_CRASHES = int(os.environ.get("PYSTATA_SHIM_DEGRADE_CRASHES", "3"))
DEGRADED_WINDOW_SEC = float(os.environ.get("PYSTATA_SHIM_DEGRADE_WINDOW_SEC", "600"))
DEGRADED_COOLDOWN_SEC = float(os.environ.get("PYSTATA_SHIM_DEGRADE_COOLDOWN_SEC", "300"))

CAPTURE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:.()]{0,127}$")
CALLER_TAG_RE = re.compile(r"^[\x20-\x7E]{0,128}$")  # printable ASCII only


def _alias_map(words: dict[str, int]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for word, min_len in words.items():
        for size in range(min_len, len(word) + 1):
            aliases[word[:size]] = word
    return aliases


UNSAFE_DO_PREFIX_ALIASES = _alias_map(
    {
        "capture": 3,
        "quietly": 3,
        "noisily": 3,
    }
)
UNSAFE_HOST_ESCAPE_ALIASES = _alias_map(
    {
        "shell": 2,
        "xshell": 2,
        "python": 2,
        "unix": 2,
        "winexec": len("winexec"),
        "javacall": len("javacall"),
        "mata": len("mata"),
    }
)
UNSAFE_HOST_ESCAPE_COMMANDS = set(UNSAFE_HOST_ESCAPE_ALIASES.values())
INDIRECT_DO_COMMANDS = {"do", "run", "include"}


# --- Token loading -------------------------------------------------------

def _fatal(msg: str) -> "None":
    """Emit a one-line error on stderr and exit before importing Stata."""
    sys.stderr.write(f"pystata-shim: {msg}\n")
    sys.stderr.flush()
    sys.exit(2)


def _load_token() -> str:
    token = os.environ.get("PYSTATA_SHIM_TOKEN", "")
    if not token and TOKEN_FILE.exists():
        try:
            mode = TOKEN_FILE.stat().st_mode & 0o777
        except OSError as exc:
            _fatal(f"cannot stat token file {TOKEN_FILE}: {exc}")
        # Stricter than a "warn on world-readable" policy on purpose: on a
        # LaunchAgent stderr warnings go into a log no operator reads, so
        # any permission laxness is refused outright.
        if mode & 0o077:
            _fatal(
                f"token file {TOKEN_FILE} is mode {mode:o}; tighten it"
                " with `chmod 600` (server will not start with group/other"
                " permissions)."
            )
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN_LEN:
        _fatal(
            "PYSTATA_SHIM_TOKEN must be set (or written to"
            f" {TOKEN_FILE}) and at least {MIN_TOKEN_LEN} characters."
            " Generate one with `openssl rand -hex 32`."
        )
    return token


def _enforce_loopback_bind() -> None:
    """Refuse to run on a non-loopback interface unless explicitly allowed.

    Honored regardless of entry point (start.sh, bare uvicorn, pytest).
    """
    host = os.environ.get("PYSTATA_SHIM_HOST", "127.0.0.1")
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    if os.environ.get("PYSTATA_SHIM_ALLOW_NON_LOOPBACK", "0") == "1":
        sys.stderr.write(
            f"pystata-shim: WARNING binding non-loopback interface {host};"
            " there is no TLS on this transport.\n"
        )
        return
    _fatal(
        f"refusing to run with PYSTATA_SHIM_HOST={host!r} (non-loopback)."
        " Set PYSTATA_SHIM_ALLOW_NON_LOOPBACK=1 only if you fully understand"
        " the risk (no TLS, shared-secret only)."
    )


_enforce_loopback_bind()


_EXPECTED_TOKEN = _load_token()


def _check_token(header_value: str | None) -> None:
    if not header_value or not secrets.compare_digest(header_value, _EXPECTED_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing X-Shim-Token")


# --- Helpers -------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_job_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-"
        + secrets.token_hex(4)
    )


def _clamp_timeout(raw: int, default: int) -> int:
    try:
        timeout = int(raw)
    except Exception:
        timeout = default
    return max(1, min(timeout, MAX_TIMEOUT_SEC))


_log_lock = threading.Lock()


def _log_job(row: dict[str, Any]) -> None:
    row = {**row, "ts_utc": row.get("ts_utc") or _now_iso()}
    line = json.dumps(row, ensure_ascii=False, default=str)
    with _log_lock:
        with open(JSONL_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _prune_old_runs() -> None:
    if not RUNS_DIR.is_dir() or RUNS_RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RUNS_RETENTION_DAYS * 86400
    for child in RUNS_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _warn_if_audit_log_huge() -> None:
    """Emit one stderr line if the audit log has grown past the warn threshold.

    The shim never silently truncates user data. Operators rotate manually
    (newsyslog, logrotate, or a quick `mv`) — see docs/security.md.
    """
    threshold_mb = int(os.environ.get("PYSTATA_SHIM_AUDIT_WARN_MB", "200"))
    if threshold_mb <= 0:
        return
    try:
        size = JSONL_PATH.stat().st_size
    except OSError:
        return
    threshold_bytes = threshold_mb * 1024 * 1024
    if size >= threshold_bytes:
        sys.stderr.write(
            f"pystata-shim: warning: audit log {JSONL_PATH} is "
            f"{size / 1024 / 1024:.0f} MiB (>= {threshold_mb} MiB)."
            " Rotate it (e.g. `mv audit.jsonl audit.jsonl.$(date +%Y%m%d)`).\n"
        )


def _iter_stata_statements(source: str) -> list[str]:
    """Split Stata source into statements, stripping comments.

    The parser is intentionally small and conservative. It understands:
    - `*` line comments when `*` is the first non-space char
    - `//` line comments
    - `///` line-continuation comments
    - `/* ... */` block comments
    - quoted strings with doubled `""`

    Statements are separated on newline or `;` once comments/strings are
    accounted for. This is sufficient for the shim's security screening.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_block_comment = False
    in_line_comment = False
    line_comment_continues = False
    line_has_non_ws = False
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        nxt2 = source[i + 2] if i + 2 < n else ""

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                if line_comment_continues:
                    line_comment_continues = False
                    if buf and not buf[-1].isspace():
                        buf.append(" ")
                    line_has_non_ws = any(not part.isspace() for part in buf)
                else:
                    stmt = "".join(buf).strip()
                    if stmt:
                        statements.append(stmt)
                    buf = []
                    line_has_non_ws = False
            i += 1
            continue

        if in_string:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if not line_has_non_ws and ch in " \t\r":
            buf.append(ch)
            i += 1
            continue

        if not line_has_non_ws and ch == "*":
            in_line_comment = True
            line_comment_continues = False
            i += 1
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "/" and nxt2 == "/":
            in_line_comment = True
            line_comment_continues = True
            i += 3
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            line_comment_continues = False
            i += 2
            continue

        if ch == '"':
            in_string = True
            buf.append(ch)
            line_has_non_ws = True
            i += 1
            continue

        if ch in {"\n", ";"}:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            line_has_non_ws = False
            i += 1
            continue

        buf.append(ch)
        if not ch.isspace():
            line_has_non_ws = True
        i += 1

    if not in_line_comment:
        stmt = "".join(buf).strip()
        if stmt:
            statements.append(stmt)
    return statements


def _strip_prefixes_and_leading_context(statement: str) -> str:
    """Remove common execution prefixes and peel off `prefix: command` forms."""
    rest = statement.strip()
    while True:
        parts = rest.split(None, 1)
        if not parts:
            return ""
        head = UNSAFE_DO_PREFIX_ALIASES.get(parts[0].lower())
        if head is None:
            break
        rest = parts[1] if len(parts) > 1 else ""

    while ":" in rest:
        token = _canonical_command(rest.split(None, 1)[0].rstrip(":").lower())
        if token in UNSAFE_HOST_ESCAPE_COMMANDS or token in INDIRECT_DO_COMMANDS or token == "cd":
            break
        rest = rest.split(":", 1)[1].strip()
        while True:
            parts = rest.split(None, 1)
            if not parts:
                return ""
            head = UNSAFE_DO_PREFIX_ALIASES.get(parts[0].lower())
            if head is None:
                break
            rest = parts[1] if len(parts) > 1 else ""
    return rest.lstrip()


def _canonical_command(token: str) -> str:
    return UNSAFE_HOST_ESCAPE_ALIASES.get(token, token)


def _split_command(statement: str) -> tuple[str | None, str]:
    rest = _strip_prefixes_and_leading_context(statement)
    if not rest:
        return None, ""
    if rest.startswith("!"):
        return "!", rest[1:].lstrip()
    if rest.startswith("$") or rest.startswith("`"):
        return rest[0], rest
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*:?)(.*)", rest, re.DOTALL)
    if not match:
        return None, rest
    command = _canonical_command(match.group(1).rstrip(":").lower())
    arg_text = match.group(2).lstrip()

    if command == "if":
        nested = _find_inline_if_command(arg_text)
        if nested is not None:
            return _split_command(nested)
        return command, arg_text

    if command == "else":
        nested = arg_text.lstrip()
        if nested:
            return _split_command(nested)
        return command, arg_text

    return command, arg_text


def _find_inline_if_command(arg_text: str) -> str | None:
    """Find the nested command in `if exp command` or `if exp { command }`."""
    seen_condition = False
    in_string = False
    paren_depth = 0
    i = 0
    n = len(arg_text)

    while i < n:
        ch = arg_text[i]
        nxt = arg_text[i + 1] if i + 1 < n else ""

        if in_string:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            seen_condition = True
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
            seen_condition = True
            i += 1
            continue

        if ch == ")" and paren_depth > 0:
            paren_depth -= 1
            seen_condition = True
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        if paren_depth == 0 and seen_condition:
            if ch == "{":
                i += 1
                continue
            if ch in {"!", "$", "`"}:
                return arg_text[i:]
            if ch.isalpha() or ch == "_":
                j = i + 1
                while j < n and (arg_text[j].isalnum() or arg_text[j] == "_"):
                    j += 1
                token = arg_text[i:j].lower()
                if (
                    token in UNSAFE_DO_PREFIX_ALIASES
                    or _canonical_command(token) in UNSAFE_HOST_ESCAPE_COMMANDS
                    or token in INDIRECT_DO_COMMANDS
                    or token in {"cd", "else", "if"}
                ):
                    return arg_text[i:]

        seen_condition = True
        i += 1

    return None


def _parse_literal_path_arg(arg_text: str) -> str | None:
    if not arg_text:
        return None
    if arg_text.startswith('"'):
        value: list[str] = []
        i = 1
        while i < len(arg_text):
            ch = arg_text[i]
            if ch == '"':
                if i + 1 < len(arg_text) and arg_text[i + 1] == '"':
                    value.append('"')
                    i += 2
                    continue
                return "".join(value)
            value.append(ch)
            i += 1
        return None
    match = re.match(r"([^,\s]+)", arg_text)
    if not match:
        return None
    return match.group(1)


def _resolve_stata_path(path_text: str, *, base_dir: Path | None) -> Path | None:
    if any(ch in path_text for ch in ("`", "$", "\x00", "\r", "\n")):
        return None
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    if base_dir is None:
        return None
    return (base_dir / candidate).resolve(strict=False)


def _reject_unsafe_do(value: str) -> str:
    if ALLOW_UNSAFE_DO:
        return value

    visited_files: set[Path] = set()

    def _reject_hidden_text(source: str, *, source_name: str) -> None:
        for ch in source:
            category = unicodedata.category(ch)
            if category == "Cf" or (category == "Cc" and ch not in "\t\r\n"):
                raise ValueError(
                    "unsafe non-printing character blocked in"
                    f" {source_name}: U+{ord(ch):04X}. Remove hidden control/format"
                    " characters or set PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 only for a"
                    " fully trusted setup."
                )

    def _walk(
        source: str,
        *,
        base_dir: Path | None,
        source_name: str,
        depth: int,
    ) -> None:
        if depth > MAX_VALIDATED_INCLUDE_DEPTH:
            raise ValueError(
                "unsafe indirect file execution blocked in"
                f" {source_name}: nested do/include/run depth exceeds"
                f" {MAX_VALIDATED_INCLUDE_DEPTH}."
            )
        _reject_hidden_text(source, source_name=source_name)
        current_dir = base_dir
        for statement in _iter_stata_statements(source):
            command, arg_text = _split_command(statement)
            if command is None:
                continue

            if command in {"$", "`"}:
                raise ValueError(
                    "unsafe dynamic command dispatch blocked in"
                    f" {source_name}: macro-expanded command names cannot be"
                    " inspected safely. Expand the command literally or set"
                    " PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 only for a fully trusted"
                    " setup."
                )

            if command == "!" or command in UNSAFE_HOST_ESCAPE_COMMANDS:
                name = "!" if command == "!" else command
                raise ValueError(
                    "unsafe host-escape directive blocked in"
                    f" {source_name}: {name!r}. The shim rejects shell / host-language"
                    " execution by default. Set PYSTATA_SHIM_ALLOW_UNSAFE_DO=1 only"
                    " for a fully trusted setup."
                )

            if command == "cd":
                path_text = _parse_literal_path_arg(arg_text)
                if path_text is not None:
                    resolved = _resolve_stata_path(path_text, base_dir=current_dir)
                    if resolved is not None:
                        current_dir = resolved
                continue

            if command in INDIRECT_DO_COMMANDS:
                path_text = _parse_literal_path_arg(arg_text)
                if path_text is None:
                    raise ValueError(
                        "unsafe indirect file execution blocked in"
                        f" {source_name}: {command!r} must use a literal path that the"
                        " shim can inspect. Dynamic do/include/run directives require"
                        " PYSTATA_SHIM_ALLOW_UNSAFE_DO=1."
                    )
                resolved = _resolve_stata_path(path_text, base_dir=current_dir)
                if resolved is None or not resolved.is_file():
                    raise ValueError(
                        "unsafe indirect file execution blocked in"
                        f" {source_name}: {command!r} target {path_text!r} is not an"
                        " existing literal file the shim can inspect."
                    )
                if resolved in visited_files:
                    continue
                try:
                    nested_size = resolved.stat().st_size
                except OSError as exc:
                    raise ValueError(
                        "unsafe indirect file execution blocked in"
                        f" {source_name}: cannot stat {resolved}: {exc}"
                    ) from exc
                if nested_size > MAX_DO_CODE_BYTES:
                    raise ValueError(
                        "unsafe indirect file execution blocked in"
                        f" {source_name}: {command!r} target {path_text!r} exceeds"
                        f" {MAX_DO_CODE_BYTES} bytes and the shim will not inspect it."
                    )
                visited_files.add(resolved)
                try:
                    nested_source = resolved.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    nested_source = resolved.read_text(encoding="utf-8", errors="replace")
                _walk(
                    nested_source,
                    base_dir=resolved.parent,
                    source_name=str(resolved),
                    depth=depth + 1,
                )

    _walk(value, base_dir=None, source_name="<request do_code>", depth=0)
    return value


# --- Request models ------------------------------------------------------

class DoRequest(BaseModel):
    do_code: str
    capture: list[str] = Field(default_factory=list)
    timeout_sec: int = 600
    caller_tag: str = ""

    @field_validator("do_code")
    @classmethod
    def _cap_do_code(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_DO_CODE_BYTES:
            raise ValueError(f"do_code exceeds {MAX_DO_CODE_BYTES} bytes")
        return _reject_unsafe_do(value)

    @field_validator("capture")
    @classmethod
    def _cap_capture(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_CAPTURE_ITEMS:
            raise ValueError(f"capture list longer than {MAX_CAPTURE_ITEMS}")
        for name in value:
            if not CAPTURE_NAME_RE.match(name):
                raise ValueError(f"capture name not allowed: {name!r}")
        return value

    @field_validator("caller_tag")
    @classmethod
    def _check_caller_tag(cls, value: str) -> str:
        if len(value) > MAX_CALLER_TAG_LEN:
            raise ValueError("caller_tag too long")
        if not CALLER_TAG_RE.match(value):
            raise ValueError("caller_tag must be printable ASCII")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def _check_timeout(cls, value: int) -> int:
        if value < 1 or value > MAX_TIMEOUT_SEC:
            raise ValueError(
                f"timeout_sec must be between 1 and {MAX_TIMEOUT_SEC}"
            )
        return value


# --- Supervisor ----------------------------------------------------------

class StataSupervisor:
    """Serialize Stata jobs and manage killable workers."""

    def __init__(self, edition: str = "mp") -> None:
        self.edition = edition
        self._slot = threading.Lock()
        self._state_lock = threading.Lock()
        self._waiting = 0
        self._current: dict[str, Any] | None = None
        self._probe_cache: dict[str, Any] | None = None
        self._probe_cache_at = 0.0
        self._crashes: list[float] = []
        self._degraded_until: float | None = None

    # --- Public state --------------------------------------------------

    def status(self, *, include_detail: bool) -> dict[str, Any]:
        # Snapshot under lock.
        with self._state_lock:
            queue_depth = self._waiting + (1 if self._current else 0)
            current = dict(self._current) if self._current else None
            probe_cache = dict(self._probe_cache) if self._probe_cache else None
            degraded_until = self._degraded_until
        now = time.time()
        # Precedence: a running job reports "busy" even if the breaker has
        # tripped from earlier crashes, because the current job is proceeding.
        # Detail reporting still carries the degraded flag.
        degraded = bool(degraded_until and now < degraded_until)

        base = {
            "ok": False,
            "pool_state": "uninitialized",
            "queue_depth": queue_depth,
        }
        if current:
            base.update({
                "ok": True,
                "pool_state": "busy",
            })
            if include_detail:
                base["current_job_id"] = current["job_id"]
                base["edition"] = self.edition
                base["breaker_tripped"] = degraded
                if probe_cache and "stata_version_number" in probe_cache:
                    base["stata_version_number"] = probe_cache["stata_version_number"]
            return base
        if degraded:
            base.update({"pool_state": "degraded"})
            if include_detail:
                base["error_code"] = "shim_degraded"
                base["init_error"] = "recent worker failures"
                base["edition"] = self.edition
                if probe_cache and "stata_version_number" in probe_cache:
                    base["stata_version_number"] = probe_cache["stata_version_number"]
            return base
        if probe_cache:
            merged = {**base, **probe_cache, "queue_depth": queue_depth}
            if not include_detail:
                # Unauth payload is deliberately minimal.
                merged = {
                    k: merged[k]
                    for k in ("ok", "pool_state", "queue_depth")
                    if k in merged
                }
            return merged
        if include_detail:
            base["edition"] = self.edition
        return base

    def health(self, *, include_detail: bool) -> dict[str, Any]:
        with self._state_lock:
            job_running = self._current is not None
            cached = self._probe_cache
            cache_age = time.time() - self._probe_cache_at

        if job_running:
            return self.status(include_detail=include_detail)

        if cached and cache_age < HEALTH_CACHE_SEC:
            return self.status(include_detail=include_detail)

        # Do not probe while a job is running (avoids a second Stata process
        # fighting for the single seat).
        if not self._slot.acquire(blocking=False):
            return self.status(include_detail=include_detail)
        try:
            probed = self._probe_once()
            with self._state_lock:
                self._probe_cache = probed
                self._probe_cache_at = time.time()
        finally:
            self._slot.release()
        return self.status(include_detail=include_detail)

    # --- Running a job ------------------------------------------------

    def run_do(
        self,
        *,
        job_id: str,
        do_code: str,
        capture: list[str],
        timeout_sec: int,
    ) -> dict[str, Any]:
        job_dir = RUNS_DIR / job_id
        request = {
            "op": "run",
            "edition": self.edition,
            "job_dir": str(job_dir),
            "do_code": do_code,
            "capture": capture,
        }
        return self._run_worker_job(
            job_id=job_id,
            request=request,
            timeout_sec=timeout_sec,
        )

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        with self._state_lock:
            if self._current and self._current["job_id"] == job_id:
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": "running",
                    "started_at": self._current["started_at"],
                    "pid": self._current["pid"],
                    "cancel_requested": bool(self._current.get("cancel_requested")),
                }
        return None

    def cancel(self, job_id: str) -> dict[str, Any]:
        proc: subprocess.Popen[str] | None = None
        matched = False
        with self._state_lock:
            if self._current and self._current["job_id"] == job_id:
                matched = True
                self._current["cancel_requested"] = True
                proc = self._current["proc"]
        if not matched:
            return {
                "ok": True,
                "job_id": job_id,
                "cancelled": False,
                "note": "no matching job",
            }
        if proc is None:
            # cancel arrived in the narrow window between slot registration
            # and Popen return; the run loop checks cancel_requested after
            # Popen and will terminate immediately.
            return {
                "ok": True,
                "job_id": job_id,
                "cancelled": True,
                "note": "cancel queued; worker start racing",
            }
        self._terminate(proc)
        return {
            "ok": True,
            "job_id": job_id,
            "cancelled": True,
            "note": "termination signal sent",
        }

    # --- Internals ----------------------------------------------------

    def _run_worker_job(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        timeout_sec: int,
    ) -> dict[str, Any]:
        timeout_sec = _clamp_timeout(timeout_sec, 600)
        if self._is_degraded():
            return {
                "ok": False,
                "job_id": job_id,
                "status": "error",
                "error_code": "shim_degraded",
                "error_detail": "recent worker failures; shim is cooling down",
                "duration_sec": 0.0,
            }
        with self._state_lock:
            self._waiting += 1
        acquired = self._slot.acquire(timeout=QUEUE_TIMEOUT_SEC)
        with self._state_lock:
            self._waiting -= 1
        if not acquired:
            return {
                "ok": False,
                "job_id": job_id,
                "status": "error",
                "error_code": "queue_timeout",
                "error_detail": "waited too long for the single-worker slot",
                "duration_sec": 0.0,
            }
        try:
            return self._finalize(
                job_id=job_id,
                result=self._invoke_worker(
                    job_id=job_id,
                    request=request,
                    timeout_sec=timeout_sec,
                    manage_slot=True,
                ),
            )
        finally:
            self._slot.release()

    def _finalize(self, *, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        duration_sec = result["duration_sec"]
        kind = result["kind"]
        if kind == "ok":
            worker_result = result["worker_result"]
            return {
                "ok": True,
                "job_id": job_id,
                "status": "done",
                "stata_rc": worker_result["rc"],
                "stata_error": worker_result.get("stata_error"),
                "stdout": worker_result.get("stdout", ""),
                "stderr": worker_result.get("stderr", ""),
                "captured": worker_result.get("captured", {}),
                "duration_sec": duration_sec,
                "job_dir": worker_result.get("job_dir"),
            }
        if kind == "timeout":
            return {
                "ok": False,
                "job_id": job_id,
                "status": "error",
                "error_code": "timeout",
                "error_detail": f"request exceeded timeout_sec={result['timeout_sec']}",
                "duration_sec": duration_sec,
            }
        if kind == "interrupted":
            return {
                "ok": False,
                "job_id": job_id,
                "status": "interrupted",
                "error_code": "job_interrupted",
                "error_detail": "job cancelled by operator request",
                "duration_sec": duration_sec,
            }
        if kind == "license_error":
            return {
                "ok": False,
                "job_id": job_id,
                "status": "error",
                "error_code": "license_error",
                "error_detail": result["detail"],
                "duration_sec": duration_sec,
            }
        return {
            "ok": False,
            "job_id": job_id,
            "status": "error",
            "error_code": "stata_crash",
            "error_detail": result["detail"],
            "duration_sec": duration_sec,
        }

    def _probe_once(self) -> dict[str, Any]:
        if self._is_degraded():
            return {
                "ok": False,
                "pool_state": "degraded",
                "error_code": "shim_degraded",
                "init_error": "recent worker failures",
                "edition": self.edition,
            }
        # Route probe artifacts to a transient tmpdir so they do not pile
        # up inside the user-visible runs/ tree.
        import tempfile
        probe_dir = Path(tempfile.mkdtemp(prefix="pystata-shim-probe-"))
        request = {
            "op": "probe",
            "edition": self.edition,
            "job_dir": str(probe_dir),
        }
        try:
            result = self._invoke_worker(
                job_id="__health__",
                request=request,
                timeout_sec=HEALTH_PROBE_TIMEOUT_SEC,
                manage_slot=False,
            )
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        if result["kind"] == "ok":
            worker_result = result.get("worker_result", {})
            payload = {
                "ok": True,
                "pool_state": "ready",
                "edition": self.edition,
            }
            if "stata_version_number" in worker_result:
                payload["stata_version_number"] = worker_result["stata_version_number"]
            return payload
        if result["kind"] == "license_error":
            return {
                "ok": False,
                "pool_state": "uninitialized",
                "error_code": "license_error",
                "init_error": result["detail"],
                "edition": self.edition,
            }
        return {
            "ok": False,
            "pool_state": "uninitialized",
            "error_code": "stata_crash",
            "init_error": result["detail"],
            "edition": self.edition,
        }

    def _invoke_worker(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        timeout_sec: int,
        manage_slot: bool,
    ) -> dict[str, Any]:
        start = time.time()
        job_dir = Path(request.get("job_dir") or RUNS_DIR / job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        request_path = job_dir / ".request.json"
        response_path = job_dir / ".response.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        if response_path.exists():
            response_path.unlink()

        worker_env = {
            k: v for k, v in os.environ.items() if k != "PYSTATA_SHIM_TOKEN"
        }
        # Pre-register the slot under the state lock BEFORE spawning so that
        # a /cancel that races the worker start still sees a running job.
        # `proc` is bound below the lock scope.
        if manage_slot:
            with self._state_lock:
                self._current = {
                    "job_id": job_id,
                    "pid": -1,
                    "started_at": _now_iso(),
                    "cancel_requested": False,
                    "proc": None,
                }

        proc = subprocess.Popen(
            [sys.executable, str(WORKER_PATH), str(request_path), str(response_path)],
            cwd=str(Path(__file__).parent),
            env=worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        current: dict[str, Any] | None = None
        cancel_already_requested = False
        if manage_slot:
            with self._state_lock:
                if self._current is not None:
                    self._current["pid"] = proc.pid
                    self._current["proc"] = proc
                    cancel_already_requested = bool(self._current.get("cancel_requested"))
        # If a /cancel landed in the pre-Popen window, terminate immediately
        # so the upstream handler sees a clean "interrupted" status.
        if cancel_already_requested:
            self._terminate(proc)

        try:
            stdout_text, stderr_text = proc.communicate(timeout=timeout_sec)
            duration_sec = time.time() - start
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            duration_sec = time.time() - start
            return {
                "kind": "timeout",
                "timeout_sec": timeout_sec,
                "duration_sec": duration_sec,
            }
        finally:
            if manage_slot:
                with self._state_lock:
                    current = self._current
                    self._current = None

        if current and current.get("cancel_requested"):
            return {"kind": "interrupted", "duration_sec": duration_sec}

        response_json: dict[str, Any] | None = None
        if response_path.exists():
            try:
                response_json = json.loads(response_path.read_text(encoding="utf-8"))
            except Exception:
                response_json = None

        if (
            proc.returncode == 0
            and response_json is not None
            and not response_json.get("worker_error")
        ):
            return {
                "kind": "ok",
                "duration_sec": duration_sec,
                "worker_result": response_json,
            }

        detail_parts: list[str] = []
        if response_json and response_json.get("worker_error"):
            detail_parts.append(response_json["worker_error"])
        if stdout_text:
            detail_parts.append(stdout_text[-2000:])
        if stderr_text:
            detail_parts.append(stderr_text[-2000:])
        detail = "\n".join(part for part in detail_parts if part).strip()
        # Exit code 78 is a worker-side convention meaning
        # "PyStata license / init failed" — see worker.py.
        if proc.returncode == 78:
            return {
                "kind": "license_error",
                "detail": detail or "PyStata license/init failed",
                "duration_sec": duration_sec,
            }
        self._record_crash()
        return {
            "kind": "crash",
            "detail": detail or f"worker exited with code {proc.returncode}",
            "duration_sec": duration_sec,
        }

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + TERMINATE_GRACE_SEC
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _record_crash(self) -> None:
        now = time.time()
        with self._state_lock:
            self._crashes = [t for t in self._crashes if now - t < DEGRADED_WINDOW_SEC]
            self._crashes.append(now)
            if len(self._crashes) >= DEGRADED_CRASHES:
                self._degraded_until = now + DEGRADED_COOLDOWN_SEC

    def _is_degraded(self) -> bool:
        with self._state_lock:
            deadline = self._degraded_until
        return bool(deadline and time.time() < deadline)


SUPERVISOR = StataSupervisor(edition=EDITION)
app = FastAPI(title="pystata-shim", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    _prune_old_runs()
    _warn_if_audit_log_huge()
    # Skip the warm-up when told to (smoke tests, CI without PyStata).
    if os.environ.get("PYSTATA_SHIM_SKIP_WARMUP") == "1":
        return
    # Warm the health cache off the critical path so uvicorn binds the
    # socket immediately. The first probe may still be slow, but at least
    # it does not block the listen socket.
    threading.Thread(
        target=lambda: SUPERVISOR.health(include_detail=False),
        name="pystata-shim-warmup",
        daemon=True,
    ).start()


# --- Routes --------------------------------------------------------------

@app.get("/health")
def health(x_shim_token: str | None = Header(default=None)) -> JSONResponse:
    include_detail = bool(
        x_shim_token and secrets.compare_digest(x_shim_token, _EXPECTED_TOKEN)
    )
    payload = SUPERVISOR.health(include_detail=include_detail)
    return JSONResponse(status_code=200, content=payload)


@app.post("/do")
def do_endpoint(
    req: DoRequest,
    x_shim_token: str | None = Header(default=None),
) -> JSONResponse:
    _check_token(x_shim_token)
    job_id = _new_job_id()
    start = time.time()
    payload = SUPERVISOR.run_do(
        job_id=job_id,
        do_code=req.do_code,
        capture=list(req.capture),
        timeout_sec=req.timeout_sec,
    )
    payload.setdefault("duration_sec", time.time() - start)
    _log_job({
        "job_id": job_id,
        "endpoint": "/do",
        "caller_tag": req.caller_tag,
        "do_code_sha256": hashlib.sha256(req.do_code.encode("utf-8")).hexdigest(),
        "duration_sec": payload.get("duration_sec"),
        "stata_rc": payload.get("stata_rc"),
        "error_code": payload.get("error_code"),
        "status": payload.get("status"),
        # Do not log full stdout in the audit row to keep the JSONL small;
        # stdout is returned in the HTTP response and kept in the job dir.
    })
    return JSONResponse(status_code=200, content=payload)


@app.get("/job/{job_id}")
def job_status(
    job_id: str,
    x_shim_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_shim_token)
    runtime = SUPERVISOR.job_status(job_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="job not currently running")
    return runtime


@app.post("/cancel/{job_id}")
def cancel_job(
    job_id: str,
    x_shim_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_shim_token)
    return SUPERVISOR.cancel(job_id)


@app.middleware("http")
async def access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
    start = time.time()
    resp = await call_next(request)
    dur = time.time() - start
    sys.stdout.write(
        f"{_now_iso()} {request.method} {request.url.path}"
        f" -> {resp.status_code} {dur * 1000:.0f}ms\n"
    )
    sys.stdout.flush()
    return resp
