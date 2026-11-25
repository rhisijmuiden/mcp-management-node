"""Microsoft 365 and Entra ID per customer (phase 19).

Every customer can have one or more tenants. Stackpilot reads them through Microsoft Graph with an app
registration in the customer's tenant (application permissions, admin consent). The client secret or the
certificate with its private key lives in the password vault of the customer (phase 18); this module reads
it from there for every new access token and writes that use to the vault's access log (at most once a day
per tenant, plus every test).

A sync reads the organisation, the users (with licences, last sign-in, admin roles, MFA registration and
Entra ID Protection risk), the licences, risk detections, security defaults and Conditional Access, the
expiry of the app's own secret or certificate, and the sign-ins since the last sync. Only sign-ins that
stand out are stored (risky, failed, from a country outside the tenant's list, or legacy authentication).
Parts the tenant does not have (Entra ID P1/P2) or the app may not read (a permission without consent) are
skipped and shown as such; the rest still works.

Write actions on users (block, unblock, sign out everywhere, reset MFA, reset the password, confirm or
dismiss risk) run from the app by an admin, or through the MCP tool ``m365_user_action``, which always
needs an admin's approval.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Environment, M365SignIn, M365Tenant, VaultItem
from mmn.services import events
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.m365")

LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Tests replace this with an httpx.MockTransport that plays Microsoft Graph.
_transport: httpx.AsyncBaseTransport | None = None

AUTH_METHODS = ("secret", "certificate")

# Application permissions (Microsoft Graph) the app registration needs, with what each one is for.
READ_PERMISSIONS: list[tuple[str, str]] = [
    ("User.Read.All", "users, licences per user"),
    ("Directory.Read.All", "organisation, admin roles, licences"),
    ("AuditLog.Read.All", "sign-ins, last sign-in, MFA registration (Entra ID P1)"),
    ("UserAuthenticationMethod.Read.All", "MFA methods per user"),
    ("IdentityRiskyUser.Read.All", "risky users (Entra ID P2)"),
    ("IdentityRiskEvent.Read.All", "risk detections (Entra ID P2)"),
    ("Policy.Read.All", "security defaults and Conditional Access"),
    ("Application.Read.All", "expiry of the app's own secret or certificate"),
]
WRITE_PERMISSIONS: list[tuple[str, str]] = [
    ("User.EnableDisableAccount.All", "block and unblock users"),
    ("User.RevokeSessions.All", "sign users out everywhere"),
    ("UserAuthenticationMethod.ReadWrite.All", "reset MFA"),
    ("User-PasswordProfile.ReadWrite.All", "reset passwords"),
    ("IdentityRiskyUser.ReadWrite.All", "confirm or dismiss user risk"),
]

ACTIONS = ("block", "unblock", "revoke_sessions", "reset_mfa", "reset_password", "confirm_compromised",
           "dismiss_risk")
# The permission each action needs (any one of them).
ACTION_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "block": ("User.EnableDisableAccount.All", "User.ReadWrite.All", "Directory.ReadWrite.All"),
    "unblock": ("User.EnableDisableAccount.All", "User.ReadWrite.All", "Directory.ReadWrite.All"),
    "revoke_sessions": ("User.RevokeSessions.All", "User.ReadWrite.All", "Directory.ReadWrite.All"),
    "reset_mfa": ("UserAuthenticationMethod.ReadWrite.All",),
    "reset_password": ("User-PasswordProfile.ReadWrite.All", "User.ReadWrite.All", "Directory.ReadWrite.All"),
    "confirm_compromised": ("IdentityRiskyUser.ReadWrite.All",),
    "dismiss_risk": ("IdentityRiskyUser.ReadWrite.All",),
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "allowed_countries": ["NL"],  # sign-ins from other countries stand out (empty: no country check)
    "stale_days": 90,  # licensed, enabled accounts without a sign-in for this long are reported
    "failed_threshold": 20,  # failed sign-ins of one user in 24 hours before it is an alert
    "secret_warn_days": 30,  # warn this long before the app's secret or certificate expires
    "alert_risky": True,
    "alert_admin_mfa": True,
    "alert_signins": True,  # successful sign-ins from another country, many failed sign-ins
    "alert_licences": True,
    "alert_secret": True,
}

# Directory roles that do not make an account an administrator here (sync accounts cannot do MFA).
NON_ADMIN_ROLES = {"Directory Readers", "Directory Synchronization Accounts", "On Premises Directory Sync Account",
                   "Guest Inviter", "Message Center Reader", "Usage Summary Reports Reader", "Reports Reader"}

LEGACY_CLIENTS = {"Exchange ActiveSync", "IMAP4", "POP3", "Authenticated SMTP", "Other clients", "MAPI Over HTTP",
                  "Offline Address Book", "Exchange Web Services", "AutoDiscover", "Exchange Online PowerShell",
                  "Outlook Anywhere (RPC over HTTP)", "Reporting Web Services", "Universal Outlook"}

# Sign-in "errors" that are interrupts (MFA asked, keep me signed in, consent, ...), not a failed sign-in.
INTERRUPTS = {50074, 50076, 50079, 50140, 50125, 50072, 50097, 50158, 70044, 16000, 16001, 16003, 50058, 50059,
              65001, 81010, 81014, 50199, 530032, 50132, 50173, 50089, 70043, 9002341}

# Friendly names of the most common licences (the rest shows its SKU part number).
SKU_NAMES: dict[str, str] = {
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "SPB": "Microsoft 365 Business Premium",
    "O365_BUSINESS": "Microsoft 365 Apps for business",
    "SMB_BUSINESS": "Microsoft 365 Apps for business",
    "OFFICESUBSCRIPTION": "Microsoft 365 Apps for enterprise",
    "STANDARDPACK": "Office 365 E1",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "SPE_F1": "Microsoft 365 F3",
    "DESKLESSPACK": "Office 365 F3",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)",
    "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "EXCHANGEDESKLESS": "Exchange Online Kiosk",
    "EXCHANGEARCHIVE_ADDON": "Exchange Online Archiving",
    "AAD_PREMIUM": "Microsoft Entra ID P1",
    "AAD_PREMIUM_P2": "Microsoft Entra ID P2",
    "EMS": "Enterprise Mobility + Security E3",
    "EMSPREMIUM": "Enterprise Mobility + Security E5",
    "INTUNE_A": "Microsoft Intune Plan 1",
    "ATP_ENTERPRISE": "Microsoft Defender for Office 365 (Plan 1)",
    "THREAT_INTELLIGENCE": "Microsoft Defender for Office 365 (Plan 2)",
    "DEFENDER_ENDPOINT_P1": "Microsoft Defender for Endpoint P1",
    "MDE_SMB": "Microsoft Defender for Business",
    "POWER_BI_STANDARD": "Power BI (free)",
    "POWER_BI_PRO": "Power BI Pro",
    "PROJECTPROFESSIONAL": "Project Plan 3",
    "VISIOCLIENT": "Visio Plan 2",
    "FLOW_FREE": "Power Automate (free)",
    "TEAMS_EXPLORATORY": "Teams Exploratory",
    "Microsoft_Teams_Exploratory_Dept": "Teams Exploratory",
    "MCOEV": "Teams Phone Standard",
    "MCOMEETADV": "Audio Conferencing",
    "PHONESYSTEM_VIRTUALUSER": "Teams Phone Resource Account",
    "Microsoft_Teams_Rooms_Basic": "Teams Rooms Basic",
    "Microsoft_Teams_Rooms_Pro": "Teams Rooms Pro",
    "WINDOWS_STORE": "Windows Store for Business",
    "RIGHTSMANAGEMENT_ADHOC": "Rights Management Adhoc",
    "STREAM": "Microsoft Stream",
    "DEVELOPERPACK_E5": "Microsoft 365 E5 Developer",
    "Microsoft_365_Copilot": "Microsoft 365 Copilot",
}

# Graph type of an authentication method -> the path to delete it (password and email stay).
METHOD_PATHS = {
    "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod": "microsoftAuthenticatorMethods",
    "#microsoft.graph.phoneAuthenticationMethod": "phoneMethods",
    "#microsoft.graph.fido2AuthenticationMethod": "fido2Methods",
    "#microsoft.graph.softwareOathAuthenticationMethod": "softwareOathMethods",
    "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod": "windowsHelloForBusinessMethods",
    "#microsoft.graph.temporaryAccessPassAuthenticationMethod": "temporaryAccessPassMethods",
    "#microsoft.graph.platformCredentialAuthenticationMethod": "platformCredentialMethods",
}
# Methods that count as a second factor.
MFA_TYPES = {"microsoftAuthenticator", "phone", "fido2", "softwareOath", "windowsHelloForBusiness",
             "platformCredential", "passKeyDeviceBound", "passKeyDeviceBoundAuthenticator",
             "microsoftAuthenticatorPush", "microsoftAuthenticatorPasswordless", "mobilePhone", "alternateMobilePhone",
             "officePhone", "hardwareOneTimePasscode", "softwareOneTimePasscode", "passKeyDeviceBoundWindowsHello",
             "macOsSecureEnclavePlatformCredential", "fido2SecurityKey"}

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOMAIN = re.compile(r"^(?=.{3,100}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")


class M365Error(Exception):
    def __init__(self, message: str, *, code: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class GraphError(M365Error):
    pass


# ------------------------------------------------------------------ settings and validation
def tenant_settings(t: M365Tenant) -> dict[str, Any]:
    return {**DEFAULT_SETTINGS, **{k: v for k, v in (t.settings or {}).items() if k in DEFAULT_SETTINGS}}


def clean_settings(data: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (data or {}).items():
        if k not in DEFAULT_SETTINGS or v is None:
            continue
        if k == "allowed_countries":
            items = v if isinstance(v, list) else re.split(r"[\s,;]+", str(v))
            codes = sorted({str(c).strip().upper() for c in items if str(c).strip()})
            for c in codes:
                if not re.fullmatch(r"[A-Z]{2}", c):
                    raise M365Error(f"'{c}' is not a two-letter country code (NL, BE, DE, ...)", code="invalid")
            out[k] = codes[:60]
        elif isinstance(DEFAULT_SETTINGS[k], bool):
            out[k] = bool(v)
        else:
            try:
                n = int(v)
            except (TypeError, ValueError) as e:
                raise M365Error(f"{k} must be a number", code="invalid") from e
            lim = {"stale_days": (7, 730), "failed_threshold": (3, 10000), "secret_warn_days": (1, 365)}[k]
            out[k] = max(lim[0], min(n, lim[1]))
    return out


def check_ids(tenant_id: str, client_id: str) -> tuple[str, str]:
    tenant_id, client_id = (tenant_id or "").strip(), (client_id or "").strip()
    if not (_GUID.match(tenant_id) or _DOMAIN.match(tenant_id)):
        raise M365Error("the tenant must be the directory (tenant) ID or a domain such as contoso.onmicrosoft.com",
                        code="invalid")
    if not _GUID.match(client_id):
        raise M365Error("the application (client) ID must be a GUID", code="invalid")
    return tenant_id, client_id.lower()


# ------------------------------------------------------------------ credentials (password vault)
_PEM_KEY = re.compile(r"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----.+?-----END (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----",
                      re.S)
_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def _payload_text(payload: dict[str, Any]) -> str:
    parts = [payload.get("password") or "", payload.get("notes") or ""]
    parts += [f.get("value") or "" for f in payload.get("fields") or []]
    return "\n".join(parts)


def parse_certificate(payload: dict[str, Any]) -> tuple[Any, Any]:
    """(private key, certificate) from a vault item: one PEM text with both blocks, in the password, the
    notes or an extra field."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    text = _payload_text(payload)
    km, cm = _PEM_KEY.search(text), _PEM_CERT.search(text)
    if not km or not cm:
        raise M365Error("the vault item must hold the private key and the certificate as PEM "
                        "(-----BEGIN PRIVATE KEY----- and -----BEGIN CERTIFICATE-----)", code="bad_credential")
    try:
        key = serialization.load_pem_private_key(km.group(0).encode(), password=None)
        cert = x509.load_pem_x509_certificate(cm.group(0).encode())
    except (ValueError, TypeError) as e:
        raise M365Error(f"the certificate or key in the vault cannot be read ({e}); a key with a passphrase is "
                        "not supported", code="bad_credential") from e
    return key, cert


