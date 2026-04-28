from pathlib import Path

def dump(path: str, needle: str, before: int = 12, after: int = 180) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines, 1):
        if needle in line:
            start = max(1, idx - before)
            end = min(len(lines), idx + after)
            print(f"### {path}:{needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break

dump("app/pjsua2_service.py", "def _register_current_thread")
dump("app/main.py", "def _run_admin_test_send_job")
dump("app/main.py", "async def admin_tools_test_send")
dump("app/main.py", "async def admin_tools_test_send_status")
