from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

starts = [409, 1179, 1637, 1640, 1844, 1892, 2225, 2405, 2490]
window = 40
seen = set()

for s in starts:
    if s in seen:
        continue
    seen.add(s)
    print(f"### {s}")
    end = min(len(lines), s + window)
    for i in range(s, end):
        print(f"{i}:{lines[i-1]}")
    print("---")
