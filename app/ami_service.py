"""
Minimal synchronous Asterisk Manager Interface (AMI) client.

Only implements what the gateway needs:
  - Login / Logoff
  - Originate (outbound call with Playback application)
  - Ping (keep-alive)

Thread-safety: each call to `originate()` opens its own short-lived connection
so that concurrent requests from FastAPI workers do not share state.
"""
from __future__ import annotations

import logging
import re
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator, Optional

from .config import Settings

log = logging.getLogger(__name__)

_CRLF = "\r\n"


class AMISocket:
    def __init__(self, host: str, port: int, timeout: int = 10):
        self.host = host
        self.port = port
        log.info("AMI TCP connect start → %s:%d", host, port)
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = ""
        banner = self._readline()
        log.debug("AMI banner received from %s:%d: %s", host, port, banner)

    def send_action(self, fields: dict[str, str]) -> None:
        msg = _CRLF.join(f"{k}: {v}" for k, v in fields.items()) + _CRLF * 2
        self._sock.sendall(msg.encode())

    def read_response(self, timeout: int = 10) -> dict[str, str]:
        """Read lines until a blank line; return key→value dict."""
        self._sock.settimeout(timeout)
        lines: list[str] = []
        while True:
            line = self._readline()
            if line == "":
                break
            lines.append(line)
        result: dict[str, str] = {}
        for line in lines:
            if ": " in line:
                k, _, v = line.partition(": ")
                result[k.strip()] = v.strip()
        return result

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass

    def _readline(self) -> str:
        while _CRLF not in self._buf:
            chunk = self._sock.recv(4096).decode(errors="replace")
            if not chunk:
                raise ConnectionError("AMI connection closed by peer")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(_CRLF)
        return line


@dataclass
class OriginateResult:
    action_id: str
    success: bool
    response: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)


