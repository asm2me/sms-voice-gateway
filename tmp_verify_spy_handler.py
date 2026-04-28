from pathlib import Path

lines = Path("app/main.py").read_text(encoding="utf-8").splitlines()
start = 4037
end = 4105
for i in range(start, min(end, len(lines)) + 1):
    print(f"{i}:{lines[i-1]}")
