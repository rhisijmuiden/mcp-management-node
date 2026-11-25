"""Uptime and SLA monitoring (phase 16).

External checks per customer: websites (HTTP(S) status, keyword, response time, certificate),
TCP ports, ping, DNS records and VPN endpoints (an SSL VPN portal, or a host behind a tunnel). A
check runs from Stackpilot itself or from the agent of a machine at the customer, for addresses
that are only reachable there.

A check is "down" after ``fail_after`` failed runs in a row; the incident starts at the first
failed run and ends at the first good one. Monitoring turns down, slow and expiring checks into
alerts (kind "uptime"), mailed like every other alert and also to the customer's uptime
recipients. The SLA is the share of the monitored time that the customer's checks were up, by
default without downtime inside the customer's maintenance window; the monthly report shows it
against the customer's target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import shlex
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Environment, Node, UptimeCheck, UptimeIncident, UptimeResult
from mmn.services import agent_hub, events

log = logging.getLogger("mmn.uptime")

KINDS = ("http", "tcp", "ping", "dns", "vpn")
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SOA", "CAA", "SRV", "PTR")
VPN_MODES = ("ssl", "tunnel")
METHODS = ("GET", "HEAD")
SEVERITIES = ("critical", "warning")
RESULT_DAYS = 35
INCIDENT_DAYS = 800
MAX_CONCURRENT = 20
BODY_LIMIT = 1024 * 1024
USER_AGENT = "Stackpilot-Uptime/1.0"
CERT_REFRESH = timedelta(hours=6)
RANGES: dict[str, tuple[timedelta, timedelta]] = {
    "24h": (timedelta(hours=24), timedelta(minutes=15)),
    "7d": (timedelta(days=7), timedelta(hours=2)),
    "30d": (timedelta(days=30), timedelta(hours=6)),
}
HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]{1,253}$")
SLA_RE = re.compile(r"(\d{2,3}(?:[.,]\d{1,4})?)\s*%")

_locks: dict[str, asyncio.Lock] = {}
_last_cert: dict[str, float] = {}


class UptimeError(Exception):
    pass


# ------------------------------------------------------------------ validation
def clean_host(value: str, what: str = "host") -> str:
    v = (value or "").strip().strip("[]")
    if not v:
        raise UptimeError(f"{what} is required")
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        pass
    if not HOST_RE.match(v) or ".." in v:
        raise UptimeError(f"{what} '{value}' is not a valid host name or IP address")
    return v.lower().rstrip(".")


def parse_status(spec: str | None) -> list[tuple[int, int]]:
    """"200-399", "200,301,302" or "200-299, 401" to ranges."""
    spec = (spec or "200-399").replace(" ", "")
    out = []
    for part in spec.split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d{3})(?:-(\d{3}))?", part)
        if not m:
            raise UptimeError(f"expected status '{spec}': use codes and ranges like 200-399 or 200,301")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (100 <= lo <= hi <= 599):
            raise UptimeError(f"expected status '{spec}': codes are 100-599")
        out.append((lo, hi))
    if not out:
        raise UptimeError("expected status is empty")
    return out


def status_ok(code: int, spec: str | None) -> bool:
    return any(lo <= code <= hi for lo, hi in parse_status(spec))


def clean_check(kind: str, target: str, port: int | None, config: dict[str, Any] | None) -> tuple[str, int | None, dict]:
    """Normalise the target, port and config of a check; raises UptimeError."""
    if kind not in KINDS:
        raise UptimeError(f"kind must be one of {', '.join(KINDS)}")
    cfg = dict(config or {})
    target = (target or "").strip()
    if port is not None and not (1 <= int(port) <= 65535):
        raise UptimeError("port must be 1-65535")
    if kind == "http":
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I) and not re.match(r"^https?://", target, re.I):
            raise UptimeError("the URL must start with http:// or https://")
        if not re.match(r"^https?://", target, re.I):
            target = "https://" + target
        if re.search(r"[\s'\"`<>\\]", target) or len(target) > 500:
            raise UptimeError("the URL may not contain spaces, quotes or backslashes")
        u = urlsplit(target)
        if u.scheme.lower() not in ("http", "https") or not u.hostname:
            raise UptimeError("give a URL like https://www.example.com/health")
        clean_host(u.hostname, "URL host")
        method = str(cfg.get("method") or "GET").upper()
        if method not in METHODS:
            raise UptimeError("method must be GET or HEAD")
        expect = str(cfg.get("expect_status") or "200-399").replace(" ", "")
        parse_status(expect)
        keyword = str(cfg.get("keyword") or "").strip()[:200]
        if keyword and method == "HEAD":
            raise UptimeError("a keyword check needs method GET")
        return target, None, {"method": method, "expect_status": expect, "keyword": keyword,
                              "keyword_absent": bool(cfg.get("keyword_absent")) and bool(keyword),
                              "verify_tls": cfg.get("verify_tls", True) is not False,
                              "follow_redirects": cfg.get("follow_redirects", True) is not False}
    if kind == "dns":
        name = clean_host(target, "record name")
        rtype = str(cfg.get("record_type") or "A").upper()
        if rtype not in RECORD_TYPES:
            raise UptimeError(f"record type must be one of {', '.join(RECORD_TYPES)}")
        server = str(cfg.get("server") or "").strip()
        if server:
            try:
                server = str(ipaddress.ip_address(server))
            except ValueError as e:
                raise UptimeError("DNS server must be an IP address (empty = the system resolver)") from e
        return name, None, {"record_type": rtype, "expect": str(cfg.get("expect") or "").strip()[:200],
                            "server": server}
    raw = re.sub(r"^[a-z]+://", "", target, flags=re.I).split("/")[0]
    m = re.fullmatch(r"\[([^\]]+)\](?::(\d{1,5}))?", raw) or re.fullmatch(r"([^:]+):(\d{1,5})", raw)
    host_raw, in_port = (m.group(1), m.group(2)) if m else (raw, None)
    if in_port and not port:
        port = int(in_port)
        if not 1 <= port <= 65535:
            raise UptimeError("port must be 1-65535")
    host = clean_host(host_raw)
    if kind == "tcp":
        if not port:
            raise UptimeError("a TCP check needs a port")
        return host, int(port), {}
    if kind == "ping":
        return host, None, {}
    mode = str(cfg.get("mode") or "ssl")
    if mode not in VPN_MODES:
        raise UptimeError("VPN mode must be ssl (the VPN portal answers TLS) or tunnel (a host behind the tunnel "
                          "answers ping)")
    if mode == "ssl":
        return host, int(port or 443), {"mode": mode}
    return host, None, {"mode": mode}


def label(c: UptimeCheck) -> str:
    if c.kind == "http":
        return c.target
    if c.kind == "dns":
        return f"{(c.config or {}).get('record_type') or 'A'} {c.target}"
    return f"{c.target}:{c.port}" if c.port else c.target


# ------------------------------------------------------------------ results
@dataclass
class Result:
    ok: bool | None  # None = could not check (the probe agent is offline)
    ms: float | None = None
    code: int | None = None
    error: str | None = None
    cert_expires_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        return {"ok": self.ok, "ms": round(self.ms, 1) if self.ms is not None else None, "code": self.code,
                "error": self.error, "cert_expires_at": self.cert_expires_at.isoformat() if self.cert_expires_at else None,
                **self.detail}


def _err(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError | TimeoutError):
        return "timed out"
    msg = str(e) or type(e).__name__
    return msg[:300]


def body_check(body: str, cfg: dict[str, Any]) -> str | None:
    kw = cfg.get("keyword")
    if not kw:
        return None
    found = kw.lower() in (body or "").lower()
    if cfg.get("keyword_absent") and found:
        return f"the page contains '{kw}'"
    if not cfg.get("keyword_absent") and not found:
        return f"keyword '{kw}' not found on the page"
    return None


def http_verdict(code: int, body: str, cfg: dict[str, Any]) -> str | None:
    if not status_ok(code, cfg.get("expect_status")):
        return f"HTTP status {code} (expected {cfg.get('expect_status') or '200-399'})"
    return body_check(body, cfg)


# ------------------------------------------------------------------ probes from Stackpilot
async def _cert_of(host: str, port: int, timeout: float) -> datetime | None:
    from mmn.services.nightly import endpoint_cert

    try:
        return await endpoint_cert(host, port, timeout=timeout)
    except Exception:  # noqa: BLE001 - the certificate is extra information
        return None


async def probe_http(c: UptimeCheck) -> Result:
    import httpx

    cfg = c.config or {}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(verify=cfg.get("verify_tls", True), follow_redirects=cfg.get("follow_redirects", True),
                                     timeout=c.timeout_seconds, headers={"User-Agent": USER_AGENT}) as cl:
            async with cl.stream(cfg.get("method") or "GET", c.target) as r:
                chunks, size = [], 0
                if cfg.get("keyword"):
                    async for chunk in r.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= BODY_LIMIT:
                            break
                ms = (time.perf_counter() - started) * 1000
                body = b"".join(chunks).decode(r.encoding or "utf-8", "replace") if chunks else ""
                code = r.status_code
    except httpx.HTTPError as e:
        msg = _err(e)
        if isinstance(e, httpx.ConnectError) and "CERTIFICATE_VERIFY_FAILED" in msg:
            msg = "TLS certificate is not valid: " + msg.split("certificate verify failed:")[-1].strip(" )")[:200]
        return Result(False, (time.perf_counter() - started) * 1000, error=msg)
    except Exception as e:  # noqa: BLE001
        return Result(False, (time.perf_counter() - started) * 1000, error=_err(e))
    cert = None
    u = urlsplit(c.target)
    if u.scheme == "https" and (time.monotonic() - _last_cert.get(c.id, 0) > CERT_REFRESH.total_seconds()
                                or c.cert_expires_at is None):
        cert = await _cert_of(u.hostname or "", u.port or 443, min(10, c.timeout_seconds))
        if cert:
            _last_cert[c.id] = time.monotonic()
    bad = http_verdict(code, body, cfg)
    return Result(bad is None, ms, code=code, error=bad, cert_expires_at=cert)


async def probe_tcp(host: str, port: int, timeout: float, *, tls: bool = False) -> Result:
    started = time.perf_counter()
    try:
        ctx = None
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if tls else None),
                                      timeout)
        ms = (time.perf_counter() - started) * 1000
        cert = None
        if tls:
            from cryptography import x509

            der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
            cert = x509.load_der_x509_certificate(der).not_valid_after_utc if der else None
        w.close()
        return Result(True, ms, cert_expires_at=cert)
    except Exception as e:  # noqa: BLE001
        return Result(False, (time.perf_counter() - started) * 1000, error=_err(e))


def parse_ping(stdout: str, rc: int) -> Result:
    """iputils ping output ("3 packets transmitted, 2 received", "rtt min/avg/max/mdev = ...")."""
    m = re.search(r"(\d+) (?:packets )?received", stdout)
    received = int(m.group(1)) if m else 0
    rtt = re.search(r"=\s*[\d.]+/([\d.]+)/", stdout)
    if received > 0:
        return Result(True, float(rtt.group(1)) if rtt else None)
    last = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    hint = next((ln for ln in last if "unknown host" in ln.lower() or "not permitted" in ln.lower()
                 or "failure in name" in ln.lower() or "unreachable" in ln.lower()), None)
    return Result(False, None, error=(hint or "no reply")[:300])


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


async def icmp_ping(host: str, timeout: float, count: int = 3) -> Result:
    """Ping with an unprivileged ICMP socket (no ping binary or root needed; uses the kernel's
    net.ipv4.ping_group_range). Raises PermissionError when the system does not allow it."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_DGRAM), timeout)
    except Exception as e:  # noqa: BLE001
        return Result(False, error=f"cannot resolve {host}: {_err(e)}")
    fam, addr = infos[0][0], infos[0][4]
    v4 = fam == socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_DGRAM, socket.IPPROTO_ICMP if v4 else socket.IPPROTO_ICMPV6)
    sock.setblocking(False)
    times: list[float] = []
    try:
        for seq in range(1, count + 1):
            body = b"stackpilot-uptime"
            head = struct.pack("!BBHHH", 8 if v4 else 128, 0, 0, 0, seq)
            pkt = struct.pack("!BBHHH", 8 if v4 else 128, 0, _checksum(head + body) if v4 else 0, 0, seq) + body
            started = time.perf_counter()
            await loop.sock_sendto(sock, pkt, addr)
            deadline = started + timeout
            while (left := deadline - time.perf_counter()) > 0:
                try:
                    data = await asyncio.wait_for(loop.sock_recv(sock, 2048), left)
                except TimeoutError:
                    break
                if len(data) >= 8 and data[0] == (0 if v4 else 129) and struct.unpack("!H", data[6:8])[0] == seq:
                    times.append((time.perf_counter() - started) * 1000)
                    break
            if seq < count:
                await asyncio.sleep(0.2)
    finally:
        sock.close()
    if times:
        return Result(True, sum(times) / len(times))
    return Result(False, error="no reply")