class AMIService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def _connection(self) -> Generator[AMISocket, None, None]:
        s = AMISocket(
            self.settings.ami_host,
            self.settings.ami_port,
            timeout=self.settings.ami_connection_timeout,
        )
        logged_in = False
        try:
            log.info("AMI login start → %s:%d as %s", self.settings.ami_host, self.settings.ami_port, self.settings.ami_username)
            s.send_action(
                {
                    "Action": "Login",
                    "Username": self.settings.ami_username,
                    "Secret": self.settings.ami_secret,
                }
            )
            resp = s.read_response(timeout=self.settings.ami_connection_timeout)
            if resp.get("Response") != "Success":
                message = resp.get("Message", "unknown")
                log.warning("AMI login failed for %s:%d: %s", self.settings.ami_host, self.settings.ami_port, message)
                raise PermissionError(f"AMI login failed: {message}")
            logged_in = True
            log.info("AMI login succeeded for %s:%d", self.settings.ami_host, self.settings.ami_port)
            yield s
        finally:
            try:
                if logged_in:
                    log.info("AMI logoff start → %s:%d", self.settings.ami_host, self.settings.ami_port)
                    s.send_action({"Action": "Logoff"})
                    log.info("AMI logoff completed for %s:%d", self.settings.ami_host, self.settings.ami_port)
            except Exception as exc:
                log.debug("AMI logoff issue for %s:%d: %s", self.settings.ami_host, self.settings.ami_port, exc)
            finally:
                s.close()
                log.info("AMI TCP disconnect complete for %s:%d", self.settings.ami_host, self.settings.ami_port)

    def ping_detail(self) -> dict[str, object]:
        """Test AMI connectivity and return detailed result for admin/health usage."""
        result: dict[str, object] = {
            "ok": False,
            "summary": "AMI connection test failed.",
            "details": [
                f"Target: {self.settings.ami_host}:{self.settings.ami_port}",
                f"Username: {self.settings.ami_username}",
                f"Connection timeout: {self.settings.ami_connection_timeout}s",
                f"Response timeout: {self.settings.ami_response_timeout}s",
            ],
        }
        try:
            with self._connection() as s:
                s.send_action({"Action": "Ping"})
                while True:
                    resp = s.read_response(timeout=self.settings.ami_response_timeout)
                    if not resp:
                        result["summary"] = "AMI ping returned no response."
                        return result
                    event_name = resp.get("Event")
                    if event_name:
                        result["details"].append(f"Event packet ignored during ping: {event_name}")
                        log.debug("AMI ping ignoring event packet: %s", event_name)
                        continue

                    response_value = resp.get("Response", "")
                    message_value = resp.get("Message", "")
                    ping_value = resp.get("Ping", "")
                    result["details"].append(f"Ping response field: {response_value or '(missing)'}")
                    if message_value:
                        result["details"].append(f"Ping message: {message_value}")
                    if ping_value:
                        result["details"].append(f"Ping field: {ping_value}")

                    success = response_value == "Success" or ping_value.lower() == "pong"
                    if success:
                        result["ok"] = True
                        result["summary"] = "AMI login and ping succeeded."
                        log.info("AMI ping response success for %s:%d", self.settings.ami_host, self.settings.ami_port)
                    else:
                        result["summary"] = "AMI login succeeded but ping did not return success."
                        log.warning(
                            "AMI ping response failure for %s:%d: response=%s ping=%s message=%s",
                            self.settings.ami_host,
                            self.settings.ami_port,
                            response_value or "(missing)",
                            ping_value or "(missing)",
                            message_value or "(missing)",
                        )
                    return result
        except Exception as exc:
            result["details"].append(f"Error: {type(exc).__name__}: {exc}")
            log.warning("AMI ping failed for %s:%d: %s", self.settings.ami_host, self.settings.ami_port, exc)
            return result

    def ping(self) -> bool:
        """Test AMI connectivity. Returns True on success."""
        return bool(self.ping_detail()["ok"])

    def originate_playback(
        self,
        phone_number: str,
        asterisk_sound_ref: str,
        extra_vars: Optional[dict[str, str]] = None,
    ) -> OriginateResult:
        """
        Dial `phone_number` via the configured SIP trunk.
        When the call is answered, Asterisk plays `asterisk_sound_ref`
        via the [sms-voice-otp] dialplan context (which handles repeats).

        asterisk_sound_ref: e.g. "sms_otp/abc123def456" (no .wav extension)
        """
        action_id = f"sms_{uuid.uuid4().hex[:12]}"
        channel = f"{self.settings.sip_channel_prefix}/{_e164(phone_number)}"

        vars_dict: dict[str, str] = {
            "OTP_SOUND": asterisk_sound_ref,
        }
        if extra_vars:
            vars_dict.update(extra_vars)
        variable_str = ",".join(f"{k}={v}" for k, v in vars_dict.items())

        action: dict[str, str] = {
            "Action": "Originate",
            "ActionID": action_id,
            "Channel": channel,
            "CallerID": self.settings.outbound_caller_id,
            "Timeout": str(self.settings.call_answer_timeout * 1000),
            "Context": self.settings.asterisk_context,
            "Exten": self.settings.asterisk_exten,
            "Priority": self.settings.asterisk_priority,
            "Async": "true",
            "Variable": variable_str,
        }

        log.info("AMI originate start → %s via %s (action=%s)", phone_number, channel, action_id)

        try:
            with self._connection() as s:
                s.send_action(action)
                while True:
                    resp = s.read_response(timeout=self.settings.ami_response_timeout)
                    if resp.get("Event"):
                        log.debug("AMI originate ignoring event packet: %s", resp.get("Event"))
                        continue
                    break
                success = resp.get("Response") == "Success"
                result = OriginateResult(
                    action_id=action_id,
                    success=success,
                    response=resp.get("Response", ""),
                    message=resp.get("Message", ""),
                    raw=resp,
                )
                if success:
                    log.info("AMI originate response success for %s (action=%s)", phone_number, action_id)
                else:
                    log.warning(
                        "AMI originate response failure for %s (action=%s): %s",
                        phone_number,
                        action_id,
                        resp.get("Message", "unknown"),
                    )
                return result
        except Exception as exc:
            log.exception("AMI originate exception for %s (action=%s)", phone_number, action_id)
            return OriginateResult(
                action_id=action_id,
                success=False,
                response="Error",
                message=str(exc),
            )

    def get_active_channels(self) -> list[dict]:
        """Return list of currently active channels (for health/debug endpoint)."""
        try:
            with self._connection() as s:
                s.send_action({"Action": "CoreShowChannels"})
                channels = []
                while True:
                    resp = s.read_response(timeout=self.settings.ami_response_timeout)
                    if resp.get("Response") == "Success":
                        continue
                    if resp.get("Event") == "CoreShowChannel":
                        channels.append(resp)
                    elif resp.get("Event") == "CoreShowChannelsComplete":
                        break
                    elif not resp:
                        break
                log.info("AMI active channels query completed for %s:%d count=%d", self.settings.ami_host, self.settings.ami_port, len(channels))
                return channels
        except Exception as exc:
            log.warning("AMI active channels query failed for %s:%d: %s", self.settings.ami_host, self.settings.ami_port, exc)
            return []


def _e164(number: str) -> str:
    """Strip everything except digits and leading +."""
    cleaned = re.sub(r"[^\d+]", "", number)
    return cleaned
