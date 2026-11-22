from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PG_ADMIN_URL = os.environ.get("MMN_TEST_PG_ADMIN", "postgresql://postgres@127.0.0.1:5433/postgres")
TEST_DB = "mmn_test"
DATA_DIR = Path(tempfile.mkdtemp(prefix="mmn-test-"))

_user_url = os.environ.get("MMN_TEST_DATABASE_URL", "postgresql+asyncpg://mmn:mmn@127.0.0.1:5433/" + TEST_DB)
os.environ.update({
    "MMN_DATABASE_URL": _user_url,
    "MMN_DATA_DIR": str(DATA_DIR),
    "MMN_INSTALL_DIR": str(DATA_DIR / "opt"),
    "MMN_RUN_SCHEDULER": "false",
    "MMN_INSECURE_COOKIES": "true",
    "MMN_LOG_LEVEL": "warning",
    "MMN_AGENT_DIST": str(DATA_DIR / "agent-dist"),
})


def _psql(sql: str) -> None:
    subprocess.run(["psql", PG_ADMIN_URL, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql], check=True)


@pytest.fixture(scope="session", autouse=True)
def database():
    if not os.environ.get("MMN_TEST_DATABASE_URL"):
        _psql(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        _psql(f"CREATE DATABASE {TEST_DB} OWNER mmn")
    subprocess.run([sys.executable, "-m", "mmn.cli", "migrate"], check=True)
    yield


@pytest.fixture(scope="session")
async def app():
    from mmn.main import create_app

    application = create_app()
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner():
        # The lifespan must be entered and left in the same task (anyio cancel scopes).
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await started.wait()
    yield application
    stop.set()
    await task


@pytest.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def csrf(client) -> dict[str, str]:
    return {"x-csrf-token": client.cookies.get("mmn_csrf", "")}


ADMIN = {"username": "admin", "password": "correct horse battery", "email": "admin@example.com"}


@pytest.fixture
async def admin_client(client):
    """A client logged in as the admin (creates the admin on first use)."""
    status = (await client.get("/api/auth/status")).json()
    if status["setup_required"]:
        tok = DATA_DIR / "setup-token"
        tok.write_text("test-setup-token\n")
        r = await client.post("/api/auth/setup", json={**ADMIN, "setup_token": "test-setup-token"})
        assert r.status_code == 200, r.text
    else:
        r = await client.post("/api/auth/login", json={"username": ADMIN["username"], "password": ADMIN["password"]})
        assert r.status_code == 200, r.text
    client.headers.update(csrf(client))
    return client


class _SmtpSink:
    def __init__(self):
        from email import message_from_bytes, policy

        self._parse = lambda b: message_from_bytes(b, policy=policy.default)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.port = 0

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd API
        await self.queue.put(self._parse(envelope.content))
        return "250 OK"

    async def wait(self, timeout: float = 10):
        return await asyncio.wait_for(self.queue.get(), timeout)


@pytest.fixture(scope="session")
def smtp_server():
    import socket

    from aiosmtpd.controller import Controller

    sink = _SmtpSink()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    sink.port = s.getsockname()[1]
    s.close()
    ctrl = Controller(sink, hostname="127.0.0.1", port=sink.port)
    ctrl.start()
    yield sink
    ctrl.stop()
