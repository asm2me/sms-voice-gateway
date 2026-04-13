"""
Direct SIP User-Agent service using PJSUA2/PJSIP Python bindings.

This module is intentionally defensive:
- It imports PJSUA2 lazily so the app can still start when bindings are missing.
- It returns structured results instead of raising on environment/binding errors.
- It does not depend on Asterisk AMI or originate flows.

The service is designed to support:
- selecting a SIP account profile
- registering to a SIP host with username/password
- placing an outbound call to a destination number
- preparing playback of generated TTS audio

The exact playback bridge depends on the availability of the PJSUA2 audio APIs
and on how the generated audio is packaged for the runtime. When playback cannot
be completed by this helper alone, the result will indicate the reason and
expose the call/session state for the upper layer to act on.
"""
from __future__ import annotations

import logging
import re
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Settings

log = logging.getLogger(__name__)


def _safe_import_pjsua2() -> tuple[object | None, str]:
    try:
        import pjsua2 as pj  # type: ignore
        return pj, ""
    except Exception as exc:  # pragma: no cover - import availability depends on deployment
        return None, f"{type(exc).__name__}: {exc}"


def _normalise_number(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", (raw or "").strip())
    if not cleaned:
        raise ValueError(f"Invalid destination number: {raw!r}")
    return cleaned


def _host_with_port(host: str, port: int | str | None) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    if host.startswith("[") and "]" in host:
        return host if not port else f"{host}:{int(port)}"
    if ":" in host and host.count(":") > 1:
        return host if not port else f"[{host}]:{int(port)}"
    if ":" in host:
        return host
    return host if not port else f"{host}:{int(port)}"


@dataclass
class SipAccountProfile:
    """
    SIP account/profile definition persisted in config storage.

    The service accepts plain dictionaries too, but this dataclass documents the
    expected shape and keeps the runtime implementation self-describing.
    """
    id: str
    display_name: str = ""
    sip_uri: str = ""
    domain: str = ""
    username: str = ""
    password: str = ""
    registrar_uri: str = ""
    proxy_uri: str = ""
    transport: str = "UDP"
    caller_id: str = ""
    enabled: bool = True
    registration_timeout: int = 300
    auth_realm: str = ""
    concurrency_limit: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SipCallRequest:
    destination_number: str
    audio_path: str = ""
    account_id: str = ""
    display_name: str = ""
    caller_id: str = ""
    timeout_seconds: int = 30
    playback_repeats: int = 1
    playback_pause_ms: int = 0
    extra_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class PJSUA2Result:
    success: bool
    account_id: str = ""
    call_id: str = ""
    destination_number: str = ""
    registered: bool = False
    audio_path: str = ""
    message: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.success


@dataclass
class PJSUA2RegistrationResult:
    success: bool
    account_id: str = ""
    message: str = ""
    error: str = ""
    status_code: int = 0
    status_text: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class PJSUA2UnavailableError(RuntimeError):
    pass


_TRUNK_CONCURRENCY_LOCK = threading.RLock()
_TRUNK_ACTIVE_CALLS: dict[str, int] = {}


class PJSipUASession:
    """
    Lightweight wrapper around a running PJSUA2 endpoint.

    The endpoint is created lazily and shared per service instance. For safety,
    call operations are guarded by a lock because PJSUA2 call/account object
    state is not designed for uncontrolled concurrent mutation.
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pj, self.import_error = _safe_import_pjsua2()
        self._lock = threading.RLock()
        self._endpoint = None
        self._transport = None
        self._account = None
        self._current_profile: SipAccountProfile | None = None
        self._registered = False
        self._last_call = None

    @property
    def available(self) -> bool:
        return self._pj is not None

    def ensure_available(self) -> None:
        if self._pj is None:
            raise PJSUA2UnavailableError(
                "PJSUA2 bindings are unavailable"
                + (f": {self.import_error}" if self.import_error else "")
            )

    def initialize(self) -> PJSUA2RegistrationResult:
        with self._lock:
            if not self.available:
                return PJSUA2RegistrationResult(
                    success=False,
                    error="PJSUA2 bindings are unavailable",
                    details={"import_error": self.import_error},
                )
            try:
                if self._endpoint is not None:
                    return PJSUA2RegistrationResult(
                        success=True,
                        message="PJSUA2 endpoint already initialised",
                        details={"already_initialised": True},
                    )

                pj = self._pj
                ep = pj.Endpoint()
                ep.libCreate()

                ep_cfg = pj.EpConfig()
                with suppress(Exception):
                    ep_cfg.logConfig.level = 3
                    ep_cfg.logConfig.consoleLevel = 3

                ep.libInit(ep_cfg)

                transport_cfg = pj.TransportConfig()
                with suppress(Exception):
                    transport_cfg.port = 0
                transport_type = getattr(pj, "PJSIP_TRANSPORT_UDP", None)
                if transport_type is None:
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_TCP", 0)

                transport = ep.transportCreate(transport_type, transport_cfg)
                ep.libStart()

                self._endpoint = ep
                self._transport = transport

                return PJSUA2RegistrationResult(
                    success=True,
                    message="PJSUA2 endpoint initialised",
                    details={"transport": "created"},
                )
            except Exception as exc:
                log.exception("PJSUA2 endpoint initialisation failed")
                self.shutdown()
                return PJSUA2RegistrationResult(
                    success=False,
                    error=str(exc),
                    details={"exception": f"{type(exc).__name__}: {exc}"},
                )

    def shutdown(self) -> None:
        with self._lock:
            if self._account is not None:
                with suppress(Exception):
                    self._account.shutdown()
            self._account = None
            self._current_profile = None
            self._registered = False
            self._last_call = None

            if self._endpoint is not None:
                with suppress(Exception):
                    self._endpoint.libDestroy()
            self._endpoint = None
            self._transport = None

    def select_profile(self, profile: SipAccountProfile | dict[str, Any]) -> SipAccountProfile:
        if isinstance(profile, SipAccountProfile):
            return profile

        profile_id = str(profile.get("id", "")).strip()
        if not profile_id:
            raise ValueError("SIP profile is missing an id")

        return SipAccountProfile(
            id=profile_id,
            display_name=str(profile.get("display_name", "")),
            sip_uri=str(profile.get("sip_uri", "")),
            domain=str(profile.get("domain", "")),
            username=str(profile.get("username", "")),
            password=str(profile.get("password", "")),
            registrar_uri=str(profile.get("registrar_uri", "")),
            proxy_uri=str(profile.get("proxy_uri", "")),
            transport=str(profile.get("transport", "UDP")),
            caller_id=str(profile.get("caller_id", "")),
            enabled=bool(profile.get("enabled", True)),
            registration_timeout=int(profile.get("registration_timeout", 300) or 300),
            auth_realm=str(profile.get("auth_realm", "")),
            concurrency_limit=max(1, int(profile.get("concurrency_limit", 1) or 1)),
            extra=dict(profile.get("extra", {}) or {}),
        )

    def register_account(self, profile: SipAccountProfile | dict[str, Any]) -> PJSUA2RegistrationResult:
        with self._lock:
            init_result = self.initialize()
            if not init_result.success:
                return init_result

            try:
                pj = self._pj
                assert pj is not None
                selected = self.select_profile(profile)

                if not selected.enabled:
                    return PJSUA2RegistrationResult(
                        success=False,
                        account_id=selected.id,
                        error="SIP profile is disabled",
                    )

                if self._account is not None and self._current_profile and self._current_profile.id == selected.id:
                    return PJSUA2RegistrationResult(
                        success=True,
                        account_id=selected.id,
                        message="SIP profile already active",
                        details={"already_registered": self._registered},
                    )

                if self._account is not None and self._current_profile and self._current_profile.id != selected.id:
                    if self._account is not None:
                        with suppress(Exception):
                            self._account.shutdown()
                    self._account = None
                    self._current_profile = None
                    self._registered = False
                    self._last_call = None

                if self._endpoint is None:
                    init_result = self.initialize()
                    if not init_result.success:
                        return init_result

                account_cfg = pj.AccountConfig()
                account_cfg.idUri = selected.sip_uri or self._build_id_uri(selected)
                account_cfg.regConfig.registerOnAdd = True

                registrar_uri = selected.registrar_uri or self._build_registrar_uri(selected)
                account_cfg.regConfig.registrarUri = registrar_uri

                if selected.username or selected.password:
                    cred_info = pj.AuthCredInfo(
                        "digest",
                        selected.auth_realm or "*",
                        selected.username,
                        0,
                        selected.password,
                    )
                    account_cfg.sipConfig.authCreds.append(cred_info)

                if selected.proxy_uri:
                    account_cfg.sipConfig.proxies.append(selected.proxy_uri)

                account = _GatewayAccount(self, selected.id)
                account.create(account_cfg)

                self._account = account
                self._current_profile = selected
                registration_probe = self._wait_for_registration(account, timeout_seconds=12.0)
                self._registered = registration_probe["success"]

                if not registration_probe["success"]:
                    return PJSUA2RegistrationResult(
                        success=False,
                        account_id=selected.id,
                        error=registration_probe["error"] or "SIP trunk registration did not complete successfully",
                        status_code=registration_probe["status_code"],
                        status_text=registration_probe["status_text"],
                        details={
                            "id_uri": account_cfg.idUri,
                            "registrar_uri": registrar_uri,
                            "username": selected.username,
                            "transport": selected.transport,
                            "probe": registration_probe,
                        },
                    )

                return PJSUA2RegistrationResult(
                    success=True,
                    account_id=selected.id,
                    message="SIP account registered successfully",
                    status_code=registration_probe["status_code"],
                    status_text=registration_probe["status_text"],
                    details={
                        "id_uri": account_cfg.idUri,
                        "registrar_uri": registrar_uri,
                        "username": selected.username,
                        "transport": selected.transport,
                        "probe": registration_probe,
                    },
                )
            except Exception as exc:
                log.exception("SIP registration failed")
                self._account = None
                self._current_profile = None
                self._registered = False
                return PJSUA2RegistrationResult(
                    success=False,
                    account_id=getattr(profile, "id", "") if isinstance(profile, SipAccountProfile) else str(profile.get("id", "")),
                    error=str(exc),
                    details={"exception": f"{type(exc).__name__}: {exc}"},
                )

    def place_outbound_call(
        self,
        request: SipCallRequest | dict[str, Any],
        *,
        profile: SipAccountProfile | dict[str, Any] | None = None,
    ) -> PJSUA2Result:
        with self._lock:
            if isinstance(request, dict):
                request = SipCallRequest(
                    destination_number=str(request.get("destination_number", "")),
                    audio_path=str(request.get("audio_path", "")),
                    account_id=str(request.get("account_id", "")),
                    display_name=str(request.get("display_name", "")),
                    caller_id=str(request.get("caller_id", "")),
                    timeout_seconds=int(request.get("timeout_seconds", 30) or 30),
                    playback_repeats=int(request.get("playback_repeats", 1) or 1),
                    playback_pause_ms=int(request.get("playback_pause_ms", 0) or 0),
                    extra_vars=dict(request.get("extra_vars", {}) or {}),
                )

            try:
                destination = _normalise_number(request.destination_number)
            except ValueError as exc:
                return PJSUA2Result(
                    success=False,
                    destination_number=request.destination_number,
                    account_id=request.account_id,
                    error=str(exc),
                )

            if profile is not None:
                selected = self.select_profile(profile)
                if not self._current_profile or self._current_profile.id != selected.id:
                    reg = self.register_account(selected)
                    if not reg.success:
                        return PJSUA2Result(
                            success=False,
                            account_id=selected.id,
                            destination_number=destination,
                            error=reg.error or reg.message or "SIP registration failed",
                            details={"registration": reg.details},
                        )

            if not self._account or not self._current_profile:
                return PJSUA2Result(
                    success=False,
                    destination_number=destination,
                    error="No SIP account is active",
                )

            if not self._registered:
                return PJSUA2Result(
                    success=False,
                    account_id=self._current_profile.id,
                    destination_number=destination,
                    error="SIP account is not registered",
                )

            try:
                pj = self._pj
                assert pj is not None
                account_id = self._current_profile.id
                concurrency_limit = max(1, int(self._current_profile.concurrency_limit or 1))

                with _TRUNK_CONCURRENCY_LOCK:
                    active_calls = _TRUNK_ACTIVE_CALLS.get(account_id, 0)
                    if active_calls >= concurrency_limit:
                        return PJSUA2Result(
                            success=False,
                            account_id=account_id,
                            destination_number=destination,
                            audio_path=request.audio_path,
                            registered=True,
                            error=f"SIP trunk concurrency limit reached ({concurrency_limit})",
                            details={
                                "active_calls": active_calls,
                                "concurrency_limit": concurrency_limit,
                            },
                        )
                    _TRUNK_ACTIVE_CALLS[account_id] = active_calls + 1

                call_started = False
                try:
                    call_id = f"pj_{int(time.time() * 1000)}"
                    call = self._account.makeCall(
                        self._build_sip_uri(destination),
                        _CallCallbackHolder(self, account_id, call_id),
                    )
                    self._last_call = call
                    call_started = True

                    playback_result = self.prepare_playback(
                        request.audio_path,
                        destination_number=destination,
                        account_id=account_id,
                        repeat_count=request.playback_repeats,
                        pause_ms=request.playback_pause_ms,
                    )

                    return PJSUA2Result(
                        success=True,
                        account_id=account_id,
                        call_id=call_id,
                        destination_number=destination,
                        registered=True,
                        audio_path=request.audio_path,
                        message="Outbound SIP call started",
                        details={
                            "playback": playback_result.details,
                            "call_state": "initiated",
                            "display_name": request.display_name,
                            "caller_id": request.caller_id or self._current_profile.caller_id,
                            "extra_vars": request.extra_vars,
                            "active_calls": _TRUNK_ACTIVE_CALLS.get(account_id, 0),
                            "concurrency_limit": concurrency_limit,
                        },
                    )
                finally:
                    if not call_started:
                        with _TRUNK_CONCURRENCY_LOCK:
                            current = _TRUNK_ACTIVE_CALLS.get(account_id, 0)
                            if current <= 1:
                                _TRUNK_ACTIVE_CALLS.pop(account_id, None)
                            else:
                                _TRUNK_ACTIVE_CALLS[account_id] = current - 1
            except Exception as exc:
                log.exception("Outbound SIP call failed")
                return PJSUA2Result(
                    success=False,
                    account_id=self._current_profile.id,
                    destination_number=destination,
                    audio_path=request.audio_path,
                    registered=True,
                    error=str(exc),
                    details={"exception": f"{type(exc).__name__}: {exc}"},
                )

    def prepare_playback(
        self,
        audio_path: str,
        *,
        destination_number: str = "",
        account_id: str = "",
        repeat_count: int = 1,
        pause_ms: int = 0,
    ) -> PJSUA2Result:
        audio_file = Path(audio_path) if audio_path else None
        if not audio_file or not audio_file.exists():
            return PJSUA2Result(
                success=False,
                account_id=account_id,
                destination_number=destination_number,
                audio_path=audio_path,
                error="Audio file does not exist",
            )

        try:
            repeat_count = max(1, int(repeat_count))
            pause_ms = max(0, int(pause_ms))
        except Exception:
            repeat_count = 1
            pause_ms = 0

        try:
            tmp_manifest = tempfile.NamedTemporaryFile("w", delete=False, prefix="pjsua2_playback_", suffix=".txt", encoding="utf-8")
            manifest_path = Path(tmp_manifest.name)
            with tmp_manifest:
                for idx in range(repeat_count):
                    tmp_manifest.write(str(audio_file.resolve()))
                    tmp_manifest.write("\n")
                    if pause_ms and idx < repeat_count - 1:
                        tmp_manifest.write(f"PAUSE_MS={pause_ms}\n")

            return PJSUA2Result(
                success=True,
                account_id=account_id,
                destination_number=destination_number,
                audio_path=str(audio_file),
                message="Playback manifest prepared",
                details={
                    "manifest_path": str(manifest_path),
                    "repeat_count": repeat_count,
                    "pause_ms": pause_ms,
                    "playback_mode": "manifest",
                },
            )
        except Exception as exc:
            return PJSUA2Result(
                success=False,
                account_id=account_id,
                destination_number=destination_number,
                audio_path=audio_path,
                error=str(exc),
                details={"exception": f"{type(exc).__name__}: {exc}"},
            )

    def status_detail(self) -> dict[str, Any]:
        current_account_id = self._current_profile.id if self._current_profile else ""
        with _TRUNK_CONCURRENCY_LOCK:
            active_calls = _TRUNK_ACTIVE_CALLS.get(current_account_id, 0) if current_account_id else 0
        return {
            "available": self.available,
            "registered": self._registered,
            "current_account_id": current_account_id,
            "import_error": self.import_error,
            "has_endpoint": self._endpoint is not None,
            "has_call": self._last_call is not None,
            "active_calls": active_calls,
        }

    def _wait_for_registration(self, account: "_GatewayAccount", *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        deadline = time.time() + max(1.0, timeout_seconds)
        last_info: dict[str, Any] = {}

        while time.time() < deadline:
            with suppress(Exception):
                self._endpoint.libHandleEvents(50)

            info = account.registration_info()
            if info:
                last_info = info
                if info.get("success"):
                    return {
                        "success": True,
                        "status_code": int(info.get("status_code") or 0),
                        "status_text": str(info.get("status_text") or "OK"),
                        "registered": bool(info.get("registered")),
                        "reason": "",
                        "polls": info.get("polls", 0),
                    }
                if info.get("terminal_failure"):
                    return {
                        "success": False,
                        "status_code": int(info.get("status_code") or 0),
                        "status_text": str(info.get("status_text") or ""),
                        "registered": bool(info.get("registered")),
                        "error": f"Registrar replied with {info.get('status_code')}: {info.get('status_text') or 'registration failed'}",
                        "polls": info.get("polls", 0),
                    }

            time.sleep(0.2)

        status_code = int(last_info.get("status_code") or 0)
        status_text = str(last_info.get("status_text") or "")
        return {
            "success": False,
            "status_code": status_code,
            "status_text": status_text,
            "registered": bool(last_info.get("registered")),
            "error": (
                f"Timed out waiting for SIP registration response ({status_code} {status_text}).".strip()
                if status_code or status_text
                else "Timed out waiting for SIP registration response."
            ),
            "polls": last_info.get("polls", 0),
        }

    def _build_id_uri(self, profile: SipAccountProfile) -> str:
        if profile.sip_uri:
            return profile.sip_uri
        user = profile.username or profile.id
        host = profile.domain or str(profile.extra.get("host", "") or "")
        port = profile.extra.get("port")
        target = _host_with_port(host, port)
        if not target:
            fallback = self.settings.sip_gateway_domain if hasattr(self.settings, "sip_gateway_domain") else ""
            target = _host_with_port(fallback, port)
        if not target:
            raise ValueError("SIP profile requires either sip_uri or domain/host")
        return f"sip:{user}@{target}"

    def _build_registrar_uri(self, profile: SipAccountProfile) -> str:
        if profile.registrar_uri:
            return profile.registrar_uri
        host = profile.domain or str(profile.extra.get("host", "") or "")
        port = profile.extra.get("port")
        target = _host_with_port(host, port)
        if not target:
            raise ValueError("SIP profile requires registrar_uri or domain/host")
        transport = (profile.transport or "").strip().lower()
        transport_suffix = f";transport={transport}" if transport else ""
        return f"sip:{target}{transport_suffix}"

    def _build_sip_uri(self, number: str) -> str:
        profile = self._current_profile
        if profile:
            host = profile.domain or str(profile.extra.get("host", "") or "")
            port = profile.extra.get("port")
            target = _host_with_port(host, port)
            if target:
                return f"sip:{number}@{target}"
        return f"sip:{number}"

    def close(self) -> None:
        self.shutdown()

    def __enter__(self) -> "PJSipUASession":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class _GatewayAccount:
    """
    Minimal account adapter to keep the implementation realistic without assuming
    a specific PJSUA2 subclass hierarchy in every runtime.
    """
    def __init__(self, session: PJSipUASession, account_id: str):
        self._session = session
        self._account_id = account_id
        self._account = None
        self._poll_count = 0

    def create(self, account_cfg: Any) -> None:
        pj = self._session._pj
        if pj is None:
            raise PJSUA2UnavailableError("PJSUA2 bindings are unavailable")
        account = pj.Account()
        account.create(account_cfg)
        self._account = account

    def makeCall(self, uri: str, callback: Any) -> Any:
        if self._account is None:
            raise RuntimeError("SIP account is not initialised")
        call = self._account.makeCall(uri, callback)
        return call

    def registration_info(self) -> dict[str, Any]:
        if self._account is None:
            return {}

        self._poll_count += 1
        info_obj = None
        with suppress(Exception):
            info_obj = self._account.getInfo()
        if info_obj is None:
            return {"polls": self._poll_count}

        status_code = 0
        status_text = ""
        registered = False

        for attr in ("regStatus", "reg_status"):
            with suppress(Exception):
                value = getattr(info_obj, attr)
                if value is not None:
                    status_code = int(value)
                    break

        for attr in ("regStatusText", "reg_status_text"):
            with suppress(Exception):
                value = getattr(info_obj, attr)
                if value is not None:
                    status_text = str(value)
                    break

        for attr in ("regIsActive", "reg_is_active"):
            with suppress(Exception):
                value = getattr(info_obj, attr)
                registered = bool(value)
                break

        success = registered and 200 <= status_code < 300
        terminal_failure = status_code >= 300

        return {
            "registered": registered,
            "status_code": status_code,
            "status_text": status_text,
            "success": success,
            "terminal_failure": terminal_failure,
            "polls": self._poll_count,
        }

    def shutdown(self) -> None:
        if self._account is not None:
            with suppress(Exception):
                self._account.shutdown()
        self._account = None
        self._poll_count = 0


class _CallCallbackHolder:
    """
    Lightweight callback adapter that releases per-trunk concurrency slots when a
    call transitions into a terminal/disconnected state.

    The implementation stays defensive because Python PJSUA2 bindings can differ
    slightly between runtimes and distributions.
    """
    _TERMINAL_STATES = {
        "PJSIP_INV_STATE_DISCONNECTED",
        "DISCONNECTED",
        "disconnected",
        "CALL_DISCONNECTED",
    }

    def __init__(self, session: PJSipUASession, account_id: str, call_id: str):
        self._session = session
        self._account_id = account_id
        self._call_id = call_id
        self._released = False
        self._lock = threading.Lock()

    def _release_slot(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True

        with _TRUNK_CONCURRENCY_LOCK:
            current = _TRUNK_ACTIVE_CALLS.get(self._account_id, 0)
            if current <= 1:
                _TRUNK_ACTIVE_CALLS.pop(self._account_id, None)
            else:
                _TRUNK_ACTIVE_CALLS[self._account_id] = current - 1

        log.info(
            "Released SIP trunk concurrency slot account=%s call_id=%s active_calls=%s",
            self._account_id,
            self._call_id,
            _TRUNK_ACTIVE_CALLS.get(self._account_id, 0),
        )

    def onCallState(self, *args: Any, **kwargs: Any) -> None:
        call_obj = args[0] if args else None
        info_obj = None

        with suppress(Exception):
            if call_obj is not None and hasattr(call_obj, "getInfo"):
                info_obj = call_obj.getInfo()

        state_name = ""
        state_text = ""
        last_status_code = 0

        if info_obj is not None:
            for attr in ("stateText", "state_text"):
                with suppress(Exception):
                    value = getattr(info_obj, attr)
                    if value is not None:
                        state_text = str(value)
                        break

            for attr in ("lastStatusCode", "last_status_code"):
                with suppress(Exception):
                    value = getattr(info_obj, attr)
                    if value is not None:
                        last_status_code = int(value)
                        break

            for attr in ("state", "callState", "call_state"):
                with suppress(Exception):
                    value = getattr(info_obj, attr)
                    if value is not None:
                        state_name = str(value)
                        break

        if (
            state_name in self._TERMINAL_STATES
            or state_text.upper() == "DISCONNECTED"
            or last_status_code >= 300
        ):
            self._release_slot()

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None
        return _noop


def build_pjsua2_service(settings: Settings) -> PJSipUASession:
    return PJSipUASession(settings)
