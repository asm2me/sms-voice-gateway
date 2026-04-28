from pathlib import Path

def dump(path: str, ranges: list[tuple[int, int]]) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for start, end in ranges:
        print(f"### {path} {start}-{end}")
        for i in range(start, min(end, len(lines)) + 1):
            print(f"{i}:{lines[i-1]}")
        print("---")

dump("app/pjsua2_service.py", [(280, 360), (2353, 2415), (2465, 2545)])
dump("app/main.py", [(4037, 4095)])
