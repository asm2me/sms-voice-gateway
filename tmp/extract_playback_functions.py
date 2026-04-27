from pathlib import Path

lines = Path("app/pjsua2_service.py").read_text(encoding="utf-8").splitlines()
targets = {
    "set_playback_context",
    "_maybe_start_playback",
    "_try_start_playback",
    "onCallState",
    "onCallMediaState",
}

def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))

for i, line in enumerate(lines):
    stripped = line.lstrip(" ")
    if stripped.startswith("def ") and any(stripped.startswith(f"def {name}") for name in targets):
        start = i
        base_indent = indent_of(line)
        end = len(lines)
        for j in range(i + 1, len(lines)):
            current = lines[j]
            if current.strip() == "":
                continue
            if indent_of(current) <= base_indent and current.lstrip(" ").startswith(("def ", "class ")):
                end = j
                break
        print("=== FUNCTION START ===")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
