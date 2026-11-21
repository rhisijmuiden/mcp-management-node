from __future__ import annotations

import ssl
import tempfile
from pathlib import Path

import pytest
from servers import free_port, self_signed_cert


async def test_mail_settings_hide_secrets_and_send(admin_client, smtp_server):
    c = admin_client
    r = await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                                "security": "none", "from_address": "app@example.com",
                                                "from_name": "MMN", "password": "hunter2-secret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["password_set"] is True and "hunter2" not in r.text and "password_enc" not in body
    # Username empty: no AUTH is attempted even though a password is stored.
    r = await c.post("/api/settings/mail/test", json={"to": "someone@example.com"})
    assert r.json() == {"ok": True}
    msg = await smtp_server.wait()
    assert msg["To"] == "someone@example.com"
    assert "MMN" in msg["From"]
    assert msg.get_body(("html",)) is not None
    await c.put("/api/settings/mail", json={"enabled": False})


async def test_test_mail_uses_unsaved_values(admin_client, smtp_server):
    c = admin_client
    await c.put("/api/settings/mail", json={"host": "192.0.2.1", "port": 1, "security": "none",
                                            "from_address": "app@example.com"})
    r = await c.post("/api/settings/mail/test", json={
        "to": "x@example.com", "settings": {"host": "127.0.0.1", "port": smtp_server.port}})
    assert r.json()["ok"] is True
    await smtp_server.wait()


async def test_starttls_and_dkim(admin_client):
    from aiosmtpd.controller import Controller
    from conftest import _SmtpSink
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = self_signed_cert(Path(tempfile.mkdtemp()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(pem)
    sink = _SmtpSink()
    sink.port = free_port()
    ctrl = Controller(sink, hostname="127.0.0.1", port=sink.port, tls_context=ctx, require_starttls=True)
    ctrl.start()
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        c = admin_client
        r = await c.put("/api/settings/mail", json={
            "enabled": True, "host": "127.0.0.1", "port": sink.port, "security": "starttls", "verify_tls": True,
            "from_address": "app@example.com", "dkim_enabled": True, "dkim_domain": "example.com",
            "dkim_selector": "mmn", "dkim_private_key": key_pem})
        assert r.status_code == 200 and r.json()["dkim_key_set"]
        # Self-signed certificate: refused while verification is on.
        r = await c.post("/api/settings/mail/test", json={"to": "x@example.com"})
        assert r.json()["ok"] is False and "CERTIFICATE" in r.json()["error"].upper()
        await c.put("/api/settings/mail", json={"verify_tls": False})
        r = await c.post("/api/settings/mail/test", json={"to": "x@example.com"})
        assert r.json()["ok"] is True, r.json()
        msg = await sink.wait()
        assert "d=example.com" in msg["DKIM-Signature"] and "s=mmn" in msg["DKIM-Signature"]
        r = await c.put("/api/settings/mail", json={"dkim_private_key": "not a key"})
        assert r.status_code == 400
    finally:
        ctrl.stop()
        await admin_client.put("/api/settings/mail", json={"enabled": False, "dkim_enabled": False,
                                                           "verify_tls": True})


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_translations_complete(lang):
    from mmn.i18n import STRINGS

    assert set(STRINGS[lang]) == set(STRINGS["en"])
