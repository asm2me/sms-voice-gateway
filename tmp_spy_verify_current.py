from pathlib import Path

def dump(path: str, needle: str, before: int = 8, after: int = 120) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines, 1):
        if line.startswith(needle):
            start = max(1, idx - before)
            end = min(len(lines), idx + after)
            print(f"### {path}:{needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break

dump("app/main.py", "async def admin_spy_start")
dump("app/pjsua2_service.py", "def request_spy_start")
dump("app/pjsua2_service.py", "def _process_spy_commands")
