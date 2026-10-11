"""Minimal IPv4 DNS lookup used when the system resolver gives up on a host.

Why this exists: some home routers answer NXDOMAIN to the AAAA query of a name
that only has an A record behind a CNAME (seen with `fdc.nal.usda.gov`). The
operating system resolver then reports the host as unknown although its IPv4
address resolves fine. When that happens the downloader asks the configured
name servers for the A record itself, with this small standard-library client,
and connects to the returned address while still verifying the TLS certificate
against the original host name.

Only what the fallback needs is implemented: one question, record type A,
over UDP, with CNAME chains followed inside the answer section.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
from pathlib import Path

RESOLV_CONF = Path("/etc/resolv.conf")
DNS_PORT = 53
QUERY_TIMEOUT_SECONDS = 4.0
TYPE_A = 1
TYPE_CNAME = 5
CLASS_IN = 1


def system_nameservers(resolv_conf: Path = RESOLV_CONF) -> list[str]:
    """Return the name server addresses listed in resolv.conf.

    Args:
        resolv_conf: Path of the resolver configuration file.

    Returns:
        The addresses in file order, without IPv6 zone identifiers. Empty when
        the file does not exist (Windows) or lists no server.
    """
    try:
        lines = resolv_conf.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    servers: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            address = parts[1].split("%", 1)[0]
            try:
                ipaddress.ip_address(address)
            except ValueError:
                continue
            servers.append(address)
    return servers


def build_query(host: str, query_id: int) -> bytes:
    """Build a DNS query packet asking for the A record of a host.

    Args:
        host: Host name to look up.
        query_id: 16-bit identifier echoed back by the server.

    Returns:
        The packet bytes: a header with the recursion-desired bit set, then one
        question of type A and class IN.
    """
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = b""
    for label in host.rstrip(".").split("."):
        encoded = label.encode("idna")
        question += bytes([len(encoded)]) + encoded
    question += b"\x00" + struct.pack("!HH", TYPE_A, CLASS_IN)
    return header + question


def _skip_name(packet: bytes, offset: int) -> int:
    """Return the offset just after the (possibly compressed) name at offset.

    Args:
        packet: The whole DNS message.
        offset: Where the name starts.

    Returns:
        The offset of the first byte following the name.
    """
    while True:
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def parse_a_records(packet: bytes, query_id: int) -> list[str]:
    """Extract the IPv4 addresses from a DNS response.

    Args:
        packet: The response bytes.
        query_id: Identifier of the query the response must answer.

    Returns:
        Every A record of the answer section, in order. Empty when the
        response does not match the query, reports an error or is malformed.
    """
    try:
        ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
        if ident != query_id or flags & 0x000F != 0:
            return []
        offset = 12
        for _ in range(questions):
            offset = _skip_name(packet, offset) + 4
        addresses: list[str] = []
        for _ in range(answers):
            offset = _skip_name(packet, offset)
            rtype, rclass, _, rdlength = struct.unpack("!HHIH", packet[offset : offset + 10])
            offset += 10
            if rtype == TYPE_A and rclass == CLASS_IN and rdlength == 4:
                addresses.append(socket.inet_ntoa(packet[offset : offset + 4]))
            offset += rdlength
        return addresses
    except (IndexError, struct.error):
        return []


def resolve_ipv4(host: str, nameservers: list[str] | None = None) -> list[str]:
    """Look up the A records of a host directly on the configured name servers.

    Args:
        host: Host name to resolve.
        nameservers: Servers to ask, in order. Defaults to those of resolv.conf.

    Returns:
        The IPv4 addresses given by the first server that answers with at
        least one. Empty when no server does.
    """
    servers = system_nameservers() if nameservers is None else nameservers
    for server in servers:
        query_id = secrets.randbelow(0x10000)
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(QUERY_TIMEOUT_SECONDS)
                sock.sendto(build_query(host, query_id), (server, DNS_PORT))
                packet, _ = sock.recvfrom(4096)
        except OSError:
            continue
        addresses = parse_a_records(packet, query_id)
        if addresses:
            return addresses
    return []
