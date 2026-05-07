"""Tests for the new container-session lifecycle contract:

- the launchd plist template does not enable RunAtLoad or KeepAlive
  by default (the shim is no longer a login-time daemon)
- setup_host.sh treats LaunchAgent install as an opt-in, not the
  default path
- the example Docker run wiring carries the lifecycle label
"""

from __future__ import annotations

import os
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
    # The public name is the only allowed one for the project's own
    # identity. The single intentional reference to the legacy
    # `com.openstata.pystata-shim.plist` label inside the foreign-
    # listener cleanup hint is whitelisted: an upgrading user may need
    # to remove that file. Keep that exception narrow.
    body_lower = body.lower()
    legacy_plist_string = "com.openstata.pystata-shim.plist"
    legacy_count = body_lower.count(legacy_plist_string)
    sanitized = body_lower.replace(legacy_plist_string, "")
    assert "openstata" not in sanitized, (
        "shimctl.sh references the legacy 'openstata' name in a place "
        "that is NOT the legacy-LaunchAgent cleanup hint; the only allowed "
        "use is the literal `com.openstata.pystata-shim.plist` filename"
    )
    assert legacy_count > 0, (
        "shimctl.sh must mention the legacy `com.openstata.pystata-shim.plist` "
        "filename in its foreign-listener cleanup hint so users upgrading "
        "from the old install know where to look"
    )
    # Must also reference the public reverse-DNS label for its own runtime.
    assert "com.pystata.shim" in body, (
        "shimctl.sh must reference the public com.pystata.shim label"
    )


def test_example_justfile_exists_and_uses_neutral_label():
    assert JUSTFILE.exists(), f"missing {JUSTFILE}"
    body = JUSTFILE.read_text(encoding="utf-8")
    assert "openstata" not in body.lower(), (
        "Justfile must not reference the legacy 'openstata' name"
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


def test_example_justfile_mounts_client_and_sets_pythonpath():
    """Without the client mount the container has no `stata-do` and
    cannot `import pystata_shim`. Without PYTHONPATH the Python import
    fails even when the directory is mounted. Without PATH the CLI is
    not on a non-login shell's lookup path. Lock all three."""
    body = JUSTFILE.read_text(encoding="utf-8")
    assert ":/opt/pystata-shim-client:ro" in body, (
        "examples/justfile/Justfile must bind-mount client/ at "
        "/opt/pystata-shim-client read-only"
    )
    assert "PYTHONPATH=/opt/pystata-shim-client" in body, (
        "examples/justfile/Justfile must set PYTHONPATH so "
        "`import pystata_shim` works inside the container"
    )
    assert "/opt/pystata-shim-client:" in body and 'PATH="' in body, (
        "examples/justfile/Justfile must put /opt/pystata-shim-client "
        "first on PATH so `stata-do` resolves in non-login shells"
    )


def test_example_justfile_mounts_profile_d_snippet():
    """The Justfile's `shell` recipe uses `bash -l` / `bash -lc`. On
    Debian-based images /etc/profile rewrites PATH from scratch,
    dropping anything we set via `docker create -e PATH=...`. The
    profile.d snippet bind-mount is what keeps `stata-do` discoverable
    in interactive login shells."""
    body = JUSTFILE.read_text(encoding="utf-8")
    assert "/etc/profile.d/pystata-shim.sh:ro" in body, (
        "examples/justfile/Justfile must bind-mount the profile.d "
        "snippet so login shells re-add /opt/pystata-shim-client to PATH"
    )


def test_profile_d_snippet_exists_and_idempotent():
    """The profile snippet has to exist on disk (the Justfile fails
    fast if missing) and must guard against double-prepending PATH or
    PYTHONPATH; profile is sourced on every login and many container
    workflows nest login shells."""
    snippet = REPO / "client" / "profile.d" / "pystata-shim.sh"
    assert snippet.exists(), f"missing profile.d snippet at {snippet}"
    body = snippet.read_text(encoding="utf-8")
    # Idempotency check: must inspect PATH before prepending.
    assert '*":/opt/pystata-shim-client:"*' in body, (
        "profile.d snippet must guard against duplicating "
        "/opt/pystata-shim-client on PATH when sourced repeatedly"
    )
    # Both PATH and PYTHONPATH must end up exported. Allow either a
    # single combined `export PATH PYTHONPATH` or two separate exports.
    has_path_export = (
        "export PATH" in body and ("PATH PYTHONPATH" in body or "export PATH=" in body or body.count("export PATH") >= 1)
    )
    has_pythonpath_export = "PYTHONPATH" in body and ("PATH PYTHONPATH" in body or "export PYTHONPATH" in body)
    assert has_path_export and has_pythonpath_export, (
        "profile.d snippet must export both PATH and PYTHONPATH"
    )


def test_profile_d_snippet_executes_correctly_under_real_shell():
    """Behavior-level check: source the snippet twice in a fresh /bin/sh
    under both empty and populated PYTHONPATH, and verify that
    /opt/pystata-shim-client lands exactly once at the front of PATH and
    PYTHONPATH (and that the snippet is parse-clean)."""
    import subprocess

    snippet = REPO / "client" / "profile.d" / "pystata-shim.sh"

    scenarios = [
        # (label, env-overrides, expected-prefix-occurrences)
        ("unset PYTHONPATH", {"PYTHONPATH": None}, 1),
        ("empty PYTHONPATH",  {"PYTHONPATH": ""}, 1),
        ("populated PYTHONPATH without ours",
         {"PYTHONPATH": "/x:/y"}, 1),
        ("populated PYTHONPATH already containing ours",
         {"PYTHONPATH": "/x:/opt/pystata-shim-client:/y"}, 1),
    ]

    for label, env_changes, expected_count in scenarios:
        env = dict(os.environ)
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
        for k, v in env_changes.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        # Source twice to exercise the idempotency guard.
        cmd = (
            f". {snippet}; . {snippet}; "
            "printf 'PATH=%s\\nPYTHONPATH=%s\\n' \"$PATH\" \"${PYTHONPATH:-}\""
        )
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, (
            f"[{label}] /bin/sh exited {proc.returncode}; stderr={proc.stderr!r}"
        )
        out = proc.stdout
        path_line = next(
            (line for line in out.splitlines() if line.startswith("PATH=")),
            None,
        )
        pythonpath_line = next(
            (line for line in out.splitlines() if line.startswith("PYTHONPATH=")),
            None,
        )
        assert path_line is not None, f"[{label}] missing PATH line in {out!r}"
        assert pythonpath_line is not None, (
            f"[{label}] missing PYTHONPATH line in {out!r}"
        )
        path_value = path_line[len("PATH="):]
        pp_value = pythonpath_line[len("PYTHONPATH="):]
        # Each should contain /opt/pystata-shim-client exactly once.
        path_count = path_value.split(":").count("/opt/pystata-shim-client")
        pp_count = pp_value.split(":").count("/opt/pystata-shim-client")
        assert path_count == expected_count, (
            f"[{label}] expected /opt/pystata-shim-client to appear "
            f"{expected_count}x in PATH after double-source, got {path_count}: "
            f"{path_value}"
        )
        assert pp_count == expected_count, (
            f"[{label}] expected /opt/pystata-shim-client to appear "
            f"{expected_count}x in PYTHONPATH after double-source, got "
            f"{pp_count}: {pp_value}"
        )
        # And the prepended copy must come first.
        assert path_value.split(":")[0] == "/opt/pystata-shim-client", (
            f"[{label}] /opt/pystata-shim-client must be first on PATH; got "
            f"{path_value}"
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
