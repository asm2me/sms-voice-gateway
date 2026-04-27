from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
start = 2280
end = 2365
for i in range(start - 1, min(len(lines), end)):
    print(f"{i+1}: {lines[i]}")
