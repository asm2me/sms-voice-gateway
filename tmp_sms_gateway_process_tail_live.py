from pathlib import Path

lines = Path("app/sms_handler.py").read_text(encoding="utf-8").splitlines()
for i in range(430, 621):
    print(f"{i}:{lines[i-1]}")
