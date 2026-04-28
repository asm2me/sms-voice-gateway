from pathlib import Path

def dump(path: str, ranges: list[tuple[int, int]]) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for start, end in ranges:
        print(f"### {path} {start}-{end}")
        for i in range(start, min(end, len(lines)) + 1):
            print(f"{i}:{lines[i-1]}")
        print("---")

dump("app/pjsua2_service.py", [(1045, 1188), (2686, 2765), (497, 620)])
dump("app/sms_handler.py", [(200, 340), (430, 520)])
dump("app/main.py", [(1508, 1605), (2758, 2868)])
