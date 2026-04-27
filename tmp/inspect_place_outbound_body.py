from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
start = 520
end = 780
for j in range(start - 1, min(len(lines), end)):
    print(f"{j+1}: {lines[j]}")
