from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import SIPAccount, SMPPAccount, Settings, SystemUser

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_STORE_PATH = DATA_DIR / "config.json"

BOOTSTRAP_ONLY_FIELDS = {
    "host",
    "port",
    "debug",
    "admin_username",
    "admin_password",
}

ADMIN_MANAGED_FIELDS = set(Settings.model_fields) - BOOTSTRAP_ONLY_FIELDS

_DEFAULT_SIP_ACCOUNT_ID = "default-sip"
_DEFAULT_SMPP_ACCOUNT_ID = "default-smpp"
_DEFAULT_SYSTEM_USER_ID = "portal-admin"
_DEFAULT_SYSTEM_USER_PERMISSIONS = [
    "Overview — Read",
    "Health — Read",
    "Health — Write",
    "Configuration — Read",
    "Configuration — Write",
    "Delivery Reports — Read",
    "Delivery Reports — Write",
    "System Users — Write",
]


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_persistent_config(path: Path | str = CONFIG_STORE_PATH) -> dict[str, Any]:
    store_path = Path(path)
    if not store_path.exists():
        return {}

    try:
        with store_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        log.warning("Config store JSON is invalid at %s: %s", store_path, exc)
        return {}
    except OSError as exc:
        log.warning("Unable to read config store at %s: %s", store_path, exc)
        return {}

    if not isinstance(data, dict):
        log.warning("Config store at %s did not contain a JSON object", store_path)
        return {}

    return data


