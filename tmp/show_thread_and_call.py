from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
targets = (
    "def _register_current_thread(",
    "def _wait_for_registration(",
    "def place_outbound_call(",
)

for i, line in enumerate(lines):
    stripped = line.lstrip()
    if any(stripped.startswith(t) for t in targets):
        start = max(0, i - 20)
        end = min(len(lines), i + 220)
        print("=== BLOCK ===")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
