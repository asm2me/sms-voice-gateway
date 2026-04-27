from pathlib import Path

lines = Path("app/tts_service.py").read_text(encoding="utf-8").splitlines()
targets = ("def get_or_create_audio(", "def prepare_playback(")

def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))

for i, line in enumerate(lines):
    stripped = line.lstrip(" ")
    if stripped.startswith("def ") and any(stripped.startswith(t) for t in targets):
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
