"""The app's private certificate authority for agents (mutual TLS).

* The CA key and certificate live in the database (setting ``agent_ca``, key encrypted), so they
  are part of every backup and survive a restore.
* Each agent gets a client certificate when it enrolls with a one-time code. Its SHA-256
  fingerprint is stored on the node; removing the node rejects the certificate.
* Nginx terminates TLS on the agent port with a server certificate from the same CA and only
  forwards agent requests with a verified client certificate. The files Nginx reads are written to
  ``<data dir>/agent-tls`` by :func:`ensure_tls_files`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services.settings_store import get_setting, set_setting

CA_YEARS = 20
CLIENT_YEARS = 10
SERVER_YEARS = 10


class AgentCAError(Exception):
    pass


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def pretty_fingerprint(fp: str) -> str:
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MCP Management Node"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _new_ca() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    name = _name(f"MCP Management Node agent CA {secrets.token_hex(3)}")
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365 * CA_YEARS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem


class CA:
    def __init__(self, cert_pem: str, key_pem: str, proxy_secret: str) -> None:
        self.cert_pem = cert_pem
        self.cert = x509.load_pem_x509_certificate(cert_pem.encode())
        self.key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        self.proxy_secret = proxy_secret

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.cert)

    def _builder(self, subject: x509.Name, public_key: Any, years: int) -> x509.CertificateBuilder:
        now = datetime.now(UTC)
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(self.cert.subject)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365 * years))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                               critical=False))

    def sign_client(self, csr_pem: str, node_id: str) -> x509.Certificate:
        try:
            csr = x509.load_pem_x509_csr(csr_pem.encode())
        except ValueError as e:
            raise AgentCAError("invalid certificate request") from e
        if not csr.is_signature_valid:
            raise AgentCAError("certificate request signature is invalid")
        pub = csr.public_key()
        if not isinstance(pub, ec.EllipticCurvePublicKey) and getattr(pub, "key_size", 0) < 2048:
            raise AgentCAError("key too weak")
        b = (self._builder(_name(f"node:{node_id}"), pub, CLIENT_YEARS)
             .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                          crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
        return b.sign(self.key, hashes.SHA256())

    def server_cert(self, names: list[str]) -> tuple[str, str]:
        key = ec.generate_private_key(ec.SECP256R1())
        sans: list[x509.GeneralName] = []
        for n in dict.fromkeys(names or ["localhost"]):
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(n)))
            except ValueError:
                sans.append(x509.DNSName(n))
        b = (self._builder(_name("mmn-agent-endpoint"), key.public_key(), SERVER_YEARS)
             .add_extension(x509.SubjectAlternativeName(sans), critical=False)
             .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                          crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
        cert = b.sign(self.key, hashes.SHA256())
        # The chain includes the CA so new agents can check it against the pinned fingerprint.
        chain = cert.public_bytes(serialization.Encoding.PEM).decode() + self.cert_pem
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        return chain, key_pem


_cache: dict[str, CA] = {}


async def get_ca(db: AsyncSession) -> CA:
    """Load the CA, creating it on first use."""
    cfg = await get_setting(db, "agent_ca")
    cert_pem, key_pem = cfg.get("cert_pem"), decrypt_secret(cfg.get("key_enc"))
    secret = decrypt_secret(cfg.get("proxy_secret_enc"))
    if not cert_pem or not key_pem:
        cert_pem, key_pem = _new_ca()
        secret = secrets.token_urlsafe(32)
        await set_setting(db, "agent_ca", {"cert_pem": cert_pem, "key_enc": encrypt_secret(key_pem),
                                           "proxy_secret_enc": encrypt_secret(secret),
                                           "created_at": datetime.now(UTC).isoformat()})
        await db.commit()
    elif not secret:
        secret = secrets.token_urlsafe(32)
        await set_setting(db, "agent_ca", {"proxy_secret_enc": encrypt_secret(secret)})
        await db.commit()
    ca = _cache.get(cert_pem)
    if ca is None or ca.proxy_secret != secret:
        _cache.clear()
        ca = _cache[cert_pem] = CA(cert_pem, key_pem, secret)
    return ca


def _write(path: Path, data: str, mode: int) -> bool:
    """Write when changed. Returns True if the file changed."""
    if path.exists() and path.read_text() == data:
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


async def ensure_tls_files(db: AsyncSession, names: list[str] | None = None, *, renew: bool = False) -> bool:
    """Write ca.crt, server.crt, server.key and proxy-secret for Nginx. Returns True when anything
    changed (Nginx then needs a reload)."""
    ca = await get_ca(db)
    d = get_settings().agent_tls_dir
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o750)
    changed = _write(d / "ca.crt", ca.cert_pem, 0o644)
    changed |= _write(d / "proxy-secret", ca.proxy_secret + "\n", 0o640)
    srv = d / "server.crt"
    need_new = renew or not srv.exists() or not (d / "server.key").exists()
    if not need_new:
        try:
            chain = x509.load_pem_x509_certificates(srv.read_bytes())
            leaf = chain[0]
            need_new = (leaf.issuer != ca.cert.subject
                        or leaf.not_valid_after_utc < datetime.now(UTC) + timedelta(days=30))
            if names and not need_new:
                have = set(str(v) for v in leaf.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
                have |= set(str(v) for v in leaf.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress))
                need_new = not set(names) <= have
        except (ValueError, IndexError, x509.ExtensionNotFound):
            need_new = True
    if need_new:
        chain_pem, key_pem = ca.server_cert(names or ["localhost"])
        _write(d / "server.key", key_pem, 0o600)
        _write(srv, chain_pem, 0o644)
        changed = True
    return changed


def parse_client_cert(escaped_pem: str) -> x509.Certificate | None:
    """Nginx passes the client certificate URL-encoded in a header ($ssl_client_escaped_cert)."""
    from urllib.parse import unquote

    if not escaped_pem:
        return None
    try:
        return x509.load_pem_x509_certificate(unquote(escaped_pem).encode())
    except ValueError:
        return None


def verify_client(ca: CA, cert: x509.Certificate) -> bool:
    """Defence in depth: Nginx already verified the chain; check issuer, signature and validity again."""
    now = datetime.now(UTC)
    if cert.issuer != ca.cert.subject or not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        return False
    try:
        cert.verify_directly_issued_by(ca.cert)
    except (ValueError, TypeError, Exception):  # noqa: BLE001 - InvalidSignature and friends
        return False
    return True
