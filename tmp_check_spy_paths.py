from pathlib import Path

def dump(path: str, start: int, end: int) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"### {path} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")

dump("app/main.py", 4038, 4095)
dump("app/pjsua2_service.py", 280, 320)
dump("app/pjsua2_service.py", 2353, 2445)
