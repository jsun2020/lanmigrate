"""LAN device discovery over mDNS (PRD F1, C.3).

Receiver announces _lanmigrate._tcp.local. with its fingerprint in the TXT
record; sender browses for it. Manual IP:port entry remains the fallback for
networks that block mDNS.
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

SERVICE_TYPE = "_lanmigrate._tcp.local."


@dataclass
class Receiver:
    name: str
    host: str
    port: int
    fingerprint: str


def local_ip() -> str:
    """Best-effort LAN IP. UDP connect sends no packets; the target just has
    to be a routable address (223.5.5.5 = AliDNS, reachable in CN networks)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Announcer:
    """Receiver-side mDNS broadcast. start() registers, stop() unregisters."""

    def __init__(self, port: int, fingerprint: str, name: str | None = None):
        self.port = port
        self.fingerprint = fingerprint
        self.name = name or socket.gethostname()
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
        ip = local_ip()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties={"fp": self.fingerprint, "v": "1"},
        )
        self._zc = Zeroconf()
        self._zc.register_service(self._info)

    def stop(self) -> None:
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
            finally:
                self._zc.close()
        self._zc = None
        self._info = None


class _Collector(ServiceListener):
    def __init__(self):
        self.found: dict[str, Receiver] = {}
        self.event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=2000)
        if info is None or not info.addresses:
            return
        props = {
            k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else (v or "")
            for k, v in (info.properties or {}).items()
        }
        display = name.removesuffix("." + SERVICE_TYPE)
        self.found[name] = Receiver(
            name=display,
            host=socket.inet_ntoa(info.addresses[0]),
            port=info.port or 0,
            fingerprint=props.get("fp", ""),
        )
        self.event.set()

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.found.pop(name, None)


def discover(timeout: float = 5.0) -> list[Receiver]:
    """Browse the LAN for receivers for up to `timeout` seconds."""
    zc = Zeroconf()
    collector = _Collector()
    browser = ServiceBrowser(zc, SERVICE_TYPE, collector)
    try:
        collector.event.wait(timeout)
        # small grace period so several receivers can all show up
        if collector.found:
            threading.Event().wait(1.0)
    finally:
        browser.cancel()
        zc.close()
    return list(collector.found.values())
