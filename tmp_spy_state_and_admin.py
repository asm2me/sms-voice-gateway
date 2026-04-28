from pathlib import Path

def dump(path: str, start: int, end: int) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"### {path} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")

dump("app/pjsua2_service.py", 2360, 2415)
dump("app/pjsua2_service.py", 2460, 2525)
dump("app/main.py", 4037, 4095)
