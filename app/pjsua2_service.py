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
import uuid
import wave
import os
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


def _extract_sip_user(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-zA-Z]+:", "", text)
    text = text.strip("<>\"' ")
    if ";" in text:
        text = text.split(";", 1)[0]
    if "?" in text:
        text = text.split("?", 1)[0]
    if "@" in text:
        text = text.split("@", 1)[0]
    text = text.strip()
    if ":" in text and not text.startswith("+"):
        text = text.rsplit(":", 1)[-1]
    return text.strip()


def _extract_display_destination(raw: str) -> str:
    candidate = _extract_sip_user(raw)
    if not candidate:
        return ""
    try:
        return _normalise_number(candidate)
    except Exception:
        return candidate[:64]




def _host_with_port(host: str, port: int | str | None) -> str:
    host = (host or "").strip()
    if not host:
        return ""

    port_value = None
    try:
        if port not in (None, "", 0, "0"):
            port_value = int(port)
    except Exception:
        port_value = None

    if host.startswith("[") and "]" in host:
        return host if port_value is None else f"{host}:{port_value}"
    if ":" in host and host.count(":") > 1:
        return host if port_value is None else f"[{host}]:{port_value}"
    if ":" in host:
        host_name, host_port = host.rsplit(":", 1)
        if host_port.isdigit():
            return host
    return host if port_value is None else f"{host}:{port_value}"


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
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    registrar_uri: str = ""
    proxy_uri: str = ""
    transport: str = "UDP"
    caller_id: str = ""
    enabled: bool = True
    registration_timeout: int = 300
    auth_realm: str = ""
    concurrency_limit: int = 0
    preferred_codecs: list[str] = field(default_factory=list)
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
    enable_recording: bool = False
    extra_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class PJSUA2Result:
    success: bool
    account_id: str = ""
    call_id: str = ""
    destination_number: str = ""
    registered: bool = False
    audio_path: str = ""
    recording_path: str = ""
    message: str = ""
    error: str = ""
    answered: bool = False
    delivered: bool = False
    read: bool = False
    playback_seconds: float = 0.0
    audio_duration_seconds: float = 0.0
    call_duration_seconds: float = 0.0
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
    probe_mode: str = ""
    options_sent: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class PJSUA2UnavailableError(RuntimeError):
    pass


_TRUNK_CONCURRENCY_LOCK = threading.RLock()
_TRUNK_ACTIVE_CALLS: dict[str, int] = {}
_TRUNK_CALL_STATES: dict[str, dict[str, Any]] = {}
_TRUNK_CALL_STATE_RETENTION_SECONDS = 30.0
_PJSUA_GLOBAL_LOCK = threading.RLock()
_PJSUA_GLOBAL_ENDPOINT = None
_PJSUA_GLOBAL_TRANSPORT = None
_PJSUA_GLOBAL_SESSIONS: dict[str, "PJSipUASession"] = {}
_PJSUA_REGISTERED_THREADS: dict[int, set[int]] = {}
_PJSUA_THREAD_DESCS: dict[int, Any] = {}
_PJSUA_PLAYER_LOCK = threading.RLock()
_PJSUA_EVENT_PUMP_LOCKS: dict[int, threading.RLock] = {}
_PJSUA_ACTIVE_PLAYERS: dict[str, dict[str, Any]] = {}
_PJSUA_RETIRED_PLAYERS: list[dict[str, Any]] = []
_PJSUA_AUDIO_SETUP_LOCKS: dict[str, threading.RLock] = {}
# Serializes all PJSUA2 media operations (createPlayer, startTransmit,
# stopTransmit, recorder creation/destruction) across calls. PJSIP group
# locks assert when one call's media object is touched while another call
# holds the lock owner thread, so we keep all media bridge transitions on a
# single thread at a time.
_PJSUA_MEDIA_OP_LOCK = threading.RLock()


def _pjsua_registration_key(endpoint: object | None) -> int:
    return id(endpoint) if endpoint is not None else 0


def _pjsua_event_pump_lock(endpoint: object | None) -> threading.RLock:
    key = _pjsua_registration_key(endpoint)
    with _PJSUA_GLOBAL_LOCK:
        lock = _PJSUA_EVENT_PUMP_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PJSUA_EVENT_PUMP_LOCKS[key] = lock
        return lock


def _release_player(call_id: str, *, stop_transmit: bool = True) -> None:
    with _PJSUA_PLAYER_LOCK:
        player_state = _PJSUA_ACTIVE_PLAYERS.pop(call_id, None)
        # Clean up the per-call audio setup lock
        setup_lock = _PJSUA_AUDIO_SETUP_LOCKS.pop(call_id, None)
    if not player_state:
        return

    wav_path = player_state.get("wav_path")
    cleanup_wav = bool(player_state.get("cleanup_wav"))
    player = player_state.get("player")
    call_audio_media = player_state.get("call_audio_media")
    recorder = player_state.get("recorder")
    stop_transmit_error = ""

    # Some PJSUA2 builds assert if AudioMediaPlayer destruction happens from a
    # callback/event thread. Keep released player objects strongly referenced
    # after detaching them so Python GC does not immediately run native cleanup on
    # an unsafe thread.
    if stop_transmit:
        with _PJSUA_MEDIA_OP_LOCK:
            try:
                if player is not None and call_audio_media is not None and hasattr(player, "stopTransmit"):
                    player.stopTransmit(call_audio_media)
            except Exception as exc:
                stop_transmit_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "Outbound SIP player stopTransmit failed call_id=%s error=%s",
                    call_id,
                    stop_transmit_error,
                )

            try:
                if player is not None and recorder is not None and hasattr(player, "stopTransmit"):
                    player.stopTransmit(recorder)
            except Exception as exc:
                extra_error = f"{type(exc).__name__}: {exc}"
                stop_transmit_error = f"{stop_transmit_error}; {extra_error}".strip("; ")
                log.warning(
                    "Outbound SIP player->recorder stopTransmit failed call_id=%s error=%s",
                    call_id,
                    extra_error,
                )

            try:
                if call_audio_media is not None and recorder is not None and hasattr(call_audio_media, "stopTransmit"):
                    call_audio_media.stopTransmit(recorder)
            except Exception as exc:
                extra_error = f"{type(exc).__name__}: {exc}"
                stop_transmit_error = f"{stop_transmit_error}; {extra_error}".strip("; ")
                log.warning(
                    "Outbound SIP call media->recorder stopTransmit failed call_id=%s error=%s",
                    call_id,
                    extra_error,
                )

    retired_state = dict(player_state)
    retired_state["released_at"] = time.time()
    retired_state["stop_transmit"] = stop_transmit
    retired_state["stop_transmit_error"] = stop_transmit_error
    retired_state["call_audio_media"] = None

    with _PJSUA_PLAYER_LOCK:
        _PJSUA_RETIRED_PLAYERS.append(retired_state)
        if len(_PJSUA_RETIRED_PLAYERS) > 32:
            del _PJSUA_RETIRED_PLAYERS[:-32]

    if cleanup_wav and wav_path:
        with suppress(Exception):
            Path(str(wav_path)).unlink(missing_ok=True)


