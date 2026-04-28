from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for i in range(914, 1046):
    print(f"{i}:{lines[i-1]}")
