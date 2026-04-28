from pathlib import Path

lines = Path("app/main.py").read_text(encoding="utf-8").splitlines()
for idx, line in enumerate(lines, 1):
    if line.startswith("def _run_admin_test_send_job"):
        start = max(1, idx - 12)
        end = min(len(lines), idx + 220)
        print(f"### _run_admin_test_send_job @ {idx}")
        for i in range(start, end + 1):
            print(f"{i}:{lines[i-1]}")
        break
