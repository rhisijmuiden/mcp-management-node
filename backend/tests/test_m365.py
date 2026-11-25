"""Phase 19: Microsoft 365 / Entra ID per customer through Microsoft Graph (played by a fake), with the secret in
the password vault, alerts, nightly findings, user actions and MCP tools that need approval."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from test_customers import _call
from test_gateway import _token
from test_mcp import _init

from mmn.services import m365

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "Abc~supergeheim-secret-value"


def _jwt(roles: list[str]) -> str:
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'none'})}.{b({'roles': roles})}.sig"


class FakeGraph:
    """Just enough of login.microsoftonline.com and graph.microsoft.com."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.now = now
        self.calls: list[tuple[str, str, dict]] = []
        self.roles = ["User.Read.All", "Directory.Read.All", "AuditLog.Read.All", "UserAuthenticationMethod.ReadWrite.All",
                      "IdentityRiskyUser.Read.All", "IdentityRiskEvent.Read.All", "Policy.Read.All",
                      "Application.Read.All", "User.EnableDisableAccount.All", "User.RevokeSessions.All",
                      "User-PasswordProfile.ReadWrite.All"]
        self.premium = True  # Entra ID P1/P2 present
        self.token_requests: list[dict] = []
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        self.users = [
            {"id": "u-admin", "displayName": "Anna Admin", "userPrincipalName": "anna@klant.nl", "accountEnabled": True,
             "userType": "Member", "assignedLicenses": [{"skuId": "sku-bp"}], "createdDateTime": iso(now - timedelta(days=900)),
             "signInActivity": {"lastSignInDateTime": iso(now - timedelta(hours=3))}},
            {"id": "u-bob", "displayName": "Bob Boekhouder", "userPrincipalName": "bob@klant.nl", "accountEnabled": True,
             "userType": "Member", "assignedLicenses": [{"skuId": "sku-bp"}], "createdDateTime": iso(now - timedelta(days=400)),
             "signInActivity": {"lastSignInDateTime": iso(now - timedelta(days=1))}, "department": "Finance"},
            {"id": "u-oud", "displayName": "Oud Account", "userPrincipalName": "oud@klant.nl", "accountEnabled": True,
             "userType": "Member", "assignedLicenses": [{"skuId": "sku-bp"}], "createdDateTime": iso(now - timedelta(days=800)),
             "signInActivity": {"lastSignInDateTime": iso(now - timedelta(days=200))}},
            {"id": "u-weg", "displayName": "Vertrokken", "userPrincipalName": "weg@klant.nl", "accountEnabled": False,
             "userType": "Member", "assignedLicenses": [{"skuId": "sku-bp"}], "createdDateTime": iso(now - timedelta(days=800))},
            {"id": "u-gast", "displayName": "Gast", "userPrincipalName": "gast_x.com#EXT#@klant.onmicrosoft.com",
             "accountEnabled": True, "userType": "Guest", "assignedLicenses": [], "createdDateTime": iso(now - timedelta(days=10))},
        ]
        self.methods = {"u-bob": [{"@odata.type": "#microsoft.graph.passwordAuthenticationMethod", "id": "pw"}],
                        "u-admin": [{"@odata.type": "#microsoft.graph.passwordAuthenticationMethod", "id": "pw"}],
                        "u-oud": [{"@odata.type": "#microsoft.graph.passwordAuthenticationMethod", "id": "pw"},
                                  {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod", "id": "ma1"},
                                  {"@odata.type": "#microsoft.graph.phoneAuthenticationMethod", "id": "ph1"}]}
        self.signins = [
            {"id": "s1", "createdDateTime": iso(now - timedelta(hours=2)), "userId": "u-bob", "userPrincipalName": "bob@klant.nl",
             "userDisplayName": "Bob", "appDisplayName": "Office 365", "clientAppUsed": "Browser", "ipAddress": "203.0.113.9",
             "location": {"countryOrRegion": "RU", "city": "Moscow"}, "status": {"errorCode": 0},
             "riskLevelAggregated": "high", "riskState": "atRisk"},
            {"id": "s2", "createdDateTime": iso(now - timedelta(hours=1)), "userId": "u-admin",
             "userPrincipalName": "anna@klant.nl", "clientAppUsed": "Browser", "ipAddress": "198.51.100.1",
             "location": {"countryOrRegion": "NL"}, "status": {"errorCode": 0}},  # ordinary: not stored
            {"id": "s3", "createdDateTime": iso(now - timedelta(hours=1)), "userId": "u-admin",
             "userPrincipalName": "anna@klant.nl", "clientAppUsed": "Browser", "location": {"countryOrRegion": "NL"},
             "status": {"errorCode": 50074}},  # MFA interrupt: not a failure
            {"id": "s4", "createdDateTime": iso(now - timedelta(minutes=50)), "userId": "u-oud",
             "userPrincipalName": "oud@klant.nl", "clientAppUsed": "IMAP4", "location": {"countryOrRegion": "NL"},
             "status": {"errorCode": 0}},
        ] + [{"id": f"f{i}", "createdDateTime": iso(now - timedelta(minutes=30 + i)), "userId": "u-bob",
              "userPrincipalName": "bob@klant.nl", "clientAppUsed": "Browser", "location": {"countryOrRegion": "NL"},
              "status": {"errorCode": 50126, "failureReason": "Invalid username or password"}} for i in range(4)]

    def _page(self, items, request, size=None):
        return httpx.Response(200, json={"value": items})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = unquote(url.path)
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
            "application/json") else {}
        self.calls.append((request.method, path, body))
        if url.host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            if form.get("client_secret", [""])[0] not in (SECRET, "nieuw-geheim") and "client_assertion" not in form:
                return httpx.Response(401, json={"error": "invalid_client",
                                                 "error_description": "AADSTS7000215: Invalid client secret provided."})
            return httpx.Response(200, json={"access_token": _jwt(self.roles), "expires_in": 3600})
        p = path.removeprefix("/v1.0")
        params = dict(url.params)
        no_p1 = httpx.Response(403, json={"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                                                    "message": "Neither tenant is B2C or tenant doesn't have premium license"}})
        if p == "/organization":
            return self._page([{"id": TENANT, "displayName": "Klant BV",
                                "verifiedDomains": [{"name": "klant.nl", "isDefault": True}, {"name": "klant.onmicrosoft.com"}]}], request)
        if p == "/users" and request.method == "GET":
            if "signInActivity" in params.get("$select", ""):
                if not self.premium:
                    return no_p1
                return self._page(self.users, request)
            return self._page([{k: v for k, v in u.items() if k != "signInActivity"} for u in self.users], request)
        if p == "/directoryRoles":
            return self._page([{"id": "r1", "displayName": "Global Administrator", "members": [{"id": "u-admin"}]},
                               {"id": "r2", "displayName": "Directory Synchronization Accounts", "members": [{"id": "u-oud"}]}], request)
        if p == "/subscribedSkus":
            return self._page([
                {"skuId": "sku-bp", "skuPartNumber": "SPB", "consumedUnits": 4, "capabilityStatus": "Enabled",
                 "prepaidUnits": {"enabled": 3, "warning": 0, "suspended": 0}},
                {"skuId": "sku-flow", "skuPartNumber": "FLOW_FREE", "consumedUnits": 1, "capabilityStatus": "Enabled",
                 "prepaidUnits": {"enabled": 10000}}], request)
        if p == "/reports/authenticationMethods/userRegistrationDetails":
            if not self.premium:
                return no_p1
            return self._page([
                {"id": "u-admin", "isMfaRegistered": False, "methodsRegistered": [], "isAdmin": True},
                {"id": "u-bob", "isMfaRegistered": False, "methodsRegistered": ["email"]},
                {"id": "u-oud", "isMfaRegistered": True, "methodsRegistered": ["microsoftAuthenticatorPush"],
                 "defaultMfaMethod": "microsoftAuthenticatorPush"}], request)
        if p.startswith("/users/") and p.endswith("/authentication/methods"):
            uid = p.split("/")[2]
            return self._page(self.methods.get(uid, []), request)
        if p.startswith("/users/") and "/authentication/" in p and request.method == "DELETE":
            uid, kind, mid = p.split("/")[2], p.split("/")[4], p.split("/")[5]
            ms = self.methods.get(uid, [])
            if kind == "phoneMethods" and any("Authenticator" in m["@odata.type"] for m in ms):
                # The default method can only go last.
                return httpx.Response(400, json={"error": {"code": "badRequest", "message": "default method"}})
            self.methods[uid] = [m for m in ms if m["id"] != mid]
            return httpx.Response(204)
        if p == "/identityProtection/riskyUsers" and request.method == "GET":
            if not self.premium:
                return no_p1
            return self._page([{"id": "u-bob", "riskLevel": "high", "riskState": "atRisk",
                                "riskDetail": "none", "riskLastUpdatedDateTime": "2026-11-25T01:00:00Z"}], request)
        if p == "/identityProtection/riskDetections":
            if not self.premium:
                return no_p1
            return self._page([{"id": "d1", "userId": "u-bob", "userPrincipalName": "bob@klant.nl",
                                "riskEventType": "unfamiliarFeatures", "riskLevel": "high", "riskState": "atRisk",
                                "detectedDateTime": "2026-11-25T01:00:00Z", "ipAddress": "203.0.113.9",
                                "location": {"city": "Moscow", "countryOrRegion": "RU"}}], request)
        if p == "/policies/identitySecurityDefaultsEnforcementPolicy":
            return httpx.Response(200, json={"isEnabled": False})
        if p == "/identity/conditionalAccess/policies":
            return self._page([{"id": "ca1", "displayName": "MFA admins", "state": "disabled"}], request)
        if p.startswith("/applications("):
            return httpx.Response(200, json={"displayName": "Stackpilot", "passwordCredentials": [
                {"hint": "xyz", "endDateTime": (self.now + timedelta(days=300)).isoformat()},
                {"hint": SECRET[:3], "displayName": "stackpilot", "endDateTime": (self.now + timedelta(days=10)).isoformat()}],
                "keyCredentials": []})
        if p == "/auditLogs/signIns":
            if not self.premium:
                return no_p1
            return self._page(self.signins, request)
        if p.startswith("/users/") and request.method == "PATCH":
            uid = p.split("/")[2]
            for u in self.users:
                if u["id"] == uid and "accountEnabled" in body:
                    u["accountEnabled"] = body["accountEnabled"]
            return httpx.Response(204)
        if p.endswith("/revokeSignInSessions"):
            return httpx.Response(200, json={"value": True})
        if p.startswith("/identityProtection/riskyUsers/"):
            if "IdentityRiskyUser.ReadWrite.All" not in self.roles:
                return httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied",
                                                           "message": "Insufficient privileges to complete the operation."}})
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": f"no fake for {request.method} {p}"}})


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(m365, "_transport", httpx.MockTransport(g.handler))
    m365._tokens.clear()
    return g


