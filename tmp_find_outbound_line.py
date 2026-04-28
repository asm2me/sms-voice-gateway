from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
for idx, line in enumerate(lines, 1):
    if line.startswith("    def place_outbound_call") or line.startswith("def place_outbound_call") or line.startswith("    def _acquire_concurrency_slot") or line.startswith("def _acquire_concurrency_slot"):
        print(f"{idx}:{line.strip()}")