def cert_info(cert: Any) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes

    return {"thumbprint": cert.fingerprint(hashes.SHA1()).hex().upper(),  # noqa: S303 - what Entra shows
            "subject": cert.subject.rfc4514_string(),
            "expires": cert.not_valid_after_utc.isoformat()}


def generate_certificate(name: str, years: int = 2) -> tuple[str, str, dict[str, Any]]:
    """A self-signed certificate for the app registration: (PEM with key and certificate for the vault,
    PEM of the certificate alone to upload in Entra, info)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cn = re.sub(r"[^\w .-]", "", f"Stackpilot {name}")[:60] or "Stackpilot"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365 * years))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem + cert_pem, cert_pem, cert_info(cert)


def public_certificate(payload: dict[str, Any]) -> str:
    _, cert = parse_certificate(payload)
    from cryptography.hazmat.primitives import serialization

    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def client_assertion(tenant: str, client_id: str, key: Any, cert: Any) -> str:
    """The signed JWT that proves the app owns the certificate (RS256, x5t = SHA-1 thumbprint)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "x5t": _b64url(cert.fingerprint(hashes.SHA1()))}  # noqa: S303
    claims = {"aud": f"{LOGIN_URL}/{tenant}/oauth2/v2.0/token", "iss": client_id, "sub": client_id,
              "jti": uuid.uuid4().hex, "nbf": now - 60, "iat": now, "exp": now + 600}
    signing = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{_b64url(sig)}"


