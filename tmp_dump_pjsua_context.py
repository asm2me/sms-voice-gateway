from pathlib import Path

path = Path("app/pjsua2_service.py")
lines = path.read_text(encoding="utf-8").splitlines()

patterns = [
    "def place_outbound_call",
    "def status_detail",
    "_PJSUA_AUDIO_SETUP_LOCKS",
    "_TRUNK_CALL_STATES",
    "setup_lock",
]

seen = set()
for idx, line in enumerate(lines):
    if any(p in line for p in patterns):
        if idx in seen:
            continue
        seen.add(idx)
        start = max(0, idx - 30)
        end = min(len(lines), idx + 120)
        print(f"--- match line {idx + 1} ---")
        for i in range(start, end):
            print(f"{i + 1}: {lines[i]}")
        print()
