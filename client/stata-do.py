#!/usr/bin/env python3
"""stata-do — run a local `.do` file through the pystata-shim host service.

Usage:
    stata-do /workspace/path/to/file.do
    stata-do /workspace/path/to/file.do --cwd /workspace
    stata-do /workspace/path/to/file.do --timeout 1800 --tag my-agent
    stata-do --health

The `.do` file must live under PYSTATA_SHIM_CONTAINER_WORKSPACE (default
`/workspace`). The container path is translated to the matching host
path before `cd` / `do` are assembled. Write outputs with paths relative
to `--cwd`, not absolute container paths.

Exit codes:
    0  on success with stata_rc == 0
    2  on shim-level error (auth, transport, timeout, crash, bad args)
    N  where N is the non-zero Stata return code from Stata itself

Environment variables (see client/pystata_shim.py for full list):
    PYSTATA_SHIM_BASE_URL
    PYSTATA_SHIM_TOKEN
    PYSTATA_SHIM_CONTAINER_WORKSPACE
    PYSTATA_SHIM_HOST_WORKSPACE
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `pystata_shim` importable whether the client dir is on sys.path
# or stata-do.py is dropped somewhere else that can still find it.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pystata_shim  # noqa: E402


def _format_health(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stata-do",
        description="Run a .do file through the pystata-shim host service.",
    )
    parser.add_argument(
        "do_file",
        nargs="?",
        help="path to the .do file (must be under the container workspace)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="container-side working directory for the run (default: parent of do_file)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="request timeout in seconds (default: 1800, max: 3600)",
    )
    parser.add_argument(
        "--tag",
        default="",
        help="opaque caller tag written to the shim audit log",
    )
    parser.add_argument(
        "--capture",
        action="append",
        default=[],
        help=(
            "Stata name to capture after the run, e.g. 'e(b)' or 'e(N)'."
            " Quote values containing parentheses to keep the shell from"
            " parsing them as a function definition. Repeatable."
        ),
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="print the authenticated health payload and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full shim JSON response instead of stdout/stderr",
    )
    args = parser.parse_args(argv)

    SHIM_ERROR = 2
    if args.health:
        try:
            payload = pystata_shim.health_full()
        except pystata_shim.ShimError as exc:
            print(f"stata-do: health failed: {exc}", file=sys.stderr)
            return SHIM_ERROR
        print(_format_health(payload))
        return 0 if payload.get("ok") else SHIM_ERROR

    if not args.do_file:
        parser.error("do_file is required unless --health is given")

    try:
        result = pystata_shim.run_do_file(
            args.do_file,
            working_dir=args.cwd,
            capture=args.capture,
            timeout_sec=args.timeout,
            caller_tag=args.tag,
        )
    except pystata_shim.ShimError as exc:
        print(f"stata-do: {exc}", file=sys.stderr)
        return SHIM_ERROR

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        stdout_text = result.get("stdout", "")
        stderr_text = result.get("stderr", "")
        if stdout_text:
            sys.stdout.write(stdout_text)
            if not stdout_text.endswith("\n"):
                sys.stdout.write("\n")
        if stderr_text:
            sys.stderr.write(stderr_text)
            if not stderr_text.endswith("\n"):
                sys.stderr.write("\n")

    if not result.get("ok"):
        code = result.get("error_code", "error")
        detail = result.get("error_detail", "")
        print(f"stata-do: shim error: {code}: {detail}", file=sys.stderr)
        return SHIM_ERROR

    return int(result.get("stata_rc") or 0)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
