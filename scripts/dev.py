from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    subprocess.check_call(cmd)


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a virtualenv, install deps, and optionally run the app.")
    parser.add_argument("--setup-only", action="store_true", help="Only create the virtualenv and install dependencies.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / ".env")

    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")

    venv_dir = root / ".venv"
    py = venv_python(venv_dir)

    if not py.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)])

    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-r", str(root / "requirements.txt")])

    if args.setup_only:
        print("Setup complete.")
        return 0

    run([str(py), "-m", "uvicorn", "app.main:app", "--host", host, "--port", port, "--reload"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
