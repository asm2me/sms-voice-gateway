from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for start, end in [(1045, 1185), (2362, 2465), (2686, 2765)]:
    print(f"### {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")
