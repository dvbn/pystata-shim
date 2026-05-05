# Smoke tests

These tests exercise the **shim layer itself** without requiring a
working Stata on the machine running the test. They verify:

- the server refuses to start with a too-short token
- the server refuses to start with a world-readable token file
- the server refuses to bind a non-loopback address by default
- `/health` returns a thin unauthenticated payload
- authenticated `/health` includes Stata version detail when a worker
  probe has reported it
- `POST /do`, `GET /job/{id}`, `POST /cancel/{id}` all require the
  token and return 401 without it
- pydantic validators reject oversize `do_code`, bad `capture` names,
  control-char `caller_tag`, and out-of-range `timeout_sec`
- the audit log is written with the expected row shape
- the client-side path-mapping helpers raise cleanly when misconfigured
- the client-side stdlib HTTP transport parses JSON, sends the token
  header, and reports HTTP errors without a real network

What the smoke tests do NOT cover:

- anything that requires PyStata to actually import and init
- end-to-end behavior against a real Stata install

For a real end-to-end check against a Mac with Stata, use the recipe
in `examples/docker-run/run.sh`.

## Running

```bash
# From the repo root:
python3 -m venv /tmp/pystata-shim-smoke-venv
/tmp/pystata-shim-smoke-venv/bin/python -m pip install -r requirements-smoke.txt
PYTHONDONTWRITEBYTECODE=1 /tmp/pystata-shim-smoke-venv/bin/python -m pytest tests/smoke/ -v
```

Using `PYTHONDONTWRITEBYTECODE=1` keeps the working tree free of
`__pycache__/` clutter while you run the tests.

Or run each script directly:

```bash
python3 tests/smoke/test_server_boot.py
python3 tests/smoke/test_auth_and_validation.py
python3 tests/smoke/test_client_paths.py
```

Every file can be run with a plain `python3 ...` and exits with a
non-zero code on failure so it works without pytest installed.
