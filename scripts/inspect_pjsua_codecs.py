from app.config_store import load_settings_from_store
from app.main import _build_sip_profile_from_account
from app.pjsua2_service import build_pjsua2_service


def main() -> None:
    settings = load_settings_from_store()
    account = next((item for item in settings.sip_accounts if item.id == "default-sip"), None)
    if account is None and settings.sip_accounts:
        account = settings.sip_accounts[0]

    print("ACCOUNT", account.id if account else None, "PREFERRED", list(getattr(account, "preferred_codecs", []) or []) if account else [])

    profile = _build_sip_profile_from_account(account) if account else None
    if profile is not None:
        print("PROFILE_PREF", list(profile.preferred_codecs or []), "EXTRA_PREF", list(profile.extra.get("preferred_codecs", []) or []))

    service = build_pjsua2_service(settings)
    result = service.initialize()
    print("INIT", result.success, result.error)

    endpoint = getattr(service, "_endpoint", None)
    codec_ids: list[str] = []
    if endpoint is not None:
        for info in list(endpoint.codecEnum2()):
            codec_id = ""
            for attr in ("codecId", "codec_id", "codecName", "codec_name"):
                if hasattr(info, attr):
                    value = getattr(info, attr)
                    if value is not None:
                        codec_id = str(value).strip()
                        if codec_id:
                            break
            if codec_id:
                codec_ids.append(codec_id)

    print("CODECS", codec_ids)
    service.close()


if __name__ == "__main__":
    main()
