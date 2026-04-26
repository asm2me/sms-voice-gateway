from pathlib import Path

path = Path("app/pjsua2_service.py")
lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

for start, end in ((1760, 2018), (2288, 2368)):
    print(f"--- {start}-{end} ---")
    for i in range(start - 1, min(end, len(lines))):
        print(f"{i + 1}: {lines[i]}")
