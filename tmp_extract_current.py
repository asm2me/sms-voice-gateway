from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()

targets = [
    ("_release_player", 280, 120),
    ("_flush_retired_players", 330, 120),
    ("wait_for_completion", 2550, 180),
]

for name, start, count in targets:
    print(f"### {name} @ {start}")
    end = min(len(lines) + 1, start + count)
    for i in range(start, end):
        print(f"{i}:{lines[i-1]}")
    print("---")
