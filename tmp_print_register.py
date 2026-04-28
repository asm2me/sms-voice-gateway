from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for idx, line in enumerate(lines, 1):
    if line.startswith("def _register_current_thread"):
        start = max(1, idx - 12)
        end = min(len(lines), idx + 120)
        print(f"### _register_current_thread @ {idx}")
        for i in range(start, end + 1):
            print(f"{i}:{lines[i-1]}")
        break