class PJSipUASession:
    """
    Lightweight wrapper around a running PJSUA2 endpoint.

    The endpoint is created lazily and shared per service instance. For safety,
    call operations are guarded by a lock because PJSUA2 call/account object
    state is not designed for uncontrolled concurrent mutation.
    """
    def __init__(self, settings: Settings, *, isolated: bool = False):
        self.settings = settings
        self._pj, self.import_error = _safe_import_pjsua2()
        self._lock = threading.RLock()
        self._endpoint = None
        self._transport = None
        self._account = None
        self._current_profile: SipAccountProfile | None = None
        self._registered = False
        self._last_call = None
        self._isolated = bool(isolated)

    @property
    def available(self) -> bool:
        return self._pj is not None

    def ensure_available(self) -> None:
        if self._pj is None:
            raise PJSUA2UnavailableError(
                "PJSUA2 bindings are unavailable"
                + (f": {self.import_error}" if self.import_error else "")
            )

    def _register_current_thread(self) -> None:
        if self._pj is None:
            return

        thread_id = threading.get_ident()
        registration_key = _pjsua_registration_key(self._endpoint)
        with _PJSUA_GLOBAL_LOCK:
            registered_threads = _PJSUA_REGISTERED_THREADS.setdefault(registration_key, set())
            if thread_id in registered_threads:
                return

        # Critical: if pj already knows this thread (because it's one of pj's
        # own internal worker threads invoking us via a callback), DO NOT call
        # libRegisterThread on it. Re-registering a pj-native thread corrupts
        # the thread descriptor table and causes later pj_thread_this()
        # / grp_lock_unset_owner_thread assertions to fire (pj/lock.c:279).
        if self._endpoint is not None:
            is_registered_fn = getattr(self._endpoint, "libIsThreadRegistered", None)
            if callable(is_registered_fn):
                try:
                    if bool(is_registered_fn()):
                        with _PJSUA_GLOBAL_LOCK:
                            _PJSUA_REGISTERED_THREADS.setdefault(registration_key, set()).add(thread_id)
                        return
                except Exception:
                    # If the check itself fails, fall through to the
                    # registration attempt below — it's no worse than before.
                    pass

        name = f"py-{thread_id}"[:31]
        desc = _PJSUA_THREAD_DESCS.get(thread_id)
        if desc is None:
            desc = [0] * 64
            _PJSUA_THREAD_DESCS[thread_id] = desc

        register_candidates: list[tuple[Any, tuple[Any, ...]]] = []

        if self._endpoint is not None:
            register_fn = getattr(self._endpoint, "libRegisterThread", None)
            if callable(register_fn):
                register_candidates.append((register_fn, (name, desc)))
                register_candidates.append((register_fn, (name,)))

        module_register = getattr(self._pj, "libRegisterThread", None)
        if callable(module_register):
            register_candidates.append((module_register, (name, desc)))
            register_candidates.append((module_register, (name,)))

        thread_register = getattr(self._pj, "threadRegister", None)
        if callable(thread_register):
            register_candidates.append((thread_register, (name, desc)))
            register_candidates.append((thread_register, (name,)))

        for register_fn, args in register_candidates:
            try:
                register_fn(*args)
                with _PJSUA_GLOBAL_LOCK:
                    registered_threads = _PJSUA_REGISTERED_THREADS.setdefault(registration_key, set())
                    registered_threads.add(thread_id)
                return
            except TypeError:
                continue
            except Exception:
                continue

    def _pump_events(self, timeout_ms: int = 50) -> None:
        endpoint = self._endpoint
        if endpoint is None:
            return
        with _pjsua_event_pump_lock(endpoint):
            with suppress(Exception):
                endpoint.libHandleEvents(int(timeout_ms or 0))

    def initialize(self) -> PJSUA2RegistrationResult:
        global _PJSUA_GLOBAL_ENDPOINT, _PJSUA_GLOBAL_TRANSPORT

        self._register_current_thread()
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
                        details={"already_initialised": True, "isolated": self._isolated},
                    )

                pj = self._pj
                assert pj is not None

                if not self._isolated:
                    with _PJSUA_GLOBAL_LOCK:
                        if _PJSUA_GLOBAL_ENDPOINT is not None:
                            self._endpoint = _PJSUA_GLOBAL_ENDPOINT
                            self._transport = _PJSUA_GLOBAL_TRANSPORT
                            self._register_current_thread()
                            return PJSUA2RegistrationResult(
                                success=True,
                                message="PJSUA2 endpoint already initialised",
                                details={"already_initialised": True, "shared_global_endpoint": True, "isolated": False},
                            )

                ep = pj.Endpoint()
                ep.libCreate()

                ep_cfg = pj.EpConfig()
                with suppress(Exception):
                    ep_cfg.logConfig.level = 3
                    ep_cfg.logConfig.consoleLevel = 3
                with suppress(Exception):
                    ep_cfg.medConfig.noVad = True
                with suppress(Exception):
                    ep_cfg.uaConfig.threadCnt = 0
                with suppress(Exception):
                    ep_cfg.uaConfig.mainThreadOnly = True
                with suppress(Exception):
                    ep_cfg.medConfig.hasIoqueue = False
                with suppress(Exception):
                    ep_cfg.medConfig.clockRate = 16000
                with suppress(Exception):
                    ep_cfg.medConfig.sndClockRate = 16000

                ep.libInit(ep_cfg)

                transport_cfg = pj.TransportConfig()
                with suppress(Exception):
                    transport_cfg.port = int(getattr(self.settings, "sip_port", 0) or 0)
                with suppress(Exception):
                    transport_cfg.port = int(getattr(self.settings, "sip_listen_port", transport_cfg.port) or transport_cfg.port)
                if not getattr(transport_cfg, "port", 0):
                    with suppress(Exception):
                        transport_cfg.port = int(getattr(self._current_profile, "port", 0) or 0)
                if not getattr(transport_cfg, "port", 0):
                    transport_cfg.port = 5060
                local_transport_port = 0
                with suppress(Exception):
                    transport_cfg.port = local_transport_port

                transport_name = str(getattr(self._current_profile, "transport", "") or "").strip().upper()
                if transport_name == "TCP":
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_TCP", None)
                elif transport_name == "TLS":
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_TLS", None)
                else:
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_UDP", None)
                if transport_type is None:
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_UDP", None)
                if transport_type is None:
                    transport_type = getattr(pj, "PJSIP_TRANSPORT_TCP", 0)

                transport = ep.transportCreate(transport_type, transport_cfg)
                ep.libStart()

                with suppress(Exception):
                    aud_mgr = ep.audDevManager()
                    use_null_sound_device = bool(getattr(self.settings, "use_null_sound_device", False))
                    log.info(
                        "PJSIP audio device manager account=%s use_null_sound_device=%s isolated=%s",
                        self._account_id if hasattr(self, "_account_id") else "init",
                        use_null_sound_device,
                        self._isolated,
                    )
                    if use_null_sound_device and hasattr(aud_mgr, "setNullDev"):
                        aud_mgr.setNullDev()
                        log.info("PJSIP null sound device enabled")
                    elif hasattr(aud_mgr, "setNullDev"):
                        with suppress(Exception):
                            aud_mgr.setNullDev()

                self._endpoint = ep
                self._transport = transport

                if not self._isolated:
                    with _PJSUA_GLOBAL_LOCK:
                        _PJSUA_GLOBAL_ENDPOINT = ep
                        _PJSUA_GLOBAL_TRANSPORT = transport

                self._register_current_thread()
                return PJSUA2RegistrationResult(
                    success=True,
                    message="PJSUA2 endpoint initialised",
                    details={"transport": "created", "isolated": self._isolated},
                )
            except Exception as exc:
                log.exception("PJSUA2 endpoint initialisation failed")
                self.shutdown()
                return PJSUA2RegistrationResult(
                    success=False,
                    error=str(exc),
                    details={"exception": f"{type(exc).__name__}: {exc}", "isolated": self._isolated},
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

            endpoint = self._endpoint
            self._endpoint = None
            self._transport = None

            if self._isolated and endpoint is not None:
                with suppress(Exception):
                    endpoint.libDestroy()
                with _PJSUA_GLOBAL_LOCK:
                    endpoint_key = _pjsua_registration_key(endpoint)
                    _PJSUA_REGISTERED_THREADS.pop(endpoint_key, None)
                    _PJSUA_EVENT_PUMP_LOCKS.pop(endpoint_key, None)

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
            host=str(profile.get("host", "")),
            port=int(profile.get("port", 0) or 0),
            username=str(profile.get("username", "")),
            password=str(profile.get("password", "")),
            registrar_uri=str(profile.get("registrar_uri", "")),
            proxy_uri=str(profile.get("proxy_uri", "")),
            transport=str(profile.get("transport", "UDP")),
            caller_id=str(profile.get("caller_id", "")),
            enabled=bool(profile.get("enabled", True)),
            registration_timeout=int(profile.get("registration_timeout", 300) or 300),
            auth_realm=str(profile.get("auth_realm", "")),
            concurrency_limit=max(0, int(profile.get("concurrency_limit", 0) or 0)),
            preferred_codecs=[str(item).strip() for item in (profile.get("preferred_codecs", []) or []) if str(item).strip()],
            extra=dict(profile.get("extra", {}) or {}),
        )

    def register_account(self, profile: SipAccountProfile | dict[str, Any]) -> PJSUA2RegistrationResult:
        self._register_current_thread()
        with self._lock:
            try:
                pj = self._pj
                assert pj is not None
                selected = self.select_profile(profile)
                existing_profile = self._current_profile
                self._current_profile = selected

                init_result = self.initialize()
                if not init_result.success:
                    return init_result

                self._register_current_thread()
                if not selected.enabled:
                    return PJSUA2RegistrationResult(
                        success=False,
                        account_id=selected.id,
                        error="SIP profile is disabled",
                    )

                if (
                    self._account is not None
                    and existing_profile is not None
                    and existing_profile.id == selected.id
                ):
                    return PJSUA2RegistrationResult(
                        success=True,
                        account_id=selected.id,
                        message="SIP profile already active",
                        details={"already_registered": self._registered},
                    )

                if (
                    self._account is not None
                    and existing_profile is not None
                    and existing_profile.id != selected.id
                ):
                    with suppress(Exception):
                        self._account.shutdown()
                    self._account = None
                    self._registered = False
                    self._last_call = None
                    self._current_profile = None

                if self._endpoint is None:
                    init_result = self.initialize()
                    if not init_result.success:
                        return init_result

                account_cfg = pj.AccountConfig()
                account_cfg.idUri = selected.sip_uri or self._build_id_uri(selected)

                should_register = bool(selected.extra.get("register", True))
                account_cfg.regConfig.registerOnAdd = should_register

                registrar_uri = selected.registrar_uri or self._build_registrar_uri(selected)
                account_cfg.regConfig.registrarUri = registrar_uri

                with suppress(Exception):
                    sip_transport = (selected.transport or "").strip().lower()
                    if sip_transport:
                        contact_target = _host_with_port(
                            selected.domain or selected.host or str(selected.extra.get("host", "") or ""),
                            selected.port or selected.extra.get("port"),
                        )
                        if contact_target:
                            account_cfg.sipConfig.contactUri = (
                                f"sip:{selected.username or selected.id}@{contact_target};transport={sip_transport}"
                            )

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

                self._apply_codec_preferences(selected)
                account = _GatewayAccount(self, selected.id)
                account.create(account_cfg)

                self._account = account
                self._current_profile = selected

                if should_register:
                    wait_result = self._wait_for_registration(account)
                    self._registered = bool(wait_result.get("success"))
                    return PJSUA2RegistrationResult(
                        success=bool(wait_result.get("success")),
                        account_id=selected.id,
                        message="SIP registration succeeded" if wait_result.get("success") else "",
                        error=str(wait_result.get("error") or ""),
                        status_code=int(wait_result.get("status_code") or 0),
                        status_text=str(wait_result.get("status_text") or ""),
                        probe_mode="register",
                        details={
                            "id_uri": account_cfg.idUri,
                            "registrar_uri": registrar_uri,
                            "username": selected.username,
                            "transport": selected.transport,
                            "register_on_add": True,
                            "registered": bool(wait_result.get("registered")),
                            "polls": wait_result.get("polls", 0),
                        },
                    )

                self._registered = True
                return PJSUA2RegistrationResult(
                    success=True,
                    account_id=selected.id,
                    message="SIP account prepared for direct outbound use without trunk registration",
                    status_code=200,
                    status_text="OK",
                    probe_mode="prepare",
                    details={
                        "id_uri": account_cfg.idUri,
                        "registrar_uri": registrar_uri,
                        "username": selected.username,
                        "transport": selected.transport,
                        "register_on_add": False,
                        "options_target": registrar_uri,
                        "prepared_for_direct_outbound": True,
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
        self._register_current_thread()
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
                enable_recording=bool(request.get("enable_recording", False)),
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

        selected_profile = self.select_profile(profile) if profile is not None else None

        try:
            with self._lock:
                if selected_profile is not None:
                    if not self._current_profile or self._current_profile.id != selected_profile.id:
                        reg = self.register_account(selected_profile)
                        if not reg.success:
                            return PJSUA2Result(
                                success=False,
                                account_id=selected_profile.id,
                                destination_number=destination,
                                error=reg.error or reg.message or "SIP registration failed",
                                details={"registration": reg.details},
                            )
                    else:
                        # Same SIP account id but settings such as
                        # concurrency_limit, caller_id, preferred_codecs may
                        # have been updated in admin UI. Refresh the cached
                        # profile so subsequent calls see the new values
                        # without forcing a re-register (registration-side
                        # fields like host/port/credentials are unchanged on
                        # this path because the id is unchanged and the
                        # profile was just rebuilt from current settings).
                        self._current_profile = selected_profile

                self._register_current_thread()
                if not self._account or not self._current_profile:
                    return PJSUA2Result(
                        success=False,
                        destination_number=destination,
                        error="No SIP account is active",
                    )

                pj = self._pj
                assert pj is not None
                account = self._account
                current_profile = self._current_profile
                account_id = current_profile.id
                concurrency_limit = max(0, int(current_profile.concurrency_limit or 0))

                call_id = f"pj_{uuid.uuid4().hex}"
                invite_uri = self._build_sip_uri(destination)
                callback = _CallCallbackHolder(self, account_id, call_id)
                slot_allocated = False

                with _TRUNK_CONCURRENCY_LOCK:
                    active_calls = _TRUNK_ACTIVE_CALLS.get(account_id, 0)
                    if concurrency_limit > 0 and active_calls >= concurrency_limit:
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
                    now = time.time()
                    _TRUNK_CALL_STATES[call_id] = {
                        "call_id": call_id,
                        "account_id": account_id,
                        "state": "dialing",
                        "state_label": "dialing",
                        "last_status_code": 0,
                        "destination_number": destination,
                        "answered": False,
                        "updated_at": now,
                        "connected_at": None,
                        "hangup_at": None,
                        "expires_at": now + _TRUNK_CALL_STATE_RETENTION_SECONDS,
                    }
                    slot_allocated = True

                def _release_allocated_call_slot() -> None:
                    with _TRUNK_CONCURRENCY_LOCK:
                        current = _TRUNK_ACTIVE_CALLS.get(account_id, 0)
                        if current <= 1:
                            _TRUNK_ACTIVE_CALLS.pop(account_id, None)
                        else:
                            _TRUNK_ACTIVE_CALLS[account_id] = current - 1
                        _TRUNK_CALL_STATES.pop(call_id, None)

                try:
                    call = account.makeCall(
                        invite_uri,
                        callback,
                    )
                except Exception:
                    _release_allocated_call_slot()
                    raise
                callback.attach_call(call)
                log.info(
                    "Starting outbound SIP INVITE account=%s destination=%s uri=%s transport=%s trunk_host=%s trunk_port=%s",
                    account_id,
                    destination,
                    invite_uri,
                    (current_profile.transport if current_profile else ""),
                    (
                        current_profile.domain
                        or current_profile.host
                        or str(current_profile.extra.get("host", "") or "")
                        if current_profile
                        else ""
                    ),
                    (
                        current_profile.port
                        or current_profile.extra.get("port")
                        if current_profile
                        else ""
                    ),
                )
                self._last_call = call

            try:
                # NOTE: do not call call.getInfo() / iterate getInfo().media from
                # this (request) thread. PJSUA2 fires onCallState/onCallMediaState
                # on its own worker thread concurrently, and dropping the media
                # object refs here triggers pj group-lock destructor races
                # (pj/lock.c:279 grp_lock_unset_owner_thread assertion).
                # The actual call-info/media inspection happens inside the
                # callbacks, where it is thread-safe.

                playback_result = self.prepare_playback(
                    request.audio_path,
                    destination_number=destination,
                    account_id=account_id,
                    repeat_count=request.playback_repeats,
                    pause_ms=request.playback_pause_ms,
                )
                callback.set_playback_context(
                    audio_path=playback_result.audio_path or request.audio_path,
                    audio_duration_seconds=float(playback_result.audio_duration_seconds or 0.0),
                    repeat_count=request.playback_repeats,
                    pause_ms=request.playback_pause_ms,
                    cleanup_wav=bool(playback_result.details.get("cleanup_wav")) if playback_result.details else False,
                    enable_recording=bool(request.enable_recording),
                    destination_number=destination,
                )
                call_outcome = callback.wait_for_completion(
                    max(
                        1.0,
                        float(request.timeout_seconds or 30)
                        + float(playback_result.audio_duration_seconds or 0.0)
                        + max(5.0, float(request.playback_pause_ms or 0) / 1000.0),
                    )
                )

                answered = bool(call_outcome.get("answered"))
                playback_started = bool(call_outcome.get("playback_started"))
                playback_error = str(call_outcome.get("playback_error") or "")
                playback_seconds = float(call_outcome.get("playback_seconds") or 0.0)
                audio_duration_seconds = float(playback_result.audio_duration_seconds or 0.0)
                disconnect_reason = str(call_outcome.get("disconnect_reason") or "")
                last_status_code = int(call_outcome.get("last_status_code") or 0)

                delivered = answered and playback_started and bool(playback_result.success)
                read = delivered and audio_duration_seconds > 0 and playback_seconds >= (audio_duration_seconds * 0.5)
                remote_hangup_after_answer = answered and last_status_code == 200 and disconnect_reason.upper() == "DISCONNECTED"

                return PJSUA2Result(
                    success=delivered,
                    account_id=account_id,
                    call_id=call_id,
                    destination_number=destination,
                    registered=True,
                    audio_path=request.audio_path,
                    error=playback_error if answered and not delivered and playback_error else "",
                    message=(
                        "Outbound SIP call answered and playback completed"
                        if read
                        else "Outbound SIP call answered but playback did not start"
                        if answered and not playback_started
                        else "Outbound SIP call answered"
                        if answered
                        else "Outbound SIP call was not answered"
                    ),
                    answered=answered,
                    delivered=delivered,
                    read=read,
                    playback_seconds=playback_seconds,
                    audio_duration_seconds=audio_duration_seconds,
                    details={
                        "playback": {
                            **(playback_result.details or {}),
                            "playback_seconds": playback_seconds,
                            "read_threshold_seconds": audio_duration_seconds * 0.5 if audio_duration_seconds > 0 else 0.0,
                        },
                        "playback_prepared": bool(playback_result.success),
                        "playback_started": playback_started,
                        "playback_error": playback_error,
                        "call_state": str(call_outcome.get("state_text") or "completed"),
                        "remote_hangup_after_answer": remote_hangup_after_answer,
                        "display_name": request.display_name,
                        "caller_id": request.caller_id or current_profile.caller_id,
                        "extra_vars": request.extra_vars,
                        "active_calls": _TRUNK_ACTIVE_CALLS.get(account_id, 0),
                        "all_active_calls": dict(_TRUNK_ACTIVE_CALLS),
                        "concurrency_limit": concurrency_limit,
                        "answered": answered,
                        "delivered": delivered,
                        "read": read,
                        "last_status_code": last_status_code,
                        "disconnect_reason": disconnect_reason,
                    },
                )
            except Exception as exc:
                if slot_allocated:
                    _release_allocated_call_slot()
                log.exception("Outbound SIP call failed")
                return PJSUA2Result(
                    success=False,
                    account_id=self._current_profile.id if self._current_profile else request.account_id,
                    destination_number=destination,
                    audio_path=request.audio_path,
                    registered=True,
                    error=str(exc),
                    details={"exception": f"{type(exc).__name__}: {exc}"},
                )
        except Exception as exc:
            if slot_allocated:
                _release_allocated_call_slot()
            log.exception("Outbound SIP call failed")
            return PJSUA2Result(
                success=False,
                account_id=self._current_profile.id if self._current_profile else request.account_id,
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
            prepared_audio_path, cleanup_wav, single_audio_duration_seconds, total_duration_seconds = _prepare_playback_wav(
                audio_file,
                repeat_count=repeat_count,
                pause_ms=pause_ms,
            )

            return PJSUA2Result(
                success=True,
                account_id=account_id,
                destination_number=destination_number,
                audio_path=str(prepared_audio_path),
                message="Playback audio prepared",
                audio_duration_seconds=total_duration_seconds,
                details={
                    "prepared_audio_path": str(prepared_audio_path),
                    "cleanup_wav": cleanup_wav,
                    "repeat_count": repeat_count,
                    "pause_ms": pause_ms,
                    "playback_mode": "wav",
                    "audio_duration_seconds": total_duration_seconds,
                    "single_audio_duration_seconds": single_audio_duration_seconds,
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
        now = time.time()
        with _TRUNK_CONCURRENCY_LOCK:
            active_state_names = {
                "CALLING",
                "DIALING",
                "EARLY",
                "RINGING",
                "ACTIVE",
                "ANSWERED",
                "CONFIRMED",
                "CONNECTED",
            }
            expired_call_ids = [
                call_id
                for call_id, item in _TRUNK_CALL_STATES.items()
                if float(item.get("expires_at") or 0.0)
                and float(item.get("expires_at") or 0.0) <= now
                and str(item.get("state") or item.get("state_label") or "").strip().upper() not in active_state_names
            ]
            for call_id in expired_call_ids:
                _TRUNK_CALL_STATES.pop(call_id, None)

            active_calls = _TRUNK_ACTIVE_CALLS.get(current_account_id, 0) if current_account_id else 0
            all_active_calls = dict(_TRUNK_ACTIVE_CALLS)
            total_active_calls = sum(int(value or 0) for value in all_active_calls.values())
            active_call_items = sorted(
                (dict(item) for item in _TRUNK_CALL_STATES.values()),
                key=lambda item: float(item.get("updated_at") or 0.0),
                reverse=True,
            )
        return {
            "available": self.available,
            "registered": self._registered,
            "current_account_id": current_account_id,
            "import_error": self.import_error,
            "has_endpoint": self._endpoint is not None,
            "has_call": self._last_call is not None,
            "active_calls": active_calls,
            "total_active_calls": total_active_calls,
            "all_active_calls": all_active_calls,
            "active_call_items": active_call_items,
        }

    def _wait_for_registration(self, account: "_GatewayAccount", *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        self._register_current_thread()
        deadline = time.time() + max(1.0, timeout_seconds)
        last_info: dict[str, Any] = {}

        while time.time() < deadline:
            self._pump_events(50)

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

    def _apply_codec_preferences(self, profile: SipAccountProfile) -> None:
        raw_preferences = profile.preferred_codecs or profile.extra.get("preferred_codecs", []) or []
        codec_aliases = {
            "G729": {"G729", "G.729", "G729A", "G729AB"},
            "G723": {"G723", "G723.1", "G.723", "G.723.1"},
            "PCMU": {"PCMU", "ULAW", "MU-LAW", "G711U", "G711MU"},
            "PCMA": {"PCMA", "ALAW", "A-LAW", "G711A"},
        }
        codec_preferences = [str(codec).strip().upper() for codec in raw_preferences if str(codec).strip()]
        if not codec_preferences or self._endpoint is None or self._pj is None:
            return

        def _normalize_codec_name(value: str) -> str:
            normalized = str(value or "").strip().upper().replace(".", "").replace("-", "").replace("_", "").replace("/", "")
            for canonical, aliases in codec_aliases.items():
                alias_tokens = {
                    alias.upper().replace(".", "").replace("-", "").replace("_", "").replace("/", "")
                    for alias in aliases
                }
                if normalized in alias_tokens or canonical.upper().replace(".", "").replace("-", "").replace("_", "").replace("/", "") in normalized:
                    return canonical
            return normalized

        try:
            codec_info_list = []
            with suppress(Exception):
                codec_info_list = list(self._endpoint.codecEnum2())

            available_codec_ids: list[str] = []
            available_codec_names: dict[str, str] = {}
            matched_codec_ids: list[str] = []
            normalized_preferences = [_normalize_codec_name(codec) for codec in codec_preferences]

            for info in codec_info_list:
                codec_id = ""
                for attr in ("codecId", "codec_id", "codecName", "codec_name"):
                    with suppress(Exception):
                        value = getattr(info, attr)
                        if value is not None:
                            codec_id = str(value).strip()
                            if codec_id:
                                break
                if not codec_id:
                    continue

                available_codec_ids.append(codec_id)
                normalized_codec_id = _normalize_codec_name(codec_id)
                available_codec_names[codec_id] = normalized_codec_id
                if normalized_codec_id in normalized_preferences:
                    matched_codec_ids.append(codec_id)

            if not matched_codec_ids:
                log.warning(
                    "Requested SIP codecs are unavailable account=%s requested=%s normalized=%s available=%s",
                    profile.id,
                    codec_preferences,
                    normalized_preferences,
                    available_codec_ids,
                )
                return

            for codec_id in available_codec_ids:
                priority = 0
                if codec_id in matched_codec_ids:
                    priority = 255 if codec_id == matched_codec_ids[0] else max(128, 255 - (matched_codec_ids.index(codec_id) * 10))
                with suppress(Exception):
                    self._endpoint.codecSetPriority(codec_id, priority)

            log.info(
                "Applied SIP codec preferences account=%s requested=%s normalized=%s matched=%s matched_normalized=%s",
                profile.id,
                codec_preferences,
                normalized_preferences,
                matched_codec_ids,
                [available_codec_names.get(codec_id, codec_id) for codec_id in matched_codec_ids],
            )
        except Exception as exc:
            log.warning("Failed applying SIP codec preferences for account=%s: %s", profile.id, exc)

    def _send_options_probe(self, profile: SipAccountProfile) -> dict[str, Any]:
        target = profile.registrar_uri or self._build_registrar_uri(profile)
        try:
            self._pump_events(50)
            log.info(
                "Prepared SIP OPTIONS reachability probe for account=%s target=%s transport=%s",
                profile.id,
                target,
                profile.transport,
            )
            return {
                "success": True,
                "message": "SIP account prepared for direct outbound use without trunk registration",
                "status_code": 0,
                "status_text": "OPTIONS probe prepared",
                "options_sent": True,
                "target": target,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "status_code": 0,
                "status_text": "",
                "options_sent": False,
                "target": target,
            }

    def _build_id_uri(self, profile: SipAccountProfile) -> str:
        if profile.sip_uri:
            return profile.sip_uri
        user = profile.username or profile.id
        host = profile.domain or profile.host or str(profile.extra.get("host", "") or "")
        port = profile.port or profile.extra.get("port")
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
        host = profile.domain or profile.host or str(profile.extra.get("host", "") or "")
        port = profile.port or profile.extra.get("port")
        target = _host_with_port(host, port)
        if not target:
            raise ValueError("SIP profile requires registrar_uri or domain/host")
        return f"sip:{target};lr"

    def _build_sip_uri(self, number: str) -> str:
        profile = self._current_profile
        if profile:
            host = profile.domain or profile.host or str(profile.extra.get("host", "") or "")
            port = profile.port or profile.extra.get("port")
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

        self._session._register_current_thread()
        pj = self._session._pj
        if pj is None:
            raise PJSUA2UnavailableError("PJSUA2 bindings are unavailable")

        call = _GatewayCall(self._account, callback)
        prm = pj.CallOpParam(True)

        with suppress(Exception):
            call_prm = getattr(prm, "opt", None)
            if call_prm is not None and hasattr(call_prm, "audioCount"):
                call_prm.audioCount = 1
            if call_prm is not None and hasattr(call_prm, "videoCount"):
                call_prm.videoCount = 0

        call.makeCall(uri, prm)
        with _TRUNK_CONCURRENCY_LOCK:
            current_state = str(_TRUNK_CALL_STATES.get(callback._call_id, {}).get("state", "")).strip().upper()
        if current_state not in {"EARLY", "RINGING", "ACTIVE", "ANSWERED", "CONFIRMED", "CONNECTED", "DISCONNECTED", "HUNGUP", "MISSED"}:
            with suppress(Exception):
                callback._set_runtime_state(
                    state="CALLING",
                    last_status_code=0,
                    destination_number=_extract_display_destination(uri),
                )
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


def _probe_audio_duration_seconds(audio_path: Path) -> float:
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            frame_rate = int(wav_file.getframerate() or 0)
            frame_count = int(wav_file.getnframes() or 0)
            if frame_rate <= 0 or frame_count <= 0:
                return 0.0
            return frame_count / float(frame_rate)
    except Exception:
        return 0.0


def _prepare_playback_wav(
    audio_path: Path,
    *,
    repeat_count: int,
    pause_ms: int,
) -> tuple[Path, bool, float, float]:
    single_audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
    normalized_repeat_count = max(1, int(repeat_count or 1))
    normalized_pause_ms = max(0, int(pause_ms or 0))

    if normalized_repeat_count == 1 and normalized_pause_ms == 0:
        return (
            audio_path.resolve(),
            False,
            single_audio_duration_seconds,
            single_audio_duration_seconds,
        )

    with wave.open(str(audio_path), "rb") as source_wav:
        frame_count = int(source_wav.getnframes() or 0)
        audio_frames = source_wav.readframes(frame_count)
        params = source_wav.getparams()
        frame_rate = int(source_wav.getframerate() or 0)
        sample_width = int(source_wav.getsampwidth() or 0)
        channels = int(source_wav.getnchannels() or 0)

    if frame_rate <= 0 or sample_width <= 0 or channels <= 0:
        raise ValueError("Audio file has invalid WAV parameters")

    pause_frame_count = int(round(frame_rate * (normalized_pause_ms / 1000.0)))
    silence_frames = b"\x00" * pause_frame_count * sample_width * channels

    tmp_wav = tempfile.NamedTemporaryFile(
        "wb",
        delete=False,
        prefix="pjsua2_playback_",
        suffix=".wav",
    )
    generated_audio_path = Path(tmp_wav.name)
    tmp_wav.close()

    try:
        with wave.open(str(generated_audio_path), "wb") as output_wav:
            output_wav.setparams(params)
            for idx in range(normalized_repeat_count):
                output_wav.writeframes(audio_frames)
                if silence_frames and idx < normalized_repeat_count - 1:
                    output_wav.writeframes(silence_frames)
    except Exception:
        with suppress(Exception):
            generated_audio_path.unlink(missing_ok=True)
        raise

    total_duration_seconds = max(0.0, single_audio_duration_seconds * normalized_repeat_count) + max(
        0.0,
        (normalized_pause_ms / 1000.0) * max(0, normalized_repeat_count - 1),
    )
    return (
        generated_audio_path,
        True,
        single_audio_duration_seconds,
        total_duration_seconds,
    )


def _recordings_dir(settings: Settings) -> Path:
    recordings_dir = (Path(__file__).resolve().parent.parent / "data" / "call_recordings").resolve()
    recordings_dir.mkdir(parents=True, exist_ok=True)
    return recordings_dir


def _build_recording_output_path(
    settings: Settings,
    *,
    account_id: str = "",
    destination_number: str = "",
    call_id: str = "",
) -> Path:
    safe_account = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(account_id or "sip"))
    safe_destination = re.sub(r"[^0-9+]+", "", str(destination_number or "")) or "unknown"
    safe_call_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(call_id or f"call_{int(time.time() * 1000)}"))
    filename = f"{safe_account}_{safe_destination}_{safe_call_id}.wav"
    return _recordings_dir(settings) / filename


class _GatewayCall:
    """
    Best-effort PJSUA2 call object that keeps Python-side callbacks reachable.

    Some PJSUA2 builds only invoke `onCallState` / `onCallMediaState` when the
    Python object itself subclasses `pj.Call`. Other builds tolerate wrapping.
    This helper attempts the real subclass first, then falls back to delegation.
    """
    def __new__(cls, account: Any, callback: "_CallCallbackHolder"):
        pj, _ = _safe_import_pjsua2()
        if pj is None:
            raise PJSUA2UnavailableError("PJSUA2 bindings are unavailable")

        try:
            subclass_type = type(
                "_RuntimeGatewayCall",
                (pj.Call,),
                {
                    "__module__": __name__,
                    "__init__": lambda self, acc, cb: _gateway_call_subclass_init(self, pj, acc, cb),
                    "onCallState": lambda self, prm=None: _gateway_call_handle_state(self, prm),
                    "onCallMediaState": lambda self, prm=None: _gateway_call_handle_media(self, prm),
                },
            )
            instance = object.__new__(subclass_type)
            subclass_type.__init__(instance, account, callback)
            return instance
        except Exception:
            instance = object.__new__(cls)
            return instance

    def __init__(self, account: Any, callback: "_CallCallbackHolder"):
        if hasattr(self, "_callback"):
            return
        pj, _ = _safe_import_pjsua2()
        if pj is None:
            raise PJSUA2UnavailableError("PJSUA2 bindings are unavailable")
        self._pj = pj
        self._callback = callback
        self._call = pj.Call(account, -1)

    def makeCall(self, uri: str, prm: Any) -> None:
        if hasattr(self, "_call"):
            self._call.makeCall(uri, prm)
            return
        super(type(self), self).makeCall(uri, prm)

    def getInfo(self) -> Any:
        if hasattr(self, "_call"):
            return self._call.getInfo()
        return super(type(self), self).getInfo()

    def __getattr__(self, name: str) -> Any:
        if hasattr(self, "_call"):
            return getattr(self._call, name)
        raise AttributeError(name)

    def onCallState(self, prm: Any = None) -> None:
        self._callback.onCallState(self)

    def onCallMediaState(self, prm: Any = None) -> None:
        self._callback.onCallMediaState(self)


def _gateway_call_subclass_init(self: Any, pj: Any, account: Any, callback: "_CallCallbackHolder") -> None:
    pj.Call.__init__(self, account, -1)
    self._pj = pj
    self._callback = callback


def _gateway_call_handle_state(call_obj: Any, prm: Any = None) -> None:
    callback = getattr(call_obj, "_callback", None)
    if callback is not None:
        session = getattr(callback, "_session", None)
        if session is not None:
            session._register_current_thread()
        callback.onCallState(call_obj)


def _gateway_call_handle_media(call_obj: Any, prm: Any = None) -> None:
    callback = getattr(call_obj, "_callback", None)
    if callback is not None:
        session = getattr(callback, "_session", None)
        if session is not None:
            session._register_current_thread()
        callback.onCallMediaState(call_obj)


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
    _ACTIVE_STATES = {
        "ACTIVE",
        "ANSWERED",
        "CONFIRMED",
        "CONNECTED",
        "CALLING",
        "EARLY",
        "RINGING",
    }

    def __init__(self, session: PJSipUASession, account_id: str, call_id: str):
        self._session = session
        self._account_id = account_id
        self._call_id = call_id
        self._released = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._answered_at: float | None = None
        self._disconnected_at: float | None = None
        self._last_status_code = 0
        self._state_text = ""
        self._disconnect_reason = ""
        self._playback_started_at: float | None = None
        self._playback_finished_at: float | None = None
        self._playback_audio_path = ""
        self._playback_audio_duration_seconds = 0.0
        self._playback_repeat_count = 1
        self._playback_pause_ms = 0
        self._playback_started = False
        self._playback_pending = False
        self._playback_ready = False
        self._playback_error = ""
        self._playback_hangup_requested = False
        self._hangup_requested_at: float | None = None
        self._playback_cleanup_wav = False
        self._scheduled_hangup_at: float | None = None
        self._playback_bridge_released = False
        self._player = None
        self._player_media = None
        self._recorder = None
        self._recording_enabled = False
        self._recording_path = ""
        self._recording_error = ""
        self._destination_number = ""
        self._call_obj = None

    def _set_runtime_state(
        self,
        *,
        state: str,
        last_status_code: int = 0,
        destination_number: str = "",
    ) -> None:
        with _TRUNK_CONCURRENCY_LOCK:
            current = _TRUNK_CALL_STATES.get(self._call_id, {})
            now = time.time()
            normalized_state = str(state or "").strip()
            destination_value = destination_number or str(current.get("destination_number", "") or "")
            _TRUNK_CALL_STATES[self._call_id] = {
                **current,
                "call_id": self._call_id,
                "account_id": self._account_id,
                "state": normalized_state,
                "state_label": normalized_state or str(current.get("state_label", "") or ""),
                "last_status_code": int(last_status_code or 0),
                "destination_number": destination_value,
                "answered": self._answered_at is not None or normalized_state.upper() in self._ACTIVE_STATES,
                "updated_at": now,
                "connected_at": self._answered_at,
                "hangup_at": current.get("hangup_at"),
                "expires_at": now + _TRUNK_CALL_STATE_RETENTION_SECONDS,
            }

    def _release_playback_bridge(self, *, stop_transmit: bool, reason: str) -> None:
        if self._playback_bridge_released:
            return

        self._playback_bridge_released = True
        _release_player(self._call_id, stop_transmit=stop_transmit)
        self._player = None
        self._player_media = None
        self._recorder = None
        self._playback_pending = False
        self._playback_ready = False

        log.info(
            "Outbound SIP playback bridge released account=%s call_id=%s stop_transmit=%s reason=%s",
            self._account_id,
            self._call_id,
            stop_transmit,
            reason,
        )

    def _release_slot(self) -> None:
        self._session._register_current_thread()
        with self._lock:
            if self._released:
                return
            self._released = True

        self._release_playback_bridge(stop_transmit=False, reason="call_disconnected")
        self._player = None
        self._player_media = None
        self._recorder = None
        self._call_obj = None
        self._playback_pending = False
        self._playback_ready = False
        self._hangup_requested_at = None

        # Clean up the per-call audio setup lock if it exists
        with _PJSUA_PLAYER_LOCK:
            _PJSUA_AUDIO_SETUP_LOCKS.pop(self._call_id, None)

        with _TRUNK_CONCURRENCY_LOCK:
            current = _TRUNK_ACTIVE_CALLS.get(self._account_id, 0)
            if current <= 1:
                _TRUNK_ACTIVE_CALLS.pop(self._account_id, None)
            else:
                _TRUNK_ACTIVE_CALLS[self._account_id] = current - 1
            retained = dict(_TRUNK_CALL_STATES.get(self._call_id, {}))
            now = time.time()
            retained.update(
                {
                    "call_id": self._call_id,
                    "account_id": self._account_id,
                    "state": "HUNGUP" if self._answered_at is not None else "MISSED",
                    "answered": self._answered_at is not None,
                    "connected_at": self._answered_at,
                    "hangup_at": self._disconnected_at or now,
                    "updated_at": now,
                    "expires_at": now + _TRUNK_CALL_STATE_RETENTION_SECONDS,
                    "last_status_code": self._last_status_code,
                }
            )
            _TRUNK_CALL_STATES[self._call_id] = retained

        log.info(
            "Released SIP trunk concurrency slot account=%s call_id=%s active_calls=%s",
            self._account_id,
            self._call_id,
            _TRUNK_ACTIVE_CALLS.get(self._account_id, 0),
        )
        self._done.set()

    def set_playback_context(
        self,
        *,
        audio_path: str,
        audio_duration_seconds: float,
        repeat_count: int,
        pause_ms: int,
        cleanup_wav: bool = False,
        enable_recording: bool = False,
        destination_number: str = "",
    ) -> None:
        self._playback_audio_path = str(audio_path or "")
        self._playback_audio_duration_seconds = max(0.0, float(audio_duration_seconds or 0.0))
        self._playback_repeat_count = max(1, int(repeat_count or 1))
        self._playback_pause_ms = max(0, int(pause_ms or 0))
        self._playback_cleanup_wav = bool(cleanup_wav)
        self._recording_enabled = bool(enable_recording)
        self._destination_number = str(destination_number or "")
        self._recording_path = ""
        self._recording_error = ""
        self._playback_hangup_requested = False
        self._hangup_requested_at = None
        self._scheduled_hangup_at = None
        self._playback_bridge_released = False

    def attach_call(self, call_obj: Any) -> None:
        if call_obj is not None:
            self._call_obj = call_obj

    def _try_start_playback(self, call_obj: Any) -> bool:
        log.info(
            "Outbound SIP _try_start_playback called account=%s call_id=%s playback_started=%s audio_path=%s audio_duration=%.3f",
            self._account_id,
            self._call_id,
            self._playback_started,
            self._playback_audio_path[:100] if self._playback_audio_path else "",
            self._playback_audio_duration_seconds,
        )
        if self._playback_started:
            log.info("Outbound SIP playback already started, skipping account=%s call_id=%s", self._account_id, self._call_id)
            return True
        if not self._playback_audio_path:
            self._playback_error = "Playback audio path is empty"
            log.warning(
                "Outbound SIP playback skipped account=%s call_id=%s reason=%s",
                self._account_id,
                self._call_id,
                self._playback_error,
            )
            return False

        wav_path = Path(self._playback_audio_path)
        if not wav_path.exists():
            self._playback_error = "Playback audio file does not exist"
            log.warning(
                "Outbound SIP playback skipped account=%s call_id=%s path=%s reason=%s",
                self._account_id,
                self._call_id,
                self._playback_audio_path,
                self._playback_error,
            )
            return False

        # Get or create per-call audio setup lock to prevent race conditions
        with _PJSUA_PLAYER_LOCK:
            setup_lock = _PJSUA_AUDIO_SETUP_LOCKS.get(self._call_id)
            if setup_lock is None:
                setup_lock = threading.RLock()
                _PJSUA_AUDIO_SETUP_LOCKS[self._call_id] = setup_lock

        try:
            # Keep setup scoped to the call so unrelated calls can progress
            # concurrently. Shared player registries remain protected separately.
            with setup_lock:
                # Double-check playback_started after acquiring lock
                if self._playback_started:
                    log.info("Outbound SIP playback already started after lock acquire account=%s call_id=%s", self._account_id, self._call_id)
                    return True

                self._session._register_current_thread()
                pj = self._session._pj
                if pj is None:
                    self._playback_error = "PJSUA2 bindings are unavailable during playback"
                    return False

                call_audio_media = None
                with suppress(Exception):
                    call_info = call_obj.getInfo()
                    media_list = getattr(call_info, "media", None)
                    if media_list is not None:
                        for idx, media in enumerate(list(media_list)):
                            media_type = getattr(media, "type", None)
                            media_status = getattr(media, "status", None)
                            log.info(
                                "Outbound SIP media candidate account=%s call_id=%s index=%s type=%s status=%s",
                                self._account_id,
                                self._call_id,
                                idx,
                                media_type,
                                media_status,
                            )
                            with suppress(Exception):
                                call_audio_media = call_obj.getAudioMedia(idx)
                                if call_audio_media is not None:
                                    break

                if call_audio_media is None:
                    with suppress(Exception):
                        call_audio_media = call_obj.getAudioMedia(-1)

                if call_audio_media is None:
                    self._playback_error = "Call audio media is unavailable"
                    log.warning(
                        "Outbound SIP playback failed account=%s call_id=%s reason=%s",
                        self._account_id,
                        self._call_id,
                        self._playback_error,
                    )
                    return False

                log.info(
                    "Outbound SIP creating audio player account=%s call_id=%s wav_path=%s wav_exists=%s wav_size=%s",
                    self._account_id,
                    self._call_id,
                    str(wav_path),
                    wav_path.exists(),
                    wav_path.stat().st_size if wav_path.exists() else 0,
                )
                player = pj.AudioMediaPlayer()
                player_create_options = int(getattr(pj, "PJMEDIA_FILE_NO_LOOP", 1) or 1)
                try:
                    player.createPlayer(str(wav_path), player_create_options)
                except TypeError:
                    self._playback_error = "createPlayer() rejected PJMEDIA_FILE_NO_LOOP; refusing unsafe looping fallback"
                    log.error(
                        "Outbound SIP player.createPlayer() rejected no-loop option account=%s call_id=%s error=%s",
                        self._account_id,
                        self._call_id,
                        self._playback_error,
                    )
                    with suppress(Exception):
                        player = None
                    return False

                recorder = None
                recording_path = ""
                if self._recording_enabled:
                    try:
                        recording_output_path = _build_recording_output_path(
                            self._session.settings,
                            account_id=self._account_id,
                            destination_number=self._destination_number,
                            call_id=self._call_id,
                        )
                        recorder = pj.AudioMediaRecorder()
                        recorder.createRecorder(str(recording_output_path))
                        recording_path = str(recording_output_path)
                        self._recording_path = recording_path
                        self._recording_error = ""
                        log.info(
                            "Outbound SIP recording prepared account=%s call_id=%s path=%s",
                            self._account_id,
                            self._call_id,
                            recording_path,
                        )
                    except Exception as exc:
                        recorder = None
                        self._recording_path = ""
                        self._recording_error = f"{type(exc).__name__}: {exc}"
                        log.warning(
                            "Outbound SIP recording setup failed account=%s call_id=%s error=%s",
                            self._account_id,
                            self._call_id,
                            self._recording_error,
                        )

                log.info(
                    "Outbound SIP player created account=%s call_id=%s player=%s call_audio_media=%s",
                    self._account_id,
                    self._call_id,
                    str(player),
                    str(call_audio_media),
                )

                log.info(
                    "Outbound SIP starting audio transmission account=%s call_id=%s wav_path=%s audio_duration=%.3f",
                    self._account_id,
                    self._call_id,
                    str(wav_path),
                    self._playback_audio_duration_seconds,
                )

                try:
                    player.startTransmit(call_audio_media)
                    if recorder is not None:
                        with suppress(Exception):
                            player.startTransmit(recorder)
                        with suppress(Exception):
                            call_audio_media.startTransmit(recorder)
                    log.info(
                        "Outbound SIP startTransmit called successfully account=%s call_id=%s player=%s call_audio_media=%s recorder=%s",
                        self._account_id,
                        self._call_id,
                        str(player),
                        str(call_audio_media),
                        str(recorder),
                    )
                except Exception as exc:
                    self._playback_error = f"startTransmit failed: {type(exc).__name__}: {exc}"
                    log.error(
                        "Outbound SIP startTransmit failed account=%s call_id=%s error=%s",
                        self._account_id,
                        self._call_id,
                        self._playback_error,
                    )
                    with suppress(Exception):
                        player = None
                    with suppress(Exception):
                        recorder = None
                    return False

                with _PJSUA_PLAYER_LOCK:
                    _PJSUA_ACTIVE_PLAYERS[self._call_id] = {
                        "player": player,
                        "player_media": player,
                        "call_audio_media": call_audio_media,
                        "recorder": recorder,
                        "recording_path": recording_path,
                        "cleanup_wav": self._playback_cleanup_wav,
                        "wav_path": str(wav_path),
                    }

                self._player = player
                self._player_media = player
                self._recorder = recorder
                self._call_obj = call_obj
                self._playback_started = True
                self._playback_started_at = time.time()
                self._playback_finished_at = None
                self._playback_pending = False
                self._scheduled_hangup_at = None
                self._playback_bridge_released = False

                log.info(
                    "Outbound SIP playback started account=%s call_id=%s path=%s duration=%.3f repeats=%s pause_ms=%s",
                    self._account_id,
                    self._call_id,
                    str(wav_path),
                    self._playback_audio_duration_seconds,
                    self._playback_repeat_count,
                    self._playback_pause_ms,
                )
                return True
        except Exception as exc:
            self._playback_error = f"{type(exc).__name__}: {exc}"
            log.exception(
                "Outbound SIP playback start failed account=%s call_id=%s path=%s",
                self._account_id,
                self._call_id,
                self._playback_audio_path,
            )
            return False

    def onCallState(self, *args: Any, **kwargs: Any) -> None:
        self._session._register_current_thread()
        call_obj = args[0] if args else None
        if call_obj is not None:
            self._call_obj = call_obj
        info_obj = None

        with suppress(Exception):
            if call_obj is not None and hasattr(call_obj, "getInfo"):
                info_obj = call_obj.getInfo()

        state_name = ""
        state_text = ""
        state_value: Any = None
        last_status_code = 0
        remote_uri = ""

        log.info(
            "Outbound SIP onCallState called account=%s call_id=%s info_obj=%s call_obj=%s",
            self._account_id,
            self._call_id,
            info_obj is not None,
            call_obj is not None,
        )

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
            self._last_status_code = last_status_code

            for attr in ("remoteUri", "remote_uri"):
                with suppress(Exception):
                    value = getattr(info_obj, attr)
                    if value is not None:
                        remote_uri = str(value)
                        break

            for attr in ("state", "callState", "call_state"):
                with suppress(Exception):
                    value = getattr(info_obj, attr)
                    if value is not None:
                        state_value = value
                        state_name = str(value)
                        break

        self._state_text = state_text or state_name or self._state_text
        normalized_state = (state_text or state_name or "").strip()
        if self._answered_at is not None and normalized_state.upper() == "CONFIRMED":
            normalized_state = "ACTIVE"
        elif normalized_state.upper() == "EARLY":
            normalized_state = "RINGING"
        if self._answered_at is not None and normalized_state.upper() in {"DIALING", "CALLING"}:
            normalized_state = "ACTIVE"
        destination_value = _extract_display_destination(remote_uri) if remote_uri else ""
        self._set_runtime_state(
            state=normalized_state or self._state_text,
            last_status_code=last_status_code,
            destination_number=destination_value,
        )
        log.info(
            "Outbound SIP call state account=%s call_id=%s state=%s status=%s",
            self._account_id,
            self._call_id,
            state_text or state_name,
            last_status_code,
        )

        answered_state = (
            last_status_code == 200
            or normalized_state.upper() in {"CONFIRMED", "ACTIVE", "CONNECTED"}
            or state_text.upper() == "CONFIRMED"
            or state_name.upper() == "CONFIRMED"
        )
        if answered_state and self._answered_at is None:
            self._answered_at = time.time()
            self._set_runtime_state(
                state="ANSWERED",
                last_status_code=last_status_code,
                destination_number=_extract_display_destination(remote_uri) if remote_uri else "",
            )
            log.info(
                "Outbound SIP call marked answered account=%s call_id=%s state=%s status=%s playback_started=%s audio_path=%s",
                self._account_id,
                self._call_id,
                state_text or state_name,
                last_status_code,
                self._playback_started,
                self._playback_audio_path[:80] if self._playback_audio_path else "",
            )
            if self._playback_audio_path and not self._playback_started:
                self._playback_pending = True
                self._playback_ready = True
                log.info(
                    "Outbound SIP set playback_pending=True account=%s call_id=%s",
                    self._account_id,
                    self._call_id,
                )
            elif self._playback_audio_path and self._playback_started and self._playback_error:
                log.warning(
                    "Outbound SIP playback started but has error account=%s call_id=%s error=%s",
                    self._account_id,
                    self._call_id,
                    self._playback_error,
                )

        if (
            state_text.upper() == "CONFIRMED"
            or state_name.upper() == "CONFIRMED"
        ):
            if self._answered_at is None:
                self._answered_at = time.time()
                self._set_runtime_state(
                    state="ACTIVE",
                    last_status_code=last_status_code,
                    destination_number=_extract_display_destination(remote_uri) if remote_uri else "",
                )
            log.info(
                "Outbound SIP call confirmed account=%s call_id=%s state=%s status=%s",
                self._account_id,
                self._call_id,
                state_text or state_name,
                last_status_code,
            )
            if call_obj is not None and not self._playback_started:
                self._playback_pending = True
                self._playback_ready = True

        log.info(
            "Outbound SIP checking terminal state account=%s call_id=%s state_name=%s state_text=%s last_status_code=%s answered_at=%s",
            self._account_id,
            self._call_id,
            state_name,
            state_text,
            last_status_code,
            self._answered_at,
        )
        normalized_state_name = state_name.strip().upper()
        normalized_state_text = state_text.strip().upper()
        terminal_state_detected = (
            "DISCONNECTED" in normalized_state_name
            or "DISCONNECTED" in normalized_state_text
        )
        if not terminal_state_detected:
            with suppress(Exception):
                terminal_state_detected = int(state_value) == 6
        if not terminal_state_detected and last_status_code >= 300 and self._answered_at is None:
            terminal_state_detected = True

        if terminal_state_detected:
            log.warning(
                "Outbound SIP call disconnecting account=%s call_id=%s state_name=%s state_text=%s last_status_code=%s answered_at=%s reason=%s",
                self._account_id,
                self._call_id,
                state_name,
                state_text,
                last_status_code,
                self._answered_at,
                state_text or state_name or self._disconnect_reason,
            )
            self._disconnected_at = time.time()
            self._disconnect_reason = state_text or state_name or self._disconnect_reason
            self._release_slot()

    def onCallMediaState(self, *args: Any, **kwargs: Any) -> None:
        self._session._register_current_thread()
        call_obj = args[0] if args else None
        if call_obj is not None:
            self._call_obj = call_obj
        if call_obj is None:
            return

        answered = self._answered_at is not None
        log.info(
            "Outbound SIP media state changed account=%s call_id=%s answered=%s playback_started=%s audio_path=%s",
            self._account_id,
            self._call_id,
            answered,
            self._playback_started,
            self._playback_audio_path[:80] if self._playback_audio_path else "",
        )

        if not answered:
            return

        if self._playback_audio_path and not self._playback_started:
            self._playback_ready = True
            self._playback_pending = True
            log.info(
                "Outbound SIP onCallMediaState marked playback pending account=%s call_id=%s answered=%s",
                self._account_id,
                self._call_id,
                answered,
            )
            started = self._try_start_playback(call_obj)
            if started:
                self._playback_pending = False
                log.info(
                    "Outbound SIP playback started directly from media-state callback account=%s call_id=%s",
                    self._account_id,
                    self._call_id,
                )

    def _hangup_call(self, *, reason: str, force: bool = False) -> None:
        if self._done.is_set():
            return
        if self._playback_hangup_requested and not force:
            return

        call_obj = self._call_obj
        if call_obj is None:
            self._playback_error = "hangup failed: call object is unavailable"
            log.warning(
                "Outbound SIP hangup request skipped account=%s call_id=%s reason=%s error=%s",
                self._account_id,
                self._call_id,
                reason,
                self._playback_error,
            )
            return

        try:
            pj = self._session._pj
            self._session._register_current_thread()
            if pj is not None:
                self._playback_hangup_requested = True
                self._hangup_requested_at = time.time()
                hangup_param = pj.CallOpParam()
                with suppress(Exception):
                    hangup_param.statusCode = 200
                call_obj.hangup(hangup_param)
                log.info(
                    "Outbound SIP hangup requested account=%s call_id=%s reason=%s force=%s",
                    self._account_id,
                    self._call_id,
                    reason,
                    force,
                )
        except Exception as exc:
            self._playback_error = f"hangup failed: {type(exc).__name__}: {exc}"
            log.warning(
                "Outbound SIP hangup request failed account=%s call_id=%s reason=%s force=%s error=%s",
                self._account_id,
                self._call_id,
                reason,
                force,
                self._playback_error,
            )

    def wait_for_completion(self, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.time() + max(1.0, timeout_seconds)
        media_check_counter = 0
        last_state_dump_at = 0.0
        loop_start = time.time()
        while time.time() < deadline:
            self._session._pump_events(50)

            media_check_counter += 1

            # Periodic full-state debug dump (every ~2s) so the journal shows
            # exactly which playback flags are set/unset while the call is in
            # progress. Existing logs only fire on transitions; if a flag is
            # stuck wrong, there's no other record. Safe to leave on — it's
            # one INFO line every 2s per active call.
            now_ts = time.time()
            if now_ts - last_state_dump_at >= 2.0:
                last_state_dump_at = now_ts
                wav_size = -1
                if self._playback_audio_path:
                    try:
                        wav_size = Path(self._playback_audio_path).stat().st_size
                    except Exception:
                        wav_size = -1
                log.info(
                    "Outbound SIP wait state account=%s call_id=%s elapsed=%.1fs "
                    "answered_at=%s state_text=%s last_status=%s "
                    "playback_ready=%s playback_pending=%s playback_started=%s "
                    "playback_started_at=%s playback_finished_at=%s "
                    "playback_error=%s audio_path=%s wav_size=%s "
                    "duration=%.3f hangup_requested=%s done=%s",
                    self._account_id,
                    self._call_id,
                    now_ts - loop_start,
                    self._answered_at,
                    self._state_text,
                    self._last_status_code,
                    self._playback_ready,
                    self._playback_pending,
                    self._playback_started,
                    self._playback_started_at,
                    self._playback_finished_at,
                    (self._playback_error or "")[:120],
                    (self._playback_audio_path or "")[:80],
                    wav_size,
                    self._playback_audio_duration_seconds,
                    self._playback_hangup_requested,
                    self._done.is_set(),
                )

            if self._playback_ready and self._playback_pending and not self._playback_started and self._answered_at is not None:
                call_obj = self._call_obj
                if call_obj is not None:
                    log.info(
                        "Outbound SIP polling for media readiness (check %s) account=%s call_id=%s",
                        media_check_counter,
                        self._account_id,
                        self._call_id,
                    )
                    started = self._try_start_playback(call_obj)
                    self._playback_pending = bool(not started and self._playback_audio_path)
                    if started:
                        log.info(
                            "Outbound SIP playback started via polling account=%s call_id=%s",
                            self._account_id,
                            self._call_id,
                        )

            if self._playback_started and self._playback_started_at is not None:
                expected_end = self._playback_started_at + self._playback_audio_duration_seconds
                if (
                    self._playback_audio_duration_seconds > 0
                    and self._playback_finished_at is None
                    and time.time() >= expected_end
                ):
                    self._playback_finished_at = expected_end
                    self._scheduled_hangup_at = expected_end
                    self._release_playback_bridge(stop_transmit=True, reason="playback_complete")
                    log.info(
                        "Outbound SIP playback finished account=%s call_id=%s duration=%.3f",
                        self._account_id,
                        self._call_id,
                        self._playback_audio_duration_seconds,
                    )
                    self._hangup_call(reason="playback_complete")

            if (
                self._playback_hangup_requested
                and not self._done.is_set()
                and self._hangup_requested_at is not None
                and time.time() >= (self._hangup_requested_at + 0.75)
            ):
                log.warning(
                    "Outbound SIP hangup still pending, retrying account=%s call_id=%s state=%s last_status=%s",
                    self._account_id,
                    self._call_id,
                    self._state_text,
                    self._last_status_code,
                )
                self._hangup_call(reason="playback_complete_retry", force=True)

            playback_start_grace_seconds = max(
                15.0,
                min(30.0, self._playback_audio_duration_seconds + 8.0),
            )
            if (
                self._answered_at is not None
                and not self._playback_started
                and not self._done.is_set()
                and time.time() >= (self._answered_at + playback_start_grace_seconds)
            ):
                log.warning(
                    "Outbound SIP playback never started after %.1fs, hanging up account=%s call_id=%s error=%s",
                    playback_start_grace_seconds,
                    self._account_id,
                    self._call_id,
                    self._playback_error or "timeout",
                )
                self._hangup_call(reason="playback_start_timeout")

            if (
                self._answered_at is not None
                and not self._playback_hangup_requested
                and not self._done.is_set()
                and time.time() >= max(self._answered_at, deadline - 1.0)
            ):
                self._hangup_call(reason=f"deadline_reached:{deadline}")

            if self._done.wait(0.1):
                break

        if not self._done.is_set():
            log.warning(
                "Outbound SIP wait timed out account=%s call_id=%s answered=%s playback_started=%s state=%s",
                self._account_id,
                self._call_id,
                self._answered_at is not None,
                self._playback_started,
                self._state_text,
            )
            self._disconnect_reason = self._disconnect_reason or "timeout"
            self._hangup_call(reason="wait_timeout", force=True)
            if not self._done.wait(1.0) and not self._released:
                self._disconnected_at = self._disconnected_at or time.time()
                self._release_slot()

        answered = self._answered_at is not None
        if answered:
            end_time = self._disconnected_at or time.time()
            call_duration_seconds = max(0.0, end_time - float(self._answered_at or end_time))
            playback_seconds = call_duration_seconds
        else:
            call_duration_seconds = 0.0
            playback_seconds = 0.0

        return {
            "answered": answered,
            "playback_seconds": playback_seconds,
            "call_duration_seconds": call_duration_seconds,
            "recording_path": self._recording_path,
            "recording_enabled": self._recording_enabled,
            "recording_error": self._recording_error,
            "last_status_code": self._last_status_code,
            "state_text": self._state_text,
            "disconnect_reason": self._disconnect_reason,
            "completed": self._done.is_set(),
            "answered_at": self._answered_at,
            "disconnected_at": self._disconnected_at,
            "playback_started": self._playback_started,
            "playback_pending": self._playback_pending,
            "playback_error": self._playback_error,
            "playback_started_at": self._playback_started_at,
            "playback_finished_at": self._playback_finished_at,
        }

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None
        return _noop


def build_pjsua2_service(
    settings: Settings,
    *,
    scope: str = "default",
    isolated: bool = False,
) -> PJSipUASession:
    if isolated:
        return PJSipUASession(settings, isolated=True)

    scope_key = str(scope or "default").strip() or "default"
    with _PJSUA_GLOBAL_LOCK:
        session = _PJSUA_GLOBAL_SESSIONS.get(scope_key)
        if session is None:
            session = PJSipUASession(settings)
            _PJSUA_GLOBAL_SESSIONS[scope_key] = session
        else:
            session.settings = settings
        return session
