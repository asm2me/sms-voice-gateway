from pathlib import Path

text = Path("app/pjsua2_service.py").read_text(encoding="utf-8")
print("prepare_playback" in text)
print("libCall" in text)
print("setNullDev" in text)
