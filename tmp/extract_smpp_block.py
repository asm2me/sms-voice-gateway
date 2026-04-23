from pathlib import Path

path = Path("app/main.py")
lines = path.read_text(encoding="utf-8").splitlines()

patterns = [
    "def _normalize_static_message_part_audio",
    "def _store_smpp_account_uploaded_audio",
    "async def admin_add_smpp_account",
    "def _resolve_uploaded_smpp_part_audio",
    "static_message_digit_audio",
]

for pattern in patterns:
    print(f"\n=== {pattern} ===")
    for idx, line in enumerate(lines):
        if pattern in line:
            start = max(0, idx - 40)
            end = min(len(lines), idx + 220)
            for i in range(start, end):
                print(f"{i+1}: {lines[i]}")
            break
    else:
        print("NOT FOUND")
