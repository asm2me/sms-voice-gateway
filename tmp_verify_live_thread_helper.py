from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for needle in ("def __init__(self, settings: Settings, *, isolated: bool = False)", "def _register_current_thread", "_thread_registration_token", "registered_endpoint_keys"):
    for idx, line in enumerate(lines, 1):
        if needle in line:
            start = max(1, idx - 15)
            end = min(len(lines), idx + 120)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
