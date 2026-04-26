from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
patterns = [
    'scope_key = str(scope or "default").strip() or "default"',
    "def get_",
    "return session",
    "_PJSUA_GLOBAL_SESSIONS",
]

seen = set()
for pat in patterns:
    matches = [i for i, line in enumerate(lines) if pat in line]
    for idx in matches[:1]:
        if idx in seen:
            continue
        seen.add(idx)
        print("###", pat, idx + 1)
        start = max(0, idx - 25)
        end = min(len(lines), idx + 80)
        for i in range(start, end):
            print(f"{i + 1:04d}: {lines[i]}")
        print("---")
