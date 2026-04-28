from pathlib import Path

def dump(path: str, ranges: list[tuple[int, int]]) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for start, end in ranges:
        print(f"### {path} {start}-{end}")
        for i in range(start, min(end, len(lines)) + 1):
            print(f"{i}:{lines[i-1]}")
        print("---")

dump("app/main.py", [(1508, 1605), (2758, 2868), (3008, 3055), (4037, 4095)])
dump("app/sms_handler.py", [(202, 340), (430, 520)])
