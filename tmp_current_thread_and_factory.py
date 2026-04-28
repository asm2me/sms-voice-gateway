from pathlib import Path

def dump(path: str, start: int, end: int) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"### {path} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")

dump("app/pjsua2_service.py", 497, 620)
dump("app/pjsua2_service.py", 2958, 3015)
dump("app/main.py", 1508, 1605)
dump("app/main.py", 2758, 2868)
