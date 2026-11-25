"""Certificate monitoring per customer (phase 20).

Two kinds of places are watched:

* TLS endpoints: websites, mail servers (STARTTLS), SSL VPN portals, RDP gateways. Stackpilot connects
  itself, or lets the agent of a machine at the customer connect (``probe_node_id``) for addresses only
  reachable inside the network. HTTPS uptime checks and SSL VPN checks (phase 16) become endpoints
  automatically. Besides the expiry, the chain (trusted or not) and the host name are checked.
* Certificate stores of servers and Proxmox VE nodes: certificate files on Linux (Proxmox VE and Proxmox
  Backup Server, Let's Encrypt, nginx, Apache, Postfix, Dovecot, HAProxy and extra paths you add) and the
  LocalMachine "My" and "WebHosting" stores on Windows (certificates with a private key), with whether a
  certificate is bound to IIS/HTTP.sys or RDP.

Everything is read through the agent's existing ``exec`` command: the agent itself does not change.
Results are kept in ``certificates``; alerts (kind ``cert``) open in time before the expiry and the
nightly checks turn them into findings for the morning summary.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import logging
import re
import ssl
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import CertEndpoint, Certificate, Environment, Node, UptimeCheck
from mmn.services import agent_hub
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.certs")

STARTTLS = ("none", "smtp", "imap", "pop3", "ftp")
STATUSES = ("ok", "expiring", "critical", "expired", "error", "ignored")
HOST_RE = re.compile(r"^(?=.{1,253}$)(\*\.)?[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9_])?(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9_])?)*\.?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_./*@+-]{1,250}$")


class CertError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    d = _aware(d)
    return d.isoformat() if d else None


def clean_host(value: str) -> str:
    v = re.sub(r"^[a-z]+://", "", (value or "").strip(), flags=re.I).split("/")[0].strip().strip("[]")
    if not v:
        raise CertError("host is required")
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        pass
    if not HOST_RE.match(v) or ".." in v or v.startswith("*"):
        raise CertError(f"'{value}' is not a valid host name or IP address")
    return v.lower().rstrip(".")


def host_port(text: str) -> tuple[str, int] | None:
    """"host", "host:8443", "https://host/path" or "[::1]:443" to (host, port)."""
    text = re.sub(r"^[a-z]+://", "", (text or "").strip(), flags=re.I).split("/")[0].strip()
    m = re.fullmatch(r"\[([^\]]+)\](?::(\d{1,5}))?", text) or re.fullmatch(r"([^:\s]+)(?::(\d{1,5}))?", text)
    if not m:
        return None
    return m.group(1), int(m.group(2) or 443)


def clean_paths(paths: list[str] | None) -> list[str]:
    out = []
    for p in paths or []:
        p = str(p).strip()
        if not p:
            continue
        if not PATH_RE.match(p) or ".." in p:
            raise CertError(f"'{p}' is not an absolute path (letters, digits and / . _ - * only)")
        out.append(p)
    return out[:50]


def cn_of(subject: str | None) -> str:
    m = re.search(r"(?:^|[,/]\s*)CN\s*=\s*([^,/]+)", subject or "")
    return m.group(1).strip() if m else (subject or "").strip()


def name_matches(host: str, names: list[str]) -> bool:
    """RFC 6125 matching of a host name against the certificate's DNS names (one wildcard label)."""
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h)
        return any(n.lower() == str(ip) for n in names)
    except ValueError:
        pass
    for n in names:
        n = n.lower().rstrip(".")
        if n == h:
            return True
        if n.startswith("*.") and "." in h:
            if h.split(".", 1)[1] == n[2:] and h.split(".", 1)[0]:
                return True
    return False


def key_text(pub: Any) -> str | None:
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa

    if isinstance(pub, rsa.RSAPublicKey):
        return f"RSA {pub.key_size}"
    if isinstance(pub, ec.EllipticCurvePublicKey):
        return f"EC {pub.key_size}"
    if isinstance(pub, dsa.DSAPublicKey):
        return f"DSA {pub.key_size}"
    if isinstance(pub, ed25519.Ed25519PublicKey):
        return "Ed25519"
    if isinstance(pub, ed448.Ed448PublicKey):
        return "Ed448"
    return None


