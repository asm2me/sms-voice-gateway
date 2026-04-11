from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    subprocess.check_call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a virtualenv, install deps, and optionally run the app.")
    parser.add_argument("--setup-only", action="store_true", help="Only create the virtualenv and install dependencies.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    venv_dir = root / ".venv"
    py = venv_dir / "Scripts" / "python.exe"
    pip = venv_dir / "Scripts" / "pip.exe"

    if not venv_dir.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)])

    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(pip), "install", "-r", str(root / "requirements.txt")])

    if args.setup_only:
        print("Setup complete.")
        return 0

    run([str(py), "-m", "uvicorn", "app.main:app", "--reload"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
