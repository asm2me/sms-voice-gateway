from pathlib import Path

def dump(path: str, start: int, end: int) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    print(f"### {path} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")

dump("app/sms_handler.py", 200, 340)
dump("app/sms_handler.py", 430, 520)
dump("app/pjsua2_service.py", 505, 620)
dump("app/pjsua2_service.py", 914, 1165)
