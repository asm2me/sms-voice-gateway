from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for start, end in [(640, 950)]:
    print(f"=== RANGE {start}-{end} ===")
    for j in range(start - 1, min(len(lines), end)):
        print(f"{j+1}: {lines[j]}")