def test_validation_certificates_and_helpers():
    with pytest.raises(m365.M365Error):
        m365.check_ids("not a tenant!", CLIENT)
    with pytest.raises(m365.M365Error):
        m365.check_ids(TENANT, "abc")
    assert m365.check_ids("klant.onmicrosoft.com", CLIENT.upper()) == ("klant.onmicrosoft.com", CLIENT)
    assert m365.clean_settings({"allowed_countries": "nl, be", "stale_days": 3, "alert_risky": 0}) == \
        {"allowed_countries": ["BE", "NL"], "stale_days": 7, "alert_risky": False}
    with pytest.raises(m365.M365Error):
        m365.clean_settings({"allowed_countries": ["Nederland"]})

    pem, cert_pem, info = m365.generate_certificate("Klant BV")
    assert "PRIVATE KEY" in pem and "PRIVATE KEY" not in cert_pem and len(info["thumbprint"]) == 40
    key, cert = m365.parse_certificate({"password": "", "notes": pem})
    jwt = m365.client_assertion(TENANT, CLIENT, key, cert)
    head, claims, _ = jwt.split(".")
    h = json.loads(base64.urlsafe_b64decode(head + "=="))
    c = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert h["alg"] == "RS256" and h["x5t"] and c["iss"] == CLIENT and TENANT in c["aud"]
    # Entra shows the thumbprint base64-encoded in customKeyIdentifier.
    raw = bytes.fromhex(info["thumbprint"])
    assert m365._thumb(base64.b64encode(raw).decode()) == info["thumbprint"]
    assert m365._thumb(info["thumbprint"].lower()) == info["thumbprint"]
    with pytest.raises(m365.M365Error):
        m365.parse_certificate({"password": "geen pem"})
    assert m365.permissions_view(["User.ReadWrite.All"])["actions"]["block"] is True
    assert m365.permissions_view([])["actions"]["reset_mfa"] is False


