#!/usr/bin/env python3
"""Start the lakehouse API server.

Run from anywhere in the repo:
    python3 run_server.py
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SERVICE_DIR = REPO_ROOT / "services" / "lakehouse"
VENV_PYTHON = SERVICE_DIR / ".venv" / "bin" / "python3"

# Use the venv python if present, otherwise fall back to whatever is running this script
python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

os.chdir(SERVICE_DIR)

subprocess.run([
    python, "-m", "uvicorn",
    "src.main:app",
    "--host", "0.0.0.0",
    "--port", "3002",
    "--reload",
], check=True)
