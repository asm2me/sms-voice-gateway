from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
needles = ("def _register_current_thread", "def place_outbound_call", "def _wait_for_registration")

for i, line in enumerate(lines):
    if any(line.lstrip().startswith(needle) for needle in needles):
        start = max(0, i - 25)
        end = min(len(lines), i + 260)
        print("=== BLOCK ===")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
