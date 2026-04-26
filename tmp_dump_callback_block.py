from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
start = 1985
end = min(2105, len(lines))
for i in range(start, end):
    print(f"{i + 1:04d}: {lines[i]}")
