from pathlib import Path

lines = Path("app/sms_handler.py").read_text(encoding="utf-8").splitlines()
for start, end in [(202, 260), (260, 340), (430, 520)]:
    print(f"### {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")
