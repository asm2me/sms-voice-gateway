from pathlib import Path

lines = Path("app/cache.py").read_text(encoding="utf-8").splitlines()
targets = ("def get_audio_path(", "def set_audio_path(", "def cleanup", "def prune", "class AudioCache")

def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))

for i, line in enumerate(lines):
    stripped = line.lstrip(" ")
    if stripped.startswith("def ") and any(stripped.startswith(t) for t in targets) or stripped.startswith("class AudioCache"):
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
        print("=== BLOCK ===")
        for j in range(start, end):
            print(f"{j+1}: {lines[j]}")