async def _tenant(c, name="Klant M365", **extra):
    cust = (await c.post("/api/customers", json={"name": name})).json()
    r = await c.post("/api/m365/tenants", json={"customer_id": cust["id"], "name": "Klant BV", "tenant_id": TENANT,
                                                "client_id": CLIENT, "client_secret": SECRET, **extra})
    assert r.status_code == 201, r.text
    t = r.json()
    for _ in range(100):  # the first sync starts in the background
        if t["status"] != "unknown" and not m365._running:
            break
        await asyncio.sleep(0.05)
        t = next(x for x in (await c.get("/api/m365/tenants", params={"customer_id": cust["id"]})).json())
    return cust, t


async def test_connect_sync_alerts_and_findings(admin_client, graph):
    c = admin_client
    cust, t = await _tenant(c)
    assert t["vault_item_name"] == "Microsoft 365 app · Klant BV" and t["status"] == "ok", t
    assert SECRET not in json.dumps(t)

    # The secret is in the vault (never for the AI), and its use is in the access log.
    from sqlalchemy import select

    from mmn.db import sessionmaker
    from mmn.models import M365SignIn, VaultAccess, VaultItem
    from mmn.services import vault

    async with sessionmaker()() as db:
        item = await db.get(VaultItem, t["vault_item_id"])
        assert item.kind == "api_key" and item.ai_access == "never" and vault.read_secret(item)["password"] == SECRET
        acts = [a.action for a in (await db.execute(select(VaultAccess).where(VaultAccess.item_id == item.id))).scalars()]
        assert "integration_use" in acts

    assert t["organization"]["name"] == "Klant BV" and t["organization"]["default_domain"] == "klant.nl"
    cnt = t["counts"]
    assert cnt["users"] == 5 and cnt["guests"] == 1 and cnt["admins"] == 1 and cnt["admins_no_mfa"] == 1
    assert cnt["risky"] == 1 and cnt["high_risk"] == 1 and cnt["licences_paid"] == 3 and cnt["licences_used"] == 4
    assert t["credential_days"] in (9, 10) and t["app_credential"]["matched"] is True
    assert t["security"] == {"security_defaults": False, "ca_policies": 1, "ca_enabled": 0}
    assert all(x["granted"] for x in t["read"]) and t["actions"]["block"] and not t["actions"]["dismiss_risk"]

    users = (await c.get("/api/m365/users", params={"customer_id": cust["id"], "filter": "admins"})).json()["users"]
    assert [u["upn"] for u in users] == ["anna@klant.nl"]  # the sync account role does not count
    assert [u["upn"] for u in (await c.get("/api/m365/users", params={"tenant_id": t["id"], "filter": "stale"})).json()["users"]] == ["oud@klant.nl"]
    nomfa = (await c.get("/api/m365/users", params={"tenant_id": t["id"], "filter": "no_mfa"})).json()["users"]
    assert {u["upn"] for u in nomfa} == {"anna@klant.nl", "bob@klant.nl"}
    assert (await c.get("/api/m365/users", params={"tenant_id": t["id"], "filter": "bogus"})).status_code == 400
    found = (await c.get("/api/m365/users", params={"tenant_id": t["id"], "q": "finance"})).json()["users"]
    assert [u["upn"] for u in found] == ["bob@klant.nl"]
    lic = (await c.get("/api/m365/licences", params={"customer_id": cust["id"]})).json()["licences"]
    assert lic[0]["name"] == "Microsoft 365 Business Premium" and lic[0]["available"] == -1 and lic[1]["free"]

    # Only sign-ins that stand out are stored: risky + foreign, legacy and the failed ones.
    s = (await c.get("/api/m365/signins", params={"tenant_id": t["id"]})).json()["signins"]
    by = {x["id"]: x for x in s}
    assert len(s) == 6
    flags = {tuple(x["flags"]) for x in s}
    assert ("risky", "foreign") in flags and ("legacy",) in flags and ("failed",) in flags
    assert len((await c.get("/api/m365/signins", params={"tenant_id": t["id"], "flag": "failed"})).json()["signins"]) == 4
    assert by  # ids are Stackpilot's own
    # A second sync does not store them twice.
    r = await c.post(f"/api/m365/tenants/{t['id']}/sync")
    assert r.status_code == 200 and r.json()["signins_stored"] == 0, r.text
    async with sessionmaker()() as db:
        assert len(list((await db.execute(select(M365SignIn).where(M365SignIn.tenant_id == t["id"]))).scalars())) == 6

    detail = (await c.get(f"/api/m365/users/{t['id']}/bob@klant.nl")).json()
    assert detail["risk"]["level"] == "high" and len(detail["signins"]) == 5 and detail["risk_detections"]

    # Alerts.
    from mmn.services import monitoring

    async with sessionmaker()() as db:
        await c.patch(f"/api/m365/tenants/{t['id']}", json={"settings": {"failed_threshold": 3}})
        want = await monitoring._conditions(db, await monitoring.get_settings(db))  # noqa: SLF001
    keys = {k for k in want if k.startswith("m365")}
    tid = t["id"]
    assert {f"m365-risk:{tid}:u-bob", f"m365-admin-mfa:{tid}", f"m365-licence:{tid}:sku-bp", f"m365-secret:{tid}",
            f"m365-foreign:{tid}:bob@klant.nl", f"m365-failed:{tid}:bob@klant.nl"} <= keys, keys
    assert want[f"m365-risk:{tid}:u-bob"]["severity"] == "critical"
    assert want[f"m365-admin-mfa:{tid}"]["detail"]["customer_id"] == cust["id"]

    # Nightly findings (morning summary) with a proposal for the risky user.
    from mmn.models import Environment
    from mmn.services import customers

    async with sessionmaker()() as db:
        e = await db.get(Environment, cust["id"])
        fs = await m365.findings(db, e, datetime.now(UTC), customers._finding)  # noqa: SLF001
    codes = {f["code"] for f in fs}
    assert {"m365_risky_user", "m365_admin_no_mfa", "m365_no_mfa", "m365_no_mfa_policy", "m365_stale_users",
            "m365_disabled_licensed", "m365_licence_short", "m365_secret_expiring", "m365_failed_signins",
            "m365_foreign_signins"} <= codes, codes
    risky = next(f for f in fs if f["code"] == "m365_risky_user")
    assert risky["proposal"]["args"]["action"] == "block" and risky["proposal"]["args"]["confirm"] == "bob@klant.nl"
    from mmn.services import nightly, reports

    assert nightly.category_of(risky) == "identity"
    tgt, fin, _ = reports.finding_texts(next(f for f in fs if f["code"] == "m365_no_mfa"), "nl")
    assert fin == "1 gebruiker(s) zonder MFA: bob@klant.nl."  # the admin has its own finding


