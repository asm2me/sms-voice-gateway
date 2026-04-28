from pathlib import Path

def dump(path: str, start: int, end: int) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"### {path} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")

dump("app/sms_handler.py", 470, 670)
dump("app/pjsua2_service.py", 279, 370)
dump("app/pjsua2_service.py", 2362, 2415)
