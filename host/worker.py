"""Single-job PyStata worker for the pystata-shim host service.

This process is spawned fresh per job. It imports `pystata`, runs either
a probe or one `.do` code block, writes a JSON response file, and exits.

Exit codes:
  0   worker wrote a valid response file
  78  PyStata license / init failed (conventional sysexits EX_CONFIG)
  1   other unhandled failure; `worker_error` is in the response file

The parent (host/app.py) relies on these conventions to distinguish
license errors from generic crashes.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

# PyStata is shipped with the Stata install. Tell Python where to find it.
# Users with a non-default install path should override PYSTATA_UTILITIES.
STATA_UTIL_DIR = os.environ.get(
    "PYSTATA_UTILITIES",
    "/Applications/StataNow/utilities",
)
if STATA_UTIL_DIR not in sys.path:
    sys.path.insert(0, STATA_UTIL_DIR)


# --- Result coercion -----------------------------------------------------

def _coerce(val: Any) -> Any:
    """JSON-safe coercion for values returned from sfi."""
    try:
        import numpy as np  # type: ignore

        if isinstance(val, np.ndarray):
            return _coerce(val.tolist())
        if isinstance(val, np.generic):
            val = val.item()
    except Exception:
        pass
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return val
    if isinstance(val, (list, tuple)):
        return [_coerce(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _coerce(v) for k, v in val.items()}
    if isinstance(val, (str, int, bool)) or val is None:
        return val
    try:
        num = float(val)
        if num != num or num in (float("inf"), float("-inf")):
            return None
        return num
    except Exception:
        return str(val)


def _resolve(name: str, sfi: Any) -> Any:
    """Resolve a capture name against Stata's sfi API.

    v1 supports: scalars, globals, and named matrices with row/col names.
    Locals and pattern fallbacks are intentionally out of scope.
    """
    # Explicit scalar(NAME) form.
    scalar_match = re.fullmatch(r"\s*scalar\(([^)]+)\)\s*", name)
    if scalar_match:
        try:
            return _coerce(sfi.Scalar.getValue(scalar_match.group(1).strip()))
        except Exception:
            return None

    # Try a plain scalar first.
    try:
        val = sfi.Scalar.getValue(name)
        if val is not None and val == val:
            return _coerce(val)
    except Exception:
        pass

    # Then a matrix (supports `e(b)`, `e(V)`, etc. shape).
    try:
        mat = sfi.Matrix.get(name)
        if mat is not None:
            try:
                rows = list(sfi.Matrix.getRowNames(name))
            except Exception:
                rows = []
            try:
                cols = list(sfi.Matrix.getColNames(name))
            except Exception:
                cols = []
            return {"values": _coerce(mat), "rows": rows, "cols": cols}
    except Exception:
        pass

    # Then a global macro.
    try:
        val = sfi.Macro.getGlobal(name)
        if val is not None:
            return val
    except Exception:
        pass
    return None


# --- Operations ----------------------------------------------------------

def _run_stata_job(
    *,
    edition: str,
    job_dir: Path,
    do_code: str,
    capture: list[str],
) -> dict[str, Any]:
    from pystata import config as _cfg  # type: ignore

    _cfg.init(edition)
    from pystata import stata as _stata  # type: ignore
    import sfi  # type: ignore

    job_dir.mkdir(parents=True, exist_ok=True)
    # A fresh working directory and a clean state for each job.
    prologue = (
        "capture log close _all\n"
        "clear all\n"
        "set more off\n"
        f'cd "{job_dir}"\n'
        "discard\n"
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    stata_error: str | None = None
    stata_rc = 0
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            _stata.run(prologue, quietly=True, echo=False, inline=False)
            _stata.run(do_code, quietly=False, echo=True, inline=False)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        rc_match = re.search(r"r\((\d+)\);", msg)
        if rc_match:
            stata_rc = int(rc_match.group(1))
            stata_error = msg.strip()
            stderr_buf.write(stata_error + "\n")
        else:
            raise

    captured: dict[str, Any] = {}
    for name in capture:
        captured[name] = _resolve(name, sfi)

    return {
        "rc": stata_rc,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "stata_error": stata_error,
        "captured": captured,
        "job_dir": str(job_dir),
    }


def _probe(edition: str) -> dict[str, Any]:
    from pystata import config as _cfg  # type: ignore

    _cfg.init(edition)
    import sfi  # type: ignore

    return {
        "ok": True,
        "edition": edition,
        "stata_version_number": _coerce(sfi.Scalar.getValue("c(stata_version)")),
    }


# --- Entry point ---------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: worker.py <request.json> <response.json>", file=sys.stderr)
        return 64

    request_path = Path(argv[1])
    response_path = Path(argv[2])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        op = request["op"]
        edition = request.get("edition", "mp")
        if op == "probe":
            response = _probe(edition)
        elif op == "run":
            response = _run_stata_job(
                edition=edition,
                job_dir=Path(request["job_dir"]),
                do_code=request["do_code"],
                capture=list(request.get("capture", [])),
            )
        else:
            raise ValueError(f"unknown worker op: {op}")
        response_path.write_text(
            json.dumps(response, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        detail = {
            "worker_error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        response_path.write_text(
            json.dumps(detail, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        if "license" in str(exc).lower():
            return 78
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
