from pathlib import Path

path = Path("app/pjsua2_service.py")
lines = path.read_text(encoding="utf-8").splitlines()

ranges = [
    (1, 260),
    (880, 1260),
    (1590, 1885),
]

for start, end in ranges:
    print(f"--- lines {start}-{end} ---")
    for i in range(start - 1, min(len(lines), end)):
        print(f"{i + 1}: {lines[i]}")
    print()
