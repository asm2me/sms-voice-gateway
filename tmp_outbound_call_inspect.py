from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

for needle in ("def _acquire_concurrency_slot", "def place_outbound_call", "def _wait_for_registration"):
    for idx, line in enumerate(lines, 1):
        if line.startswith(needle):
            start = max(1, idx - 12)
            end = min(len(lines), idx + 220)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
