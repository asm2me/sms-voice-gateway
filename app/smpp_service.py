from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from .config import Settings

log = logging.getLogger(__name__)

_SM_PP_BIND_RESP = 0x80000002
_SM_PP_UNBIND = 0x00000006
_SM_PP_UNBIND_RESP = 0x80000006
_SM_PP_BIND_TRANSMITTER = 0x00000002
_SM_PP_BIND_RECEIVER = 0x00000001
_SM_PP_BIND_TRANSCEIVER = 0x00000009


@dataclass
class SMPPResult:
    ok: bool
    message: str = ""
    details: dict = None


class SMPPService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.settings.smpp_enabled)

    @property
    def is_listening(self) -> bool:
        return self._server is not None and not self._stop_event.is_set()

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> None:
        self._last_error = ""
        if not self.enabled:
            log.info("SMPP listener disabled")
            return
        if self._server is not None:
            return

        host = self.settings.smpp_host
        port = self.settings.smpp_port
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind((host, port))
            self._server.listen(5)
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._serve, name="smpp-listener", daemon=True)
            self._thread.start()
            log.info("SMPP listener started on %s:%s", host, port)
        except Exception as exc:
            self._last_error = str(exc)
            if self._server is not None:
                try:
                    self._server.close()
                except Exception:
                    pass
                self._server = None
            raise

    def stop(self) -> None:
        self._stop_event.set()
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        self._thread = None
        log.info("SMPP listener stopped")

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop_event.is_set():
            try:
                self._server.settimeout(1.0)
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        try:
            log.info("SMPP connection from %s:%s", addr[0], addr[1])
            while not self._stop_event.is_set():
                header = self._recv_exact(conn, 16)
                if not header:
                    break
                length = int.from_bytes(header[0:4], "big")
                command_id = int.from_bytes(header[4:8], "big")
                command_status = int.from_bytes(header[8:12], "big")
                sequence = int.from_bytes(header[12:16], "big")
                body = self._recv_exact(conn, length - 16) if length > 16 else b""
                if command_id == _SM_PP_UNBIND:
                    self._send_pdu(conn, _SM_PP_UNBIND_RESP, 0, sequence, b"")
                    break
                if command_id in {_SM_PP_BIND_RECEIVER, _SM_PP_BIND_TRANSMITTER, _SM_PP_BIND_TRANSCEIVER}:
                    if self._authenticate(body):
                        self._send_pdu(conn, _SM_PP_BIND_RESP | (command_id & 0xFF), 0, sequence, b"\x00")
                        log.info("SMPP bind success from %s:%s", addr[0], addr[1])
                    else:
                        self._send_pdu(conn, _SM_PP_BIND_RESP | (command_id & 0xFF), 0x0000000E, sequence, b"\x00")
                        log.warning("SMPP bind failed from %s:%s", addr[0], addr[1])
                        break
                else:
                    log.debug("SMPP command_id=0x%08x status=%d seq=%d", command_id, command_status, sequence)
        except Exception as exc:
            log.debug("SMPP client error: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _authenticate(self, body: bytes) -> bool:
        if not self.settings.smpp_username and not self.settings.smpp_password:
            return True
        parts = body.split(b"\x00")
        system_id = parts[0].decode("utf-8", "ignore") if parts else ""
        password = parts[1].decode("utf-8", "ignore") if len(parts) > 1 else ""
        return system_id == self.settings.smpp_username and password == self.settings.smpp_password

    def _send_pdu(self, conn: socket.socket, command_id: int, command_status: int, sequence: int, body: bytes) -> None:
        length = 16 + len(body)
        pdu = (
            length.to_bytes(4, "big")
            + command_id.to_bytes(4, "big")
            + command_status.to_bytes(4, "big")
            + sequence.to_bytes(4, "big")
            + body
        )
        conn.sendall(pdu)

    def _recv_exact(self, conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return b""
            data += chunk
        return data
