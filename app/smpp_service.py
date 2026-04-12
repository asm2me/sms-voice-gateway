from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from .admin_reports import record_inbox_message
from .config import Settings
from .config_store import get_default_smpp_account, get_sip_account_for_smpp_username

log = logging.getLogger(__name__)

_SMPP_COMMAND_NAMES = {
    0x00000001: "bind_receiver",
    0x00000002: "bind_transmitter",
    0x00000004: "submit_sm",
    0x00000005: "deliver_sm",
    0x00000006: "unbind",
    0x00000009: "bind_transceiver",
    0x00000015: "enquire_link",
    0x80000000: "generic_nack",
    0x80000001: "bind_receiver_resp",
    0x80000002: "bind_transmitter_resp",
    0x80000004: "submit_sm_resp",
    0x80000005: "deliver_sm_resp",
    0x80000006: "unbind_resp",
    0x80000009: "bind_transceiver_resp",
    0x80000015: "enquire_link_resp",
}

_SMPP_STATUS_NAMES = {
    0x00000000: "ESME_ROK",
    0x00000003: "ESME_RINVCMDID",
    0x0000000E: "ESME_RINVPASWD",
    0x00000033: "ESME_RINVVER",
}

_SM_PP_GENERIC_NACK = 0x80000000
_SM_PP_BIND_RECEIVER = 0x00000001
_SM_PP_BIND_RECEIVER_RESP = 0x80000001
_SM_PP_BIND_TRANSMITTER = 0x00000002
_SM_PP_BIND_TRANSMITTER_RESP = 0x80000002
_SM_PP_SUBMIT_SM = 0x00000004
_SM_PP_SUBMIT_SM_RESP = 0x80000004
_SM_PP_DELIVER_SM = 0x00000005
_SM_PP_UNBIND = 0x00000006
_SM_PP_UNBIND_RESP = 0x80000006
_SM_PP_BIND_TRANSCEIVER = 0x00000009
_SM_PP_BIND_TRANSCEIVER_RESP = 0x80000009
_SM_PP_ENQUIRE_LINK = 0x00000015
_SM_PP_ENQUIRE_LINK_RESP = 0x80000015


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
        self._active_clients: dict[socket.socket, dict[str, str]] = {}

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
        self._active_clients.clear()
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
                log.info(
                    "SMPP PDU from %s:%s command=%s(0x%08x) status=%s(0x%08x) seq=%d length=%d body_hex=%s",
                    addr[0],
                    addr[1],
                    _SMPP_COMMAND_NAMES.get(command_id, "unknown"),
                    command_id,
                    _SMPP_STATUS_NAMES.get(command_status, "unknown_status"),
                    command_status,
                    sequence,
                    length,
                    body[:200].hex(),
                )
                if command_id == _SM_PP_UNBIND:
                    self._send_pdu(conn, _SM_PP_UNBIND_RESP, 0, sequence, b"")
                    log.info("SMPP unbind handled for %s:%s", addr[0], addr[1])
                    break
                if command_id in {_SM_PP_BIND_RECEIVER, _SM_PP_BIND_TRANSMITTER, _SM_PP_BIND_TRANSCEIVER}:
                    bind_response_command_id = {
                        _SM_PP_BIND_RECEIVER: _SM_PP_BIND_RECEIVER_RESP,
                        _SM_PP_BIND_TRANSMITTER: _SM_PP_BIND_TRANSMITTER_RESP,
                        _SM_PP_BIND_TRANSCEIVER: _SM_PP_BIND_TRANSCEIVER_RESP,
                    }[command_id]
                    bind_fields = self._parse_bind_fields(body)
                    interface_version = bind_fields["interface_version"]
                    if not self._supported_interface_version(interface_version):
                        self._send_pdu(conn, bind_response_command_id, 0x00000033, sequence, b"\x00")
                        log.warning(
                            "SMPP bind failed from %s:%s due to unsupported interface_version=%r",
                            addr[0],
                            addr[1],
                            interface_version,
                        )
                        break
                    if self._authenticate(body):
                        self._active_clients[conn] = {
                            "smpp_username": bind_fields["system_id"],
                            "client_addr": f"{addr[0]}:{addr[1]}",
                        }
                        self._send_pdu(conn, bind_response_command_id, 0, sequence, b"\x00")
                        log.info(
                            "SMPP bind success from %s:%s using interface_version=0x%02x",
                            addr[0],
                            addr[1],
                            interface_version,
                        )
                    else:
                        self._send_pdu(conn, bind_response_command_id, 0x0000000E, sequence, b"\x00")
                        log.warning("SMPP bind failed from %s:%s", addr[0], addr[1])
                        break
                elif command_id == _SM_PP_ENQUIRE_LINK:
                    self._send_pdu(conn, _SM_PP_ENQUIRE_LINK_RESP, 0, sequence, b"")
                    log.info("SMPP enquire_link handled for %s:%s", addr[0], addr[1])
                elif command_id == _SM_PP_SUBMIT_SM:
                    submit_fields = self._parse_submit_sm(body)
                    smpp_username = self._active_clients.get(conn, {}).get("smpp_username", "")
                    sip_account = get_sip_account_for_smpp_username(self.settings, smpp_username)
                    record_inbox_message(
                        self.settings,
                        from_number=submit_fields["source_addr"],
                        to_number=submit_fields["destination_addr"],
                        provider="smpp",
                        body=submit_fields["short_message"],
                        source=f"smpp:{addr[0]}:{addr[1]}",
                        status="received",
                    )
                    log.info(
                        "SMPP inbox message stored from=%s to=%s text=%r source=%s smpp_username=%s sip_account_id=%s",
                        submit_fields["source_addr"],
                        submit_fields["destination_addr"],
                        submit_fields["short_message"][:120],
                        f"smpp:{addr[0]}:{addr[1]}",
                        smpp_username,
                        sip_account.id if sip_account else "",
                    )
                    message_id = f"smpp-{sequence}".encode("ascii", "ignore") + b"\x00"
                    self._send_pdu(conn, _SM_PP_SUBMIT_SM_RESP, 0, sequence, message_id)
                    log.info(
                        "SMPP submit_sm acknowledged for %s:%s from=%s to=%s text=%r",
                        addr[0],
                        addr[1],
                        submit_fields["source_addr"],
                        submit_fields["destination_addr"],
                        submit_fields["short_message"][:120],
                    )
                else:
                    self._send_pdu(conn, _SM_PP_GENERIC_NACK, 0x00000003, sequence, b"")
                    log.debug(
                        "SMPP unhandled PDU from %s:%s command=%s(0x%08x) status=%s(0x%08x) seq=%d body_hex=%s",
                        addr[0],
                        addr[1],
                        _SMPP_COMMAND_NAMES.get(command_id, "unknown"),
                        command_id,
                        _SMPP_STATUS_NAMES.get(command_status, "unknown_status"),
                        command_status,
                        sequence,
                        body[:200].hex(),
                    )
        except Exception as exc:
            log.debug("SMPP client error: %s", exc)
        finally:
            try:
                self._active_clients.pop(conn, None)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _parse_bind_fields(self, body: bytes) -> dict:
        parts = body.split(b"\x00")
        system_id = parts[0].decode("utf-8", "ignore").strip() if len(parts) > 0 else ""
        password = parts[1].decode("utf-8", "ignore").strip() if len(parts) > 1 else ""
        system_type = parts[2].decode("utf-8", "ignore").strip() if len(parts) > 2 else ""

        remainder = b""
        zero_count = 0
        for index, byte in enumerate(body):
            if byte == 0:
                zero_count += 1
                if zero_count == 3:
                    remainder = body[index + 1 :]
                    break

        interface_version = remainder[0] if len(remainder) >= 1 else None
        addr_ton = remainder[1] if len(remainder) >= 2 else None
        addr_npi = remainder[2] if len(remainder) >= 3 else None
        address_range = ""
        if len(remainder) >= 4:
            address_range = remainder[3:].split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()

        return {
            "system_id": system_id,
            "password": password,
            "system_type": system_type,
            "interface_version": interface_version,
            "addr_ton": addr_ton,
            "addr_npi": addr_npi,
            "address_range": address_range,
        }

    def _read_cstring(self, data: bytes, offset: int) -> tuple[str, int]:
        if offset >= len(data):
            return "", offset
        end = data.find(b"\x00", offset)
        if end == -1:
            end = len(data)
            next_offset = len(data)
        else:
            next_offset = end + 1
        return data[offset:end].decode("utf-8", "ignore").strip(), next_offset

    def _parse_submit_sm(self, body: bytes) -> dict:
        offset = 0
        service_type, offset = self._read_cstring(body, offset)
        source_addr_ton = body[offset] if offset < len(body) else 0
        offset += 1
        source_addr_npi = body[offset] if offset < len(body) else 0
        offset += 1
        source_addr, offset = self._read_cstring(body, offset)
        dest_addr_ton = body[offset] if offset < len(body) else 0
        offset += 1
        dest_addr_npi = body[offset] if offset < len(body) else 0
        offset += 1
        destination_addr, offset = self._read_cstring(body, offset)

        esm_class = body[offset] if offset < len(body) else 0
        offset += 1
        protocol_id = body[offset] if offset < len(body) else 0
        offset += 1
        priority_flag = body[offset] if offset < len(body) else 0
        offset += 1
        schedule_delivery_time, offset = self._read_cstring(body, offset)
        validity_period, offset = self._read_cstring(body, offset)
        registered_delivery = body[offset] if offset < len(body) else 0
        offset += 1
        replace_if_present_flag = body[offset] if offset < len(body) else 0
        offset += 1
        data_coding = body[offset] if offset < len(body) else 0
        offset += 1
        sm_default_msg_id = body[offset] if offset < len(body) else 0
        offset += 1
        sm_length = body[offset] if offset < len(body) else 0
        offset += 1
        short_message_bytes = body[offset : offset + sm_length] if offset <= len(body) else b""

        if data_coding == 8:
            short_message = short_message_bytes.decode("utf-16-be", "ignore")
        else:
            short_message = short_message_bytes.decode("latin-1", "ignore")

        return {
            "service_type": service_type,
            "source_addr_ton": source_addr_ton,
            "source_addr_npi": source_addr_npi,
            "source_addr": source_addr,
            "dest_addr_ton": dest_addr_ton,
            "dest_addr_npi": dest_addr_npi,
            "destination_addr": destination_addr,
            "esm_class": esm_class,
            "protocol_id": protocol_id,
            "priority_flag": priority_flag,
            "schedule_delivery_time": schedule_delivery_time,
            "validity_period": validity_period,
            "registered_delivery": registered_delivery,
            "replace_if_present_flag": replace_if_present_flag,
            "data_coding": data_coding,
            "sm_default_msg_id": sm_default_msg_id,
            "sm_length": sm_length,
            "short_message": short_message,
        }

    def _supported_interface_version(self, interface_version: int | None) -> bool:
        return interface_version in {0x34, 0x50}

    def _authenticate(self, body: bytes) -> bool:
        bind_fields = self._parse_bind_fields(body)
        system_id = bind_fields["system_id"]
        password = bind_fields["password"]
        interface_version = bind_fields["interface_version"]
        expected_system_id = (self.settings.smpp_username or "").strip()
        expected_password = (self.settings.smpp_password or "").strip()
        default_smpp_account = get_default_smpp_account(self.settings)

        if default_smpp_account and default_smpp_account.username:
            expected_system_id = default_smpp_account.username.strip()
            expected_password = default_smpp_account.password.strip()

        if not self._supported_interface_version(interface_version):
            log.warning(
                "SMPP bind version mismatch from client: system_id=%r interface_version=%r supported_versions=%s",
                system_id,
                interface_version,
                "0x34,0x50",
            )
            return False

        if not self.settings.smpp_username and not self.settings.smpp_password and not default_smpp_account:
            return True

        auth_ok = system_id == expected_system_id and password == expected_password
        if not auth_ok:
            log.warning(
                "SMPP bind auth mismatch from client: system_id=%r expected_system_id=%r password_len=%d expected_password_len=%d interface_version=%r",
                system_id,
                expected_system_id,
                len(password),
                len(expected_password),
                interface_version,
            )
        return auth_ok

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