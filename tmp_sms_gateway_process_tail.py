from pathlib import Path

lines = Path("app/sms_handler.py").read_text(encoding="utf-8").splitlines()
for i in range(457, min(650, len(lines)) + 1):
    print(f"{i}:{lines[i-1]}")
