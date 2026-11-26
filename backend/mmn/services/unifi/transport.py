"""How the app reaches a UniFi API.

* ``direct``: this server makes the HTTPS call (console on the same network, a VPN, or api.ui.com).
* ``agent``: an agent in the customer's network makes the call (``http.request``), so a console behind NAT
  needs no port forward. The response travels back over the agent's mTLS connection.
* ``cloud``: the Network Integration API of a console through the Site Manager cloud connector
  (``https://api.ui.com/v1/connector/consoles/<host id>/...``), with the Site Manager API key.

Local consoles mostly use a self-signed certificate. ``tls_mode`` is ``verify`` (system CAs), ``pin`` (accept only
the certificate with this SHA-256 fingerprint; checked during the TLS handshake, before the API key is sent) or
``insecure`` (no check; not recommended).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

CLOUD_BASE = "https://api.ui.com"
MAX_BODY = 16 * 1024 * 1024


class UnifiError(Exception):
    """A UniFi API call failed; ``code`` is a short machine readable reason."""

    def __init__(self, message: str, code: str = "error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Target:
    base_url: str  # without trailing slash, e.g. https://10.0.0.1/proxy/network/integration
    headers: dict[str, str]
    transport: str = "direct"  # direct | agent
    agent_node_id: str | None = None
    tls_mode: str = "verify"
    fingerprint: str | None = None
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def normalize_fingerprint(fp: str | None) -> str | None:
    if not fp:
        return None
    h = fp.replace(":", "").replace(" ", "").strip().lower()
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise UnifiError("the certificate fingerprint must be a SHA-256 hash (64 hex characters)", "bad_fingerprint")
    return h


def pretty_fingerprint(h: str) -> str:
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2)).upper()


def _pinned_context(fingerprint: str) -> ssl.SSLContext:
    """A TLS context that only completes the handshake with the certificate whose SHA-256 is ``fingerprint``.
    The check runs inside ``do_handshake``, so nothing (no API key) is sent to another server."""
    want = normalize_fingerprint(fingerprint)

    class PinnedSSLObject(ssl.SSLObject):
        def do_handshake(self) -> None:
            super().do_handshake()
            der = self.getpeercert(binary_form=True) or b""
            if hashlib.sha256(der).hexdigest() != want:
                raise ssl.SSLCertVerificationError(
                    "certificate fingerprint does not match the pinned fingerprint "
                    f"(got {pretty_fingerprint(hashlib.sha256(der).hexdigest())})")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.sslobject_class = PinnedSSLObject
    return ctx


def ssl_context(tls_mode: str, fingerprint: str | None) -> ssl.SSLContext | bool:
    if tls_mode == "pin":
        if not fingerprint:
            raise UnifiError("pinning needs a certificate fingerprint", "bad_fingerprint")
        return _pinned_context(fingerprint)
    if tls_mode == "insecure":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return True


def _url(t: Target, path: str, params: dict[str, Any] | None) -> str:
    url = t.base_url.rstrip("/") + "/" + path.lstrip("/")
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += ("&" if "?" in url else "?") + urlencode(clean, doseq=True)
    return url


def _error_for(status: int, body: Any, label: str) -> UnifiError:
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("msg") or body.get("error") or "")
    elif isinstance(body, str):
        msg = body.strip()[:300]
    where = f"{label}: " if label else ""
    if status == 401:
        return UnifiError(f"{where}the API key was rejected (HTTP 401)", "unauthorized", status)
    if status == 403 and "owner" in msg.lower():
        return UnifiError(f"{where}the API key was not made by the owner of this console (HTTP 403). Create the key "
                          f"in UniFi Site Manager while signed in as the owner, or inside the UniFi Fabric that holds "
                          f"the console", "not_owner", status)
    if status == 403:
        return UnifiError(f"{where}the API key has no permission for this (read-only key?) {msg}".strip(),
                          "forbidden", status)
    if status == 404:
        return UnifiError(f"{where}not found, or not supported by this UniFi version (HTTP 404) {msg}".strip(),
                          "not_found", status)
    if status == 429:
        return UnifiError(f"{where}rate limited by UniFi (HTTP 429), try again in a minute", "rate_limited", status)
    if status in (408, 504):
        return UnifiError(f"{where}the console did not answer in time (HTTP {status})", "timeout", status)
    return UnifiError(f"{where}HTTP {status} {msg}".strip(), "http_error", status)


def _decode(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _direct(t: Target, method: str, url: str, body: bytes | None, headers: dict[str, str],
                  timeout: float) -> tuple[int, str]:
    verify = ssl_context(t.tls_mode, t.fingerprint)
    try:
        async with httpx.AsyncClient(verify=verify, timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, url, content=body, headers=headers)
            return r.status_code, r.text
    except ssl.SSLError as e:
        raise UnifiError(f"TLS: {e}", "tls") from e
    except httpx.ConnectError as e:
        cause = e.__cause__ or e.__context__
        if isinstance(cause, ssl.SSLError) or "CERTIFICATE" in str(e).upper() or "fingerprint" in str(e):
            raise UnifiError(f"TLS: {e}", "tls") from e
        raise UnifiError(f"cannot connect: {e}", "unreachable") from e
    except httpx.TimeoutException as e:
        raise UnifiError("the console did not answer in time", "timeout") from e
    except httpx.HTTPError as e:
        raise UnifiError(f"connection failed: {e}", "unreachable") from e


async def _via_agent(t: Target, method: str, url: str, body: bytes | None, headers: dict[str, str],
                     timeout: float) -> tuple[int, str]:
    from mmn.services import agent_hub

    if not t.agent_node_id:
        raise UnifiError("no agent chosen for this connection", "no_agent")
    params = {"method": method, "url": url, "headers": headers, "timeout": int(timeout),
              "body": base64.b64encode(body).decode() if body else "",
              "insecure": t.tls_mode in ("insecure", "pin"),
              "fingerprint": normalize_fingerprint(t.fingerprint) if t.tls_mode == "pin" else ""}
    try:
        res = await agent_hub.request(t.agent_node_id, "http.request", params, timeout=timeout + 10)
    except agent_hub.AgentOffline as e:
        raise UnifiError("the agent that reaches this console is offline", "agent_offline") from e
    except agent_hub.AgentError as e:
        msg = str(e)
        if "unknown method" in msg.lower():
            raise UnifiError("the agent is too old for UniFi relaying; update the agent", "agent_outdated") from e
        code = "tls" if "fingerprint" in msg or "certificate" in msg.lower() else "unreachable"
        raise UnifiError(f"via agent: {msg}", code) from e
    except TimeoutError as e:
        raise UnifiError("the console did not answer in time", "timeout") from e
    raw = base64.b64decode(res.get("body") or "") if res.get("body_b64") else (res.get("body") or "").encode()
    return int(res.get("status") or 0), raw.decode(errors="replace")


async def request(t: Target, method: str, path: str, *, params: dict[str, Any] | None = None,
                  json_body: Any = None, timeout: float = 25) -> Any:
    url = _url(t, path, params)
    headers = {"Accept": "application/json", **t.headers}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    send = _via_agent if t.transport == "agent" else _direct
    status, text = await send(t, method.upper(), url, body, headers, timeout)
    if len(text) > MAX_BODY:
        raise UnifiError("the response is too large", "too_large")
    data = _decode(text)
    if "/v1/connector/" in url and status in (502, 503):
        raise UnifiError(f"{t.label + ': ' if t.label else ''}the UniFi cloud connector cannot reach this console "
                         f"(HTTP {status}). It must be online, adopted in UniFi Site Manager and run UniFi OS 5.0.9 or "
                         f"newer, and the key must have the Network scope for it", "connector", status)
    if status >= 400 or status == 0:
        raise _error_for(status, data, t.label)
    return data


async def fetch_certificate(url: str, *, agent_node_id: str | None = None, timeout: float = 10) -> dict[str, Any]:
    """The certificate a server presents now (SHA-256 fingerprint, subject, issuer, expiry), to pin it."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnifiError("enter an https:// address", "bad_url")
    if agent_node_id:
        from mmn.services import agent_hub

        try:
            res = await agent_hub.request(agent_node_id, "http.request",
                                          {"method": "HEAD", "url": url, "headers": {}, "timeout": int(timeout),
                                           "insecure": True, "fingerprint": "", "body": ""}, timeout=timeout + 10)
        except agent_hub.AgentOffline as e:
            raise UnifiError("the agent is offline", "agent_offline") from e
        except agent_hub.AgentError as e:
            raise UnifiError(f"via agent: {e}", "unreachable") from e
        cert = res.get("peer_certificate") or {}
        if not cert.get("sha256"):
            raise UnifiError("the agent returned no certificate", "tls")
        return {**cert, "fingerprint": pretty_fingerprint(cert["sha256"])}
    port = parts.port or 443
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, port, ssl=ctx,
                                                                   server_hostname=parts.hostname), timeout)
    except (OSError, TimeoutError) as e:
        raise UnifiError(f"cannot connect: {e}", "unreachable") from e
    try:
        obj = writer.get_extra_info("ssl_object")
        der = obj.getpeercert(binary_form=True)
    finally:
        writer.close()
    from cryptography import x509

    c = x509.load_der_x509_certificate(der)
    h = hashlib.sha256(der).hexdigest()
    return {"sha256": h, "fingerprint": pretty_fingerprint(h), "subject": c.subject.rfc4514_string(),
            "issuer": c.issuer.rfc4514_string(), "not_after": c.not_valid_after_utc.isoformat(),
            "self_signed": c.subject == c.issuer}
