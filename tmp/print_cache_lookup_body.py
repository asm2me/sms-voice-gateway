from pathlib import Path

lines = Path("app/cache.py").read_text(encoding="utf-8").splitlines()
start = 63
end = 140
for i in range(start - 1, min(len(lines), end)):
    print(f"{i+1}: {lines[i]}")