async def probe_ping(host: str, timeout: int) -> Result:
    try:
        return await icmp_ping(host, timeout)
    except PermissionError:
        pass  # ICMP sockets not allowed for this user: try the ping binary
    except OSError as e:
        return Result(False, error=_err(e))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "3", "-i", "0.2", "-W", str(max(1, timeout)), "-n", "-q", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
    except FileNotFoundError:
        return Result(False, error="ICMP is not allowed for the Stackpilot service and ping is not installed "
                                   "(set net.ipv4.ping_group_range or apt install iputils-ping)")
    except Exception as e:  # noqa: BLE001
        return Result(False, error=_err(e))
    return parse_ping(out.decode("utf-8", "replace"), proc.returncode or 0)


def dns_verdict(values: list[str], cfg: dict[str, Any]) -> str | None:
    if not values:
        return "no records"
    expect = (cfg.get("expect") or "").strip().lower().rstrip(".")
    if expect and not any(expect in v.lower().rstrip(".") for v in values):
        return f"'{cfg.get('expect')}' not in the answer ({', '.join(values[:5])[:200]})"
    return None


async def probe_dns(c: UptimeCheck) -> Result:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    cfg = c.config or {}
    r = dns.asyncresolver.Resolver(configure=not cfg.get("server"))
    if cfg.get("server"):
        r.nameservers = [cfg["server"]]
    started = time.perf_counter()
    try:
        ans = await r.resolve(c.target, cfg.get("record_type") or "A", lifetime=c.timeout_seconds, search=False)
        values = [x.to_text().strip('"') for x in ans]
    except dns.resolver.NXDOMAIN:
        return Result(False, (time.perf_counter() - started) * 1000, error="the name does not exist (NXDOMAIN)")
    except dns.resolver.NoAnswer:
        values = []
    except dns.exception.Timeout:
        return Result(False, (time.perf_counter() - started) * 1000, error="timed out")
    except Exception as e:  # noqa: BLE001
        return Result(False, (time.perf_counter() - started) * 1000, error=_err(e))
    ms = (time.perf_counter() - started) * 1000
    bad = dns_verdict(values, cfg)
    return Result(bad is None, ms, error=bad, detail={"values": values[:20]})