def save_persistent_config(
    values: dict[str, Any],
    path: Path | str = CONFIG_STORE_PATH,
) -> Path:
    store_path = Path(path)
    _ensure_parent_dir(store_path)

    with store_path.open("w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

    return store_path


def _coerce_sip_account(raw: Any) -> SIPAccount | None:
    if isinstance(raw, SIPAccount):
        return raw
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    if not data.get("id"):
        data["id"] = _DEFAULT_SIP_ACCOUNT_ID
    if "label" not in data:
        data["label"] = data.get("id", "")
    return SIPAccount(**data)


def _coerce_smpp_account(raw: Any) -> SMPPAccount | None:
    if isinstance(raw, SMPPAccount):
        return raw
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    if not data.get("id"):
        data["id"] = _DEFAULT_SMPP_ACCOUNT_ID
    if "label" not in data:
        data["label"] = data.get("id", "")
    if not str(data.get("username", "")).strip():
        log.warning("Skipping SMPP account with empty username in config store: %r", data)
        return None
    try:
        return SMPPAccount(**data)
    except ValidationError as exc:
        log.warning("Skipping invalid SMPP account in config store: %s", exc)
        return None


def _coerce_system_user(raw: Any) -> SystemUser | None:
    if isinstance(raw, SystemUser):
        return raw
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    if not data.get("id"):
        data["id"] = _DEFAULT_SYSTEM_USER_ID
    if not data.get("username"):
        data["username"] = "admin"
    permissions = data.get("permissions")
    if not isinstance(permissions, list):
        data["permissions"] = list(_DEFAULT_SYSTEM_USER_PERMISSIONS)
    return SystemUser(**data)


def _normalize_account_lists(
    values: dict[str, Any],
) -> tuple[list[SIPAccount], list[SMPPAccount], dict[str, str], list[SystemUser]]:
    sip_accounts_raw = values.get("sip_accounts")
    smpp_accounts_raw = values.get("smpp_accounts")
    assignments_raw = values.get("smpp_sip_assignments")
    system_users_raw = values.get("system_users")

    sip_accounts: list[SIPAccount] = []
    smpp_accounts: list[SMPPAccount] = []
    smpp_sip_assignments: dict[str, str] = {}
    system_users: list[SystemUser] = []

    if isinstance(sip_accounts_raw, list):
        for item in sip_accounts_raw:
            account = _coerce_sip_account(item)
            if account is not None:
                sip_accounts.append(account)

    if isinstance(smpp_accounts_raw, list):
        for item in smpp_accounts_raw:
            account = _coerce_smpp_account(item)
            if account is not None:
                smpp_accounts.append(account)

    if isinstance(assignments_raw, dict):
        for smpp_username, sip_account_id in assignments_raw.items():
            if isinstance(smpp_username, str) and isinstance(sip_account_id, str):
                smpp_sip_assignments[smpp_username] = sip_account_id

    if isinstance(system_users_raw, list):
        for item in system_users_raw:
            user = _coerce_system_user(item)
            if user is not None:
                system_users.append(user)

    if not sip_accounts and not smpp_accounts and not smpp_sip_assignments:
        legacy_smpp_username = str(values.get("smpp_username") or "").strip()
        legacy_smpp_password = str(values.get("smpp_password") or "").strip()
        legacy_sip_channel_prefix = str(values.get("sip_channel_prefix") or "").strip()
        legacy_outbound_caller_id = str(values.get("outbound_caller_id") or "").strip()

        if legacy_sip_channel_prefix or legacy_outbound_caller_id:
            sip_accounts.append(
                SIPAccount(
                    id=_DEFAULT_SIP_ACCOUNT_ID,
                    label="Default SIP",
                    host="",
                    username="",
                    password="",
                    transport="udp",
                    port=5060,
                    domain="",
                    display_name="",
                    from_user="",
                    from_domain="",
                    enabled=True,
                    default_for_outbound=True,
                    register=True,
                    outbound_proxy="",
                    extra={
                        "legacy_channel_prefix": legacy_sip_channel_prefix,
                        "legacy_outbound_caller_id": legacy_outbound_caller_id,
                    },
                )
            )

        if legacy_smpp_username or legacy_smpp_password:
            smpp_account_username = legacy_smpp_username or "smpp"
            smpp_accounts.append(
                SMPPAccount(
                    id=_DEFAULT_SMPP_ACCOUNT_ID,
                    label="Default SMPP",
                    username=smpp_account_username,
                    password=legacy_smpp_password,
                    enabled=True,
                    default_for_inbound=True,
                    default_sip_account_id=_DEFAULT_SIP_ACCOUNT_ID if sip_accounts else "",
                    extra={},
                )
            )
            if sip_accounts:
                smpp_sip_assignments[smpp_account_username] = _DEFAULT_SIP_ACCOUNT_ID

    if sip_accounts and not any(account.default_for_outbound for account in sip_accounts):
        sip_accounts[0] = sip_accounts[0].model_copy(update={"default_for_outbound": True})

    if smpp_accounts and not any(account.default_for_inbound for account in smpp_accounts):
        smpp_accounts[0] = smpp_accounts[0].model_copy(update={"default_for_inbound": True})

    if not system_users:
        system_users.append(
            SystemUser(
                id=_DEFAULT_SYSTEM_USER_ID,
                username="admin",
                password="",
                role="Administrator",
                enabled=True,
                auth_source="Bootstrap / Environment",
                permissions=list(_DEFAULT_SYSTEM_USER_PERMISSIONS),
            )
        )

    return sip_accounts, smpp_accounts, smpp_sip_assignments, system_users


def build_settings_data(settings: Settings) -> dict[str, Any]:
    data = settings.model_dump()
    for field in BOOTSTRAP_ONLY_FIELDS:
        data.pop(field, None)

    sip_accounts = data.pop("sip_accounts", [])
    smpp_accounts = data.pop("smpp_accounts", [])
    smpp_sip_assignments = data.pop("smpp_sip_assignments", {})
    system_users = data.pop("system_users", [])

    data["sip_accounts"] = [account.model_dump(by_alias=True) if isinstance(account, SIPAccount) else account for account in sip_accounts]
    data["smpp_accounts"] = [account.model_dump() if isinstance(account, SMPPAccount) else account for account in smpp_accounts]
    data["smpp_sip_assignments"] = dict(smpp_sip_assignments)
    data["system_users"] = [user.model_dump() if isinstance(user, SystemUser) else user for user in system_users]
    return data


def _sanitize_settings_payload(values: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(values)

    def _coerce_min_int(field_name: str, minimum: int) -> None:
        raw_value = sanitized.get(field_name)
        if raw_value in (None, ""):
            return
        try:
            numeric_value = int(raw_value)
        except Exception:
            log.warning("Ignoring invalid persisted setting %s=%r", field_name, raw_value)
            sanitized.pop(field_name, None)
            return
        if numeric_value < minimum:
            log.warning(
                "Clamping invalid persisted setting %s=%r to minimum %s",
                field_name,
                raw_value,
                minimum,
            )
            sanitized[field_name] = minimum
        else:
            sanitized[field_name] = numeric_value

    _coerce_min_int("playback_repeats", 1)
    _coerce_min_int("playback_pause_ms", 0)
    _coerce_min_int("delivery_retry_count", 0)
    _coerce_min_int("delivery_retry_interval_seconds", 0)
    _coerce_min_int("delivery_report_max_items", 1)
    _coerce_min_int("call_answer_timeout", 1)
    _coerce_min_int("audio_cache_ttl", 1)

    return sanitized


def load_settings_from_store(path: Path | str = CONFIG_STORE_PATH) -> Settings:
    env_settings = Settings()
    data = load_persistent_config(path)
    if not data:
        return env_settings

    merged = env_settings.model_dump()
    for key, value in data.items():
        if key in BOOTSTRAP_ONLY_FIELDS:
            continue
        merged[key] = value

    sip_accounts, smpp_accounts, smpp_sip_assignments, system_users = _normalize_account_lists(merged)
    merged["sip_accounts"] = sip_accounts
    merged["smpp_accounts"] = smpp_accounts
    merged["smpp_sip_assignments"] = smpp_sip_assignments
    merged["system_users"] = system_users
    merged = _sanitize_settings_payload(merged)

    return Settings(**merged)


def save_settings_to_store(settings: Settings, path: Path | str = CONFIG_STORE_PATH) -> Path:
    return save_persistent_config(build_settings_data(settings), path)


def _get_enabled_sip_account_by_id(settings: Settings, sip_account_id: str) -> SIPAccount | None:
    target_id = (sip_account_id or "").strip()
    if not target_id:
        return None

    for account in settings.sip_accounts:
        if account.enabled and account.id == target_id:
            return account

    return None


def _get_enabled_smpp_account_by_username(settings: Settings, smpp_username: str) -> SMPPAccount | None:
    username = (smpp_username or "").strip()
    if not username:
        return None

    for account in settings.smpp_accounts:
        if account.enabled and account.username == username:
            return account

    return None


def get_sip_account_for_smpp_username(settings: Settings, smpp_username: str) -> SIPAccount | None:
    username = (smpp_username or "").strip()
    if not username:
        return get_default_sip_account(settings)

    assigned_sip_id = (settings.smpp_sip_assignments or {}).get(username, "").strip()
    if assigned_sip_id:
        assigned_account = _get_enabled_sip_account_by_id(settings, assigned_sip_id)
        if assigned_account is not None:
            return assigned_account

    smpp_account = _get_enabled_smpp_account_by_username(settings, username)
    if smpp_account is not None:
        configured_sip_account = _get_enabled_sip_account_by_id(settings, smpp_account.default_sip_account_id)
        if configured_sip_account is not None:
            return configured_sip_account

    for account in settings.sip_accounts:
        if account.default_for_outbound and account.enabled:
            return account

    return get_default_sip_account(settings)


def get_default_sip_account(settings: Settings) -> SIPAccount | None:
    for account in settings.sip_accounts:
        if account.enabled and account.default_for_outbound:
            return account
    for account in settings.sip_accounts:
        if account.enabled:
            return account
    return None


def get_default_smpp_account(settings: Settings) -> SMPPAccount | None:
    for account in settings.smpp_accounts:
        if account.enabled and account.default_for_inbound:
            return account
    for account in settings.smpp_accounts:
        if account.enabled:
            return account
    return None


def ensure_default_accounts(settings: Settings) -> Settings:
    sip_accounts = list(settings.sip_accounts)
    smpp_accounts = list(settings.smpp_accounts)
    assignments = dict(settings.smpp_sip_assignments)
    system_users = list(settings.system_users)

    if not sip_accounts:
        sip_accounts.append(
            SIPAccount(
                id=_DEFAULT_SIP_ACCOUNT_ID,
                label="Default SIP",
                enabled=True,
                default_for_outbound=True,
            )
        )

    if not smpp_accounts:
        smpp_accounts.append(
            SMPPAccount(
                id=_DEFAULT_SMPP_ACCOUNT_ID,
                label="Default SMPP",
                username=settings.smpp_username or "smpp",
                password=settings.smpp_password or "smpp_secret",
                enabled=True,
                default_for_inbound=True,
                default_sip_account_id=sip_accounts[0].id,
            )
        )

    if not any(account.default_for_outbound for account in sip_accounts):
        sip_accounts[0] = sip_accounts[0].model_copy(update={"default_for_outbound": True})

    if not any(account.default_for_inbound for account in smpp_accounts):
        smpp_accounts[0] = smpp_accounts[0].model_copy(update={"default_for_inbound": True})

    if smpp_accounts and sip_accounts:
        for account in smpp_accounts:
            if account.username and account.username not in assignments:
                assignments[account.username] = account.default_sip_account_id or sip_accounts[0].id

    if not system_users:
        system_users.append(
            SystemUser(
                id=_DEFAULT_SYSTEM_USER_ID,
                username=settings.admin_username or "admin",
                password=settings.admin_password or "",
                role="Administrator",
                enabled=True,
                auth_source="Bootstrap / Environment",
                permissions=list(_DEFAULT_SYSTEM_USER_PERMISSIONS),
            )
        )

    return settings.model_copy(
        update={
            "sip_accounts": sip_accounts,
            "smpp_accounts": smpp_accounts,
            "smpp_sip_assignments": assignments,
            "system_users": system_users,
        }
    )