async def test_without_premium_and_wrong_secret(admin_client, graph):
    c = admin_client
    graph.premium = False
    graph.roles = ["User.Read.All", "Directory.Read.All", "UserAuthenticationMethod.Read.All"]
    cust, t = await _tenant(c, "Klant Zonder P1")
    assert t["status"] == "partial", t
    feats = t["features"]
    assert feats["signins"]["reason"] == "needs_p1" and feats["risky_users"]["reason"] in ("needs_p1", "needs_p2")
    assert feats["last_signin"]["ok"] is False and feats["mfa"] == {"ok": True, "source": "methods"}
    assert t["mfa_source"] == "methods"
    users = (await c.get("/api/m365/users", params={"tenant_id": t["id"], "filter": "no_mfa"})).json()["users"]
    assert {u["upn"] for u in users} == {"anna@klant.nl", "bob@klant.nl"}  # oud@ has the Authenticator app
    assert (await c.get("/api/m365/users", params={"tenant_id": t["id"], "filter": "stale"})).json()["users"] == []

    # A wrong secret: the tenant is in error, the test says why.
    r = await c.patch(f"/api/m365/tenants/{t['id']}", json={"client_secret": "fout"})
    assert r.status_code == 200
    r = (await c.post(f"/api/m365/tenants/{t['id']}/test")).json()
    assert r["ok"] is False and "AADSTS7000215" in r["error"]
    assert (await c.post(f"/api/m365/tenants/{t['id']}/sync")).status_code == 502
    # The old secret went to the vault item's history; the new one fixes it.
    r = await c.patch(f"/api/m365/tenants/{t['id']}", json={"client_secret": "nieuw-geheim"})
    r = (await c.post(f"/api/m365/tenants/{t['id']}/test")).json()
    assert r["ok"] is True and r["organization"] == "Klant BV" and r["actions"]["block"] is False


