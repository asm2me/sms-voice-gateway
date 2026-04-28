from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

for needle in ("def __init__(self, settings: Settings, *, isolated: bool = False)", "def build_pjsua2_service"):
    for idx, line in enumerate(lines, 1):
        if line.startswith(needle):
            start = max(1, idx - 12)
            end = min(len(lines), idx + 180)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
