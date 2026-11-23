"""Talking to another MCP server (an "upstream" in the MCP hub).

Transports:

* ``http``: Streamable HTTP. Stackpilot makes the call itself, or an agent in the customer's network makes it
  (``http.request`` over the agent's mTLS connection), so a server on the LAN needs no open port. The
  JSON-RPC is done here (initialize, the ``Mcp-Session-Id``, JSON or SSE answers), so it works the same
  both ways.
* ``sse``: the older HTTP+SSE transport (a GET stream plus POSTs), directly from Stackpilot.
* ``stdio``: a command that Stackpilot starts on this server (for example ``github-mcp-server stdio``);
  it runs as the Stackpilot service user.

``sse`` and ``stdio`` keep one connection per upstream open in a background task, which closes after
some minutes without calls.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import os
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from mmn.version import get_build_info

log = logging.getLogger("mmn.hub")

PROTOCOL_VERSION = "2025-06-18"
MAX_BODY = 16 * 1024 * 1024
IDLE_SECONDS = 600


class HubError(Exception):
    """Calling an upstream failed; ``code`` is a short machine readable reason."""

    def __init__(self, message: str, code: str = "error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class RemoteToolError(HubError):
    """The upstream answered with a JSON-RPC error."""


@dataclass
class Endpoint:
    """Everything needed to reach one upstream; built from an McpUpstream row with its secrets."""

    key: str  # upstream id; also the key of cached sessions
    transport: str
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    agent_node_id: str | None = None
    tls_mode: str = "verify"
    fingerprint: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 60
    label: str = ""

    def signature(self) -> str:
        """Changes when the connection settings change, so cached sessions are dropped."""
        raw = json.dumps([self.transport, self.url, sorted(self.headers.items()), self.agent_node_id, self.tls_mode,
                          self.fingerprint, self.command, self.args, sorted(self.env.items())])
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()


def _client_info() -> dict[str, str]:
    return {"name": "stackpilot-hub", "version": get_build_info().version}


# ------------------------------------------------------------------ HTTP sending (direct or via agent)
Sender = Callable[[Endpoint, str, str, bytes | None, dict[str, str], float],
                  Awaitable[tuple[int, dict[str, str], bytes]]]


async def _send_direct(ep: Endpoint, method: str, url: str, body: bytes | None, headers: dict[str, str],
                       timeout: float) -> tuple[int, dict[str, str], bytes]:
    from mmn.services.unifi.transport import UnifiError, ssl_context

    try:
        verify = ssl_context(ep.tls_mode, ep.fingerprint)
    except UnifiError as e:
        raise HubError(str(e), "bad_fingerprint") from e
    try:
        async with httpx.AsyncClient(verify=verify, timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, url, content=body, headers=headers)
            data = r.content
    except httpx.TimeoutException as e:
        raise HubError("the MCP server did not answer in time", "timeout") from e
    except httpx.ConnectError as e:
        if "CERTIFICATE" in str(e).upper() or "fingerprint" in str(e):
            raise HubError(f"TLS: {e}", "tls") from e
        raise HubError(f"cannot connect: {e}", "unreachable") from e
    except httpx.HTTPError as e:
        raise HubError(f"connection failed: {e}", "unreachable") from e
    if len(data) > MAX_BODY:
        raise HubError("the answer is too large", "too_large")
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, data


async def _send_agent(ep: Endpoint, method: str, url: str, body: bytes | None, headers: dict[str, str],
                      timeout: float) -> tuple[int, dict[str, str], bytes]:
    from mmn.services import agent_hub
    from mmn.services.unifi.transport import normalize_fingerprint

    if not ep.agent_node_id:
        raise HubError("no agent chosen for this MCP server", "no_agent")
    t = int(min(max(timeout, 5), 120))
    params = {"method": method, "url": url, "headers": headers, "timeout": t,
              "body": base64.b64encode(body).decode() if body else "",
              "insecure": ep.tls_mode in ("insecure", "pin"),
              "fingerprint": normalize_fingerprint(ep.fingerprint) if ep.tls_mode == "pin" and ep.fingerprint else ""}
    try:
        res = await agent_hub.request(ep.agent_node_id, "http.request", params, timeout=t + 10)
    except agent_hub.AgentOffline as e:
        raise HubError("the agent that reaches this MCP server is offline", "agent_offline") from e
    except agent_hub.AgentError as e:
        msg = str(e)
        if "unknown method" in msg.lower():
            raise HubError("the agent is too old to relay; update the agent", "agent_outdated") from e
        code = "tls" if "fingerprint" in msg or "certificate" in msg.lower() else "unreachable"
        raise HubError(f"via agent: {msg}", code) from e
    except TimeoutError as e:
        raise HubError("the MCP server did not answer in time", "timeout") from e
    raw = base64.b64decode(res.get("body") or "") if res.get("body_b64") else (res.get("body") or "").encode()
    hdrs = {str(k).lower(): str(v) for k, v in (res.get("headers") or {}).items()}
    return int(res.get("status") or 0), hdrs, raw


# ------------------------------------------------------------------ Streamable HTTP client
@dataclass
class _HttpSession:
    signature: str
    session_id: str | None
    protocol: str
    server_info: dict[str, Any]
    instructions: str | None
    capabilities: dict[str, Any]
    created: float


_http_sessions: dict[str, _HttpSession] = {}
_http_locks: dict[str, asyncio.Lock] = {}
_ids = itertools.count(1)


def _parse_sse(text: str) -> list[Any]:
    out, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line == "" and data:
            with contextlib.suppress(ValueError):
                out.append(json.loads("\n".join(data)))
            data = []
    return out


def _pick(messages: list[Any], rid: int) -> dict[str, Any] | None:
    flat: list[Any] = []
    for m in messages:
        flat.extend(m if isinstance(m, list) else [m])
    for m in flat:
        if isinstance(m, dict) and m.get("id") == rid and ("result" in m or "error" in m):
            return m
    return None


def _http_error(status: int, body: bytes, headers: dict[str, str]) -> HubError:
    text = body.decode(errors="replace").strip()[:300]
    if status == 401:
        return HubError("the MCP server rejected the credentials (HTTP 401)", "unauthorized", status)
    if status == 403:
        return HubError(f"the MCP server refused access (HTTP 403) {text}".strip(), "forbidden", status)
    if status == 404:
        return HubError("the MCP endpoint was not found (HTTP 404); check the URL", "not_found", status)
    if status == 429:
        return HubError("rate limited by the MCP server (HTTP 429)", "rate_limited", status)
    if status in (408, 504):
        return HubError(f"the MCP server did not answer in time (HTTP {status})", "timeout", status)
    return HubError(f"HTTP {status} {text}".strip(), "http_error", status)


class HttpClient:
    def __init__(self, ep: Endpoint, sender: Sender | None = None):
        self.ep = ep
        self.send = sender or (_send_agent if ep.agent_node_id else _send_direct)

    def _headers(self, s: _HttpSession | None) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "User-Agent": f"Stackpilot/{get_build_info().version}", **self.ep.headers}
        if s is not None:
            h["MCP-Protocol-Version"] = s.protocol
            if s.session_id:
                h["Mcp-Session-Id"] = s.session_id
        return h

    async def _post(self, payload: dict[str, Any], s: _HttpSession | None,
                    timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        if not self.ep.url:
            raise HubError("no URL set", "bad_config")
        body = json.dumps(payload).encode()
        return await self.send(self.ep, "POST", self.ep.url, body, self._headers(s), timeout or self.ep.timeout)

    async def _rpc(self, method: str, params: dict[str, Any] | None, s: _HttpSession | None,
                   timeout: float | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        rid = next(_ids)
        status, headers, body = await self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                                                  "params": params or {}}, s, timeout)
        if status >= 400 or status == 0:
            if status == 400 and self.ep.agent_node_id and s is not None and not s.session_id:
                raise HubError("the MCP server wants a session the agent did not pass on; update the agent "
                               "(0.9.0 or newer)", "agent_outdated", status)
            raise _http_error(status, body, headers)
        text = body.decode(errors="replace")
        ctype = headers.get("content-type", "")
        msgs = _parse_sse(text) if "text/event-stream" in ctype else []
        if not msgs:
            try:
                msgs = [json.loads(text)] if text.strip() else []
            except ValueError:
                msgs = _parse_sse(text)
        m = _pick(msgs, rid)
        if m is None:
            raise HubError("the MCP server sent no answer to the request", "bad_response")
        if "error" in m:
            err = m["error"] or {}
            raise RemoteToolError(str(err.get("message") or err)[:1000], "rpc_error")
        return m.get("result") or {}, headers

    async def _initialize(self) -> _HttpSession:
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _client_info()}
        result, headers = await self._rpc("initialize", params, None, timeout=min(self.ep.timeout, 30))
        s = _HttpSession(signature=self.ep.signature(), session_id=headers.get("mcp-session-id"),
                         protocol=str(result.get("protocolVersion") or PROTOCOL_VERSION),
                         server_info=result.get("serverInfo") or {}, instructions=result.get("instructions"),
                         capabilities=result.get("capabilities") or {}, created=time.monotonic())
        with contextlib.suppress(HubError):
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, s, timeout=15)
        return s

    async def session(self, fresh: bool = False) -> _HttpSession:
        lock = _http_locks.setdefault(self.ep.key, asyncio.Lock())
        async with lock:
            s = _http_sessions.get(self.ep.key)
            if fresh or s is None or s.signature != self.ep.signature() or time.monotonic() - s.created > 3600:
                s = await self._initialize()
                _http_sessions[self.ep.key] = s
            return s

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float | None = None) -> dict[str, Any]:
        s = await self.session()
        try:
            result, _ = await self._rpc(method, params, s, timeout)
            return result
        except HubError as e:
            # A server that restarted forgets its sessions (404), or wants a new one (400).
            if e.status not in (400, 404) or not s.session_id:
                raise
        s = await self.session(fresh=True)
        result, _ = await self._rpc(method, params, s, timeout)
        return result

    async def info(self) -> dict[str, Any]:
        s = await self.session(fresh=True)
        return {"server_info": s.server_info, "instructions": s.instructions, "protocol": s.protocol,
                "capabilities": s.capabilities}

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(50):
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in res.get("tools") or [] if isinstance(t, dict))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout)


def forget(key: str) -> None:
    _http_sessions.pop(key, None)
    w = _workers.pop(key, None)
    if w is not None:
        w.stop()


# ------------------------------------------------------------------ stdio / SSE through the SDK, kept open
def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    return obj


class _Worker:
    """One open SDK client in its own task; calls are queued to it."""

    def __init__(self, ep: Endpoint):
        self.ep = ep
        self.signature = ep.signature()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run())
        self.info: dict[str, Any] = {}

    def stop(self) -> None:
        self.task.cancel()

    def _client(self):
        from mcp.client.client import Client

        if self.ep.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters

            if not self.ep.command:
                raise HubError("no command set", "bad_config")
            parts = shlex.split(self.ep.command)
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
            params = StdioServerParameters(command=parts[0], args=parts[1:] + list(self.ep.args),
                                           env={**env, **self.ep.env})
            return Client(params, mode="legacy", read_timeout_seconds=self.ep.timeout)
        from mcp.client.sse import sse_client

        if not self.ep.url:
            raise HubError("no URL set", "bad_config")
        return Client(sse_client(self.ep.url, headers=dict(self.ep.headers), timeout=15), mode="legacy",
                      read_timeout_seconds=self.ep.timeout)

    async def _run(self) -> None:
        try:
            async with self._client() as c:
                self.info = {"server_info": _dump(c.server_info) or {}, "instructions": c.instructions,
                             "protocol": c.protocol_version, "capabilities": _dump(c.server_capabilities) or {}}
                self.ready.set_result(True)
                while True:
                    try:
                        fn, fut = await asyncio.wait_for(self.queue.get(), IDLE_SECONDS)
                    except TimeoutError:
                        return
                    if fut.cancelled():
                        continue
                    try:
                        fut.set_result(await fn(c))
                    except Exception as e:  # noqa: BLE001 - handed to the caller
                        if not fut.done():
                            fut.set_exception(e)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - also ExceptionGroups from anyio
            err = _unwrap(e)
            if not self.ready.done():
                self.ready.set_exception(err)
            while not self.queue.empty():
                _, fut = self.queue.get_nowait()
                if not fut.done():
                    fut.set_exception(err)
        finally:
            if _workers.get(self.ep.key) is self:
                _workers.pop(self.ep.key, None)

    async def run(self, fn, timeout: float) -> Any:
        await asyncio.wait_for(asyncio.shield(self.ready), 30)
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((fn, fut))
        return await asyncio.wait_for(fut, timeout)


def _unwrap(e: BaseException) -> HubError:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, HubError):
        return e
    if isinstance(e, FileNotFoundError):
        return HubError(f"command not found: {e.filename or e}", "not_found")
    if isinstance(e, httpx.HTTPStatusError):
        return _http_error(e.response.status_code, e.response.content, {})
    if isinstance(e, (httpx.ConnectError, OSError)):
        return HubError(f"cannot connect: {e}", "unreachable")
    return HubError(f"{type(e).__name__}: {e}"[:1000], "error")


_workers: dict[str, _Worker] = {}


class SdkClient:
    def __init__(self, ep: Endpoint):
        self.ep = ep

    def _worker(self, fresh: bool = False) -> _Worker:
        w = _workers.get(self.ep.key)
        if w is not None and (fresh or w.signature != self.ep.signature() or w.task.done()):
            w.stop()
            w = None
        if w is None:
            w = _Worker(self.ep)
            _workers[self.ep.key] = w
        return w

    async def _run(self, fn, timeout: float | None = None, fresh: bool = False) -> Any:
        w = self._worker(fresh)
        try:
            return await w.run(fn, timeout or self.ep.timeout)
        except TimeoutError as e:
            raise HubError("the MCP server did not answer in time", "timeout") from e
        except HubError:
            raise
        except Exception as e:  # noqa: BLE001
            from mcp.shared.exceptions import MCPError

            if isinstance(e, MCPError):
                raise RemoteToolError(str(e)[:1000], "rpc_error") from e
            raise _unwrap(e) from e

    async def info(self) -> dict[str, Any]:
        w = self._worker(fresh=True)
        try:
            await asyncio.wait_for(asyncio.shield(w.ready), 30)
        except TimeoutError as e:
            raise HubError("the MCP server did not start in time", "timeout") from e
        except HubError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _unwrap(e) from e
        return w.info

    async def list_tools(self) -> list[dict[str, Any]]:
        async def fn(c):
            res = await c.list_tools()
            return [_dump(t) for t in res.tools]

        return await self._run(fn)

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        async def fn(c):
            return _dump(await c.call_tool(name, arguments))

        return await self._run(fn, timeout)


def client_for(ep: Endpoint) -> HttpClient | SdkClient:
    if ep.transport == "http":
        return HttpClient(ep)
    if ep.transport in ("sse", "stdio"):
        if ep.agent_node_id:
            raise HubError("only Streamable HTTP servers can be reached through an agent", "bad_config")
        return SdkClient(ep)
    raise HubError(f"unknown transport {ep.transport}", "bad_config")
