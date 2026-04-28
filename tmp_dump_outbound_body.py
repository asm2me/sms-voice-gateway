from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

for start, end, label in [
    (900, 1045, "place_outbound_call"),
    (520, 620, "_register_current_thread"),
]:
    print(f"### {label} {start}-{end}")
    for i in range(start, min(end, len(lines)) + 1):
        print(f"{i}:{lines[i-1]}")
    print("---")
