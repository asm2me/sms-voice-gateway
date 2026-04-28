from pathlib import Path

def dump(path: str, needles: list[str], before: int = 12, after: int = 180) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for needle in needles:
        for idx, line in enumerate(lines, 1):
            if line.startswith(needle):
                start = max(1, idx - before)
                end = min(len(lines), idx + after)
                print(f"### {path}:{needle} @ {idx}")
                for i in range(start, end + 1):
                    print(f"{i}:{lines[i-1]}")
                print("---")
                break

dump("app/pjsua2_service.py", ["def _register_current_thread", "def build_pjsua2_service"])
dump("app/main.py", ["def _run_admin_test_send_job", "async def admin_tools_test_send", "async def admin_tools_test_send_status"])
