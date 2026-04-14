from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without .md extension)."""
    path = _DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent prompt not found: {path}")
    return path.read_text()


def load_asset(filename: str) -> str:
    """Load a raw asset file by filename (e.g. 'workspace_mcp_server.py')."""
    path = _DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Asset not found: {path}")
    return path.read_text()
