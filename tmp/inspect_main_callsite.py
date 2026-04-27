from pathlib import Path

lines = Path("app/main.py").read_text(encoding="utf-8").splitlines()
for start, end in [(1, 260), (260, 520)]:
    print(f"=== RANGE {start}-{end} ===")
    for i in range(start - 1, min(len(lines), end)):
        print(f"{i+1}: {lines[i]}")
