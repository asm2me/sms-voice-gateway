from pathlib import Path

def dump(path: str, ranges: list[tuple[int, int]]) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for start, end in ranges:
        print(f"### {path} {start}-{end}")
        for i in range(start, min(end, len(lines)) + 1):
            print(f"{i}:{lines[i-1]}")
        print("---")

dump("app/sms_handler.py", [(470, 645)])
dump("app/pjsua2_service.py", [(281, 345), (2362, 2418)])
dump("app/main.py", [(4037, 4095)])
 