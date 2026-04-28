from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for start, end in [(930, 1185), (1185, 1325), (1325, 1415)]:
    print(f"### {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")