def cert_info(der: bytes) -> dict[str, Any]:
    """The fields Stackpilot keeps of a DER certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    c = x509.load_der_x509_certificate(der)
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(x) for x in san.get_values_for_type(x509.DNSName)]
        sans += [str(x) for x in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        sans = []
    subject = c.subject.rfc4514_string()
    issuer = c.issuer.rfc4514_string()
    try:
        pub = key_text(c.public_key())
    except Exception:  # noqa: BLE001 - unusual key types
        pub = None
    return {"subject": subject[:500], "common_name": cn_of(subject)[:255] or None, "sans": sans[:100],
            "issuer": issuer[:500], "serial": format(c.serial_number, "X")[:100],
            "fingerprint": c.fingerprint(hashes.SHA256()).hex().upper(),
            "not_before": c.not_valid_before_utc, "not_after": c.not_valid_after_utc, "key_info": pub,
            "self_signed": c.subject == c.issuer}


def pem_blocks(text: str) -> list[bytes]:
    out = []
    for m in re.finditer(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", text or "", re.S):
        try:
            out.append(base64.b64decode("".join(m.group(1).split())))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------------ endpoints: Stackpilot itself
async def _starttls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, proto: str, timeout: float) -> None:
    async def line() -> str:
        return (await asyncio.wait_for(reader.readline(), timeout)).decode("latin-1", "replace")

    async def until(pred) -> str:
        for _ in range(50):
            text = await line()
            if not text:
                raise CertError("the server closed the connection before STARTTLS")
            if pred(text):
                return text
        raise CertError("no answer to STARTTLS")

    async def send(text: str) -> None:
        writer.write(text.encode())
        await writer.drain()

    if proto == "smtp":
        await until(lambda t: t[:3].isdigit() and t[3:4] != "-")
        await send("EHLO stackpilot.local\r\n")
        await until(lambda t: t[:3].isdigit() and t[3:4] != "-")
        await send("STARTTLS\r\n")
        t = await until(lambda t: t[:3].isdigit() and t[3:4] != "-")
        if not t.startswith("220"):
            raise CertError(f"STARTTLS refused: {t.strip()[:100]}")
    elif proto == "imap":
        await line()
        await send("a1 STARTTLS\r\n")
        t = await until(lambda t: t.startswith("a1 "))
        if not t.upper().startswith("A1 OK"):
            raise CertError(f"STARTTLS refused: {t.strip()[:100]}")
    elif proto == "pop3":
        await line()
        await send("STLS\r\n")
        t = await line()
        if not t.startswith("+OK"):
            raise CertError(f"STLS refused: {t.strip()[:100]}")
    elif proto == "ftp":
        await until(lambda t: t[:3].isdigit() and t[3:4] != "-")
        await send("AUTH TLS\r\n")
        t = await until(lambda t: t[:3].isdigit() and t[3:4] != "-")
        if not t.startswith("234"):
            raise CertError(f"AUTH TLS refused: {t.strip()[:100]}")


async def _handshake(host: str, port: int, sni: str, starttls: str, verify: bool, timeout: float) -> bytes:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = False  # the name is checked separately (one clear error per problem)
    server_name = None if _is_ip(sni) else sni
    if starttls == "none":
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=server_name),
                                           timeout)
    else:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        try:
            await _starttls(reader, writer, starttls, timeout)
            await asyncio.wait_for(writer.start_tls(ctx, server_hostname=server_name), timeout)
        except BaseException:
            writer.close()
            raise
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        raise CertError("the server sent no certificate")
    return der


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _short(e: BaseException) -> str:
    if isinstance(e, ssl.SSLCertVerificationError):
        return (e.verify_message or str(e))[:300]
    if isinstance(e, TimeoutError | asyncio.TimeoutError):
        return "timed out"
    return (str(e) or type(e).__name__)[:300]


async def fetch_direct(host: str, port: int, sni: str | None = None, starttls: str = "none",
                       timeout: float = 10) -> dict[str, Any]:
    """Connect from Stackpilot: the certificate, whether the chain is trusted and the name matches."""
    sni = sni or host
    trusted, trust_error = True, None
    try:
        der = await _handshake(host, port, sni, starttls, True, timeout)
    except ssl.SSLCertVerificationError as e:
        trusted, trust_error = False, _short(e)
        der = await _handshake(host, port, sni, starttls, False, timeout)
    info = cert_info(der)
    return {**info, "trusted": trusted, "trust_error": trust_error,
            "hostname_ok": name_matches(sni, info["sans"] or [info["common_name"] or ""])}


# ------------------------------------------------------------------ endpoints: through an agent
def _q(v: str) -> str:
    """A host name, IP address or server name for a shell or PowerShell script (validated, no quoting needed)."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", v or ""):
        raise CertError(f"'{v}' cannot be used in a probe script")
    return v


def probe_script(windows: bool, host: str, port: int, sni: str, starttls: str) -> str:
    host, sni = _q(host), _q(sni)
    port = int(port)
    if windows:
        if starttls != "none":
            raise CertError("STARTTLS through a Windows machine is not supported; use a Linux machine or Stackpilot")
        return (
            "$ErrorActionPreference='Stop'\n"
            "try {\n"
            f"  $c=New-Object Net.Sockets.TcpClient; $iar=$c.BeginConnect('{host}',{port},$null,$null)\n"
            "  if(-not $iar.AsyncWaitHandle.WaitOne(10000)){ throw 'timed out' }\n"
            "  $c.EndConnect($iar); $c.ReceiveTimeout=10000; $c.SendTimeout=10000\n"
            "  $script:verr='None'\n"
            "  $cb=[Net.Security.RemoteCertificateValidationCallback]{param($a,$b,$ch,$e) $script:verr=[string]$e; $true}\n"
            "  $s=New-Object Net.Security.SslStream($c.GetStream(),$false,$cb)\n"
            f"  $s.AuthenticateAsClient('{sni}',$null,[Security.Authentication.SslProtocols]4032,$false)\n"
            "  $x=New-Object Security.Cryptography.X509Certificates.X509Certificate2($s.RemoteCertificate)\n"
            "  \"VERIFY`t$script:verr\"\n"
            "  '-----BEGIN CERTIFICATE-----'; [Convert]::ToBase64String($x.RawData,'InsertLineBreaks'); "
            "'-----END CERTIFICATE-----'\n"
            "  $s.Dispose(); $c.Close()\n"
            "} catch { \"ERROR`t$($_.Exception.Message)\" }\n")
    st = f" -starttls {starttls}" if starttls != "none" else ""
    return (
        "set +e\ncommand -v openssl >/dev/null 2>&1 || { printf 'ERROR\\topenssl is not installed\\n'; exit 0; }\n"
        f"out=$(echo | timeout 20 openssl s_client -connect '{host}:{port}' -servername '{sni}'{st} 2>&1)\n"
        "code=$(printf '%s\\n' \"$out\" | sed -n 's/^ *Verify return code: *//p' | tail -n 1)\n"
        "printf 'VERIFY\\t%s\\n' \"$code\"\n"
        "printf '%s\\n' \"$out\" | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' | "
        "awk '/-----END CERTIFICATE-----/{print; exit} {print}'\n"
        "printf '%s\\n' \"$out\" | grep -q 'BEGIN CERTIFICATE' || printf 'ERROR\\t%s\\n' \"$(printf '%s' \"$out\" | "
        "grep -iE 'errno|error|refused|timed out|unknown|connect:' | head -n 1 | cut -c1-200)\"\n"
        "exit 0\n")


