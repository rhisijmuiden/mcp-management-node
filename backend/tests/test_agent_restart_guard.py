"""Updating a Windows agent with the broken 0.7.1/0.8.0 restart arms a scheduled task first."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from mmn.services import nodes


def test_guard_only_for_broken_windows_versions():
    assert nodes.needs_restart_guard("windows", "0.7.1")
    assert nodes.needs_restart_guard("Windows", "0.8.0")
    assert not nodes.needs_restart_guard("windows", "0.7.0")
    assert not nodes.needs_restart_guard("windows", "0.8.1")
    assert not nodes.needs_restart_guard("linux", "0.8.0")
    assert not nodes.needs_restart_guard(None, None)


def test_restart_task_script():
    s = nodes.windows_restart_task_script()
    assert "Register-ScheduledTask -TaskName 'Stackpilot agent restart'" in s
    assert "-UserId 'SYSTEM'" in s and "AddSeconds(90)" in s and "-Force" in s
    # The loop is a single-quoted PowerShell string, so its own quotes are doubled.
    assert "Start-Service -Name ''mmn-agent''" in s and "''Stopped''" in s
    assert s.rstrip().endswith("'armed'")


def _patch(monkeypatch, node, exec_result):
    calls: list[str] = []

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def get_node(db, node_id):
        return node

    async def request(node_id, method, params=None, timeout=60):
        calls.append(method)
        assert method == "exec" and "Register-ScheduledTask" in params["command"]
        return exec_result

    async def start_job(node_id, kind, method, params, **kw):
        calls.append(method)
        return SimpleNamespace(id="job1")

    monkeypatch.setattr(nodes, "sessionmaker", lambda: session)
    monkeypatch.setattr(nodes, "get_node", get_node)
    monkeypatch.setattr(nodes.agent_hub, "get", lambda node_id: None)
    monkeypatch.setattr(nodes.agent_hub, "request", request)
    monkeypatch.setattr(nodes.agent_packages, "package_for",
                        lambda os, arch, kind: ("mmn-agent-windows-amd64.exe", {"sha256": "a" * 64}))
    monkeypatch.setattr(nodes.agent_packages, "bundled_version", lambda: "0.8.1")
    monkeypatch.setattr(nodes, "start_job", start_job)
    return calls


async def test_update_arms_task_before_update(monkeypatch):
    node = SimpleNamespace(id="n1", os="windows", arch="amd64", install_kind="windows", agent_version="0.8.0")
    calls = _patch(monkeypatch, node, {"exit_code": 0, "stdout": "armed\n", "stderr": ""})
    await nodes.update_agent("n1")
    assert calls == ["exec", "agent.update"]


async def test_update_refused_when_task_cannot_be_armed(monkeypatch):
    node = SimpleNamespace(id="n1", os="windows", arch="amd64", install_kind="windows", agent_version="0.7.1")
    calls = _patch(monkeypatch, node, {"exit_code": 1, "stdout": "", "stderr": "Access is denied"})
    with pytest.raises(nodes.NodeError, match="Access is denied"):
        await nodes.update_agent("n1")
    assert calls == ["exec"]


async def test_no_task_for_fixed_or_linux_agents(monkeypatch):
    for os_name, version in (("windows", "0.7.0"), ("linux", "0.8.0")):
        node = SimpleNamespace(id="n1", os=os_name, arch="amd64", install_kind="binary", agent_version=version)
        calls = _patch(monkeypatch, node, {})
        await nodes.update_agent("n1")
        assert calls == ["agent.update"]
