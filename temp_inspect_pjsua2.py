from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8", errors="replace").splitlines()

ranges = [
    (1780, 2015),
    (2140, 2275),
    (2355, 2415),
]
for start, end in ranges:
    print(f"--- {start}-{end} ---")
    for i in range(start - 1, min(len(lines), end)):
        print(f"{i + 1}: {lines[i]}")
