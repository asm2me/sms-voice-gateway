from pathlib import Path

lines = Path("app/main.py").read_text(encoding="utf-8").splitlines()

for needle in ("def _run_admin_test_send_job", "async def admin_tools_test_send", "async def admin_tools_test_send_status"):
    for idx, line in enumerate(lines, 1):
        if line.startswith(needle):
            start = max(1, idx - 12)
            end = min(len(lines), idx + 220)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
