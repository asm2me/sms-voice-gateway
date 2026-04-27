from pathlib import Path

for file_path, ranges in (
    ("app/tts_service.py", [(517, 620)]),
    ("app/cache.py", [(63, 140)]),
):
    lines = Path(file_path).read_text(encoding="utf-8").splitlines()
    print(f"=== FILE {file_path} ===")
    for start, end in ranges:
        print(f"=== RANGE {start}-{end} ===")
        for i in range(start - 1, min(len(lines), end)):
            print(f"{i+1}: {lines[i]}")
