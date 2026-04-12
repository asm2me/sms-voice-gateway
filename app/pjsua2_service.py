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
    details: dict[str, Any] = field(default_factory=dict)


class PJSUA2UnavailableError(RuntimeError):
    pass


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

                self.shutdown()
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
                self._registered = True

                return PJSUA2RegistrationResult(
                    success=True,
                    account_id=selected.id,
                    message="SIP account registered",
                    details={
                        "id_uri": account_cfg.idUri,
                        "registrar_uri": registrar_uri,
                        "username": selected.username,
                        "transport": selected.transport,
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
                call_id = f"pj_{int(time.time() * 1000)}"
                call = self._account.makeCall(self._build_sip_uri(destination), _CallCallbackHolder())
                self._last_call = call

                playback_result = self.prepare_playback(
                    request.audio_path,
                    destination_number=destination,
                    account_id=self._current_profile.id,
                    repeat_count=request.playback_repeats,
                    pause_ms=request.playback_pause_ms,
                )

                return PJSUA2Result(
                    success=True,
                    account_id=self._current_profile.id,
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
                    },
                )
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
        return {
            "available": self.available,
            "registered": self._registered,
            "current_account_id": self._current_profile.id if self._current_profile else "",
            "import_error": self.import_error,
            "has_endpoint": self._endpoint is not None,
            "has_call": self._last_call is not None,
        }

    def _build_id_uri(self, profile: SipAccountProfile) -> str:
        if profile.sip_uri:
            return profile.sip_uri
        user = profile.username or profile.id
        domain = profile.domain or self.settings.sip_gateway_domain if hasattr(self.settings, "sip_gateway_domain") else ""
        if not domain:
            raise ValueError("SIP profile requires either sip_uri or domain")
        return f"sip:{user}@{domain}"

    def _build_registrar_uri(self, profile: SipAccountProfile) -> str:
        if profile.registrar_uri:
            return profile.registrar_uri
        domain = profile.domain or ""
        if not domain:
            raise ValueError("SIP profile requires registrar_uri or domain")
        return f"sip:{domain}"

    def _build_sip_uri(self, number: str) -> str:
        profile = self._current_profile
        if profile and profile.domain:
            return f"sip:{number}@{profile.domain}"
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

    def shutdown(self) -> None:
        if self._account is not None:
            with suppress(Exception):
                self._account.shutdown()
        self._account = None


class _CallCallbackHolder:
    """
    Placeholder callback object. Real deployments can subclass this to hook into
    call state changes, media negotiation, and playback initiation.

    The object intentionally remains minimal so the helper can be imported even
    when the binding implementation differs slightly between environments.
    """
    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None
        return _noop


def build_pjsua2_service(settings: Settings) -> PJSipUASession:
    return PJSipUASession(settings)