def jwt_claims(token: str) -> dict[str, Any]:
    """The claims of an access token (not verified: only to see which permissions were granted)."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


class Credential:
    """What a tenant needs for a token: the secret, or the certificate and its key."""

    def __init__(self, t: M365Tenant, payload: dict[str, Any]) -> None:
        self.tenant, self.client_id, self.method = t.tenant_id, t.client_id, t.auth_method
        self.secret: str | None = None
        self.key = self.cert = None
        if t.auth_method == "certificate":
            self.key, self.cert = parse_certificate(payload)
        else:
            self.secret = (payload.get("password") or "").strip()
            if not self.secret:
                raise M365Error("the vault item has no client secret in its password", code="bad_credential")
        self.fingerprint = hashlib.sha256(
            f"{t.tenant_id}|{t.client_id}|{t.auth_method}|{_payload_text(payload)}".encode()).hexdigest()

    def form(self) -> dict[str, str]:
        base = {"client_id": self.client_id, "scope": GRAPH_SCOPE, "grant_type": "client_credentials"}
        if self.secret is not None:
            return {**base, "client_secret": self.secret}
        return {**base, "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": client_assertion(self.tenant, self.client_id, self.key, self.cert)}


async def load_credential(db: AsyncSession, t: M365Tenant, *, log_use: bool = False, reason: str | None = None,
                          username: str | None = None) -> Credential:
    from mmn.services import vault

    if not t.vault_item_id:
        raise M365Error("no credential: choose the vault item with the client secret or certificate", code="no_credential")
    item = await db.get(VaultItem, t.vault_item_id)
    if item is None or item.customer_id != t.customer_id:
        raise M365Error("the vault item of this tenant is gone; choose or add the credential again", code="no_credential")
    try:
        payload = vault.read_secret(item)
    except vault.VaultError as e:
        raise M365Error(f"the vault item cannot be decrypted: {e}", code="bad_credential") from e
    cred = Credential(t, payload)
    now = datetime.now(UTC)
    if log_use or t.vault_logged_at is None or now - t.vault_logged_at > timedelta(hours=24):
        await vault.log_access(db, item, "integration_use", actor_kind="system" if not username else "user",
                               username=username, reason=reason or f"Microsoft 365 sync of {t.name}",
                               integration="m365", tenant=t.name)
        t.vault_logged_at = now
    return cred


# ------------------------------------------------------------------ Microsoft Graph
_tokens: dict[str, tuple[str, float, str, list[str]]] = {}  # tenant row id -> (token, expires, fingerprint, roles)


def forget_token(tenant_row_id: str) -> None:
    _tokens.pop(tenant_row_id, None)


def _error_of(r: httpx.Response) -> tuple[str, str]:
    try:
        body = r.json()
    except ValueError:
        return "error", (r.text or r.reason_phrase or "")[:300]
    if isinstance(body.get("error"), dict):
        return str(body["error"].get("code") or "error"), str(body["error"].get("message") or "")[:500]
    return str(body.get("error") or "error"), str(body.get("error_description") or body.get("message") or "")[:500]


def classify(e: GraphError) -> str:
    """no_permission (the app may not), needs_p1 / needs_p2 (the tenant lacks the licence), not_found, error."""
    text = f"{e.code} {e}".lower()
    if "premium" in text or "p2" in text or "license" in text or "licence" in text or "b2c" in text:
        return "needs_p2" if ("risk" in text or "p2" in text) else "needs_p1"
    if e.status in (401, 403) or "authorization_requestdenied" in text or "forbidden" in text:
        return "no_permission"
    if e.status == 404:
        return "not_found"
    return "error"


class Graph:
    def __init__(self, row_id: str, cred: Credential, timeout: float = 30) -> None:
        self.row_id, self.cred = row_id, cred
        self.client = httpx.AsyncClient(timeout=timeout, transport=_transport,
                                        headers={"User-Agent": "Stackpilot"})
        self.roles: list[str] = []

    async def __aenter__(self) -> Graph:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.aclose()

    async def token(self) -> str:
        cached = _tokens.get(self.row_id)
        if cached and cached[2] == self.cred.fingerprint and cached[1] > time.time() + 120:
            self.roles = cached[3]
            return cached[0]
        try:
            r = await self.client.post(f"{LOGIN_URL}/{quote(self.cred.tenant)}/oauth2/v2.0/token", data=self.cred.form())
        except httpx.HTTPError as e:
            raise GraphError(f"cannot reach Microsoft sign-in: {e}", code="unreachable") from e
        if r.status_code != 200:
            code, msg = _error_of(r)
            # AADSTS7000215 invalid secret, AADSTS700016 unknown app, AADSTS90002 unknown tenant,
            # AADSTS7000222 secret expired, AADSTS700027 certificate not on the app.
            raise GraphError(f"sign-in of the app refused: {msg.splitlines()[0] if msg else code}",
                             code="auth_failed", status=r.status_code)
        body = r.json()
        tok = body["access_token"]
        self.roles = sorted(jwt_claims(tok).get("roles") or [])
        _tokens[self.row_id] = (tok, time.time() + int(body.get("expires_in") or 3600), self.cred.fingerprint, self.roles)
        return tok

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      json_body: Any = None, headers: dict[str, str] | None = None) -> Any:
        url = path if path.startswith("https://") else f"{GRAPH_URL}{path}"
        for attempt in range(3):
            tok = await self.token()
            h = {"Authorization": f"Bearer {tok}", **(headers or {})}
            try:
                r = await self.client.request(method, url, params=params, json=json_body, headers=h)
            except httpx.HTTPError as e:
                raise GraphError(f"cannot reach Microsoft Graph: {e}", code="unreachable") from e
            if r.status_code == 429 or r.status_code in (502, 503, 504):
                wait = min(float(r.headers.get("Retry-After") or 2 * (attempt + 1)), 10)
                await asyncio.sleep(wait)
                continue
            if r.status_code == 401 and attempt == 0:
                forget_token(self.row_id)
                continue
            if r.status_code >= 400:
                code, msg = _error_of(r)
                raise GraphError(msg or code, code=code, status=r.status_code)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise GraphError("Microsoft Graph keeps throttling; try again later", code="throttled", status=429)

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def all(self, path: str, *, params: dict[str, Any] | None = None, max_pages: int = 20,
                  headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        body = await self.get(path, params=params, headers=headers)
        pages = 1
        while True:
            out += (body or {}).get("value") or []
            nxt = (body or {}).get("@odata.nextLink")
            if not nxt or pages >= max_pages:
                return out
            body = await self.get(nxt, headers=headers)
            pages += 1


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _graph_time(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sku_name(part: str | None) -> str:
    return SKU_NAMES.get(part or "", (part or "?").replace("_", " "))


# ------------------------------------------------------------------ sync
_running: set[str] = set()


async def _part(features: dict[str, Any], name: str, permission: str, coro) -> Any:
    try:
        res = await coro
    except GraphError as e:
        features[name] = {"ok": False, "reason": classify(e), "message": str(e)[:300], "permission": permission}
        return None
    features[name] = {"ok": True}
    return res


async def _mfa_fallback(g: Graph, users: list[dict[str, Any]], limit: int = 500) -> dict[str, dict[str, Any]]:
    """MFA per user from their authentication methods (tenants without Entra ID P1)."""
    sem = asyncio.Semaphore(6)
    out: dict[str, dict[str, Any]] = {}

    async def one(u: dict[str, Any]) -> None:
        async with sem:
            try:
                ms = await g.all(f"/users/{u['id']}/authentication/methods", max_pages=2)
            except GraphError as e:
                if classify(e) == "no_permission":
                    raise
                return
        types = [str(m.get("@odata.type") or "").removeprefix("#microsoft.graph.").removesuffix("AuthenticationMethod")
                 for m in ms]
        types = [t for t in types if t and t not in ("password",)]
        out[u["id"]] = {"registered": any(t in MFA_TYPES for t in types), "methods": sorted(set(types)),
                        "default": None, "source": "methods"}

    todo = [u for u in users if u.get("accountEnabled") and (u.get("userType") or "Member") == "Member"][:limit]
    await asyncio.gather(*(one(u) for u in todo))
    return out


def _signin_flags(s: dict[str, Any], allowed: set[str]) -> tuple[list[str], bool, int]:
    status = s.get("status") or {}
    code = int(status.get("errorCode") or 0)
    success = code == 0
    failed = not success and code not in INTERRUPTS
    flags: list[str] = []
    if (s.get("riskLevelAggregated") in ("medium", "high") or s.get("riskLevelDuringSignIn") in ("medium", "high")
            or s.get("riskState") in ("atRisk", "confirmedCompromised")):
        flags.append("risky")
    country = ((s.get("location") or {}).get("countryOrRegion") or "").upper()
    if allowed and country and country not in allowed and (success or failed):
        flags.append("foreign")
    if s.get("clientAppUsed") in LEGACY_CLIENTS and success:
        flags.append("legacy")
    if failed:
        flags.append("failed")
    return flags, success, code


async def sync(row_id: str) -> dict[str, Any]:
    """Read the tenant through Microsoft Graph and store the snapshot. Raises M365Error when the app cannot
    sign in at all (the tenant is then in status error)."""
    if row_id in _running:
        raise M365Error("this tenant is already being read", code="busy")
    _running.add(row_id)
    try:
        return await _sync(row_id)
    finally:
        _running.discard(row_id)


async def _sync(row_id: str) -> dict[str, Any]:
    started = datetime.now(UTC)
    async with sessionmaker()() as db:
        t = await db.get(M365Tenant, row_id)
        if t is None:
            raise M365Error("tenant not found", code="not_found")
        cfg = tenant_settings(t)
        app_cfg = await get_setting(db, "m365")
        try:
            cred = await load_credential(db, t)
        except M365Error as e:
            t.status, t.last_error, t.last_sync_at = "error", str(e)[:1000], started
            await db.commit()
            events.publish("m365.synced", {"tenant_id": row_id, "status": "error"})
            raise
        await db.commit()
        prev = dict(t.snapshot or {})
        since = t.signins_since or (started - timedelta(hours=24))
        since = max(since, started - timedelta(days=7))
        customer_id, client_id, method = t.customer_id, t.client_id, t.auth_method
        secret_hint = (cred.secret or "")[:3]
        cert_thumb = cert_info(cred.cert)["thumbprint"] if cred.cert is not None else None
    features: dict[str, Any] = {}
    allowed = set(cfg["allowed_countries"] or [])
    signins: list[dict[str, Any]] = []
    try:
        async with Graph(row_id, cred) as g:
            org = (await g.get("/organization", params={"$select": "id,displayName,verifiedDomains"}) or {}).get("value") or []
            features["organization"] = {"ok": True}
            sel = ("id,displayName,userPrincipalName,mail,accountEnabled,userType,assignedLicenses,createdDateTime,"
                   "onPremisesSyncEnabled,jobTitle,department")
            try:
                users = await g.all("/users", params={"$select": sel + ",signInActivity", "$top": "500"}, max_pages=40)
                features["last_signin"] = {"ok": True}
            except GraphError as e:
                if e.status not in (400, 403):
                    raise
                features["last_signin"] = {"ok": False, "reason": classify(e) if e.status == 403 else "needs_p1",
                                           "message": str(e)[:300], "permission": "AuditLog.Read.All"}
                users = await g.all("/users", params={"$select": sel, "$top": "999"}, max_pages=40)
            features["users"] = {"ok": True}
            roles = await _part(features, "roles", "Directory.Read.All", g.all(
                "/directoryRoles", params={"$select": "id,displayName", "$expand": "members($select=id)"}))
            skus = await _part(features, "licences", "Directory.Read.All", g.all("/subscribedSkus"))
            reg = await _part(features, "mfa", "AuditLog.Read.All", g.all(
                "/reports/authenticationMethods/userRegistrationDetails", params={"$top": "999"}, max_pages=40))
            mfa_source = "registration" if reg is not None else None
            mfa_fallback: dict[str, dict[str, Any]] | None = None
            fb_at = _dt((prev.get("mfa_fallback") or {}).get("at"))
            if reg is None:
                if fb_at and started - fb_at < timedelta(hours=float(app_cfg.get("mfa_fallback_hours") or 6)):
                    mfa_fallback = (prev.get("mfa_fallback") or {}).get("users") or {}
                    mfa_source = "methods"
                    features["mfa"] = {"ok": True, "source": "methods"}
                else:
                    try:
                        mfa_fallback = await _mfa_fallback(g, users)
                        fb_at = started
                        mfa_source = "methods"
                        features["mfa"] = {"ok": True, "source": "methods"}
                    except GraphError as e:
                        features["mfa"] = {"ok": False, "reason": classify(e), "message": str(e)[:300],
                                           "permission": "UserAuthenticationMethod.Read.All"}
            risky = await _part(features, "risky_users", "IdentityRiskyUser.Read.All", g.all(
                "/identityProtection/riskyUsers",
                params={"$filter": "riskState eq 'atRisk' or riskState eq 'confirmedCompromised'", "$top": "500"},
                max_pages=5))
            detections = await _part(features, "risk_detections", "IdentityRiskEvent.Read.All", g.all(
                "/identityProtection/riskDetections",
                params={"$filter": f"detectedDateTime ge {_graph_time(started - timedelta(days=7))}", "$top": "200"},
                max_pages=2))
            secdef = await _part(features, "security_defaults", "Policy.Read.All",
                                 g.get("/policies/identitySecurityDefaultsEnforcementPolicy"))
            ca = await _part(features, "conditional_access", "Policy.Read.All", g.all(
                "/identity/conditionalAccess/policies", params={"$select": "id,displayName,state"}))
            app = await _part(features, "app_credentials", "Application.Read.All", g.get(
                f"/applications(appId='{client_id}')",
                params={"$select": "displayName,passwordCredentials,keyCredentials"}))
            sig = await _part(features, "signins", "AuditLog.Read.All", g.all(
                "/auditLogs/signIns",
                params={"$filter": f"createdDateTime ge {_graph_time(since)}", "$top": "500"}, max_pages=10))
            signins = sig or []
            granted = list(g.roles)
    except GraphError as e:
        async with sessionmaker()() as db:
            t = await db.get(M365Tenant, row_id)
            if t is not None:
                t.status, t.last_error, t.last_sync_at = "error", str(e)[:1000], started
                if e.code == "auth_failed":
                    forget_token(row_id)
                await db.commit()
        events.publish("m365.synced", {"tenant_id": row_id, "status": "error"})
        raise

    # ---- build the snapshot
    sku_by_id = {s.get("skuId"): s for s in skus or []}
    role_of: dict[str, list[str]] = {}
    for r in roles or []:
        for m in r.get("members") or []:
            role_of.setdefault(m.get("id"), []).append(r.get("displayName") or "?")
    mfa_of: dict[str, dict[str, Any]] = {}
    for d in reg or []:
        mfa_of[d.get("id")] = {"registered": bool(d.get("isMfaRegistered")), "capable": d.get("isMfaCapable"),
                               "methods": sorted(d.get("methodsRegistered") or []),
                               "default": d.get("defaultMfaMethod") if d.get("defaultMfaMethod") not in (None, "none") else None,
                               "source": "registration"}
    if mfa_fallback is not None:
        mfa_of = {**mfa_fallback, **mfa_of}
    risk_of = {r.get("id"): {"level": r.get("riskLevel"), "state": r.get("riskState"), "detail": r.get("riskDetail"),
                             "updated": r.get("riskLastUpdatedDateTime")} for r in risky or []}
    out_users: list[dict[str, Any]] = []
    for u in users:
        uid = u.get("id")
        act = u.get("signInActivity") or {}
        lic = [sku_by_id.get(a.get("skuId"), {}).get("skuPartNumber") or a.get("skuId") for a in u.get("assignedLicenses") or []]
        rl = sorted(set(role_of.get(uid, [])))
        last = [d for d in (_dt(act.get("lastSignInDateTime")), _dt(act.get("lastNonInteractiveSignInDateTime"))) if d]
        out_users.append({
            "id": uid, "name": u.get("displayName"), "upn": u.get("userPrincipalName"), "mail": u.get("mail"),
            "enabled": bool(u.get("accountEnabled")), "type": (u.get("userType") or "Member").lower(),
            "licences": [x for x in lic if x], "created": u.get("createdDateTime"),
            "last_sign_in": act.get("lastSignInDateTime"), "last_activity": _iso(max(last)) if last else None,
            "synced": bool(u.get("onPremisesSyncEnabled")), "roles": rl,
            "admin": any(r not in NON_ADMIN_ROLES for r in rl),
            "mfa": mfa_of.get(uid), "risk": risk_of.get(uid), "job": u.get("jobTitle"), "department": u.get("department"),
        })
    out_users.sort(key=lambda x: (str(x.get("name") or x.get("upn") or "")).lower())
    licences = []
    for s in skus or []:
        pre = s.get("prepaidUnits") or {}
        enabled, consumed = int(pre.get("enabled") or 0), int(s.get("consumedUnits") or 0)
        part = s.get("skuPartNumber")
        licences.append({"sku_id": s.get("skuId"), "sku": part, "name": sku_name(part), "enabled": enabled,
                         "consumed": consumed, "available": enabled - consumed, "warning": int(pre.get("warning") or 0),
                         "suspended": int(pre.get("suspended") or 0), "status": s.get("capabilityStatus"),
                         "applies_to": s.get("appliesTo"), "free": enabled >= 10000})
    licences.sort(key=lambda x: (x["free"], x["name"].lower()))
    dets = [{"id": d.get("id"), "user_id": d.get("userId"), "upn": d.get("userPrincipalName"),
             "type": d.get("riskEventType"), "level": d.get("riskLevel"), "state": d.get("riskState"),
             "at": d.get("detectedDateTime"), "ip": d.get("ipAddress"),
             "location": ", ".join(x for x in ((d.get("location") or {}).get("city"),
                                               (d.get("location") or {}).get("countryOrRegion")) if x) or None}
            for d in detections or []]
    dets.sort(key=lambda x: x.get("at") or "", reverse=True)
    app_cred = None
    if app is not None:
        if method == "secret":
            creds = app.get("passwordCredentials") or []
            mine = [c for c in creds if secret_hint and c.get("hint") == secret_hint] or creds
        else:
            creds = app.get("keyCredentials") or []
            mine = [c for c in creds if cert_thumb and _thumb(c.get("customKeyIdentifier")) == cert_thumb] or creds
        ends = [d for d in (_dt(c.get("endDateTime")) for c in mine) if d]
        if ends:
            # Of several matching credentials the newest is the one in use.
            app_cred = {"expires": _iso(max(ends)),
                        "name": next((c.get("displayName") for c in mine if c.get("displayName")), None),
                        "matched": len(mine) < len(creds) or len(creds) == 1, "app": app.get("displayName")}
    enabled_members = [u for u in out_users if u["enabled"] and u["type"] == "member"]
    counts = {
        "users": len(out_users), "members": sum(1 for u in out_users if u["type"] == "member"),
        "guests": sum(1 for u in out_users if u["type"] == "guest"),
        "enabled": sum(1 for u in out_users if u["enabled"]),
        "disabled": sum(1 for u in out_users if not u["enabled"]),
        "licensed": sum(1 for u in out_users if u["licences"]),
        "admins": sum(1 for u in out_users if u["admin"] and u["enabled"]),
        "mfa_known": sum(1 for u in enabled_members if u["mfa"] is not None),
        "mfa_registered": sum(1 for u in enabled_members if (u["mfa"] or {}).get("registered")),
        "no_mfa": sum(1 for u in enabled_members if u["mfa"] is not None and not u["mfa"].get("registered")),
        "admins_no_mfa": sum(1 for u in enabled_members if u["admin"] and u["mfa"] is not None
                             and not u["mfa"].get("registered")),
        "risky": sum(1 for u in out_users if u["risk"]),
        "high_risk": sum(1 for u in out_users if (u["risk"] or {}).get("level") == "high"),
        "licences_paid": sum(x["enabled"] for x in licences if not x["free"]),
        "licences_used": sum(x["consumed"] for x in licences if not x["free"]),
    }
    security = {"security_defaults": (secdef or {}).get("isEnabled") if secdef is not None else None,
                "ca_policies": len(ca) if ca is not None else None,
                "ca_enabled": sum(1 for p in ca or [] if p.get("state") == "enabled") if ca is not None else None}
    ok_parts = [k for k, v in features.items() if v.get("ok")]
    status = "ok" if len(ok_parts) == len(features) else "partial"
    org0 = org[0] if org else {}
    snapshot = {
        "organization": {"id": org0.get("id"), "name": org0.get("displayName"),
                         "domains": [d.get("name") for d in org0.get("verifiedDomains") or []],
                         "default_domain": next((d.get("name") for d in org0.get("verifiedDomains") or [] if d.get("isDefault")), None)},
        "users": out_users, "licences": licences, "risk_detections": dets[:200], "security": security,
        "app_credential": app_cred, "counts": counts, "features": features, "granted": granted,
        "mfa_source": mfa_source, "synced_at": started.isoformat(),
    }
    if mfa_fallback is not None:
        snapshot["mfa_fallback"] = {"at": _iso(fb_at), "users": mfa_fallback}

    # ---- sign-ins that stand out
    stored = 0
    newest = since
    async with sessionmaker()() as db:
        t = await db.get(M365Tenant, row_id)
        if t is None:
            return {}
        rows = []
        for s in signins:
            at = _dt(s.get("createdDateTime"))
            if at and at > newest:
                newest = at
            flags, success, code = _signin_flags(s, allowed)
            if not flags or not s.get("id") or at is None:
                continue
            loc = s.get("location") or {}
            st = s.get("status") or {}
            rows.append({"id": uuid.uuid4().hex, "tenant_id": row_id, "customer_id": customer_id,
                         "signin_id": str(s["id"])[:64], "at": at, "user_id": s.get("userId"),
                         "upn": (s.get("userPrincipalName") or "")[:255] or None,
                         "display_name": (s.get("userDisplayName") or "")[:255] or None,
                         "app": (s.get("appDisplayName") or "")[:255] or None,
                         "client_app": (s.get("clientAppUsed") or "")[:100] or None,
                         "ip": (s.get("ipAddress") or "")[:64] or None,
                         "country": (loc.get("countryOrRegion") or "")[:8] or None, "city": (loc.get("city") or "")[:120] or None,
                         "success": success, "error_code": code or None,
                         "failure_reason": (st.get("failureReason") or "")[:500] or None,
                         "risk_level": s.get("riskLevelAggregated") or s.get("riskLevelDuringSignIn"),
                         "risk_state": s.get("riskState"), "ca_status": s.get("conditionalAccessStatus"), "flags": flags})
        if rows:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            for i in range(0, len(rows), 500):
                res = await db.execute(pg_insert(M365SignIn).values(rows[i:i + 500])
                                       .on_conflict_do_nothing(constraint="uq_m365_signin"))
                stored += res.rowcount or 0
        t.snapshot = snapshot
        t.status, t.last_sync_at, t.last_ok_at = status, started, started
        t.last_error = None if status == "ok" else "; ".join(
            f"{k}: {v.get('reason')}" for k, v in features.items() if not v.get("ok"))[:1000]
        if features.get("signins", {}).get("ok"):
            # Sign-ins reach the log a few minutes late: read the last half hour again next time.
            t.signins_since = max(since, min(newest, started) - timedelta(minutes=30))
        await db.commit()
    events.publish("m365.synced", {"tenant_id": row_id, "status": status})
    return {"status": status, "users": counts["users"], "signins_read": len(signins), "signins_stored": stored,
            "features": features}


def _thumb(v: Any) -> str | None:
    """customKeyIdentifier of a key credential (base64 of the thumbprint, or the hex thumbprint) as hex."""
    if not v:
        return None
    s = str(v)
    if re.fullmatch(r"[0-9A-Fa-f]{40}", s):
        return s.upper()
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4))
    except ValueError:
        return None
    txt = raw.decode("ascii", "ignore")
    if re.fullmatch(r"[0-9A-Fa-f]{40}", txt):
        return txt.upper()
    return raw.hex().upper() if len(raw) == 20 else None


async def test(db: AsyncSession, t: M365Tenant, *, username: str | None = None) -> dict[str, Any]:
    """Sign in as the app and read the organisation: shows the granted permissions and what is missing."""
    cred = await load_credential(db, t, log_use=True, reason=f"Microsoft 365 connection test of {t.name}", username=username)
    await db.commit()
    forget_token(t.id)
    async with Graph(t.id, cred, timeout=20) as g:
        org = (await g.get("/organization", params={"$select": "id,displayName,verifiedDomains"}) or {}).get("value") or []
        granted = set(g.roles)
    return {"organization": org[0].get("displayName") if org else None,
            "tenant_id": org[0].get("id") if org else None, **permissions_view(granted)}


def permissions_view(granted: set[str] | list[str]) -> dict[str, Any]:
    g = set(granted)
    implied = {"User.Read.All": {"User.ReadWrite.All", "Directory.Read.All", "Directory.ReadWrite.All"},
               "Directory.Read.All": {"Directory.ReadWrite.All"},
               "UserAuthenticationMethod.Read.All": {"UserAuthenticationMethod.ReadWrite.All"},
               "IdentityRiskyUser.Read.All": {"IdentityRiskyUser.ReadWrite.All"},
               "Policy.Read.All": {"Policy.ReadWrite.ConditionalAccess"},
               "Application.Read.All": {"Application.ReadWrite.All", "Application.ReadWrite.OwnedBy"},
               "User.EnableDisableAccount.All": {"User.ReadWrite.All", "Directory.ReadWrite.All"},
               "User.RevokeSessions.All": {"User.ReadWrite.All", "Directory.ReadWrite.All"},
               "User-PasswordProfile.ReadWrite.All": {"User.ReadWrite.All", "Directory.ReadWrite.All"}}

    def has(p: str) -> bool:
        return p in g or bool(implied.get(p, set()) & g)

    return {"granted": sorted(g),
            "read": [{"permission": p, "for": w, "granted": has(p)} for p, w in READ_PERMISSIONS],
            "write": [{"permission": p, "for": w, "granted": has(p)} for p, w in WRITE_PERMISSIONS],
            "actions": {a: any(has(p) for p in ps) for a, ps in ACTION_PERMISSIONS.items()}}


# ------------------------------------------------------------------ tenants
async def find_tenant(db: AsyncSession, ref: str, customer_id: str | None = None) -> M365Tenant:
    t = await db.get(M365Tenant, ref)
    if t is None:
        q = select(M365Tenant).where(func.lower(M365Tenant.name) == ref.strip().lower())
        if customer_id:
            q = q.where(M365Tenant.customer_id == customer_id)
        rows = list((await db.execute(q)).scalars())
        if len(rows) > 1:
            raise M365Error(f"more than one tenant is called {ref}; use its id", code="ambiguous")
        t = rows[0] if rows else None
    if t is None or (customer_id and t.customer_id != customer_id):
        raise M365Error(f"Microsoft 365 tenant {ref} not found", code="not_found")
    return t


async def tenants_of(db: AsyncSession, customer_id: str | None = None) -> list[M365Tenant]:
    q = select(M365Tenant).order_by(M365Tenant.name)
    if customer_id:
        q = q.where(M365Tenant.customer_id == customer_id)
    return list((await db.execute(q)).scalars())


async def tenant_for_customer(db: AsyncSession, customer_id: str, tenant: str | None = None) -> M365Tenant:
    if tenant:
        return await find_tenant(db, tenant, customer_id)
    rows = await tenants_of(db, customer_id)
    if not rows:
        raise M365Error("this customer has no Microsoft 365 tenant connected", code="not_found")
    if len(rows) > 1:
        raise M365Error("this customer has several tenants: " + ", ".join(t.name for t in rows) + "; name one",
                        code="ambiguous")
    return rows[0]


def tenant_out(t: M365Tenant, *, vault_name: str | None = None) -> dict[str, Any]:
    snap = t.snapshot or {}
    app_cred = snap.get("app_credential") or {}
    exp = _dt(app_cred.get("expires"))
    return {
        "id": t.id, "customer_id": t.customer_id, "customer_name": t.customer.name if t.customer else None,
        "name": t.name, "tenant_id": t.tenant_id, "client_id": t.client_id, "auth_method": t.auth_method,
        "vault_item_id": t.vault_item_id, "vault_item_name": vault_name, "enabled": t.enabled, "status": t.status,
        "last_sync_at": _iso(t.last_sync_at), "last_ok_at": _iso(t.last_ok_at), "last_error": t.last_error,
        "settings": tenant_settings(t), "organization": snap.get("organization") or {},
        "counts": snap.get("counts") or {}, "features": snap.get("features") or {},
        "security": snap.get("security") or {}, "mfa_source": snap.get("mfa_source"),
        "app_credential": app_cred or None,
        "credential_days": int((exp - datetime.now(UTC)).total_seconds() // 86400) if exp else None,
        **permissions_view(snap.get("granted") or []),
        "created_at": _iso(t.created_at), "created_by": t.created_by, "updated_at": _iso(t.updated_at),
        "updated_by": t.updated_by,
    }


async def vault_names(db: AsyncSession, ids: list[str | None]) -> dict[str, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {i: n for i, n in (await db.execute(select(VaultItem.id, VaultItem.name).where(VaultItem.id.in_(ids))))}


async def store_credential(db: AsyncSession, *, customer_id: str, name: str, auth_method: str, client_id: str,
                           tenant: str, secret: str | None = None, pem: str | None = None,
                           info: dict[str, Any] | None = None, item: VaultItem | None = None,
                           by: str | None = None) -> VaultItem:
    """Put a client secret or certificate in the customer's vault (a new item, or a new secret on
    ``item``: the old one goes to its history)."""
    from mmn.services import vault

    if auth_method == "secret":
        payload = {"username": client_id, "password": (secret or "").strip(),
                   "notes": f"Microsoft 365 app registration, tenant {tenant}. Used by Stackpilot to read the tenant."}
        kind = "api_key"
    else:
        fields = [{"name": "Thumbprint", "value": (info or {}).get("thumbprint") or "", "secret": False},
                  {"name": "Expires", "value": (info or {}).get("expires") or "", "secret": False}]
        payload = {"username": client_id, "password": pem or "", "fields": fields,
                   "notes": f"Microsoft 365 app registration, tenant {tenant}. Certificate with private key (PEM)."}
        kind = "certificate"
    if item is not None:
        await vault.update_item(db, item, payload, by=by)
        return item
    return await vault.create_item(db, customer_id=customer_id, name=f"Microsoft 365 app · {name}"[:200], kind=kind,
                                   url="https://entra.microsoft.com", payload=payload, tags=["m365"],
                                   ai_access="never", by=by)


# ------------------------------------------------------------------ reading the snapshot
USER_FILTERS = ("all", "admins", "no_mfa", "risky", "disabled", "guests", "stale", "unlicensed", "licensed",
                "disabled_licensed")


def stale(u: dict[str, Any], days: int, now: datetime) -> bool:
    if not u.get("enabled") or not u.get("licences") or u.get("type") != "member":
        return False
    last = _dt(u.get("last_activity")) or _dt(u.get("last_sign_in"))
    created = _dt(u.get("created"))
    ref = last or created
    return ref is not None and now - ref > timedelta(days=days) and (last is not None or created is not None)


def filter_users(t: M365Tenant, flt: str = "all", q: str | None = None) -> list[dict[str, Any]]:
    users = list((t.snapshot or {}).get("users") or [])
    now = datetime.now(UTC)
    days = int(tenant_settings(t)["stale_days"])
    has_activity = bool(((t.snapshot or {}).get("features") or {}).get("last_signin", {}).get("ok"))
    sel = {
        "all": lambda u: True,
        "admins": lambda u: u.get("admin"),
        "no_mfa": lambda u: u.get("enabled") and u.get("type") == "member" and u.get("mfa") is not None
        and not u["mfa"].get("registered"),
        "risky": lambda u: bool(u.get("risk")),
        "disabled": lambda u: not u.get("enabled"),
        "guests": lambda u: u.get("type") == "guest",
        "stale": lambda u: has_activity and stale(u, days, now),
        "unlicensed": lambda u: u.get("enabled") and u.get("type") == "member" and not u.get("licences"),
        "licensed": lambda u: bool(u.get("licences")),
        "disabled_licensed": lambda u: not u.get("enabled") and bool(u.get("licences")),
    }.get(flt)
    if sel is None:
        raise M365Error(f"filter must be one of {', '.join(USER_FILTERS)}", code="invalid")
    out = [u for u in users if sel(u)]
    if q:
        ql = q.strip().lower()
        out = [u for u in out if ql in " ".join(str(u.get(k) or "") for k in ("name", "upn", "mail", "department",
                                                                                "job")).lower()]
    return [{**u, "stale": has_activity and stale(u, days, now)} for u in out]


def find_user(t: M365Tenant, ref: str) -> dict[str, Any] | None:
    r = ref.strip().lower()
    users = (t.snapshot or {}).get("users") or []
    for key in ("id", "upn", "mail"):
        for u in users:
            if str(u.get(key) or "").lower() == r:
                return u
    named = [u for u in users if str(u.get("name") or "").lower() == r]
    return named[0] if len(named) == 1 else None


async def signins(db: AsyncSession, *, customer_id: str | None = None, tenant_id: str | None = None,
                  user: str | None = None, flag: str | None = None, hours: int = 24 * 7,
                  limit: int = 200) -> list[dict[str, Any]]:
    q = select(M365SignIn).where(M365SignIn.at >= datetime.now(UTC) - timedelta(hours=max(1, min(hours, 24 * 90))))
    if customer_id:
        q = q.where(M365SignIn.customer_id == customer_id)
    if tenant_id:
        q = q.where(M365SignIn.tenant_id == tenant_id)
    if user:
        ul = user.strip().lower()
        q = q.where((func.lower(M365SignIn.upn) == ul) | (M365SignIn.user_id == user.strip()))
    rows = list((await db.execute(q.order_by(M365SignIn.at.desc()).limit(min(limit, 1000) * (3 if flag else 1)))).scalars())
    if flag:
        rows = [r for r in rows if flag in (r.flags or [])]
    return [signin_out(r) for r in rows[:limit]]


def signin_out(r: M365SignIn) -> dict[str, Any]:
    return {"id": r.id, "tenant_id": r.tenant_id, "customer_id": r.customer_id, "at": _iso(r.at), "user_id": r.user_id,
            "upn": r.upn, "name": r.display_name, "app": r.app, "client_app": r.client_app, "ip": r.ip,
            "country": r.country, "city": r.city, "success": r.success, "error_code": r.error_code,
            "failure_reason": r.failure_reason, "risk_level": r.risk_level, "risk_state": r.risk_state,
            "ca_status": r.ca_status, "flags": r.flags or []}


async def failed_counts(db: AsyncSession, tenant_ids: list[str], hours: int = 24) -> dict[tuple[str, str], int]:
    if not tenant_ids:
        return {}
    rows = (await db.execute(
        select(M365SignIn.tenant_id, M365SignIn.upn, func.count())
        .where(M365SignIn.tenant_id.in_(tenant_ids), M365SignIn.success.is_(False),
               M365SignIn.at >= datetime.now(UTC) - timedelta(hours=hours))
        .group_by(M365SignIn.tenant_id, M365SignIn.upn))).all()
    return {(tid, upn or "?"): int(n) for tid, upn, n in rows}


async def foreign_success(db: AsyncSession, tenant_ids: list[str], hours: int = 24) -> list[M365SignIn]:
    if not tenant_ids:
        return []
    rows = (await db.execute(select(M365SignIn).where(
        M365SignIn.tenant_id.in_(tenant_ids), M365SignIn.success.is_(True),
        M365SignIn.at >= datetime.now(UTC) - timedelta(hours=hours)).order_by(M365SignIn.at.desc()))).scalars()
    return [r for r in rows if "foreign" in (r.flags or [])]


def overview(t: M365Tenant) -> dict[str, Any]:
    """The few numbers that matter, for the customer page and get_customer_context."""
    snap = t.snapshot or {}
    c = snap.get("counts") or {}
    return {"tenant": t.name, "status": t.status, "last_sync_at": _iso(t.last_sync_at), "users": c.get("enabled"),
            "admins": c.get("admins"), "no_mfa": c.get("no_mfa"), "admins_no_mfa": c.get("admins_no_mfa"),
            "risky": c.get("risky"), "licences_paid": c.get("licences_paid"), "licences_used": c.get("licences_used"),
            "security_defaults": (snap.get("security") or {}).get("security_defaults"),
            "ca_enabled": (snap.get("security") or {}).get("ca_enabled")}


# ------------------------------------------------------------------ write actions
async def user_action(t: M365Tenant, user_ref: str, action: str, *, by: str | None = None,
                      reason: str | None = None) -> dict[str, Any]:
    """Run one action on a user of the tenant. reset_password stores the temporary password as a new item in
    the customer's vault (never returned)."""
    if action not in ACTIONS:
        raise M365Error(f"action must be one of {', '.join(ACTIONS)}", code="invalid")
    async with sessionmaker()() as db:
        t = await db.get(M365Tenant, t.id)
        if t is None:
            raise M365Error("tenant not found", code="not_found")
        cred = await load_credential(db, t, log_use=True, username=by,
                                     reason=f"Microsoft 365 action {action} on {user_ref}: {reason or ''}".strip())
        await db.commit()
        u = find_user(t, user_ref)
        tenant_name, customer_id = t.name, t.customer_id
    result: dict[str, Any] = {"action": action, "tenant": tenant_name}
    async with Graph(t.id, cred) as g:
        if u is None:
            if "@" not in user_ref and not _GUID.match(user_ref.strip()):
                raise M365Error(f"user {user_ref} not found in {tenant_name} (use the sign-in name or id)", code="not_found")
            try:
                live = await g.get(f"/users/{quote(user_ref.strip())}",
                                   params={"$select": "id,displayName,userPrincipalName,accountEnabled"})
            except GraphError as e:
                raise M365Error(f"user {user_ref} not found: {e}", code="not_found") from e
            u = {"id": live["id"], "name": live.get("displayName"), "upn": live.get("userPrincipalName")}
        uid = u["id"]
        result.update(user=u.get("upn"), name=u.get("name"), user_id=uid)
        try:
            if action in ("block", "unblock"):
                await g.request("PATCH", f"/users/{uid}", json_body={"accountEnabled": action == "unblock"})
                if action == "block":
                    try:
                        await g.request("POST", f"/users/{uid}/revokeSignInSessions")
                        result["sessions_revoked"] = True
                    except GraphError as e:
                        result["sessions_revoked"] = False
                        result["note"] = f"blocked, but signing out failed: {e}"
            elif action == "revoke_sessions":
                await g.request("POST", f"/users/{uid}/revokeSignInSessions")
            elif action == "reset_mfa":
                result["removed"] = await _reset_mfa(g, uid)
            elif action == "reset_password":
                from mmn.services import vault

                temp = vault.generate_password(16, symbols=True)
                await g.request("PATCH", f"/users/{uid}", json_body={
                    "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": temp}})
                try:
                    await g.request("POST", f"/users/{uid}/revokeSignInSessions")
                except GraphError:
                    pass
                async with sessionmaker()() as db:
                    item = await vault.create_item(
                        db, customer_id=customer_id, name=f"M365 temporary password · {u.get('upn') or uid}"[:200],
                        kind="login", url="https://portal.office.com", tags=["m365"], ai_access="never", by=by,
                        payload={"username": u.get("upn") or "", "password": temp,
                                 "notes": f"Temporary password set by Stackpilot ({by or 'system'}); the user must "
                                          "change it at the next sign-in."})
                    await vault.log_access(db, item, "create", username=by, reason=reason)
                    await db.commit()
                    result["vault_item"] = {"id": item.id, "name": item.name}
            elif action in ("confirm_compromised", "dismiss_risk"):
                path = "confirmCompromised" if action == "confirm_compromised" else "dismiss"
                await g.request("POST", f"/identityProtection/riskyUsers/{path}", json_body={"userIds": [uid]})
        except GraphError as e:
            kind = classify(e)
            if kind == "no_permission":
                need = " or ".join(ACTION_PERMISSIONS[action])
                raise M365Error(f"the app may not do this: give it the application permission {need} with admin "
                                f"consent (Microsoft says: {e})", code="no_permission", status=e.status) from e
            raise M365Error(f"Microsoft Graph refused {action}: {e}", code=kind, status=e.status) from e
    # Reflect the change at once; the next sync confirms it.
    async with sessionmaker()() as db:
        t2 = await db.get(M365Tenant, t.id)
        if t2 is not None:
            snap = dict(t2.snapshot or {})
            users = []
            for x in snap.get("users") or []:
                if x.get("id") == uid:
                    x = dict(x)
                    if action in ("block", "unblock"):
                        x["enabled"] = action == "unblock"
                    elif action == "reset_mfa" and x.get("mfa") is not None:
                        x["mfa"] = {**x["mfa"], "registered": False, "methods": [], "default": None}
                    elif action in ("confirm_compromised", "dismiss_risk"):
                        x["risk"] = None if action == "dismiss_risk" else {**(x.get("risk") or {}), "level": "high",
                                                                           "state": "confirmedCompromised"}
                users.append(x)
            snap["users"] = users
            t2.snapshot = snap
            await db.commit()
    events.publish("m365.synced", {"tenant_id": t.id, "status": "action"})
    result["ok"] = True
    return result


