from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
needles = ["_register_current_thread", "libRegisterThread", "libIsThreadRegistered", "_PJSUA_THREAD_REGISTRY", "_PJSUA_REGISTERED_THREADS"]
for needle in needles:
    for idx, line in enumerate(lines, 1):
        if needle in line:
            start = max(1, idx - 12)
            end = min(len(lines), idx + 120)
            print(f"### {needle} @ {idx}")
            for i in range(start, end + 1):
                print(f"{i}:{lines[i-1]}")
            print("---")
            break
