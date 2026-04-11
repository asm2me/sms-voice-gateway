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
import textwrap
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator, Optional

from .config import Settings

log = logging.getLogger(__name__)

_CRLF = "\r\n"


# ─────────────────────────────────────────────────────────────────────────────
# Low-level AMI socket helper
# ─────────────────────────────────────────────────────────────────────────────

class AMISocket:
    def __init__(self, host: str, port: int, timeout: int = 10):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = ""
        # Read and discard the Asterisk banner line
        self._readline()

    def send_action(self, fields: dict[str, str]) -> None:
        msg = _CRLF.join(f"{k}: {v}" for k, v in fields.items()) + _CRLF * 2
        self._sock.sendall(msg.encode())

    def read_response(self, timeout: int = 10) -> dict[str, str]:
        """Read lines until a blank line; return key→value dict."""
        self._sock.settimeout(timeout)
        lines: list[str] = []
        while True:
            line = self._readline()
            if line == "":          # blank line → end of packet
                break
            lines.append(line)
        result: dict[str, str] = {}
        for line in lines:
            if ": " in line:
                k, _, v = line.partition(": ")
                result[k.strip()] = v.strip()
        return result

    def _readline(self) -> str:
        while _CRLF not in self._buf:
            chunk = self._sock.recv(4096).decode(errors="replace")
            if not chunk:
                raise ConnectionError("AMI connection closed by peer")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(_CRLF)
        return line

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# AMI result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OriginateResult:
    action_id: str
    success: bool
    response: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Public AMI service
# ─────────────────────────────────────────────────────────────────────────────

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
        try:
            # Login
            s.send_action({
                "Action": "Login",
                "Username": self.settings.ami_username,
                "Secret": self.settings.ami_secret,
            })
            resp = s.read_response(timeout=self.settings.ami_connection_timeout)
            if resp.get("Response") != "Success":
                raise PermissionError(f"AMI login failed: {resp.get('Message', resp)}")
            log.debug("AMI login OK (%s:%d)", self.settings.ami_host, self.settings.ami_port)
            yield s
        finally:
            try:
                s.send_action({"Action": "Logoff"})
            except Exception:
                pass
            s.close()

    def ping(self) -> bool:
        """Test AMI connectivity. Returns True on success."""
        try:
            with self._connection() as s:
                s.send_action({"Action": "Ping"})
                resp = s.read_response()
                return resp.get("Response") == "Success"
        except Exception as exc:
            log.warning("AMI ping failed: %s", exc)
            return False

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

        # Build variable list for the dialplan context
        vars_dict: dict[str, str] = {
            "OTP_SOUND": asterisk_sound_ref,
        }
        if extra_vars:
            vars_dict.update(extra_vars)
        # AMI encodes multiple Variable fields as repeated keys
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
            "Async": "true",          # non-blocking originate
            "Variable": variable_str,
        }

        log.info(
            "AMI Originate → %s via %s (sound=%s)",
            phone_number, channel, asterisk_sound_ref,
        )

        try:
            with self._connection() as s:
                s.send_action(action)
                # With Async=true Asterisk immediately returns "Response: Success"
                resp = s.read_response(timeout=self.settings.ami_response_timeout)
                success = resp.get("Response") == "Success"
                result = OriginateResult(
                    action_id=action_id,
                    success=success,
                    response=resp.get("Response", ""),
                    message=resp.get("Message", ""),
                    raw=resp,
                )
                if success:
                    log.info("Originate queued for %s (action=%s)", phone_number, action_id)
                else:
                    log.error("Originate failed for %s: %s", phone_number, resp)
                return result

        except Exception as exc:
            log.exception("AMI originate exception for %s", phone_number)
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
                    resp = s.read_response()
                    if resp.get("Event") == "CoreShowChannel":
                        channels.append(resp)
                    elif resp.get("Event") == "CoreShowChannelsComplete":
                        break
                    elif not resp:
                        break
                return channels
        except Exception as exc:
            log.warning("get_active_channels error: %s", exc)
            return []


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _e164(number: str) -> str:
    """Strip everything except digits and leading +."""
    cleaned = re.sub(r"[^\d+]", "", number)
    return cleaned
