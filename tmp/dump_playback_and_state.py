from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for start, end in [(1853, 1965), (2260, 2348)]:
    print(f"=== RANGE {start}-{end} ===")
    for i in range(start - 1, min(len(lines), end)):
        print(f"{i+1}: {lines[i]}")