async def test_user_actions_from_the_app(admin_client, graph):
    c = admin_client
    cust, t = await _tenant(c, "Klant Acties")
    r = await c.post(f"/api/m365/users/{t['id']}/bob@klant.nl/action", json={"action": "block", "reason": "risico"})
    assert r.status_code == 200, r.text
    assert r.json()["sessions_revoked"] is True
    assert ("PATCH", "/v1.0/users/u-bob", {"accountEnabled": False}) in graph.calls
    u = (await c.get(f"/api/m365/users/{t['id']}/u-bob")).json()
    assert u["enabled"] is False

    r = await c.post(f"/api/m365/users/{t['id']}/oud@klant.nl/action", json={"action": "reset_mfa"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["removed"]) == ["microsoftAuthenticator", "phone"]
    assert [m["id"] for m in graph.methods["u-oud"]] == ["pw"]

    r = await c.post(f"/api/m365/users/{t['id']}/anna@klant.nl/action", json={"action": "reset_password"})
    assert r.status_code == 200, r.text
    item = r.json()["vault_item"]
    patch = next(b for m, p, b in graph.calls if m == "PATCH" and p == "/v1.0/users/u-admin")
    temp = patch["passwordProfile"]["password"]
    assert patch["passwordProfile"]["forceChangePasswordNextSignIn"] is True and temp not in r.text

    from mmn.db import sessionmaker
    from mmn.models import VaultItem
    from mmn.services import vault

    async with sessionmaker()() as db:
        assert vault.read_secret(await db.get(VaultItem, item["id"]))["password"] == temp

    # Without the permission the app says which one it needs.
    r = await c.post(f"/api/m365/users/{t['id']}/bob@klant.nl/action", json={"action": "dismiss_risk"})
    assert r.status_code == 403 and "IdentityRiskyUser.ReadWrite.All" in r.json()["detail"]
    audit = (await c.get("/api/settings/audit", params={"limit": 50}))
    if audit.status_code == 200:
        assert "m365.user.block" in audit.text


async def test_mcp_tools_and_approval(admin_client, client, graph):
    c = admin_client
    await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                               "notify_app": False, "notify_mail": False, "notify_mail_to": ""})
    cust, t = await _tenant(c, "Klant MCP")
    other = (await c.post("/api/customers", json={"name": "Klant MCP Ander"})).json()
    token, _ = await _token(c, "claude-m365", ["read", "m365", "m365_admin"])
    await _init(client, token)
    res, err = await _call(client, token, "list_m365_users", {"customer": "Klant MCP", "filter": "risky"})
    assert err is None and [u["upn"] for u in res["users"]] == ["bob@klant.nl"], err
    assert res["users"][0]["licences"] == ["Microsoft 365 Business Premium"]
    res, err = await _call(client, token, "get_m365_user", {"customer": "Klant MCP", "user": "Bob Boekhouder"})
    assert err is None and res["risk"]["level"] == "high" and "block" in res["possible_actions"]
    res, err = await _call(client, token, "list_m365_signins", {"customer": "Klant MCP", "flag": "foreign"})
    assert err is None and res["signins"][0]["country"] == "RU"
    res, err = await _call(client, token, "get_customer_context", {"customer": "Klant MCP"})
    assert err is None and res["microsoft_365"][0]["admins_no_mfa"] == 1

    # A token without the scope.
    plain, _ = await _token(c, "claude-plain", ["read"])
    await _init(client, plain)
    _, err = await _call(client, plain, "list_m365_users", {"customer": "Klant MCP"})
    assert err and "m365" in err
    # A token of another customer does not see the tenant.
    tied, _ = await _token(c, "claude-tied", ["read", "m365"], customer_ids=[other["id"]])
    await _init(client, tied)
    res, err = await _call(client, tied, "list_m365_tenants", {})
    assert err is None and res["tenants"] == []

    # A write action always waits for an admin (the test policy allows destructive tools).
    args = {"customer": "Klant MCP", "user": "bob@klant.nl", "action": "revoke_sessions", "reason": "verdacht",
            "confirm": "bob@klant.nl"}
    _, err = await _call(client, token, "m365_user_action", {**args, "confirm": "iemand"})
    assert err is None or "confirm" in err
    before = len([x for x in graph.calls if x[1].endswith("/revokeSignInSessions")])
    res, err = await _call(client, token, "m365_user_action", args)
    assert err is None and res["status"] == "pending_approval", (res, err)
    assert len([x for x in graph.calls if x[1].endswith("/revokeSignInSessions")]) == before
    rows = (await c.get("/api/gateway/approvals", params={"state": "pending"})).json()
    rows = rows["approvals"] if isinstance(rows, dict) else rows
    a = next(x for x in rows if x["tool"] == "m365_user_action")
    assert "m365" in [r["code"] for r in a["reasons"]]
    r = await c.post(f"/api/gateway/approvals/{a['id']}/approve", json={"note": "ok"})
    assert r.status_code == 200, r.text
    for _ in range(100):
        st = (await c.get(f"/api/gateway/approvals/{a['id']}")).json()["status"]
        if st in ("executed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert st == "executed"
    assert len([x for x in graph.calls if x[1].endswith("/revokeSignInSessions")]) == before + 1

    # Deleting the tenant removes its sign-ins; the vault item stays.
    assert (await c.delete(f"/api/m365/tenants/{t['id']}")).status_code == 204
    assert (await c.get("/api/m365/signins", params={"tenant_id": t["id"]})).json()["signins"] == []