def parse_probe(stdout: str, sni: str) -> dict[str, Any]:
    err = verify = None
    for ln in (stdout or "").splitlines():
        parts = ln.rstrip("\r").split("\t", 1)
        if parts[0] == "ERROR" and len(parts) > 1:
            err = parts[1].strip() or "the connection failed"
        elif parts[0] == "VERIFY" and len(parts) > 1:
            verify = parts[1].strip()
    ders = pem_blocks(stdout)
    if not ders:
        raise CertError(err or "no certificate received")
    info = cert_info(ders[0])
    if verify is None:
        trusted, trust_error = None, None
    elif verify.startswith("0 ") or verify in ("0", "None"):
        trusted, trust_error = True, None
    elif verify == "RemoteCertificateNameMismatch":
        trusted, trust_error = True, None  # the name is checked separately below
    else:
        trusted, trust_error = False, verify.replace("RemoteCertificateNameMismatch", "").strip(", ")[:300] or verify[:300]
    return {**info, "trusted": trusted, "trust_error": trust_error,
            "hostname_ok": name_matches(sni, info["sans"] or [info["common_name"] or ""])}


async def fetch_via_agent(node: Node, host: str, port: int, sni: str | None, starttls: str) -> dict[str, Any]:
    if not agent_hub.is_online(node.id):
        raise CertError(f"the agent of {node.name} is not connected")
    script = probe_script(node.kind == "windows", host, port, sni or host, starttls)
    res = await agent_hub.request(node.id, "exec", {"command": script, "timeout": 45}, timeout=70) or {}
    return parse_probe(res.get("stdout") or "", sni or host)


# ------------------------------------------------------------------ certificate stores on machines
LINUX_SCRIPT = r"""set +e
command -v openssl >/dev/null 2>&1 || { printf 'ERROR\topenssl is not installed\n'; exit 0; }
files=""
add() { [ -f "$1" ] && files="$files
$1"; }
for f in /etc/pve/local/pveproxy-ssl.pem /etc/pve/local/pve-ssl.pem /etc/proxmox-backup/proxy.pem /etc/letsencrypt/live/*/cert.pem __EXTRA__; do add "$f"; done
for d in /etc/nginx /etc/apache2 /etc/httpd; do
  [ -d "$d" ] || continue
  for f in $(grep -rhoE '^[[:space:]]*(ssl_certificate|SSLCertificateFile)[[:space:]]+[^;#[:space:]]+' "$d" 2>/dev/null | awk '{print $2}' | tr -d '";'); do add "$f"; done
done
if command -v postconf >/dev/null 2>&1; then
  for f in $(postconf -h smtpd_tls_cert_file smtpd_tls_chain_files 2>/dev/null | tr ', ' '\n\n'); do add "$f"; done
fi
[ -d /etc/dovecot ] && for f in $(grep -rhoE '^[[:space:]]*ssl_cert[[:space:]]*=[[:space:]]*<[^[:space:]]+' /etc/dovecot 2>/dev/null | sed 's/.*<//'); do add "$f"; done
if [ -f /etc/haproxy/haproxy.cfg ]; then
  for f in $(grep -oE '[[:space:]]crt[[:space:]]+[^[:space:]]+' /etc/haproxy/haproxy.cfg | awk '{print $2}'); do
    if [ -d "$f" ]; then for g in "$f"/*.pem; do add "$g"; done; else add "$f"; fi
  done
fi
printf '%s\n' "$files" | sort -u | while read -r f; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  out=$(openssl x509 -noout -enddate -startdate -serial -fingerprint -sha256 -subject -issuer -nameopt RFC2253 -ext subjectAltName -in "$f" 2>/dev/null) || \
  out=$(openssl x509 -noout -enddate -startdate -serial -fingerprint -sha256 -subject -issuer -nameopt RFC2253 -in "$f" 2>/dev/null)
  [ -n "$out" ] || continue
  key=$(openssl x509 -noout -text -in "$f" 2>/dev/null | grep -m 2 -E 'Public Key Algorithm|Public-Key:' | tr -s ' ' | tr '\n' ' ')
  printf 'BEGIN\t%s\n%s\nKEY=%s\nEND\n' "$f" "$out" "$key"
done
command -v certbot >/dev/null 2>&1 && printf 'RENEW\tcertbot\n'
command -v pvenode >/dev/null 2>&1 && pvenode config get --property acme 2>/dev/null | grep -q . && printf 'RENEW\tpve-acme\n'
exit 0
"""

