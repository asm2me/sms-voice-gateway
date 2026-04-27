from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
start = 1801
end = 1853
for i in range(start - 1, min(len(lines), end)):
    print(f"{i+1}: {lines[i]}")
