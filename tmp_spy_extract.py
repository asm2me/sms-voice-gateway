from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

targets = [
    "def request_spy_start",
    "def request_spy_stop",
    "def get_spy_state",
    "def _process_spy_commands",
    "def _stop_spy_recorder",
    "def wait_for_completion",
]

for target in targets:
    for idx, line in enumerate(lines, 1):
        if target in line:
            print(f"### {target} @ {idx}")
            start = max(1, idx - 10)
            end = min(len(lines), idx + 80)
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
