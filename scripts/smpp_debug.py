from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
from pathlib import Path


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_bind_pdu(system_id: str, password: str, system_type: str, host: str, port: int, sequence: int = 1) -> bytes:
    body = (
        system_id.encode("utf-8") + b"\x00"
        + password.encode("utf-8") + b"\x00"
        + system_type.encode("utf-8") + b"\x00"
        + bytes([0x34])  # interface_version
        + bytes([0x00])  # addr_ton
        + bytes([0x00])  # addr_npi
        + b"\x00"        # address_range
    )
    command_length = 16 + len(body)
    header = struct.pack(">IIII", command_length, 0x00000009, 0x00000000, sequence)
    return header + body


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Connection closed while reading SMPP response")
        data += chunk
    return data


def read_pdu(sock: socket.socket) -> tuple[int, int, int, bytes]:
    header = recv_exact(sock, 16)
    command_length, command_id, command_status, sequence = struct.unpack(">IIII", header)
    body = recv_exact(sock, command_length - 16) if command_length > 16 else b""
    return command_id, command_status, sequence, body


def parse_cstring_prefix(body: bytes) -> str:
    prefix = body.split(b"\x00", 1)[0]
    return prefix.decode("utf-8", "ignore")


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug SMPP bind against the local gateway listener.")
    parser.add_argument("--host", default=os.getenv("SMPP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SMPP_PORT", "7070")))
    parser.add_argument("--system-id", default=os.getenv("SMPP_USERNAME", "smpp"))
    parser.add_argument("--password", default=os.getenv("SMPP_PASSWORD", "smpp_secret"))
    parser.add_argument("--system-type", default="")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / ".env")

    print(f"[SMPP DEBUG] target={args.host}:{args.port}")
    print(f"[SMPP DEBUG] system_id={args.system_id!r}")
    print(f"[SMPP DEBUG] password_length={len(args.password)}")
    print(f"[SMPP DEBUG] timeout={args.timeout}")

    bind_pdu = build_bind_pdu(
        system_id=args.system_id,
        password=args.password,
        system_type=args.system_type,
        host=args.host,
        port=args.port,
    )

    try:
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
            sock.settimeout(args.timeout)
            print("[SMPP DEBUG] TCP connect: OK")
            sock.sendall(bind_pdu)
            print("[SMPP DEBUG] bind_transceiver sent")

            command_id, command_status, sequence, body = read_pdu(sock)
            print(f"[SMPP DEBUG] response_command_id=0x{command_id:08x}")
            print(f"[SMPP DEBUG] response_status=0x{command_status:08x}")
            print(f"[SMPP DEBUG] response_sequence={sequence}")
            if body:
                print(f"[SMPP DEBUG] response_system_id={parse_cstring_prefix(body)!r}")

            if command_status == 0:
                print("[SMPP DEBUG] bind result: SUCCESS")
                return 0

            print("[SMPP DEBUG] bind result: FAILED")
            return 2
    except Exception as exc:
        print(f"[SMPP DEBUG] error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
