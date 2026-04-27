from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
needles = ("def _maybe_start_playback", "def _try_start_playback", "def wait_for_completion")
for i, line in enumerate(lines):
    if any(n in line for n in needles):
        start = max(0, i - 20)
        end = min(len(lines), i + 180)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
