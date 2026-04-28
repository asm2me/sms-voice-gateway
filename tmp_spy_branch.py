from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

needles = [
    'bucket["error"] = "call audio media not available yet"',
    'def _process_spy_commands',
]

for needle in needles:
    for idx, line in enumerate(lines, 1):
        if needle in line:
            start = max(1, idx - 20)
            end = min(len(lines), idx + 80)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