async def _reset_mfa(g: Graph, uid: str) -> list[str]:
    """Remove every MFA method (password and email stay). The default method can only go last, so a few
    rounds."""
    removed: list[str] = []
    last_error: GraphError | None = None
    for _ in range(4):
        methods = await g.all(f"/users/{uid}/authentication/methods", max_pages=2)
        todo = [(METHOD_PATHS[m.get("@odata.type")], m.get("id")) for m in methods if m.get("@odata.type") in METHOD_PATHS]
        if not todo:
            return removed
        progress = False
        for path, mid in todo:
            try:
                await g.request("DELETE", f"/users/{uid}/authentication/{path}/{mid}")
                removed.append(path.removesuffix("Methods"))
                progress = True
            except GraphError as e:
                if classify(e) == "no_permission":
                    raise
                last_error = e
        if not progress:
            break
    if last_error is not None:
        raise last_error
    return removed


# ------------------------------------------------------------------ alerts (monitoring)
async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    want: dict[str, dict[str, Any]] = {}
    tenants = list((await db.execute(select(M365Tenant).where(M365Tenant.enabled.is_(True)))).scalars())
    if not tenants:
        return want
    failed = await failed_counts(db, [t.id for t in tenants])
    foreign = await foreign_success(db, [t.id for t in tenants])
    for t in tenants:
        cfg = tenant_settings(t)
        cname = t.customer.name if t.customer else "?"
        base = {"source": f"{cname} · Microsoft 365", "customer_id": t.customer_id, "tenant_id": t.id, "tenant": t.name}
        if t.status == "error":
            since = t.last_ok_at or t.created_at
            if since and now - since > timedelta(minutes=45):
                want[f"m365:{t.id}"] = {"node_id": None, "kind": "m365", "severity": "warning", "detail": base,
                                        "message": f"Microsoft 365 {t.name} cannot be read: {(t.last_error or '')[:200]}"}
            continue
        snap = t.snapshot or {}
        users = snap.get("users") or []
        if cfg["alert_risky"]:
            for u in users:
                r = u.get("risk") or {}
                if r.get("level") in ("high", "medium") and r.get("state") in ("atRisk", "confirmedCompromised"):
                    want[f"m365-risk:{t.id}:{u['id']}"] = {
                        "node_id": None, "kind": "m365", "severity": "critical" if r["level"] == "high" else "warning",
                        "detail": {**base, "user": u.get("upn"), "risk": r.get("level")},
                        "message": f"Microsoft 365 user {u.get('upn')} ({t.name}) is at {r['level']} risk"
                                   + (" (confirmed compromised)" if r.get("state") == "confirmedCompromised" else "")}
        if cfg["alert_admin_mfa"]:
            bad = [u.get("upn") for u in users if u.get("admin") and u.get("enabled") and u.get("type") == "member"
                   and u.get("mfa") is not None and not u["mfa"].get("registered")]
            if bad:
                want[f"m365-admin-mfa:{t.id}"] = {
                    "node_id": None, "kind": "m365", "severity": "critical", "value": len(bad),
                    "detail": {**base, "users": bad[:20]},
                    "message": f"{len(bad)} Microsoft 365 admin(s) of {t.name} without MFA: {', '.join(bad[:5])}"}
        if cfg["alert_licences"]:
            for lic in snap.get("licences") or []:
                if lic.get("free"):
                    continue
                if lic["consumed"] > lic["enabled"] or lic.get("status") in ("Warning", "Suspended", "LockedOut"):
                    want[f"m365-licence:{t.id}:{lic['sku_id']}"] = {
                        "node_id": None, "kind": "m365", "severity": "warning", "detail": {**base, "sku": lic["sku"]},
                        "message": (f"Licence {lic['name']} of {t.name} is {str(lic.get('status')).lower()}"
                                    if lic.get("status") in ("Warning", "Suspended", "LockedOut") else
                                    f"Licence {lic['name']} of {t.name}: {lic['consumed']} assigned, "
                                    f"{lic['enabled']} bought")}
        if cfg["alert_secret"]:
            exp = _dt((snap.get("app_credential") or {}).get("expires"))
            if exp is not None:
                days = int((exp - now).total_seconds() // 86400)
                if days <= int(cfg["secret_warn_days"]):
                    what = "certificate" if t.auth_method == "certificate" else "client secret"
                    want[f"m365-secret:{t.id}"] = {
                        "node_id": None, "kind": "m365", "severity": "critical" if days < 7 else "warning",
                        "value": days, "detail": base,
                        "message": (f"The {what} of the Microsoft 365 app of {t.name} expired {-days} day(s) ago"
                                    if days < 0 else
                                    f"The {what} of the Microsoft 365 app of {t.name} expires in {days} day(s)")}
        if cfg["alert_signins"]:
            for (tid, upn), n in failed.items():
                if tid == t.id and n >= int(cfg["failed_threshold"]):
                    want[f"m365-failed:{t.id}:{upn.lower()}"] = {
                        "node_id": None, "kind": "m365", "severity": "warning", "value": n,
                        "detail": {**base, "user": upn},
                        "message": f"{n} failed sign-ins of {upn} ({t.name}) in 24 hours"}
            seen: set[str] = set()
            for s in foreign:
                if s.tenant_id != t.id or (s.upn or "").lower() in seen:
                    continue
                seen.add((s.upn or "").lower())
                want[f"m365-foreign:{t.id}:{(s.upn or '?').lower()}"] = {
                    "node_id": None, "kind": "m365", "severity": "warning",
                    "detail": {**base, "user": s.upn, "country": s.country, "ip": s.ip},
                    "message": f"{s.upn} ({t.name}) signed in from {s.city + ', ' if s.city else ''}{s.country} "
                               f"({s.ip})"}
    return want


# ------------------------------------------------------------------ nightly findings (morning summary)
def _names(users: list[dict[str, Any]], n: int = 5) -> str:
    names = [str(u.get("upn") or u.get("name") or "?") for u in users]
    return ", ".join(names[:n]) + (f" (+{len(names) - n})" if len(names) > n else "")


async def findings(db: AsyncSession, e: Environment, now: datetime, finding) -> list[dict[str, Any]]:
    """Findings of the customer's tenants for the nightly checks, with proposals for risky users."""
    out: list[dict[str, Any]] = []
    tenants = [t for t in await tenants_of(db, e.id) if t.enabled]
    if not tenants:
        return out
    failed = await failed_counts(db, [t.id for t in tenants])
    foreign = await foreign_success(db, [t.id for t in tenants])
    for t in tenants:
        cfg = tenant_settings(t)
        target = f"M365 {t.name}"
        if t.status == "error" or not t.snapshot:
            out.append(finding("high", "identity", target, f"Microsoft 365 {t.name} could not be read: {t.last_error or '?'}.",
                               "Check the app registration, its secret or certificate and the admin consent.",
                               code="m365_sync_error", tenant=t.name, error=(t.last_error or "?")[:200]))
            continue
        snap = t.snapshot
        users = snap.get("users") or []
        for u in users:
            r = u.get("risk") or {}
            if r.get("level") in ("high", "medium") and r.get("state") in ("atRisk", "confirmedCompromised"):
                f = finding("high" if r["level"] == "high" else "medium", "identity", u.get("upn"),
                            f"{u.get('upn')} is at {r['level']} risk in Entra ID ({r.get('detail') or r.get('state')}).",
                            "Sign the user out everywhere and reset the password; block the account when the risk is "
                            "confirmed.", "m365_user_action", code="m365_risky_user", user=u.get("upn"),
                            level=r["level"], tenant=t.name)
                f["proposal"] = {"tool": "m365_user_action", "label": "m365_revoke" if r["level"] != "high" else "m365_block",
                                 "args": {"customer": e.id, "tenant": t.id, "user": u.get("upn"),
                                          "action": "block" if r["level"] == "high" else "revoke_sessions",
                                          "confirm": u.get("upn"),
                                          "reason": f"Nightly checks: {u.get('upn')} is at {r['level']} risk"}}
                out.append(f)
        members = [u for u in users if u.get("enabled") and u.get("type") == "member"]
        admins_bad = [u for u in members if u.get("admin") and u.get("mfa") is not None and not u["mfa"].get("registered")]
        for u in admins_bad:
            out.append(finding("high", "identity", u.get("upn"),
                               f"Administrator {u.get('upn')} ({', '.join(u.get('roles') or [])}) has no MFA.",
                               "Have the admin register MFA now, or require it with security defaults or Conditional Access.",
                               code="m365_admin_no_mfa", user=u.get("upn"), roles=", ".join(u.get("roles") or []), tenant=t.name))
        no_mfa = [u for u in members if not u.get("admin") and u.get("mfa") is not None and not u["mfa"].get("registered")
                  and u.get("licences")]
        if no_mfa:
            out.append(finding("medium", "identity", target, f"{len(no_mfa)} user(s) without MFA: {_names(no_mfa)}.",
                               "Ask them to register the Microsoft Authenticator app (aka.ms/mfasetup).",
                               code="m365_no_mfa", count=len(no_mfa), names=_names(no_mfa), tenant=t.name))
        sec = snap.get("security") or {}
        if sec.get("security_defaults") is False and sec.get("ca_enabled") == 0:
            out.append(finding("medium", "identity", target,
                               f"{t.name} has neither security defaults nor an enabled Conditional Access policy.",
                               "Turn on security defaults (Entra admin center > Overview > Properties), or require MFA "
                               "with Conditional Access.", code="m365_no_mfa_policy", tenant=t.name))
        if (snap.get("features") or {}).get("last_signin", {}).get("ok"):
            days = int(cfg["stale_days"])
            st = [u for u in members if stale(u, days, now)]
            if st:
                out.append(finding("low", "identity", target,
                                   f"{len(st)} licensed account(s) without a sign-in for {days} days: {_names(st)}.",
                                   "Block or remove accounts nobody uses and free their licences.",
                                   code="m365_stale_users", count=len(st), days=days, names=_names(st), tenant=t.name))
        dl = [u for u in users if not u.get("enabled") and u.get("licences")]
        if dl:
            out.append(finding("low", "identity", target,
                               f"{len(dl)} blocked account(s) still have a licence: {_names(dl)}.",
                               "Remove the licences (or convert mailboxes to shared mailboxes) to save costs.",
                               code="m365_disabled_licensed", count=len(dl), names=_names(dl), tenant=t.name))
        for lic in snap.get("licences") or []:
            if lic.get("free"):
                continue
            if lic["consumed"] > lic["enabled"]:
                out.append(finding("medium", "identity", target,
                                   f"Licence {lic['name']}: {lic['consumed']} assigned but only {lic['enabled']} bought.",
                                   "Buy licences or remove assignments before Microsoft turns them off.",
                                   code="m365_licence_short", licence=lic["name"], consumed=lic["consumed"],
                                   enabled=lic["enabled"], tenant=t.name))
            elif lic["available"] > 0 and lic["enabled"] > 0 and lic.get("status") == "Enabled":
                out.append(finding("info", "identity", target,
                                   f"Licence {lic['name']}: {lic['available']} of {lic['enabled']} not assigned.",
                                   "Lower the number of licences at the next renewal if they stay unused.",
                                   code="m365_licence_unused", licence=lic["name"], available=lic["available"],
                                   enabled=lic["enabled"], tenant=t.name))
            if lic.get("status") in ("Warning", "Suspended", "LockedOut"):
                out.append(finding("high", "identity", target,
                                   f"Licence {lic['name']} is {str(lic['status']).lower()} (subscription expired or unpaid).",
                                   "Renew the subscription in the Microsoft 365 admin center.",
                                   code="m365_licence_status", licence=lic["name"], status=str(lic["status"]).lower(),
                                   tenant=t.name))
        exp = _dt((snap.get("app_credential") or {}).get("expires"))
        if exp is not None:
            days = int((exp - now).total_seconds() // 86400)
            if days <= int(cfg["secret_warn_days"]):
                out.append(finding("high" if days < 7 else "medium", "identity", target,
                                   f"The secret or certificate of the Stackpilot app in {t.name} expires in {days} day(s).",
                                   "Create a new client secret or certificate in Entra and save it in Stackpilot.",
                                   code="m365_secret_expiring", days=days, tenant=t.name))
        bad_users = sorted({upn for (tid, upn), n in failed.items() if tid == t.id and n >= int(cfg["failed_threshold"])})
        if bad_users:
            out.append(finding("medium", "identity", target,
                               f"Many failed sign-ins in 24 hours for: {', '.join(bad_users[:5])}.",
                               "Check whether these are password attacks; make sure MFA is on for these accounts.",
                               code="m365_failed_signins", names=", ".join(bad_users[:5]), tenant=t.name))
        fs = [s for s in foreign if s.tenant_id == t.id]
        if fs:
            who = sorted({f"{s.upn} ({s.country})" for s in fs})
            out.append(finding("medium", "identity", target,
                               f"Sign-ins from another country in 24 hours: {', '.join(who[:5])}.",
                               "Check with the users; sign them out and reset the password if it was not them.",
                               code="m365_foreign_signins", names=", ".join(who[:5]), tenant=t.name))
        missing = [k for k, v in (snap.get("features") or {}).items() if not v.get("ok") and v.get("reason") == "no_permission"]
        if missing:
            out.append(finding("info", "identity", target,
                               f"Stackpilot cannot read part of {t.name}: {', '.join(missing)}.",
                               "Give the app the missing Microsoft Graph permissions with admin consent.",
                               code="m365_missing_permissions", parts=", ".join(missing), tenant=t.name))
    return out


async def refresh_customer(env_id: str) -> None:
    """Read the customer's tenants before the nightly checks."""
    async with sessionmaker()() as db:
        ids = [t.id for t in await tenants_of(db, env_id) if t.enabled]
    for tid in ids:
        try:
            await asyncio.wait_for(sync(tid), timeout=300)
        except Exception as ex:  # noqa: BLE001 - the findings report the error
            log.info("Microsoft 365 sync of %s before the nightly checks: %s", tid, ex)


# ------------------------------------------------------------------ scheduler
async def tick() -> int:
    """Read every tenant whose last sync is older than the interval."""
    async with sessionmaker()() as db:
        cfg = await get_setting(db, "m365")
        every = timedelta(minutes=max(5, int(cfg.get("sync_minutes") or 15)))
        now = datetime.now(UTC)
        due = [t.id for t in (await db.execute(select(M365Tenant).where(M365Tenant.enabled.is_(True)))).scalars()
               if t.id not in _running and (t.last_sync_at is None or now - t.last_sync_at >= every)]
    sem = asyncio.Semaphore(4)

    async def one(tid: str) -> None:
        async with sem:
            try:
                await asyncio.wait_for(sync(tid), timeout=600)
            except Exception as ex:  # noqa: BLE001
                log.info("Microsoft 365 sync of %s: %s", tid, ex)

    await asyncio.gather(*(one(t) for t in due))
    return len(due)


async def prune() -> int:
    async with sessionmaker()() as db:
        cfg = await get_setting(db, "m365")
        keep = max(1, int(cfg.get("signin_keep_days") or 30))
        res = await db.execute(delete(M365SignIn).where(M365SignIn.at < datetime.now(UTC) - timedelta(days=keep)))
        await db.commit()
        return res.rowcount or 0