# ------------------------------------------------------------------ probes through an agent
def _ps(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def agent_script(c: UptimeCheck, windows: bool) -> str:
    """A script that the agent runs; the last line starts with SPJSON (Windows) or the markers below."""
    cfg = c.config or {}
    t = int(c.timeout_seconds)
    kind = "ping" if c.kind == "vpn" and cfg.get("mode") == "tunnel" else ("tls" if c.kind == "vpn" else c.kind)
    if windows:
        head = ("$ErrorActionPreference = 'Stop'; $r = @{ok = $false; ms = $null; code = $null; error = $null; "
                "body = $null; values = @()}; $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                "function Msg($e) { $x = $e.Exception; while ($x.InnerException) { $x = $x.InnerException }; "
                "return $x.Message }\n")
        tail = "\n$r.ms = $sw.Elapsed.TotalMilliseconds; 'SPJSON ' + ($r | ConvertTo-Json -Compress -Depth 3)"
        if kind == "ping":
            body = (f"$p = New-Object Net.NetworkInformation.Ping; $times = @(); $last = 'no reply'\n"
                    f"for ($i = 0; $i -lt 3; $i++) {{ try {{ $x = $p.Send({_ps(c.target)}, {t * 1000}); "
                    "if ($x.Status -eq 'Success') { $times += $x.RoundtripTime } else { $last = [string]$x.Status } } "
                    "catch { $last = Msg $_ } }\n"
                    "$sw.Stop(); if ($times.Count) { $r.ok = $true; $r.rtt = ($times | Measure-Object -Average).Average } "
                    "else { $r.error = $last }")
            tail = ("\n$r.ms = if ($r.rtt -ne $null) { $r.rtt } else { $null }; "
                    "'SPJSON ' + ($r | ConvertTo-Json -Compress -Depth 3)")
        elif kind in ("tcp", "tls"):
            body = (f"try {{ $c = New-Object Net.Sockets.TcpClient; $task = $c.ConnectAsync({_ps(c.target)}, {int(c.port or 443)}); "
                    f"if ($task.Wait({t * 1000}) -and $c.Connected) {{ $r.ok = $true }} else {{ $r.error = 'timed out' }}; "
                    "$c.Close() } catch { $r.error = Msg $_ }")
        elif kind == "http":
            redir = 5 if cfg.get("follow_redirects", True) else 0
            body = ("try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}\n"
                    + ("" if cfg.get("verify_tls", True) else
                       "[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }\n")
                    + f"try {{ $resp = Invoke-WebRequest -Uri {_ps(c.target)} -Method {cfg.get('method') or 'GET'} "
                    f"-UseBasicParsing -TimeoutSec {t} -MaximumRedirection {redir} -UserAgent {_ps(USER_AGENT)}; "
                    "$r.code = [int]$resp.StatusCode; $b = $resp.Content; "
                    "if ($b -is [byte[]]) { $b = [Text.Encoding]::UTF8.GetString($b) }; "
                    "if ($b) { $r.body = ([string]$b).Substring(0, [Math]::Min(([string]$b).Length, 262144)) } } "
                    "catch { if ($_.Exception.Response) { $r.code = [int]$_.Exception.Response.StatusCode } "
                    "else { $r.error = Msg $_ } }\n$r.ok = $r.code -ne $null")
        else:  # dns
            server = f" -Server {_ps(cfg['server'])}" if cfg.get("server") else ""
            body = (f"try {{ $x = Resolve-DnsName -Name {_ps(c.target)} -Type {cfg.get('record_type') or 'A'} -DnsOnly "
                    f"-QuickTimeout{server} | Where-Object {{ $_.Section -eq 'Answer' -and [string]$_.Type -eq "
                    f"{_ps(cfg.get('record_type') or 'A')} }}; "
                    "$r.values = @($x | ForEach-Object { if ($_.IPAddress) { $_.IPAddress } elseif ($_.NameExchange) "
                    "{ \"$($_.Preference) $($_.NameExchange)\" } elseif ($_.NameHost) { $_.NameHost } "
                    "elseif ($_.Strings) { $_.Strings -join '' } elseif ($_.PrimaryServer) { $_.PrimaryServer } "
                    "else { $_.Name } }); $r.ok = $true } "
                    "catch { if ((Msg $_) -match 'does not exist') { $r.error = 'the name does not exist (NXDOMAIN)' } "
                    "else { $r.error = Msg $_ } }")
        return head + body + tail
    # Linux (Proxmox VE, servers): sh with standard tools.
    q = shlex.quote
    timing = "s=$(date +%s%N); "
    done = "e=$(date +%s%N); ms=$(( (e-s)/1000000 )); "
    if kind == "ping":
        return (f"out=$(ping -c 3 -i 0.2 -W {t} -n -q {q(c.target)} 2>&1); rc=$?; printf '%s\\n' \"$out\"; "
                "echo \"SPRC $rc\"")
    if kind in ("tcp", "tls"):
        port = int(c.port or 443)
        return (timing + f"err=$(timeout {t} bash -c 'exec 3<>/dev/tcp/'{q(c.target)}'/{port}' 2>&1); rc=$?; "
                + done + "echo \"SPTCP $rc $ms $(printf '%s' \"$err\" | tr '\\n' ' ' | head -c 300)\"")
    if kind == "http":
        flags = ["-sS", "-o", '"$b"', "-w", "'%{http_code} %{time_total}'", "--max-time", str(t), "-A", q(USER_AGENT),
                 "-X", cfg.get("method") or "GET"]
        if (cfg.get("method") or "GET") == "HEAD":
            flags[-2:] = ["-I"]
        if not cfg.get("verify_tls", True):
            flags.append("-k")
        if cfg.get("follow_redirects", True):
            flags.append("-L")
        return ("command -v curl >/dev/null 2>&1 || { echo 'SPHTTP 127 000 0'; echo 'SPERR curl is not installed'; "
                "exit 0; }; b=$(mktemp); x=$(mktemp); "
                f"w=$(curl {' '.join(flags)} {q(c.target)} 2>\"$x\"); rc=$?; echo \"SPHTTP $rc $w\"; "
                "echo \"SPERR $(head -c 300 \"$x\" | tr '\\n' ' ')\"; echo SPBODY; head -c 262144 \"$b\"; "
                "rm -f \"$b\" \"$x\"")
    rtype = cfg.get("record_type") or "A"
    server = cfg.get("server") or ""
    dig = f"dig +short +time={t} +tries=1 {('@' + q(server)) if server else ''} {q(c.target)} {rtype}"
    host = f"host -t {rtype} -W {t} {q(c.target)} {q(server) if server else ''}"
    getent = f"getent {'ahostsv6' if rtype == 'AAAA' else 'ahostsv4'} {q(c.target)} | awk '{{print $1}}' | sort -u"
    return (timing + "if command -v dig >/dev/null 2>&1; then out=$(" + dig + " 2>&1); rc=$?; tool=dig; "
            "elif command -v host >/dev/null 2>&1; then out=$(" + host + " 2>&1); rc=$?; tool=host; "
            + (f"else out=$({getent}); rc=$?; tool=getent; " if rtype in ("A", "AAAA") and not server else
               "else out='install dnsutils (dig) for this DNS check'; rc=127; tool=none; ")
            + "fi; " + done + "echo \"SPDNS $rc $ms $tool\"; printf '%s\\n' \"$out\"")


def _marker(stdout: str, name: str) -> list[str] | None:
    for ln in stdout.splitlines():
        if ln.startswith(name + " ") or ln == name:
            return ln[len(name):].strip().split(" ")
    return None


def parse_agent(c: UptimeCheck, stdout: str) -> Result:
    """Turn the output of agent_script into a Result."""
    cfg = c.config or {}
    if "SPJSON " in stdout:
        raw = stdout[stdout.rindex("SPJSON ") + 7:].strip().splitlines()[0]
        try:
            d = json.loads(raw)
        except ValueError:
            return Result(False, error="could not read the result of the agent")
        ms = float(d["ms"]) if d.get("ms") is not None else None
        if c.kind == "http":
            if d.get("code") is None:
                return Result(False, ms, error=str(d.get("error") or "no response")[:300])
            bad = http_verdict(int(d["code"]), d.get("body") or "", cfg)
            return Result(bad is None, ms, code=int(d["code"]), error=bad)
        if c.kind == "dns":
            if not d.get("ok"):
                return Result(False, ms, error=str(d.get("error") or "lookup failed")[:300])
            vals = d.get("values") or []
            vals = [str(v) for v in (vals if isinstance(vals, list) else [vals])]
            bad = dns_verdict(vals, cfg)
            return Result(bad is None, ms, error=bad, detail={"values": vals[:20]})
        return Result(bool(d.get("ok")), ms, error=None if d.get("ok") else str(d.get("error") or "no reply")[:300])
    if (rc := _marker(stdout, "SPRC")) is not None:
        return parse_ping(stdout, int(rc[0] or 1))
    if (m := _marker(stdout, "SPTCP")) is not None:
        ok = m[0] == "0"
        err = " ".join(m[2:]).strip() or ("timed out" if m[0] == "124" else "connection refused")
        return Result(ok, float(m[1]) if len(m) > 1 and m[1].isdigit() else None, error=None if ok else err[:300])
    if (m := _marker(stdout, "SPHTTP")) is not None:
        rc, code = m[0], int(m[1]) if len(m) > 1 and m[1].isdigit() else 0
        ms = float(m[2]) * 1000 if len(m) > 2 and re.fullmatch(r"[\d.]+", m[2]) else None
        err = " ".join(_marker(stdout, "SPERR") or []).strip()
        if rc != "0" or not code:
            return Result(False, ms, error=(err or f"curl exit code {rc}")[:300])
        body = stdout.split("SPBODY\n", 1)[1] if "SPBODY\n" in stdout else ""
        bad = http_verdict(code, body, cfg)
        return Result(bad is None, ms, code=code, error=bad)
    if (m := _marker(stdout, "SPDNS")) is not None:
        ms = float(m[1]) if len(m) > 1 and m[1].isdigit() else None
        tool = m[2] if len(m) > 2 else ""
        all_lines = stdout.splitlines()
        idx = next(i for i, ln in enumerate(all_lines) if ln.startswith("SPDNS"))
        lines = [ln.strip() for ln in all_lines[idx + 1:] if ln.strip()]
        if tool == "host":
            if any("NXDOMAIN" in ln or "not found" in ln for ln in lines):
                return Result(False, ms, error="the name does not exist (NXDOMAIN)")
            lines = [re.split(r" has (?:IPv6 )?address | is an alias for | mail is handled by | descriptive text "
                              r"| name server | has \S+ record ", ln)[-1] for ln in lines if " " in ln]
        if m[0] != "0":
            return Result(False, ms, error=(" ".join(lines) or "lookup failed")[:300])
        lines = [ln for ln in lines if not ln.startswith(";")]
        vals = [ln.strip('"') for ln in lines]
        bad = dns_verdict(vals, cfg)
        return Result(bad is None, ms, error=bad, detail={"values": vals[:20]})
    return Result(False, error="no result from the agent")


async def probe_agent(c: UptimeCheck, node: Node) -> Result:
    if not agent_hub.is_online(node.id):
        return Result(None, error=f"the agent of {node.name} (probe) is offline")
    script = agent_script(c, node.kind == "windows")
    try:
        res = await agent_hub.request(node.id, "exec", {"command": script, "timeout": c.timeout_seconds * 3 + 5},
                                      timeout=c.timeout_seconds * 3 + 35) or {}
    except Exception as e:  # noqa: BLE001
        return Result(None, error=f"probe {node.name}: {_err(e)}")
    r = parse_agent(c, res.get("stdout") or "")
    if r.error == "no result from the agent" and res.get("stderr"):
        r.error = str(res["stderr"]).strip()[:300]
    r.detail["probe"] = node.name
    return r


async def probe(c: UptimeCheck, probe_node: Node | None = None) -> Result:
    """Run a check once (without storing anything)."""
    if probe_node is not None:
        return await probe_agent(c, probe_node)
    cfg = c.config or {}
    if c.kind == "http":
        return await probe_http(c)
    if c.kind == "tcp":
        return await probe_tcp(c.target, int(c.port or 0), c.timeout_seconds)
    if c.kind == "ping" or (c.kind == "vpn" and cfg.get("mode") == "tunnel"):
        return await probe_ping(c.target, c.timeout_seconds)
    if c.kind == "vpn":
        return await probe_tcp(c.target, int(c.port or 443), c.timeout_seconds, tls=True)
    return await probe_dns(c)


# ------------------------------------------------------------------ recording and state
def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def record(db: AsyncSession, c: UptimeCheck, r: Result, now: datetime | None = None) -> str | None:
    """Store a result and move the check's state; returns "down", "up" or None (no change)."""
    now = now or datetime.now(UTC)
    db.add(UptimeResult(check_id=c.id, at=now, ok=r.ok, ms=r.ms, code=r.code, error=(r.error or None) and r.error[:300]))
    c.last_check_at, c.last_ms, c.last_code = now, r.ms, r.code
    c.last_error = (r.error or None) and r.error[:500]
    if r.cert_expires_at:
        c.cert_expires_at = r.cert_expires_at
    before = c.status
    change = None
    if r.ok is None:
        if c.status != "down":
            c.status = "unknown"
    elif r.ok:
        c.failures, c.failing_since = 0, None
        if before == "down":
            for inc in (await db.execute(select(UptimeIncident).where(UptimeIncident.check_id == c.id,
                                                                      UptimeIncident.ended_at.is_(None)))).scalars():
                inc.ended_at = now
            change = "up"
        c.status = "degraded" if c.slow_ms and r.ms is not None and r.ms > c.slow_ms else "up"
    else:
        c.failures = (c.failures or 0) + 1
        c.failing_since = _aware(c.failing_since) or now
        if c.failures >= max(1, c.fail_after) and before != "down":
            c.status = "down"
            db.add(UptimeIncident(check_id=c.id, customer_id=c.customer_id, check_name=c.name,
                                  started_at=c.failing_since, error=(r.error or "")[:500]))
            change = "down"
    if c.status != before or c.status_since is None:
        c.status_since = now if c.status != "down" else c.failing_since
    return change


def busy(check_id: str) -> bool:
    lock = _locks.get(check_id)
    return bool(lock and lock.locked())


async def run_check(check_id: str, *, wait: bool = True) -> dict[str, Any] | None:
    """Run one stored check now and record the result. When it is running already: wait for that
    run and then run it again (wait=True), or return None (wait=False, the scheduler)."""
    lock = _locks.setdefault(check_id, asyncio.Lock())
    if lock.locked() and not wait:
        return None
    async with lock:
        return await _run_check(check_id)


async def _run_check(check_id: str) -> dict[str, Any] | None:
    async with sessionmaker()() as db:
        c = await db.get(UptimeCheck, check_id)
        if c is None:
            return None
        pn = await db.get(Node, c.probe_node_id) if c.probe_node_id else None
        r = await probe(c, pn)
        change = await record(db, c, r)
        out = {**r.out(), "status": c.status, "change": change}
        cid, name = c.customer_id, c.name
        await db.commit()
    events.publish("uptime.result", {"check_id": check_id, "customer_id": cid, "status": out["status"],
                                     "change": change, "name": name})
    return out


def due(c: UptimeCheck, now: datetime) -> bool:
    last = _aware(c.last_check_at)
    return last is None or (now - last).total_seconds() >= c.interval_seconds - 2


async def tick(now: datetime | None = None) -> int:
    """Run every check that is due (called every 15 seconds)."""
    from mmn.services import tenancy

    now = now or datetime.now(UTC)
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            rows = list((await db.execute(select(UptimeCheck.id, UptimeCheck.last_check_at, UptimeCheck.interval_seconds)
                                          .where(UptimeCheck.enabled.is_(True)))).all())
        ids = [r.id for r in rows if (r.last_check_at is None or
                                      (now - _aware(r.last_check_at)).total_seconds() >= r.interval_seconds - 2)
               and not busy(r.id)]
        if not ids:
            return 0
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def one(cid: str) -> None:
            async with sem:
                try:
                    await run_check(cid, wait=False)
                except Exception:  # noqa: BLE001
                    log.exception("uptime check %s failed", cid)

        await asyncio.gather(*(one(i) for i in ids))
    return len(ids)


async def prune() -> None:
    from mmn.services import tenancy

    now = datetime.now(UTC)
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            await db.execute(delete(UptimeResult).where(UptimeResult.at < now - timedelta(days=RESULT_DAYS)))
            await db.execute(delete(UptimeIncident).where(UptimeIncident.ended_at.is_not(None),
                                                          UptimeIncident.ended_at < now - timedelta(days=INCIDENT_DAYS)))
            await db.commit()


# ------------------------------------------------------------------ customer SLA settings
def sla_of(e: Environment) -> dict[str, Any]:
    u = e.uptime or {}
    target = u.get("target_pct")
    source = "setting" if target is not None else None
    if target is None and e.sla:
        m = SLA_RE.search(e.sla)
        if m:
            v = float(m.group(1).replace(",", "."))
            if 50 <= v <= 100:
                target, source = v, "sla_text"
    return {"target_pct": float(target) if target is not None else None, "target_source": source,
            "exclude_maintenance": u.get("exclude_maintenance", True) is not False,
            "alert_breach": u.get("alert_breach", True) is not False,
            "recipients": list(u.get("recipients") or [])}


def save_sla(e: Environment, *, target_pct: float | None = None, exclude_maintenance: bool | None = None,
             alert_breach: bool | None = None, recipients: list[str] | None = None, clear_target: bool = False) -> dict:
    from email_validator import EmailNotValidError, validate_email

    u = dict(e.uptime or {})
    if clear_target:
        u.pop("target_pct", None)
    elif target_pct is not None:
        if not 50 <= float(target_pct) <= 100:
            raise UptimeError("the SLA target is a percentage between 50 and 100, for example 99.9")
        u["target_pct"] = round(float(target_pct), 3)
    if exclude_maintenance is not None:
        u["exclude_maintenance"] = bool(exclude_maintenance)
    if alert_breach is not None:
        u["alert_breach"] = bool(alert_breach)
    if recipients is not None:
        clean = []
        for r in recipients[:20]:
            r = (r or "").strip()
            if not r:
                continue
            try:
                clean.append(validate_email(r, check_deliverability=False).normalized)
            except EmailNotValidError as ex:
                raise UptimeError(f"'{r}' is not a valid email address") from ex
        u["recipients"] = clean
    e.uptime = u
    return sla_of(e)


async def extra_recipients(db: AsyncSession, customer_ids: set[str]) -> dict[str, list[str]]:
    out = {}
    for cid in customer_ids:
        e = await db.get(Environment, cid)
        if e is not None and sla_of(e)["recipients"]:
            out[cid] = sla_of(e)["recipients"]
    return out


# ------------------------------------------------------------------ availability and SLA
Interval = tuple[datetime, datetime]


def maintenance_intervals(windows: list[Any] | None, start: datetime, stop: datetime, tz: ZoneInfo) -> list[Interval]:
    """The maintenance windows (gateway.parse_window) between start and stop, in UTC."""
    if not windows or stop <= start:
        return []
    out: list[Interval] = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = stop.astimezone(tz).date()
    while day <= last:
        for w in windows:
            if day.weekday() not in w.days:
                continue
            base = datetime.combine(day, dtime(0), tzinfo=tz)
            if w.start == w.end:
                a, b = base, datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=tz)
            elif w.start < w.end:
                a = datetime.combine(day, dtime(w.start // 60, w.start % 60), tzinfo=tz)
                b = datetime.combine(day, dtime(w.end // 60, w.end % 60), tzinfo=tz)
            else:
                a = datetime.combine(day, dtime(w.start // 60, w.start % 60), tzinfo=tz)
                b = datetime.combine(day + timedelta(days=1), dtime(w.end // 60, w.end % 60), tzinfo=tz)
            a, b = max(a.astimezone(UTC), start), min(b.astimezone(UTC), stop)
            if b > a:
                out.append((a, b))
        day += timedelta(days=1)
    return merge(out)


def merge(iv: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _seconds(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds())


def overlap_seconds(iv: list[Interval], start: datetime, stop: datetime) -> float:
    return sum(_seconds(max(a, start), min(b, stop)) for a, b in iv if min(b, stop) > max(a, start))


def downtime(incidents: list[Interval], start: datetime, stop: datetime, exclude: list[Interval]) -> float:
    """Seconds down between start and stop, leaving out the parts inside ``exclude`` (merged)."""
    down = 0.0
    for a, b in merge([(max(a, start), min(b, stop)) for a, b in incidents if min(b, stop) > max(a, start)]):
        down += _seconds(a, b) - overlap_seconds(exclude, a, b)
    return down


async def availability(db: AsyncSession, checks: list[UptimeCheck], start: datetime, stop: datetime, *,
                       exclude: list[Interval] | None = None, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per check: monitored seconds, downtime, availability % and the number of incidents."""
    now = now or datetime.now(UTC)
    stop = min(stop, now)
    out: dict[str, dict[str, Any]] = {}
    if not checks or stop <= start:
        return out
    ids = [c.id for c in checks]
    incs = list((await db.execute(select(UptimeIncident).where(
        UptimeIncident.check_id.in_(ids), UptimeIncident.started_at < stop,
        (UptimeIncident.ended_at.is_(None)) | (UptimeIncident.ended_at > start)))).scalars())
    for c in checks:
        since = max(start, _aware(c.created_at) or start)
        monitored = _seconds(since, stop)
        mine = [(_aware(i.started_at), _aware(i.ended_at) or now) for i in incs if i.check_id == c.id]
        down = downtime(mine, since, stop, exclude or [])
        excl = overlap_seconds(exclude or [], since, stop)
        counted = max(0.0, monitored - excl)
        out[c.id] = {"monitored_seconds": int(monitored), "down_seconds": int(down), "excluded_seconds": int(excl),
                     "availability": round(100 * (1 - down / counted), 3) if counted > 0 else None,
                     "incidents": len(mine)}
    return out


async def customer_sla(db: AsyncSession, e: Environment, start: datetime, stop: datetime, *,
                       now: datetime | None = None, with_incidents: bool = False) -> dict[str, Any]:
    """The uptime SLA of one customer over a period: per check and in total (time-weighted)."""
    from mmn.services import gateway, reports

    now = now or datetime.now(UTC)
    cfg = sla_of(e)
    tz = await reports.timezone(db)
    checks = [c for c in (await db.execute(select(UptimeCheck).where(UptimeCheck.customer_id == e.id)
                                           .order_by(UptimeCheck.name))).scalars()
              if _aware(c.created_at) < min(stop, now) and (c.enabled or _aware(c.updated_at) >= start)]
    windows = gateway.parse_window(e.maintenance_window) if cfg["exclude_maintenance"] else None
    excl = maintenance_intervals(windows, start, min(stop, now), tz)
    av = await availability(db, checks, start, stop, exclude=excl, now=now)
    rows, up_total, counted_total = [], 0.0, 0.0
    for c in checks:
        a = av.get(c.id)
        if not a:
            continue
        counted = a["monitored_seconds"] - a["excluded_seconds"]
        if c.in_sla and counted > 0:
            counted_total += counted
            up_total += counted - a["down_seconds"]
        rows.append({"id": c.id, "name": c.name, "kind": c.kind, "target": label(c), "in_sla": c.in_sla,
                     "status": c.status, **a, "downtime_minutes": round(a["down_seconds"] / 60, 1)})
    pct = round(100 * up_total / counted_total, 3) if counted_total > 0 else None
    target = cfg["target_pct"]
    out = {"customer_id": e.id, "customer": e.name, "start": start.isoformat(), "end": stop.isoformat(),
           "availability": pct, "target_pct": target, "target_source": cfg["target_source"],
           "met": (pct >= target) if (pct is not None and target is not None) else None,
           "allowed_downtime_minutes": round(counted_total * (1 - target / 100) / 60, 1) if target and counted_total else None,
           "downtime_minutes": round(sum(r["down_seconds"] for r in rows if r["in_sla"]) / 60, 1),
           "exclude_maintenance": cfg["exclude_maintenance"], "maintenance_window": e.maintenance_window,
           "maintenance_understood": windows is not None if e.maintenance_window else None,
           "checks": rows}
    if with_incidents and checks:
        incs = (await db.execute(select(UptimeIncident).where(
            UptimeIncident.check_id.in_([c.id for c in checks]), UptimeIncident.started_at < min(stop, now),
            (UptimeIncident.ended_at.is_(None)) | (UptimeIncident.ended_at > start))
            .order_by(UptimeIncident.started_at.desc()).limit(200))).scalars()
        out["incidents"] = [incident_out(i, now) for i in incs]
    return out


def incident_out(i: UptimeIncident, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    end = _aware(i.ended_at)
    return {"id": i.id, "check_id": i.check_id, "check": i.check_name, "customer_id": i.customer_id,
            "started_at": _aware(i.started_at).isoformat(), "ended_at": end.isoformat() if end else None,
            "duration_minutes": round(_seconds(_aware(i.started_at), end or now) / 60, 1), "error": i.error,
            "open": end is None}


async def month_to_date(db: AsyncSession, e: Environment, now: datetime | None = None) -> dict[str, Any]:
    from mmn.services import reports

    now = now or datetime.now(UTC)
    tz = await reports.timezone(db)
    start, end = reports.month_bounds(reports.current_period(now, tz), tz)
    return await customer_sla(db, e, start, end, now=now)


# ------------------------------------------------------------------ alerts (monitoring)
async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Alerts that should be open: checks that are down or slow, expiring certificates and
    customers below their SLA this month."""
    want: dict[str, dict[str, Any]] = {}
    envs = {e.id: e for e in (await db.execute(select(Environment))).scalars()}
    checks = list((await db.execute(select(UptimeCheck).where(UptimeCheck.enabled.is_(True)))).scalars())
    for c in checks:
        e = envs.get(c.customer_id)
        cname = e.name if e else "?"
        detail = {"source": f"{cname} · {c.name}", "customer_id": c.customer_id, "check_id": c.id,
                  "check": c.name, "target": label(c)}
        if c.status == "down":
            since = _aware(c.failing_since) or _aware(c.status_since) or now
            mins = int((now - since).total_seconds() // 60)
            want[f"uptime:{c.id}"] = {"node_id": c.node_id, "kind": "uptime", "severity": c.severity, "value": mins,
                                      "detail": detail,
                                      "message": f"{c.name} ({label(c)}) is down: {c.last_error or 'no response'}"}
        elif c.status == "degraded" and c.last_ms is not None:
            want[f"uptime-slow:{c.id}"] = {"node_id": c.node_id, "kind": "uptime", "severity": "warning",
                                           "value": round(c.last_ms), "detail": {**detail, "slow": True},
                                           "message": f"{c.name} responds slowly: {c.last_ms:.0f} ms "
                                                      f"(limit {c.slow_ms} ms)"}
        exp = _aware(c.cert_expires_at)
        if exp and c.cert_warn_days:
            days = int((exp - now).total_seconds() // 86400)
            if days <= c.cert_warn_days:
                want[f"uptime-cert:{c.id}"] = {
                    "node_id": c.node_id, "kind": "uptime", "severity": "critical" if days < 0 else "warning",
                    "value": days, "detail": {**detail, "cert": True},
                    "message": (f"The certificate of {c.name} expired {-days} day(s) ago" if days < 0 else
                                f"The certificate of {c.name} expires in {days} day(s)")}
    with_checks = {c.customer_id for c in checks if c.in_sla}
    for cid in with_checks:
        e = envs.get(cid)
        if e is None:
            continue
        cfg = sla_of(e)
        if cfg["target_pct"] is None or not cfg["alert_breach"]:
            continue
        s = await month_to_date(db, e, now)
        if s["availability"] is not None and s["availability"] < cfg["target_pct"]:
            want[f"sla:{cid}"] = {"node_id": None, "kind": "sla", "severity": "warning",
                                  "value": s["availability"],
                                  "detail": {"source": e.name, "customer_id": cid, "sla": True},
                                  "message": f"Uptime SLA of {e.name} is {s['availability']:.2f}% this month "
                                             f"(target {cfg['target_pct']:g}%)"}
    return want


# ------------------------------------------------------------------ output
async def stats(db: AsyncSession, checks: list[UptimeCheck], now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Availability over 24 hours, 7 and 30 days, plus the average response time of the last 24 hours."""
    now = now or datetime.now(UTC)
    out: dict[str, dict[str, Any]] = {c.id: {} for c in checks}
    if not checks:
        return out
    for key, days in (("24h", 1), ("7d", 7), ("30d", 30)):
        av = await availability(db, checks, now - timedelta(days=days), now, now=now)
        for cid, a in av.items():
            out[cid][f"uptime_{key}"] = a["availability"]
    rows = (await db.execute(text(
        "SELECT check_id, avg(ms) AS ms, count(*) FILTER (WHERE ok) AS good, count(*) FILTER (WHERE ok IS NOT NULL) AS n "
        "FROM uptime_results WHERE at >= :since AND check_id = ANY(:ids) GROUP BY check_id"),
        {"since": now - timedelta(hours=24), "ids": [c.id for c in checks]})).all()
    for r in rows:
        out[r.check_id]["avg_ms_24h"] = round(float(r.ms), 1) if r.ms is not None else None
        out[r.check_id]["runs_24h"] = int(r.n)
    return out


def check_out(c: UptimeCheck, names: dict[str, str] | None = None, st: dict[str, Any] | None = None) -> dict[str, Any]:
    names = names or {}
    exp = _aware(c.cert_expires_at)
    return {"id": c.id, "customer_id": c.customer_id, "customer_name": names.get(c.customer_id), "name": c.name,
            "kind": c.kind, "target": c.target, "port": c.port, "label": label(c), "config": c.config or {},
            "probe_node_id": c.probe_node_id, "probe_name": names.get(c.probe_node_id or "") if c.probe_node_id else None,
            "node_id": c.node_id, "node_name": names.get(c.node_id or "") if c.node_id else None,
            "enabled": c.enabled, "interval_seconds": c.interval_seconds, "timeout_seconds": c.timeout_seconds,
            "fail_after": c.fail_after, "slow_ms": c.slow_ms, "cert_warn_days": c.cert_warn_days,
            "severity": c.severity, "in_sla": c.in_sla, "status": c.status if c.enabled else "paused",
            "status_since": _aware(c.status_since).isoformat() if c.status_since else None,
            "failures": c.failures, "last_check_at": _aware(c.last_check_at).isoformat() if c.last_check_at else None,
            "last_ms": round(c.last_ms, 1) if c.last_ms is not None else None, "last_error": c.last_error,
            "last_code": c.last_code, "cert_expires_at": exp.isoformat() if exp else None,
            "cert_days": int((exp - datetime.now(UTC)).total_seconds() // 86400) if exp else None,
            "updated_at": _aware(c.updated_at).isoformat() if c.updated_at else None, "updated_by": c.updated_by,
            **(st or {})}


async def names(db: AsyncSession) -> dict[str, str]:
    out = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    out.update({n.id: n.name for n in (await db.execute(select(Node))).scalars()})
    return out


async def history(db: AsyncSession, c: UptimeCheck, range_: str = "24h", now: datetime | None = None) -> dict[str, Any]:
    if range_ not in RANGES:
        raise UptimeError("range must be one of 24h, 7d, 30d")
    now = now or datetime.now(UTC)
    back, bucket = RANGES[range_]
    since = now - back
    rows = (await db.execute(text(
        "SELECT date_bin(:bucket, at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS b, avg(ms) FILTER (WHERE ok) AS ms, "
        "max(ms) FILTER (WHERE ok) AS ms_max, count(*) FILTER (WHERE ok) AS good, "
        "count(*) FILTER (WHERE ok = false) AS bad FROM uptime_results "
        "WHERE check_id = :cid AND at >= :since GROUP BY 1 ORDER BY 1"), {"bucket": bucket, "cid": c.id, "since": since})).all()
    points = [{"at": r.b.isoformat(), "ms": round(float(r.ms), 1) if r.ms is not None else None,
               "ms_max": round(float(r.ms_max), 1) if r.ms_max is not None else None,
               "ok": int(r.good), "failed": int(r.bad)} for r in rows]
    recent = (await db.execute(select(UptimeResult).where(UptimeResult.check_id == c.id)
                               .order_by(UptimeResult.at.desc()).limit(30))).scalars()
    incs = (await db.execute(select(UptimeIncident).where(UptimeIncident.check_id == c.id)
                             .order_by(UptimeIncident.started_at.desc()).limit(50))).scalars()
    av = (await availability(db, [c], since, now, now=now)).get(c.id) or {}
    return {"range": range_, "bucket_seconds": int(bucket.total_seconds()), "points": points,
            "availability": av.get("availability"), "down_seconds": av.get("down_seconds"),
            "recent": [{"at": _aware(r.at).isoformat(), "ok": r.ok, "ms": round(r.ms, 1) if r.ms is not None else None,
                        "code": r.code, "error": r.error} for r in recent],
            "incidents": [incident_out(i, now) for i in incs]}


async def save_check(db: AsyncSession, c: UptimeCheck | None, *, by: str | None, customer_id: str, name: str,
                     kind: str, target: str, port: int | None = None, config: dict[str, Any] | None = None,
                     probe_node_id: str | None = None, node_id: str | None = None, enabled: bool = True,
                     interval_seconds: int = 60, timeout_seconds: int = 10, fail_after: int = 2,
                     slow_ms: int | None = None, cert_warn_days: int = 14, severity: str = "critical",
                     in_sla: bool = True) -> UptimeCheck:
    from mmn.services import tenancy

    name = (name or "").strip()
    if not name or len(name) > 120:
        raise UptimeError("name is required (at most 120 characters)")
    e = await db.get(Environment, customer_id)
    if e is None or not tenancy.is_allowed(customer_id):
        raise UptimeError("customer not found")
    target, port, cfg = clean_check(kind, target, port, config)
    if not 30 <= int(interval_seconds) <= 3600:
        raise UptimeError("interval must be 30-3600 seconds")
    if not 1 <= int(timeout_seconds) <= 60 or int(timeout_seconds) >= int(interval_seconds):
        raise UptimeError("timeout must be 1-60 seconds and shorter than the interval")
    if not 1 <= int(fail_after) <= 10:
        raise UptimeError("fail after must be 1-10 failed checks in a row")
    if slow_ms is not None and not 1 <= int(slow_ms) <= 120000:
        raise UptimeError("slow limit must be 1-120000 ms (empty = none)")
    if not 0 <= int(cert_warn_days) <= 120:
        raise UptimeError("certificate warning must be 0-120 days")
    if severity not in SEVERITIES:
        raise UptimeError("severity must be critical or warning")
    for nid, what in ((probe_node_id, "probe machine"), (node_id, "machine")):
        if nid:
            n = await db.get(Node, nid)
            if n is None or n.environment_id != customer_id:
                raise UptimeError(f"{what} not found at this customer")
    now = datetime.now(UTC)
    if c is None:
        c = UptimeCheck(created_by=by, created_at=now, status="unknown", failures=0)
        db.add(c)
    changed = c.kind != kind or c.target != target or c.port != port or (c.config or {}) != cfg or \
        c.probe_node_id != (probe_node_id or None)
    c.customer_id, c.name, c.kind, c.target, c.port, c.config = customer_id, name, kind, target, port, cfg
    c.probe_node_id, c.node_id, c.enabled = probe_node_id or None, node_id or None, bool(enabled)
    c.interval_seconds, c.timeout_seconds, c.fail_after = int(interval_seconds), int(timeout_seconds), int(fail_after)
    c.slow_ms, c.cert_warn_days, c.severity, c.in_sla = (int(slow_ms) if slow_ms else None), int(cert_warn_days), \
        severity, bool(in_sla)
    c.updated_at, c.updated_by = now, by
    if changed and c.id:
        # What is checked changed: start the state fresh (history and incidents stay).
        c.failures, c.failing_since, c.cert_expires_at, c.last_check_at = 0, None, None, None
        for inc in (await db.execute(select(UptimeIncident).where(UptimeIncident.check_id == c.id,
                                                                  UptimeIncident.ended_at.is_(None)))).scalars():
            inc.ended_at = now
        c.status, c.status_since = "unknown", now
    if not c.enabled:
        for inc in (await db.execute(select(UptimeIncident).where(UptimeIncident.check_id == c.id,
                                                                  UptimeIncident.ended_at.is_(None)))).scalars():
            inc.ended_at = now
        c.status, c.failures, c.failing_since = "unknown", 0, None
    await db.flush()
    return c


async def find_check(db: AsyncSession, ref: str) -> UptimeCheck:
    from sqlalchemy import func

    c = await db.get(UptimeCheck, ref)
    if c is None:
        c = (await db.execute(select(UptimeCheck).where(func.lower(UptimeCheck.name) == (ref or "").lower()))).scalars().first()
    if c is None:
        raise UptimeError("uptime check not found")
    return c


def transient(customer_id: str, kind: str, target: str, port: int | None, config: dict[str, Any] | None,
              timeout_seconds: int = 10) -> UptimeCheck:
    """An unsaved check, for "test" in the editor."""
    target, port, cfg = clean_check(kind, target, port, config)
    if not 1 <= int(timeout_seconds) <= 60:
        raise UptimeError("timeout must be 1-60 seconds")
    return UptimeCheck(id="test", customer_id=customer_id, name="test", kind=kind, target=target, port=port, config=cfg,
                       timeout_seconds=int(timeout_seconds), interval_seconds=60, fail_after=1,
                       cert_expires_at=None)