WINDOWS_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
$used = @{}
foreach ($m in [regex]::Matches(((netsh http show sslcert) -join "`n"), '[0-9a-fA-F]{40}')) { $used[$m.Value] = 1 }
$rdp = (Get-CimInstance -Namespace root\cimv2\TerminalServices -ClassName Win32_TSGeneralSetting -Filter "TerminalName='RDP-tcp'").SSLCertificateSHA1Hash
if ($rdp) { $used[$rdp] = 1 }
foreach ($st in 'My', 'WebHosting') {
  Get-ChildItem "Cert:\LocalMachine\$st" | Where-Object { $_.HasPrivateKey } | ForEach-Object {
    $alg = $_.PublicKey.Oid.FriendlyName
    $bits = ''; try { $bits = $_.PublicKey.Key.KeySize } catch { }
    $tpl = @($_.Extensions | Where-Object { $_.Oid.Value -eq '1.3.6.1.4.1.311.21.7' -or $_.Oid.Value -eq '1.3.6.1.4.1.311.20.2' }).Count
    $dns = (@($_.DnsNameList | ForEach-Object { $_.Unicode }) -join ';')
    "CERT`t$st`t$($_.Thumbprint)`t$($_.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`t$($_.NotBefore.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`t$($_.Subject)`t$($_.Issuer)`t$($_.SerialNumber)`t$dns`t$alg $bits`t$([int]$used.ContainsKey($_.Thumbprint))`t$tpl"
  }
}
"""


def linux_script(extra_paths: list[str] | None = None) -> str:
    return LINUX_SCRIPT.replace("__EXTRA__", " ".join(clean_paths(extra_paths)))


def _date(text: str) -> datetime | None:
    text = " ".join((text or "").split())
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _key_linux(text: str) -> str | None:
    t = text or ""
    m = re.search(r"\((\d+) bit\)", t)
    bits = m.group(1) if m else ""
    if "rsaEncryption" in t:
        return f"RSA {bits}".strip()
    if "id-ecPublicKey" in t:
        return f"EC {bits}".strip()
    if "ED25519" in t.upper():
        return "Ed25519"
    return None


def parse_linux(stdout: str) -> tuple[list[dict[str, Any]], set[str], str | None]:
    """(certificates, renew tools, error) from LINUX_SCRIPT."""
    certs: list[dict[str, Any]] = []
    renew: set[str] = set()
    error = None
    cur: dict[str, Any] | None = None
    san_next = False
    for raw in (stdout or "").splitlines():
        line = raw.rstrip("\r")
        if line.startswith("ERROR\t"):
            error = line.split("\t", 1)[1].strip()
        elif line.startswith("RENEW\t"):
            renew.add(line.split("\t", 1)[1].strip())
        elif line.startswith("BEGIN\t"):
            cur = {"location": line.split("\t", 1)[1].strip(), "sans": []}
            san_next = False
        elif line == "END" and cur is not None:
            if cur.get("not_after"):
                cur["common_name"] = cn_of(cur.get("subject"))[:255] or None
                cur["self_signed"] = bool(cur.get("subject")) and cur.get("subject") == cur.get("issuer")
                certs.append(cur)
            cur = None
        elif cur is not None:
            s = line.strip()
            if san_next:
                san_next = False
                cur["sans"] = [x.strip().split(":", 1)[1] for x in s.split(",")
                               if x.strip().startswith(("DNS:", "IP Address:"))][:100]
                continue
            k, _, v = s.partition("=")
            kl = k.lower()
            if kl == "notafter":
                cur["not_after"] = _date(v)
            elif kl == "notbefore":
                cur["not_before"] = _date(v)
            elif kl == "serial":
                cur["serial"] = v.strip()[:100]
            elif kl.endswith("fingerprint"):
                cur["fingerprint"] = v.replace(":", "").strip().upper()[:100]
            elif kl == "subject":
                cur["subject"] = v.strip()[:500]
            elif kl == "issuer":
                cur["issuer"] = v.strip()[:500]
            elif k == "KEY":
                cur["key_info"] = _key_linux(v)
            elif s.startswith("X509v3 Subject Alternative Name"):
                san_next = True
    return certs, renew, error


def _win_dn(dn: str) -> str:
    """"CN=x, O=y" as written by .NET: keep it, only trim."""
    return (dn or "").strip()[:500]


def parse_windows(stdout: str) -> list[dict[str, Any]]:
    """Certificates of WINDOWS_SCRIPT. Of certificates with the same subject in a store (a renewed one next
    to the old one) the one in use, else the newest, counts."""
    certs: list[dict[str, Any]] = []
    for raw in (stdout or "").splitlines():
        p = raw.rstrip("\r").split("\t")
        if p[0] != "CERT" or len(p) < 8:
            continue
        p += [""] * (13 - len(p))
        end = _date(p[3])
        if not end:
            continue
        subject = _win_dn(p[5])
        alg, _, bits = p[9].strip().partition(" ")
        key = None
        if alg:
            key = ("RSA" if alg.upper().startswith("RSA") else "EC" if alg.upper().startswith("EC") else alg) + \
                  (f" {bits.strip()}" if bits.strip() else "")
        certs.append({"location": f"Cert:\\LocalMachine\\{p[1]}", "store": p[1], "fingerprint": p[2].upper()[:100],
                      "not_after": end, "not_before": _date(p[4]), "subject": subject,
                      "common_name": cn_of(subject)[:255] or None, "issuer": _win_dn(p[6]), "serial": p[7][:100],
                      "sans": [x for x in p[8].split(";") if x][:100], "key_info": key, "in_use": p[10].strip() == "1",
                      "renew": "win-autoenroll" if p[11].strip() not in ("", "0") else None,
                      "self_signed": bool(subject) and subject == _win_dn(p[6])})
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for c in certs:
        k = (c["store"], c["subject"] or c["fingerprint"])
        o = best.get(k)
        if o is None or (c["in_use"], c["not_after"]) > (o["in_use"], o["not_after"]):
            best[k] = c
    return list(best.values())


def machine_key(node_id: str, c: dict[str, Any]) -> str:
    if c.get("store"):
        h = hashlib.sha1((c.get("subject") or c["fingerprint"]).encode()).hexdigest()[:16]
        return f"node:{node_id}:{c['store']}:{h}"
    return f"node:{node_id}:{c['location']}"[:400]


async def scan_machine(n: Node, extra_paths: list[str] | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    script = WINDOWS_SCRIPT if n.kind == "windows" else linux_script(extra_paths)
    res = await agent_hub.request(n.id, "exec", {"command": script, "timeout": 90}, timeout=120) or {}
    if n.kind == "windows":
        return parse_windows(res.get("stdout") or ""), set()
    certs, renew, err = parse_linux(res.get("stdout") or "")
    if err and not certs:
        raise CertError(err)
    for c in certs:
        if c["location"].startswith("/etc/letsencrypt/") and "certbot" in renew:
            c["renew"] = "certbot"
        elif c["location"] == "/etc/pve/local/pveproxy-ssl.pem" and "pve-acme" in renew:
            c["renew"] = "pve-acme"
    return certs, renew


# ------------------------------------------------------------------ settings
DEFAULTS: dict[str, Any] = {
    "cert_interval_hours": 6,
    "cert_warn_days": 21,
    "cert_critical_days": 7,
    "alert_certs": True,
    "alert_untrusted": True,
    "uptime_endpoints": True,
    "extra_paths": [],
}


async def settings(db: AsyncSession) -> dict[str, Any]:
    return {**DEFAULTS, **{k: v for k, v in (await get_setting(db, "netsec")).items() if k in DEFAULTS}}


# ------------------------------------------------------------------ storing
def _apply(row: Certificate, info: dict[str, Any], now: datetime) -> None:
    old_fp = row.fingerprint
    if old_fp and info.get("fingerprint") and old_fp != info["fingerprint"]:
        if row.not_after and info.get("not_after") and info["not_after"] > _aware(row.not_after):
            row.previous_not_after = row.not_after
            row.renewed_at = now
    for k in ("subject", "common_name", "sans", "issuer", "serial", "fingerprint", "not_before", "not_after",
              "key_info", "self_signed", "trusted", "trust_error", "hostname_ok", "in_use", "renew"):
        if k in info:
            setattr(row, k, info[k])
    row.last_seen = now


async def _upsert(db: AsyncSession, customer_id: str, key: str, base: dict[str, Any], info: dict[str, Any],
                  now: datetime) -> Certificate:
    row = (await db.execute(select(Certificate).where(Certificate.customer_id == customer_id,
                                                      Certificate.key == key))).scalar_one_or_none()
    if row is None:
        row = Certificate(customer_id=customer_id, key=key, first_seen=now, sans=[], **base)
        db.add(row)
    else:
        for k, v in base.items():
            setattr(row, k, v)
    _apply(row, info, now)
    return row


async def check_endpoint(ep_id: str) -> dict[str, Any]:
    """Check one endpoint now and store the result."""
    async with sessionmaker()() as db:
        ep = await db.get(CertEndpoint, ep_id)
        if ep is None:
            raise CertError("endpoint not found")
        probe = await db.get(Node, ep.probe_node_id) if ep.probe_node_id else None
        now = datetime.now(UTC)
        try:
            if probe is not None:
                info = await fetch_via_agent(probe, ep.host, ep.port, ep.sni, ep.starttls)
            else:
                info = await fetch_direct(ep.host, ep.port, ep.sni, ep.starttls)
        except Exception as e:  # noqa: BLE001
            ep.last_check_at, ep.last_error = now, _short(e)
            await db.commit()
            return {"ok": False, "error": ep.last_error}
        ep.last_check_at, ep.last_error = now, None
        row = await _upsert(db, ep.customer_id, f"ep:{ep.id}",
                            {"source": "endpoint", "endpoint_id": ep.id, "node_id": None,
                             "location": endpoint_label(ep)}, info, now)
        await db.commit()
        return {"ok": True, "certificate": cert_out(row)}


async def scan_node(node_id: str, extra_paths: list[str] | None = None) -> dict[str, Any]:
    """Read the certificate store of one machine and store what it has (certificates no longer there go)."""
    async with sessionmaker()() as db:
        n = await db.get(Node, node_id)
        if n is None:
            raise CertError("machine not found")
        if extra_paths is None:
            extra_paths = (await settings(db))["extra_paths"]
        now = datetime.now(UTC)
        certs, _ = await scan_machine(n, extra_paths)
        keys = []
        for c in certs:
            key = machine_key(n.id, c)
            keys.append(key)
            c.pop("store", None)
            loc = c.pop("location")
            await _upsert(db, n.environment_id, key, {"source": "machine", "node_id": n.id, "endpoint_id": None,
                                                      "location": loc[:500]}, c, now)
        await db.execute(delete(Certificate).where(Certificate.node_id == n.id, Certificate.source == "machine",
                                                   Certificate.key.not_in(keys or [""])))
        await db.commit()
        return {"node": n.name, "certificates": len(keys)}


async def sync_uptime_endpoints(db: AsyncSession) -> None:
    """HTTPS uptime checks and SSL VPN checks become endpoints (and go again when the check goes or changes)."""
    if not (await settings(db))["uptime_endpoints"]:
        await db.execute(delete(CertEndpoint).where(CertEndpoint.source == "uptime"))
        return
    have = {e.uptime_check_id: e for e in (await db.execute(
        select(CertEndpoint).where(CertEndpoint.source == "uptime"))).scalars()}
    seen = set()
    for c in (await db.execute(select(UptimeCheck))).scalars():
        hp = None
        if c.kind == "http" and str(c.target).lower().startswith("https://"):
            from urllib.parse import urlsplit

            u = urlsplit(c.target)
            if u.hostname:
                hp = (u.hostname, u.port or 443)
        elif c.kind == "vpn" and (c.config or {}).get("mode", "ssl") == "ssl":
            hp = (c.target, c.port or 443)
        if hp is None:
            continue
        seen.add(c.id)
        ep = have.get(c.id)
        if ep is None:
            ep = CertEndpoint(customer_id=c.customer_id, source="uptime", uptime_check_id=c.id, created_by="uptime")
            db.add(ep)
        ep.name, ep.host, ep.port, ep.probe_node_id = c.name[:120], hp[0][:255], int(hp[1]), c.probe_node_id
        ep.enabled = bool(c.enabled)
    for cid, ep in have.items():
        if cid not in seen:
            await db.delete(ep)


async def sync_nightly_endpoints(db: AsyncSession, customer_id: str, endpoints: list[str]) -> None:
    """The address list of the nightly checks (phase 12) is kept as endpoints of source "nightly"."""
    want = {}
    for text in endpoints or []:
        hp = host_port(text)
        if hp:
            want[(hp[0].lower(), hp[1])] = hp
    rows = list((await db.execute(select(CertEndpoint).where(CertEndpoint.customer_id == customer_id))).scalars())
    have = {(r.host.lower(), r.port): r for r in rows}
    for k, (h, p) in want.items():
        if k not in have:
            db.add(CertEndpoint(customer_id=customer_id, host=h, port=p, source="nightly", created_by="nightly"))
    for k, r in have.items():
        if r.source == "nightly" and k not in want:
            await db.delete(r)


_last_run: dict[str, float] = {}
_running: set[str] = set()


async def refresh_customer(env_id: str) -> dict[str, Any]:
    """Check every endpoint and read every server's certificate store of one customer."""
    if env_id in _running:
        return {"busy": True}
    _running.add(env_id)
    try:
        return await _refresh_customer(env_id)
    finally:
        _running.discard(env_id)
        _last_run[env_id] = asyncio.get_running_loop().time()


