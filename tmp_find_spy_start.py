from pathlib import Path

lines = Path("app/main.py").read_text(encoding="utf-8").splitlines()
for idx, line in enumerate(lines, 1):
    if line.startswith("async def admin_spy_start"):
        start = max(1, idx - 12)
        end = min(len(lines), idx + 120)
        print(f"### admin_spy_start @ {idx}")
        for i in range(start, end + 1):
            print(f"{i}:{lines[i-1]}")
        break
