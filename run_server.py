#!/usr/bin/env python3
"""Start a platform API server locally with --reload.

Usage:
    python3 run_server.py              # lakehouse (default)
    python3 run_server.py lakehouse    # lakehouse on :3002
    python3 run_server.py prbot        # prbot on :3003
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

SERVICES = {
    "lakehouse": {"port": "3002"},
    "prbot":     {"port": "3003"},
}

name = sys.argv[1] if len(sys.argv) > 1 else "lakehouse"

if name not in SERVICES:
    print(f"Unknown service: {name}")
    print(f"Available: {', '.join(SERVICES)}")
    sys.exit(1)

service_dir = REPO_ROOT / "services" / name
venv_python = service_dir / ".venv" / "bin" / "python3"
python = str(venv_python) if venv_python.exists() else sys.executable
port = SERVICES[name]["port"]

os.chdir(service_dir)

subprocess.run([
    python, "-m", "uvicorn",
    "src.main:app",
    "--host", "0.0.0.0",
    "--port", port,
    "--reload",
], check=True)