async def _refresh_customer(env_id: str) -> dict[str, Any]:
    from mmn.services.customers import machine_type

    async with sessionmaker()() as db:
        await sync_uptime_endpoints(db)
        await db.commit()
        cfg = await settings(db)
        eps = [e.id for e in (await db.execute(select(CertEndpoint).where(
            CertEndpoint.customer_id == env_id, CertEndpoint.enabled.is_(True)))).scalars()]
        nodes = [n.id for n in (await db.execute(select(Node).where(Node.environment_id == env_id))).scalars()
                 if machine_type(n) != "workstation" and agent_hub.is_online(n.id)]
    sem = asyncio.Semaphore(6)
    errors: list[str] = []

    async def one(coro):
        async with sem:
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                errors.append(_short(e))
                return None

    res = await asyncio.gather(*(one(check_endpoint(i)) for i in eps),
                               *(one(scan_node(i, cfg["extra_paths"])) for i in nodes))
    return {"endpoints": len(eps), "machines": len(nodes), "failed": sum(1 for r in res if r is None or
                                                                        (isinstance(r, dict) and r.get("ok") is False)),
            "errors": errors[:10]}


async def tick() -> int:
    """Refresh the customers whose certificates were read longer ago than the interval."""
    async with sessionmaker()() as db:
        cfg = await settings(db)
        ids = [e.id for e in (await db.execute(select(Environment))).scalars()]
    now = asyncio.get_running_loop().time()
    every = max(1, int(cfg["cert_interval_hours"])) * 3600
    due = [i for i in ids if i not in _running and now - _last_run.get(i, -1e12) >= every]
    for i in due[:3]:
        agent_hub.spawn(refresh_customer(i))
    return len(due[:3])


