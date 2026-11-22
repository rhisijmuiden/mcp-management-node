from __future__ import annotations

from conftest import ADMIN, DATA_DIR, csrf


async def test_setup_requires_token(client):
    status = (await client.get("/api/auth/status")).json()
    if not status["setup_required"]:
        return
    (DATA_DIR / "setup-token").write_text("right-token")
    r = await client.post("/api/auth/setup", json={**ADMIN, "setup_token": "wrong"})
    assert r.status_code == 403


async def test_login_me_csrf_logout(admin_client):
    c = admin_client
    me = (await c.get("/api/auth/me")).json()
    assert me["username"] == "admin" and me["role"] == "admin"
    # A write without the CSRF header is refused.
    r = await c.patch("/api/auth/me", json={"language": "nl"}, headers={"x-csrf-token": "nope"})
    assert r.status_code == 403
    r = await c.patch("/api/auth/me", json={"language": "nl"})
    assert r.status_code == 200 and r.json()["language"] == "nl"
    await c.patch("/api/auth/me", json={"language": "en"})
    r = await c.post("/api/auth/logout")
    assert r.status_code == 204
    c.cookies.clear()
    assert (await c.get("/api/auth/me")).status_code == 401


async def test_wrong_password(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"})
    assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope-nope"})
    assert r.status_code == 401


async def test_users_and_last_admin(admin_client):
    c = admin_client
    r = await c.post("/api/users", json={"username": "Viewer1", "password": "viewer-password-1", "role": "viewer"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert r.json()["username"] == "viewer1"
    me = (await c.get("/api/auth/me")).json()
    r = await c.patch(f"/api/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 400 and r.json()["detail"] == "last_admin"
    # A viewer cannot manage users.
    import httpx

    async with httpx.AsyncClient(transport=c._transport, base_url="http://testserver") as v:
        r = await v.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-password-1"})
        assert r.status_code == 200
        assert (await v.get("/api/users", headers=csrf(v))).status_code == 403
        assert (await v.get("/api/system/info")).status_code == 200
    assert (await c.delete(f"/api/users/{uid}")).status_code == 204


async def test_password_reset_flow(admin_client, smtp_server):
    c = admin_client
    await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                            "security": "none", "from_address": "app@example.com"})
    r = await c.post("/api/auth/forgot", json={"username_or_email": "admin@example.com"})
    assert r.status_code == 202
    msg = await smtp_server.wait()
    body = msg.get_body(("plain",)).get_content()
    token = body.split("token=")[1].split()[0]
    import httpx

    async with httpx.AsyncClient(transport=c._transport, base_url="http://testserver") as anon:
        assert (await anon.post("/api/auth/reset", json={"token": "bad", "password": "x" * 12})).status_code == 400
        r = await anon.post("/api/auth/reset", json={"token": token, "password": ADMIN["password"]})
        assert r.status_code == 204
        # Tokens are single use.
        r = await anon.post("/api/auth/reset", json={"token": token, "password": ADMIN["password"]})
        assert r.status_code == 400
    await c.put("/api/settings/mail", json={"enabled": False})
