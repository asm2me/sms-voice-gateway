from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

ranges = [(1, 1)]
for needle in (
    "def _pump_events",
    "def _release_playback_bridge",
    "def set_playback_context",
    "def attach_call",
    "def _maybe_start_playback",
):
    for idx, line in enumerate(lines, 1):
        if needle in line:
            ranges.append((idx, idx + 90))
            break

for start, end in ranges[1:]:
    print(f"### {start}")
    for i in range(start, min(len(lines) + 1, end)):
        print(f"{i}:{lines[i-1]}")
    print("---")