# ------------------------------------------------------------------ status, output
def warn_days_of(c: Certificate, ep: CertEndpoint | None, cfg: dict[str, Any]) -> int:
    if ep is not None and ep.warn_days is not None:
        return int(ep.warn_days)
    return int(cfg["cert_warn_days"])


def status_of(c: Certificate, now: datetime, warn: int, critical: int) -> str:
    if c.ignored:
        return "ignored"
    end = _aware(c.not_after)
    if end is None:
        return "error"
    days = (end - now).total_seconds() / 86400
    if days < 0:
        return "expired"
    if days <= critical:
        return "critical"
    if days <= warn:
        return "expiring"
    return "ok"


def days_left(c: Certificate, now: datetime | None = None) -> int | None:
    end = _aware(c.not_after)
    if end is None:
        return None
    return int((end - (now or datetime.now(UTC))).total_seconds() // 86400)


def endpoint_label(ep: CertEndpoint) -> str:
    h = f"[{ep.host}]" if ":" in ep.host else ep.host
    return f"{h}:{ep.port}" + (f" ({ep.starttls})" if ep.starttls != "none" else "")


def cert_out(c: Certificate, *, status: str | None = None, names: dict[str, str] | None = None,
             ep: CertEndpoint | None = None) -> dict[str, Any]:
    return {"id": c.id, "customer_id": c.customer_id, "source": c.source, "endpoint_id": c.endpoint_id,
            "node_id": c.node_id, "node_name": (names or {}).get(c.node_id or ""),
            "endpoint_name": ep.name if ep else None, "location": c.location, "subject": c.subject,
            "common_name": c.common_name, "sans": c.sans or [], "issuer": c.issuer, "issuer_name": cn_of(c.issuer),
            "serial": c.serial, "fingerprint": c.fingerprint, "not_before": _iso(c.not_before),
            "not_after": _iso(c.not_after), "days_left": days_left(c), "key_info": c.key_info,
            "self_signed": c.self_signed, "trusted": c.trusted, "trust_error": c.trust_error,
            "hostname_ok": c.hostname_ok, "in_use": c.in_use, "renew": c.renew, "ignored": c.ignored,
            "ignored_note": c.ignored_note, "previous_not_after": _iso(c.previous_not_after),
            "renewed_at": _iso(c.renewed_at), "first_seen": _iso(c.first_seen), "last_seen": _iso(c.last_seen),
            "status": status}


def endpoint_out(ep: CertEndpoint, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": ep.id, "customer_id": ep.customer_id, "name": ep.name, "host": ep.host, "port": ep.port,
            "sni": ep.sni, "starttls": ep.starttls, "probe_node_id": ep.probe_node_id,
            "probe_name": (names or {}).get(ep.probe_node_id or ""), "source": ep.source,
            "uptime_check_id": ep.uptime_check_id, "warn_days": ep.warn_days, "enabled": ep.enabled,
            "last_check_at": _iso(ep.last_check_at), "last_error": ep.last_error, "label": endpoint_label(ep)}


async def node_names(db: AsyncSession) -> dict[str, str]:
    return {n.id: n.name for n in (await db.execute(select(Node))).scalars()}


async def listing(db: AsyncSession, customer_id: str | None = None, status: str | None = None,
                  within_days: int | None = None, source: str | None = None) -> dict[str, Any]:
    cfg = await settings(db)
    now = datetime.now(UTC)
    q = select(Certificate).order_by(Certificate.not_after)
    eq = select(CertEndpoint).order_by(CertEndpoint.host)
    if customer_id:
        q = q.where(Certificate.customer_id == customer_id)
        eq = eq.where(CertEndpoint.customer_id == customer_id)
    eps = {e.id: e for e in (await db.execute(eq)).scalars()}
    names = await node_names(db)
    cust = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    out, counts = [], dict.fromkeys(STATUSES, 0)
    for c in (await db.execute(q)).scalars():
        ep = eps.get(c.endpoint_id or "")
        st = status_of(c, now, warn_days_of(c, ep, cfg), int(cfg["cert_critical_days"]))
        counts[st] = counts.get(st, 0) + 1
        if status and st != status:
            continue
        if source and c.source != source:
            continue
        d = days_left(c, now)
        if within_days is not None and (d is None or d > within_days):
            continue
        o = cert_out(c, status=st, names=names, ep=ep)
        o["customer_name"] = cust.get(c.customer_id)
        out.append(o)
    with_cert = {c["endpoint_id"] for c in out if c["endpoint_id"]}
    errors = [{**endpoint_out(e, names), "customer_name": cust.get(e.customer_id)}
              for e in eps.values() if e.enabled and e.last_error]
    counts["error"] = len(errors)
    return {"certificates": out, "counts": counts, "endpoint_errors": errors,
            "endpoints_without_certificate": [endpoint_out(e, names) for e in eps.values()
                                              if e.id not in with_cert and not e.last_error and e.enabled
                                              and not status and not source],
            "warn_days": int(cfg["cert_warn_days"]), "critical_days": int(cfg["cert_critical_days"])}


# ------------------------------------------------------------------ alerts and findings
async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    cfg = await settings(db)
    want: dict[str, dict[str, Any]] = {}
    if not cfg["alert_certs"] and not cfg["alert_untrusted"]:
        return want
    eps = {e.id: e for e in (await db.execute(select(CertEndpoint))).scalars()}
    # HTTPS uptime checks with their own certificate alert (phase 16) are not alerted twice.
    uptime_alerting = {c.id for c in (await db.execute(select(UptimeCheck).where(
        UptimeCheck.cert_warn_days > 0, UptimeCheck.enabled.is_(True)))).scalars()}
    cust = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    for c in (await db.execute(select(Certificate).where(Certificate.ignored.is_(False)))).scalars():
        ep = eps.get(c.endpoint_id or "")
        if c.source == "endpoint" and (ep is None or not ep.enabled):
            continue
        where = c.location
        label = c.common_name or c.location
        base = {"source": f"{cust.get(c.customer_id, '?')} · certificates", "customer_id": c.customer_id,
                "certificate_id": c.id, "location": where}
        st = status_of(c, now, warn_days_of(c, ep, cfg), int(cfg["cert_critical_days"]))
        dup = ep is not None and ep.uptime_check_id in uptime_alerting
        # Windows: an old certificate that nothing uses (a renewal sits next to it) is no alert.
        unused_old = c.in_use is False and c.renew == "win-autoenroll"
        if cfg["alert_certs"] and st in ("expiring", "critical", "expired") and not dup and not unused_old:
            d = days_left(c, now) or 0
            what = f"{label} ({where})" if label != where else where
            want[f"cert:{c.id}"] = {
                "node_id": c.node_id, "kind": "cert", "severity": "warning" if st == "expiring" else "critical",
                "value": d, "detail": {**base, "days": d},
                "message": (f"Certificate {what} expired {-d} day(s) ago" if st == "expired" else
                            f"Certificate {what} expires in {d} day(s)")}
        if cfg["alert_untrusted"] and c.source == "endpoint" and (c.trusted is False or c.hostname_ok is False) \
                and st != "expired":
            why = "is not trusted" + (f" ({c.trust_error})" if c.trust_error else "") if c.trusted is False \
                else "does not match the host name"
            want[f"cert-trust:{c.id}"] = {"node_id": None, "kind": "cert", "severity": "warning",
                                          "detail": base, "message": f"The certificate of {where} {why}"[:500]}
    return want


async def findings(db: AsyncSession, e: Environment, now: datetime, finding, warn_days: int | None = None) -> list[dict[str, Any]]:
    """Findings of the nightly checks: certificates that expire, untrusted or wrong certificates on
    endpoints and endpoints that cannot be checked."""
    cfg = await settings(db)
    names = await node_names(db)
    eps = {x.id: x for x in (await db.execute(select(CertEndpoint).where(CertEndpoint.customer_id == e.id))).scalars()}
    out: list[dict[str, Any]] = []
    warn_default = int(warn_days or cfg["cert_warn_days"])
    for c in (await db.execute(select(Certificate).where(Certificate.customer_id == e.id,
                                                        Certificate.ignored.is_(False)))).scalars():
        ep = eps.get(c.endpoint_id or "")
        if c.source == "endpoint" and (ep is None or not ep.enabled):
            continue
        if c.in_use is False and c.renew == "win-autoenroll":
            continue
        target = names.get(c.node_id or "", "?") if c.source == "machine" else c.location
        name = c.common_name or c.location
        if c.source == "machine" and c.location.startswith("/"):
            name = f"{name} ({c.location})" if name != c.location else name
        warn = int(ep.warn_days) if ep is not None and ep.warn_days is not None else warn_default
        end = _aware(c.not_after)
        if end is None:
            continue
        days = (end - now).total_seconds() / 86400
        date = end.strftime("%Y-%m-%d")
        f = None
        if days < 0:
            f = finding("high", "certificates", target, f"Certificate {name} expired on {date}.",
                        "Renew or replace the certificate.", "run_command" if c.renew in ("certbot", "pve-acme") else None,
                        code="cert_expired", name=name, date=date)
        elif days <= warn:
            f = finding("high" if int(days) <= int(cfg["cert_critical_days"]) else "medium", "certificates", target,
                        f"Certificate {name} expires in {int(days)} day(s), on {date}.",
                        "Renew the certificate before it expires." +
                        (" Automatic renewal seems not to work." if c.renew else ""),
                        "run_command" if c.renew in ("certbot", "pve-acme") else None,
                        code="cert_expiring", name=name, date=date, days=int(days))
        if f:
            if c.renew in ("certbot", "pve-acme"):
                f["renew"] = c.renew
            out.append(f)
        if c.source == "endpoint" and days >= 0:
            if c.trusted is False:
                out.append(finding("medium", "certificates", target,
                                   f"The certificate of {c.location} is not trusted: {c.trust_error or 'unknown issuer'}.",
                                   "Install a certificate from a public CA, or add the missing intermediate certificate "
                                   "(full chain) on the server.", code="cert_untrusted", endpoint=c.location,
                                   error=c.trust_error or "unknown issuer"))
            elif c.hostname_ok is False:
                out.append(finding("medium", "certificates", target,
                                   f"The certificate of {c.location} is for {name}, not for the address that was checked.",
                                   "Install a certificate that contains this host name.",
                                   code="cert_name_mismatch", endpoint=c.location, name=name))
    for ep in eps.values():
        if ep.enabled and ep.last_error:
            label = endpoint_label(ep)
            out.append(finding("low", "certificates", label,
                               f"The certificate of {label} could not be checked: {ep.last_error[:120]}.",
                               "Check that Stackpilot can reach the address, or remove it from the list.",
                               code="cert_endpoint_error", endpoint=label, error=ep.last_error[:120]))
    return out


# ------------------------------------------------------------------ editing
async def save_endpoint(db: AsyncSession, ep: CertEndpoint | None, *, customer_id: str, host: str, port: int = 443,
                        name: str | None = None, sni: str | None = None, starttls: str = "none",
                        probe_node_id: str | None = None, warn_days: int | None = None, enabled: bool = True,
                        by: str | None = None) -> CertEndpoint:
    if ep is not None and ep.source == "uptime":
        raise CertError("this address comes from an uptime check: change the uptime check instead")
    h = clean_host(host)
    if not 1 <= int(port) <= 65535:
        raise CertError("port must be 1-65535")
    if starttls not in STARTTLS:
        raise CertError(f"starttls must be one of {', '.join(STARTTLS)}")
    s = clean_host(sni) if sni else None
    if warn_days is not None and not 1 <= int(warn_days) <= 180:
        raise CertError("warning must be 1-180 days")
    if probe_node_id:
        n = await db.get(Node, probe_node_id)
        if n is None or n.environment_id != customer_id:
            raise CertError("the probe machine must belong to the same customer")
        if n.kind == "windows" and starttls != "none":
            raise CertError("STARTTLS through a Windows machine is not supported; pick a Linux machine")
    if await db.get(Environment, customer_id) is None:
        raise CertError("customer not found")
    dup = (await db.execute(select(CertEndpoint).where(CertEndpoint.customer_id == customer_id, CertEndpoint.host == h,
                                                       CertEndpoint.port == int(port)))).scalars().first()
    if dup is not None and (ep is None or dup.id != ep.id):
        raise CertError(f"{h}:{port} is already watched for this customer")
    if ep is None:
        ep = CertEndpoint(customer_id=customer_id, source="manual", created_by=by)
        db.add(ep)
    elif ep.customer_id != customer_id:
        raise CertError("an endpoint cannot move to another customer")
    ep.host, ep.port, ep.name, ep.sni, ep.starttls = h, int(port), (name or "").strip()[:120] or None, s, starttls
    ep.probe_node_id, ep.warn_days, ep.enabled = probe_node_id or None, warn_days, bool(enabled)
    if ep.source == "nightly":
        ep.source = "manual"
    ep.last_error = None
    await db.flush()
    return ep


async def find_endpoint(db: AsyncSession, ref: str, customer_id: str | None = None) -> CertEndpoint:
    ep = await db.get(CertEndpoint, ref)
    if ep is None:
        hp = host_port(ref)
        if hp:
            q = select(CertEndpoint).where(CertEndpoint.host == hp[0].lower().strip("[]"), CertEndpoint.port == hp[1])
            if customer_id:
                q = q.where(CertEndpoint.customer_id == customer_id)
            ep = (await db.execute(q)).scalars().first()
    if ep is None or (customer_id and ep.customer_id != customer_id):
        raise CertError(f"certificate endpoint {ref} not found")
    return ep


async def find_certificate(db: AsyncSession, ref: str, customer_id: str | None = None) -> Certificate:
    c = await db.get(Certificate, ref)
    if c is None:
        q = select(Certificate).where((Certificate.fingerprint == ref.replace(":", "").upper()) |
                                      (Certificate.location == ref))
        if customer_id:
            q = q.where(Certificate.customer_id == customer_id)
        c = (await db.execute(q)).scalars().first()
    if c is None or (customer_id and c.customer_id != customer_id):
        raise CertError(f"certificate {ref} not found")
    return c
