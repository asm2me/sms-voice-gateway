from types import SimpleNamespace
import app.pjsua2_service as svc

calls = []

class FakeEndpoint:
    def libRegisterThread(self, *args):
        calls.append(args)

class FakePJ:
    def __init__(self):
        self.Endpoint = lambda: FakeEndpoint()
        self.EpConfig = lambda: SimpleNamespace(logConfig=SimpleNamespace(level=0, consoleLevel=0), medConfig=SimpleNamespace(noVad=False, hasIoqueue=True, clockRate=0, sndClockRate=0), uaConfig=SimpleNamespace(threadCnt=0, mainThreadOnly=False))
        self.TransportConfig = lambda: SimpleNamespace(port=0)
        self.PJSIP_TRANSPORT_UDP = 0
        self.PJSIP_TRANSPORT_TCP = 1
        self.PJSIP_TRANSPORT_TLS = 2

orig_safe_import = svc._safe_import_pjsua2
orig_build = svc.PJSipUASession.initialize

try:
    def fake_safe_import():
        return FakePJ(), ""
    svc._safe_import_pjsua2 = fake_safe_import

    settings = SimpleNamespace(sip_port=5060, sip_listen_port=5060, use_null_sound_device=False)

    s1 = svc.PJSipUASession(settings, isolated=True)
    s1._endpoint = FakeEndpoint()
    s1._pj = FakePJ()
    s1._register_current_thread()

    s2 = svc.PJSipUASession(settings, isolated=True)
    s2._endpoint = FakeEndpoint()
    s2._pj = FakePJ()
    s2._register_current_thread()

    print("register_calls", len(calls))
    print("calls", calls)
finally:
    svc._safe_import_pjsua2 = orig_safe_import
