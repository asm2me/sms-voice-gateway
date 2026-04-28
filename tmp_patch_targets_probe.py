from pathlib import Path

def dump(path: str, needles: list[str], before: int = 10, after: int = 120) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for needle in needles:
        for idx, line in enumerate(lines, 1):
            if line.startswith(needle):
                start = max(1, idx - before)
                end = min(len(lines), idx + after)
                print(f"### {path}:{needle} @ {idx}")
                for i in range(start, end + 1):
                    print(f"{i}:{lines[i-1]}")
                print("---")
                break

dump("app/sms_handler.py", ["class SMSGateway", "def process"])
dump("app/pjsua2_service.py", ["def request_spy_start", "def get_spy_state", "def _read_wav_header", "def _process_spy_commands"])
