from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
needles = (
    "def onCallMediaState",
    "def onCallState",
    "def _release_playback_bridge",
    "def _release_slot",
)
for i, line in enumerate(lines):
    if any(n in line for n in needles):
        start = max(0, i - 20)
        end = min(len(lines), i + 180)
        print("---")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
