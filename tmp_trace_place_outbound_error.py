from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

for needle in ("call audio media not available yet", "def place_outbound_call", "_release_slot(", "concurrency limit reached"):
    for idx, line in enumerate(lines, 1):
        if needle in line or line.startswith(needle):
            start = max(1, idx - 25)
            end = min(len(lines), idx + 220)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
