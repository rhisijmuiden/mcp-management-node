from __future__ import annotations

import asyncio

import pytest
from servers import FtpServer, SshServer


@pytest.fixture(scope="module")
def ftp():
    s = FtpServer()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def ftps():
    s = FtpServer(tls=True)
    yield s
    s.stop()


@pytest.fixture(scope="module")
async def ssh():
    s = SshServer()
    await s.start()
    yield s
    s.stop()


async def _wait_run(c, target_id: str, n_before: int) -> dict:
    for _ in range(200):
        runs = (await c.get("/api/backups/runs", params={"target_id": target_id})).json()
        done = [r for r in runs if r["status"] != "running"]
        if len(done) > n_before:
            return done[0]
        await asyncio.sleep(0.1)
    raise AssertionError("backup did not finish")


async def _user_count(c) -> int:
    return len((await c.get("/api/users")).json())


async def _full_cycle(c, target_body: dict, remote_dir_files) -> None:
    r = await c.post("/api/backups/targets", json=target_body)
    assert r.status_code == 201, r.text
    t = r.json()
    test = (await c.post(f"/api/backups/targets/{t['id']}/test")).json()
    assert test["ok"], test

    # 1. full backup
    await c.post(f"/api/backups/targets/{t['id']}/run", json={"kind": "full"})
    full = await _wait_run(c, t["id"], 0)
    assert full["status"] == "success", full
    assert full["kind"] == "full"

    # 2. change data, then an incremental backup stores only what changed
    users_before = await _user_count(c)
    r = await c.post("/api/users", json={"username": f"inc-{t['id'][:5]}", "password": "incremental-pass"})
    assert r.status_code == 201
    await c.post(f"/api/backups/targets/{t['id']}/run", json={"kind": "auto"})
    inc = await _wait_run(c, t["id"], 1)
    assert inc["status"] == "success", inc
    assert inc["kind"] == "incremental"
    assert inc["parent_backup_id"] == full["backup_id"]
    assert 0 < inc["files_changed"] < full["files_changed"]
    names = remote_dir_files()
    assert any(n.endswith(".enc") for n in names) == bool(target_body.get("encryption_passphrase"))

    remote = (await c.get(f"/api/backups/targets/{t['id']}/remote")).json()
    assert {m["id"] for m in remote} >= {full["backup_id"], inc["backup_id"]}
    assert all(m["restorable"] for m in remote)

    # 3. restore the full backup: the new user disappears
    r = await c.post(f"/api/backups/targets/{t['id']}/restore",
                     json={"backup_id": full["backup_id"], "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    await _relogin(c)
    assert await _user_count(c) == users_before

    # 4. restore the incremental: the user is back
    r = await c.post(f"/api/backups/targets/{t['id']}/restore",
                     json={"backup_id": inc["backup_id"], "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    await _relogin(c)
    assert await _user_count(c) == users_before + 1


async def _relogin(c) -> None:
    from conftest import ADMIN, csrf

    c.cookies.clear()
    r = await c.post("/api/auth/login", json={"username": ADMIN["username"], "password": ADMIN["password"]})
    assert r.status_code == 200, r.text
    c.headers.update(csrf(c))


async def test_ftp_backup_restore(admin_client, ftp):
    await _full_cycle(admin_client, {
        "name": "FTP", "protocol": "ftp", "host": "127.0.0.1", "port": ftp.port, "username": "backup",
        "password": "s3cret-pass", "remote_path": "mmn/backups", "encryption_passphrase": "very secret",
    }, lambda: [p.name for p in (ftp.root / "mmn/backups").iterdir()])


async def test_ftps_backup_restore(admin_client, ftps):
    await _full_cycle(admin_client, {
        "name": "FTPS", "protocol": "ftps", "host": "127.0.0.1", "port": ftps.port, "username": "backup",
        "password": "s3cret-pass", "remote_path": "backups", "verify_tls": False,
    }, lambda: [p.name for p in (ftps.root / "backups").iterdir()])


async def test_sftp_backup_restore(admin_client, ssh):
    await _full_cycle(admin_client, {
        "name": "SFTP", "protocol": "sftp", "host": "127.0.0.1", "port": ssh.port, "username": "backup",
        "password": "s3cret-pass", "remote_path": "/sftp", "encryption_passphrase": "pw",
    }, lambda: [p.name for p in (ssh.root / "sftp").iterdir()])


async def test_scp_backup_restore(admin_client, ssh):
    await _full_cycle(admin_client, {
        "name": "SCP", "protocol": "scp", "host": "127.0.0.1", "port": ssh.port, "username": "backup",
        "password": "s3cret-pass", "remote_path": "scp-dir",
    }, lambda: [p.name for p in (ssh.root / "scp-dir").iterdir()])


async def test_wrong_password_and_bad_credentials(admin_client, ftp):
    c = admin_client
    r = await c.post("/api/backups/targets", json={"name": "bad", "protocol": "ftp", "host": "127.0.0.1",
                                                   "port": ftp.port, "username": "backup", "password": "nope"})
    t = r.json()
    res = (await c.post(f"/api/backups/targets/{t['id']}/test")).json()
    assert res["ok"] is False and "530" in res["error"]
    r = await c.post(f"/api/backups/targets/{t['id']}/restore", json={"backup_id": "x", "password": "wrong"})
    assert r.status_code == 400
    assert "password" not in str((await c.get("/api/backups/targets")).json()).lower().replace("password_set", "")
    await c.delete(f"/api/backups/targets/{t['id']}")


def test_crypto_roundtrip(tmp_path):
    from mmn.services.backup.crypto import DecryptionError, decrypt_file, encrypt_file

    src = tmp_path / "a"
    src.write_bytes(b"x" * (1024 * 1024 * 2 + 5))
    encrypt_file(src, tmp_path / "b", "pw")
    decrypt_file(tmp_path / "b", tmp_path / "c", "pw")
    assert (tmp_path / "c").read_bytes() == src.read_bytes()
    with pytest.raises(DecryptionError):
        decrypt_file(tmp_path / "b", tmp_path / "d", "wrong")
    data = (tmp_path / "b").read_bytes()
    (tmp_path / "t").write_bytes(data[: len(data) - 100])
    with pytest.raises(DecryptionError):
        decrypt_file(tmp_path / "t", tmp_path / "e", "pw")
