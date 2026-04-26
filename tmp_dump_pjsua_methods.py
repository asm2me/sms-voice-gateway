from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
patterns = [
    "def _try_start_playback",
    "def onCallMediaState",
    "def onCallState",
    "class _CallCallbackHolder",
]

seen = set()
for pat in patterns:
    idx = next(i for i, line in enumerate(lines) if pat in line)
    if idx in seen:
        continue
    seen.add(idx)
    print("###", pat, idx + 1)
    start = max(0, idx - 20)
    end = min(len(lines), idx + 120)
    for i in range(start, end):
        print(f"{i + 1:04d}: {lines[i]}")
    print("---")
