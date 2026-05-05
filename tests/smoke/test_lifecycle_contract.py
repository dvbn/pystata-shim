"""Tests for the new container-session lifecycle contract:

- the launchd plist template does not enable RunAtLoad or KeepAlive
  by default (the shim is no longer a login-time daemon)
- setup_host.sh treats LaunchAgent install as an opt-in, not the
  default path
- the example Docker run wiring carries the lifecycle label
"""

from __future__ import annotations

import plistlib
import re
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLIST_TEMPLATE = REPO / "host" / "launchd" / "com.pystata.shim.plist.template"
SETUP_SCRIPT = REPO / "host" / "scripts" / "setup_host.sh"
SHIMCTL_SCRIPT = REPO / "host" / "scripts" / "shimctl.sh"
DOCKER_RUN_SCRIPT = REPO / "examples" / "docker-run" / "run.sh"
JUSTFILE = REPO / "examples" / "justfile" / "Justfile"


def _render_plist_with_dummy_substitutions() -> dict:
    """Render the plist template with throwaway substitutions and parse
    the resulting dict. This is what we actually ship to launchctl, so
    XML comments and example snippets do not pollute the assertion."""
    body = PLIST_TEMPLATE.read_text(encoding="utf-8")
    subs = {
        "__START_SCRIPT__": "/tmp/fake_start.sh",
        "__PYSTATA_SHIM_HOME__": "/tmp/fake_home",
        "__PYSTATA_SHIM_HOST__": "127.0.0.1",
        "__PYSTATA_SHIM_PORT__": "8787",
        "__PYSTATA_SHIM_ALLOW_UNSAFE_DO__": "0",
        "__PYSTATA_EDITION__": "mp",
        "__PYSTATA_UTILITIES__": "/Applications/StataNow/utilities",
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    with tempfile.NamedTemporaryFile("w+", suffix=".plist", delete=False) as f:
        f.write(body)
        path = Path(f.name)
    with open(path, "rb") as f:
        return plistlib.load(f)


def test_plist_template_has_no_run_at_load_default():
    rendered = _render_plist_with_dummy_substitutions()
    assert rendered.get("RunAtLoad", False) is False, (
        "plist template must not enable RunAtLoad by default — the shim "
        "is now a container-session service, not a login-time daemon"
    )


def test_plist_template_has_no_keep_alive_default():
    rendered = _render_plist_with_dummy_substitutions()
    val = rendered.get("KeepAlive", False)
    # KeepAlive can be a bool OR a dict; only enabled if true-ish.
    if isinstance(val, dict):
        # A dict counts as enabled even if all conditions are False:
        # launchd treats the presence of the key as opt-in.
        assert not val, "plist template must not configure KeepAlive at all"
    else:
        assert val is False, (
            "plist template must not enable KeepAlive by default"
        )


def test_setup_host_treats_launch_agent_as_opt_in():
    body = SETUP_SCRIPT.read_text(encoding="utf-8")
    # The setup script must read INSTALL_LAUNCH_AGENT (or equivalent)
    # before doing any plist render or launchctl call.
    assert "INSTALL_LAUNCH_AGENT" in body, (
        "setup_host.sh must expose an opt-in flag for LaunchAgent install"
    )


def test_setup_host_prints_next_step_pointing_at_shimctl():
    body = SETUP_SCRIPT.read_text(encoding="utf-8")
    # Default path must guide the user toward the new lifecycle entry
    # point. We only require shimctl.sh to be mentioned so the message
    # text itself can change without breaking the contract.
    assert "shimctl" in body, (
        "setup_host.sh must point users at shimctl.sh as the new "
        "default way to start the shim"
    )


def test_shimctl_uses_neutral_public_label():
    body = SHIMCTL_SCRIPT.read_text(encoding="utf-8")
    assert "openstata" not in body.lower(), (
        "shimctl.sh leaks the private 'openstata' name; public repo must "
        "use neutral pystata-shim naming"
    )
    # Must reference the public reverse-DNS label.
    assert "com.pystata.shim" in body, (
        "shimctl.sh must reference the public com.pystata.shim label"
    )


def test_example_justfile_exists_and_uses_neutral_label():
    assert JUSTFILE.exists(), f"missing {JUSTFILE}"
    body = JUSTFILE.read_text(encoding="utf-8")
    assert "openstata" not in body.lower(), (
        "Justfile leaks the private 'openstata' name"
    )
    assert "com.pystata.shim.enabled=true" in body, (
        "example Justfile must use the public lifecycle label"
    )
    # Sanity: every required recipe is present.
    for recipe in ("create", "start", "stop", "status"):
        assert re.search(rf"^{recipe}\b", body, flags=re.MULTILINE), (
            f"example Justfile missing required recipe `{recipe}`"
        )


def test_example_justfile_passes_the_token():
    """Without `-e PYSTATA_SHIM_TOKEN` in `docker create`, every /do
    call from the resulting container 401s. The example wrapper that
    is the centerpiece of the lifecycle story has to actually work
    end-to-end."""
    body = JUSTFILE.read_text(encoding="utf-8")
    assert "-e PYSTATA_SHIM_TOKEN" in body, (
        "examples/justfile/Justfile must pass PYSTATA_SHIM_TOKEN to the "
        "container at create time, otherwise the wrapper produces a "
        "container that 401s on every request"
    )


def test_plist_template_substitutions_complete():
    """The plist template uses __PLACEHOLDER__ markers for every value
    setup_host.sh substitutes in. If a marker is left unreplaced, the
    plist will be loaded with literal `__START_SCRIPT__` etc. and the
    LaunchAgent will silently fail. Render with the dummy values that
    setup_host.sh would supply and assert no `__` placeholder remains."""
    rendered = _render_plist_with_dummy_substitutions()
    # plistlib.load returns a dict; we inspect the values for any
    # leftover __FOO__ token that should have been replaced.
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _walk(v)
        elif isinstance(obj, str):
            yield obj
    leftovers = [s for s in _walk(rendered) if "__" in s and s.startswith("__")]
    assert not leftovers, (
        f"plist template has unreplaced placeholder values: {leftovers}"
    )


def test_docker_run_example_carries_lifecycle_label():
    body = DOCKER_RUN_SCRIPT.read_text(encoding="utf-8")
    # The label has to be present so stop-if-unused can find the
    # container. We accept either form to allow minor reformatting.
    has_label = (
        "label=com.pystata.shim.enabled=true" in body
        or "--label com.pystata.shim.enabled=true" in body
        or "--label" in body and "com.pystata.shim.enabled" in body
    )
    assert has_label, (
        "examples/docker-run/run.sh must add the lifecycle label so "
        "shimctl.sh stop-if-unused can detect this container"
    )


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
