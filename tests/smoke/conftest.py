"""Path bootstrap so `import app` and `import pystata_shim` work."""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST_DIR = REPO / "host"
CLIENT_DIR = REPO / "client"

for p in (HOST_DIR, CLIENT_DIR):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

# Tests configure PYSTATA_SHIM_TOKEN / PYSTATA_SHIM_HOME themselves.
os.environ.pop("PYSTATA_SHIM_HOST", None)
os.environ.pop("PYSTATA_SHIM_ALLOW_NON_LOOPBACK", None)
