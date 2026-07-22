"""mDNS announcer lifecycle (PRD F15).

A stale record for our own service name (lost goodbye packet, mDNS
reflector echo) must never abort a receive with NonUniqueNameException -
registration opts into zeroconf's automatic name renaming instead.
"""
from lanmigrate import discovery


class _FakeZeroconf:
    def __init__(self):
        self.register_kwargs = None
        self.unregistered = False
        self.closed = False

    def register_service(self, info, **kwargs):
        self.register_kwargs = kwargs

    def unregister_service(self, info):
        self.unregistered = True

    def close(self):
        self.closed = True


def test_announcer_registers_with_allow_name_change(monkeypatch):
    fake = _FakeZeroconf()
    monkeypatch.setattr(discovery, "Zeroconf", lambda: fake)
    ann = discovery.Announcer(port=2022, fingerprint="fp", name="host")
    ann.start()
    assert fake.register_kwargs.get("allow_name_change") is True


def test_announcer_stop_unregisters_and_closes(monkeypatch):
    fake = _FakeZeroconf()
    monkeypatch.setattr(discovery, "Zeroconf", lambda: fake)
    ann = discovery.Announcer(port=2022, fingerprint="fp", name="host")
    ann.start()
    ann.stop()
    assert fake.unregistered is True
    assert fake.closed is True
