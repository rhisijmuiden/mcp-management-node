from __future__ import annotations

import shutil
import subprocess

import pytest

from mmn.nginx import ProxyIn, render_nginx
from mmn.services.updates import latest_for_channel
from mmn.version import channel_of_tag, parse_tag


def test_tag_ordering():
    assert parse_tag("v0.2.0-test.3") < parse_tag("v0.2.0-test.10") < parse_tag("v0.2.0") < parse_tag("v0.2.1")
    assert parse_tag("nonsense") is None
    assert channel_of_tag("v1.0.0-test.4") == "test" and channel_of_tag("v1.0.0") == "production"


def test_latest_for_channel():
    rels = [{"tag": t, "channel": channel_of_tag(t), "_ver": parse_tag(t)}
            for t in ("v0.1.0", "v0.2.0-test.1", "v0.1.1")]
    assert latest_for_channel(rels, "production")["tag"] == "v0.1.1"
    assert latest_for_channel(rels, "test")["tag"] == "v0.2.0-test.1"


async def test_info_and_channel(admin_client):
    c = admin_client
    info = (await c.get("/api/system/info")).json()
    assert info["name"] == "MCP Management Node" and info["channel"] == "production"
    assert info["updater_available"] is False
    r = await c.put("/api/system/updates/channel", json={"channel": "test"})
    assert r.status_code == 200
    assert (await c.get("/api/system/info")).json()["channel"] == "test"
    await c.put("/api/system/updates/channel", json={"channel": "production"})
    r = await c.post("/api/system/updates/apply", json={"tag": "v0.1.0"})
    assert r.status_code == 409


async def test_proxy_settings_and_seen_headers(admin_client):
    c = admin_client
    r = await c.put("/api/system/proxy", json={"mode": "proxy", "trusted_proxies": []})
    assert r.status_code == 400
    r = await c.put("/api/system/proxy", json={"mode": "proxy", "trusted_proxies": ["10.0.0.5", "fd00::/64"],
                                               "base_path": "/proxmox"})
    assert r.status_code == 200, r.text
    got = (await c.get("/api/system/proxy")).json()
    assert got["mode"] == "proxy" and got["trusted_proxies"] == ["10.0.0.5/32", "fd00::/64"]
    assert got["seen"]["client_ip"] == "127.0.0.1"


@pytest.mark.parametrize("cfg", [
    ProxyIn(mode="direct", server_name="pve.example.com"),
    ProxyIn(mode="proxy", trusted_proxies=["192.168.1.10"], base_path="/proxmox", restrict_to_proxy=True),
    ProxyIn(mode="proxy", trusted_proxies=["10.0.0.0/24"], http_port=8080),
])
def test_nginx_config_is_valid(cfg, tmp_path):
    text = render_nginx(cfg, frontend=str(tmp_path))
    assert "proxy_pass http://mmn_backend/api/" in text
    if cfg.mode == "proxy":
        assert "set_real_ip_from" in text and "ssl_certificate" not in text
    if cfg.base_path != "/":
        assert f"location ^~ {cfg.base_path}/api/" in text and "sub_filter" in text
    if not shutil.which("nginx"):
        pytest.skip("nginx not installed")
    main = tmp_path / "nginx.conf"
    site = tmp_path / "site.conf"
    version = subprocess.run(["nginx", "-v"], capture_output=True, text=True).stderr
    if "nginx/1.2" in version and int(version.split("nginx/1.")[1].split(".")[0]) < 25:
        text = text.replace("    http2 on;\n", "")  # Debian 13 ships nginx 1.26; older test hosts lack it
    site.write_text(text)
    main.write_text(f"events {{}}\nhttp {{\n include {site};\n}}\npid {tmp_path}/nginx.pid;\n"
                    f"error_log {tmp_path}/error.log;\n")
    res = subprocess.run(["nginx", "-t", "-c", str(main), "-p", str(tmp_path)], capture_output=True, text=True)
    assert "syntax is ok" in res.stderr or "ssl-cert-snakeoil" in res.stderr, res.stderr


def test_proxy_validation():
    with pytest.raises(ValueError):
        ProxyIn(server_name="evil; include /etc/passwd")
    with pytest.raises(ValueError):
        ProxyIn(base_path="/a b")
    with pytest.raises(ValueError):
        ProxyIn(trusted_proxies=["not-an-ip"])
