from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
keys = ["class CallCallback", "def onCallState", "def onCallMediaState", "def place_call"]
for key in keys:
    idx = next((i for i, line in enumerate(lines) if key in line), None)
    print(f"\n### {key} ###")
    if idx is None:
        print("NOT FOUND")
    else:
        start = max(0, idx - 5)
        end = min(len(lines), idx + 120)
        print("\n".join(lines[start:end]))
