from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from dnslib import NS, QTYPE, RCODE, RR, SOA, TXT
from dnslib.server import BaseResolver, DNSServer


@dataclass
class AuthoritativeDnsSettings:
    domain: str
    host: str
    port: int
    ttl: int
    ns_host: str
    soa_email: str
    tcp: bool


class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payloads: dict[str, str] = {}
        self._serial = int(time.time())

    def set_payloads(self, payloads: dict[str, str]) -> None:
        with self._lock:
            self._payloads = {self._normalize_name(name): payload for name, payload in payloads.items()}
            self._serial = int(time.time())

    def get_payload(self, qname: str) -> str | None:
        with self._lock:
            return self._payloads.get(self._normalize_name(qname))

    def serial(self) -> int:
        with self._lock:
            return self._serial

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.rstrip(".").lower()


class AuthoritativeResolver(BaseResolver):
    def __init__(self, settings: AuthoritativeDnsSettings, store: SnapshotStore):
        self.settings = settings
        self.store = store
        self.zone = settings.domain.rstrip(".").lower()
        self.zone_fqdn = f"{self.zone}."
        self.ns_host = settings.ns_host.rstrip(".") + "."
        self.soa_email = settings.soa_email.rstrip(".") + "."

    def _soa(self) -> RR:
        soa = SOA(self.ns_host, self.soa_email, (self.store.serial(), 3600, 600, 86400, 60))
        return RR(self.zone_fqdn, QTYPE.SOA, rdata=soa, ttl=max(60, self.settings.ttl))

    def resolve(self, request, handler):
        reply = request.reply()
        reply.header.ra = 0
        reply.header.ad = 0

        qname_obj = request.q.qname
        qname = str(qname_obj).rstrip(".").lower()
        qtype = QTYPE[request.q.qtype]

        if not (qname == self.zone or qname.endswith(f".{self.zone}")):
            reply.header.rcode = RCODE.REFUSED
            return reply

        if qtype == "NS" and qname == self.zone:
            reply.add_answer(RR(self.zone_fqdn, QTYPE.NS, rdata=NS(self.ns_host), ttl=max(60, self.settings.ttl)))
            return reply

        if qtype == "SOA" and qname == self.zone:
            reply.add_answer(self._soa())
            return reply

        if qtype != "TXT":
            reply.header.rcode = RCODE.NXDOMAIN
            reply.add_auth(self._soa())
            return reply

        payload = self.store.get_payload(qname)
        if payload is None:
            reply.header.rcode = RCODE.NXDOMAIN
            reply.add_auth(self._soa())
            return reply

        reply.add_answer(RR(qname_obj, QTYPE.TXT, rdata=TXT(payload), ttl=self.settings.ttl))
        return reply


def start_authoritative_dns(settings: AuthoritativeDnsSettings, store: SnapshotStore) -> list[DNSServer]:
    resolver = AuthoritativeResolver(settings, store)
    servers = [DNSServer(resolver, port=settings.port, address=settings.host, tcp=False)]
    servers[0].start_thread()

    if settings.tcp:
        tcp_server = DNSServer(resolver, port=settings.port, address=settings.host, tcp=True)
        tcp_server.start_thread()
        servers.append(tcp_server)

    return servers
