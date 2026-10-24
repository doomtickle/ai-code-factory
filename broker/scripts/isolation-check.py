#!/usr/bin/env python3
"""Isolation check for the compose quickstart (compose.yaml, docs/standalone.md).

Runs in the agent container's own network namespace:

    docker compose --profile check run --rm isolation-check

and prints one line per probe. It FAILS, and exits 1, if the agent can do
any of these directly: resolve an external name, reach a public IPv4 or
IPv6 address on 443, reach a public DNS resolver, resolve or reach
host.docker.internal / gateway.docker.internal, or reach any gateway
address (an `agents` network with a gateway at all is a failure: that is
the Docker host). It also FAILS unless the gateway answers MCP, the
egress guard tunnels a CONNECT to an allowlisted host (with a real TLS
handshake), and refuses a CONNECT to a non-allowlisted host and to
169.254.169.254. Adapted from the Q2 research probe
(passdesk-ops/docs/research/phase3a-w0/probe.py). Standard library only.

Environment: HTTPS_PROXY (the guard; default http://egress-guard:3128),
PASSDESK_URL (default http://passdesk:8765/mcp), PASSDESK_TOKEN (optional:
with it, initialize and tools/list must succeed; without it, a 401 is the
proof the gateway answers), ISOLATION_CHECK_IPS (optional JSON object of
label -> public IPv4 literal to try on 443, replacing the built-in set),
ALLOWED_HOST (default api.anthropic.com).
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# Public addresses the agent must NOT reach directly. Any public literal
# proves the point; these are well-known ones at the time of writing, and
# ISOLATION_CHECK_IPS replaces them. Resolution happens nowhere in this
# script's direct probes, since the container has no external DNS.
PUBLIC_V4 = {"github.com": "140.82.112.3", "pypi.org": "151.101.0.223",
             "registry.npmjs.org": "104.16.1.35", "example.com": "23.192.228.80"}
PUBLIC_V6 = ["2606:4700:4700::1111", "2620:fe::fe"]
RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
DOCKER_HOST_NAMES = ["host.docker.internal", "gateway.docker.internal"]
GATEWAY_PORTS = [22, 53, 80, 111, 443, 2375, 2376]
TIMEOUT = 4

results = []


def record(name, ok, detail, expect_ok=False):
    status = "PASS" if ok == expect_ok else "FAIL"
    results.append((status, name, "reachable" if ok else "blocked", str(detail)[:120]))


def tcp(host, port, family=socket.AF_INET, timeout=TIMEOUT):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True, "connected"
    except OSError as e:
        return False, e
    finally:
        s.close()


def resolve(name):
    try:
        return True, socket.getaddrinfo(name, 443)[0][4][0]
    except OSError as e:
        return False, e


def gateways():
    """Every IPv4 gateway in the routing table, and whether there is an IPv6 default route."""
    v4 = []
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[2] != "00000000":
                v4.append(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
    except OSError as e:
        v4.append(f"unreadable: {e}")
    # A ::/0 entry counts only if it is a real route: not RTF_REJECT (the
    # "unreachable" default an IPv6-less network leaves on lo) and not on lo.
    v6_default = False
    try:
        for line in open("/proc/net/ipv6_route"):
            fields = line.split()
            if len(fields) >= 10 and fields[0] == "0" * 32 and fields[1] == "00" \
                    and not int(fields[8], 16) & 0x200 and fields[9] != "lo":
                v6_default = True
    except OSError:
        pass
    return v4, v6_default


def own_subnet_first_address():
    """The .1 of the agent network's subnet, the address a gateway would
    have had: probed on a few ports in case something answers there."""
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 7 and fields[2] == "00000000" and fields[7] != "00000000":
                dest = int.from_bytes(bytes.fromhex(fields[1])[::-1], "big")
                return socket.inet_ntoa((dest + 1).to_bytes(4, "big"))
    except OSError:
        pass
    return None


def connect_via_guard(target, expect_ok, tls_host=None):
    proxy = urllib.parse.urlsplit(os.environ.get("HTTPS_PROXY", "http://egress-guard:3128"))
    try:
        s = socket.create_connection((proxy.hostname, proxy.port or 3128), timeout=10)
    except OSError as e:
        return record(f"CONNECT via guard {target}", False, f"guard unreachable: {e}", expect_ok=True)
    with s:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        line = s.recv(200).split(b"\r\n")[0].decode(errors="replace")
        ok = line.startswith("HTTP/1.1 200")
        if ok and tls_host:
            try:
                t = ssl.create_default_context().wrap_socket(s, server_hostname=tls_host)
                t.sendall(f"HEAD / HTTP/1.1\r\nHost: {tls_host}\r\nConnection: close\r\n\r\n".encode())
                line += " | TLS ok: " + t.recv(100).split(b"\r\n")[0].decode(errors="replace")
            except (OSError, ssl.SSLError) as e:
                ok, line = False, f"{line} | TLS failed: {e}"
        record(f"CONNECT via guard {target}", ok, line, expect_ok=expect_ok)


def mcp(url, token, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through the guard
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    # 1. No external DNS.
    for name in PUBLIC_V4:
        ok, detail = resolve(name)
        record(f"DNS {name}", ok, detail)
    # 2. No direct TCP to public v4 / v6 literals.
    try:
        ips = json.loads(os.environ["ISOLATION_CHECK_IPS"]) if os.environ.get("ISOLATION_CHECK_IPS") else PUBLIC_V4
    except ValueError:
        ips = PUBLIC_V4
    for label, ip in ips.items():
        ok, detail = tcp(ip, 443)
        record(f"TCP {label} {ip}:443 direct", ok, detail)
    for ip6 in PUBLIC_V6:
        ok, detail = tcp(ip6, 443, socket.AF_INET6)
        record(f"TCP [{ip6}]:443 direct", ok, detail)
    # 3. No public resolvers (TCP and a raw UDP query).
    for ip in RESOLVERS:
        ok, detail = tcp(ip, 53)
        record(f"TCP {ip}:53 (DNS)", ok, detail)
    try:
        query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06github\x03com\x00\x00\x01\x00\x01"
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(TIMEOUT)
        u.sendto(query, ("8.8.8.8", 53))
        u.recv(512)
        record("UDP 8.8.8.8:53 query", True, "answered")
    except OSError as e:
        record("UDP 8.8.8.8:53 query", False, e)
    finally:
        u.close()
    # 4. No Docker host names.
    for name in DOCKER_HOST_NAMES:
        ok, detail = resolve(name)
        record(f"DNS {name}", ok, detail)
        if ok:
            for port in (80, 2375, 2376, 8765):
                reach, d = tcp(detail, port)
                record(f"TCP {name} {detail}:{port}", reach, d)
    # 5. No gateway at all, and nothing answering where one would be.
    v4_gateways, v6_default = gateways()
    record("IPv4 gateway in the routing table", bool(v4_gateways),
           ", ".join(v4_gateways) or "none (gateway_mode_ipv4: isolated is in effect)")
    record("IPv6 default route", v6_default, "present" if v6_default else "none")
    for gw in v4_gateways:
        for port in GATEWAY_PORTS:
            ok, detail = tcp(gw, port, timeout=2)
            record(f"TCP gateway {gw}:{port}", ok, detail)
    first = own_subnet_first_address()
    if first:
        for port in GATEWAY_PORTS:
            ok, detail = tcp(first, port, timeout=2)
            record(f"TCP {first}:{port} (where a gateway would be)", ok, detail)
    ok, detail = tcp("169.254.169.254", 80, timeout=2)
    record("TCP 169.254.169.254:80 (metadata) direct", ok, detail)
    # 6. The gateway answers MCP.
    url = os.environ.get("PASSDESK_URL", "http://passdesk:8765/mcp")
    token = os.environ.get("PASSDESK_TOKEN", "")
    try:
        code, body = mcp(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "isolation-check", "version": "1"}})
    except (OSError, ValueError) as e:
        record(f"MCP initialize {url}", False, e, expect_ok=True)
    else:
        if token:
            server = (body or {}).get("result", {}).get("serverInfo", {})
            record(f"MCP initialize {url}", code == 200 and server.get("name") == "passdesk",
                   f"HTTP {code}, server {server.get('name')} {server.get('version')}", expect_ok=True)
            code, body = mcp(url, token, "tools/list")
            tools = [t["name"] for t in (body or {}).get("result", {}).get("tools", [])]
            record("MCP tools/list", code == 200, f"HTTP {code}, tools: {', '.join(tools) or '(none granted)'}",
                   expect_ok=True)
        else:
            record(f"MCP initialize {url} (no PASSDESK_TOKEN)", code == 401,
                   f"HTTP {code}: the gateway answers and demands a token", expect_ok=True)
    # 7. The guard: allowlisted CONNECT works end to end, others are refused.
    allowed = os.environ.get("ALLOWED_HOST", "api.anthropic.com")
    connect_via_guard(f"{allowed}:443", expect_ok=True, tls_host=allowed)
    for target in ("github.com:443", "pypi.org:443", "registry.npmjs.org:443", "169.254.169.254:443",
                   "169.254.169.254:80", "host.docker.internal:443", f"{allowed}:80"):
        connect_via_guard(target, expect_ok=False)

    width = max(len(r[1]) for r in results)
    for status, name, outcome, detail in results:
        print(f"{status:4}  {name:<{width}}  {outcome:<9}  {detail}")
    failures = sum(r[0] == "FAIL" for r in results)
    print(f"SUMMARY {len(results)} checks, {failures} FAILED" if failures else f"SUMMARY {len(results)} checks, all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
