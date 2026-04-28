from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

ranges = [(2463, 2575), (2575, 2685)]
for start, end in ranges:
    print(f"### {start}")
    for i in range(start, min(len(lines) + 1, end)):
        print(f"{i}:{lines[i-1]}")
    print("---")
