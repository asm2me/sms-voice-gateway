from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

for needle in ("def _process_spy_commands", "def request_spy_start", "def get_spy_state"):
    for idx, line in enumerate(lines, 1):
        if needle in line:
            start = max(1, idx - 10)
            end = min(len(lines), idx + 220)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
