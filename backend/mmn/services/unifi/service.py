"""UniFi connections: storage, polling (snapshot + events), overviews and actions."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Node, UnifiConnection, UnifiEvent
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services.unifi import insights
from mmn.services.unifi.apis import AccessApi, NetworkApi, SiteManagerApi
from mmn.services.unifi.transport import (
    CLOUD_BASE,
    Target,
    UnifiError,
    normalize_fingerprint,
    pretty_fingerprint,
)

log = logging.getLogger("mmn.unifi")

KINDS = ("network", "site_manager", "access")
TRANSPORTS = ("direct", "agent", "cloud")
TLS_MODES = ("verify", "pin", "insecure")
EVENT_KEEP_DAYS = 90
_locks: dict[str, asyncio.Lock] = {}


# ------------------------------------------------------------------ connections
class ConnectionIn(BaseModel):
    environment_id: str
    name: str = Field(min_length=1, max_length=100)
    kind: str
    transport: str = "direct"
    base_url: str | None = Field(default=None, max_length=500)
    agent_node_id: str | None = None
    cloud_connection_id: str | None = None
    console_id: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    read_only: bool = False
    tls_mode: str = "verify"
    tls_fingerprint: str | None = Field(default=None, max_length=100)
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError("kind must be network, site_manager or access")
        return v

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str) -> str:
        if v not in TRANSPORTS:
            raise ValueError("transport must be direct, agent or cloud")
        return v

    @field_validator("tls_mode")
    @classmethod
    def _tls(cls, v: str) -> str:
        if v not in TLS_MODES:
            raise ValueError("tls_mode must be verify, pin or insecure")
        return v


class ConnectionPatch(BaseModel):
    environment_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    transport: str | None = None
    base_url: str | None = Field(default=None, max_length=500)
    agent_node_id: str | None = None
    cloud_connection_id: str | None = None
    console_id: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)  # empty = keep
    read_only: bool | None = None
    tls_mode: str | None = None
    tls_fingerprint: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None


def default_url(kind: str, url: str | None) -> str | None:
    """Normalise the address: add the API path when only a host was given."""
    if kind == "site_manager":
        return CLOUD_BASE
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise UnifiError("enter an address like https://192.168.1.1", "bad_url")
    path = p.path.rstrip("/")
    netloc = p.netloc
    if kind == "network":
        path = re.sub(r"/v1$", "", path) or "/proxy/network/integration"
    else:  # access
        if not p.port and not path:
            netloc = f"{netloc}:12445"
        path = path or "/api/v1/developer"
    return urlunsplit((p.scheme, netloc, path, "", ""))


async def _validate(db: AsyncSession, c: UnifiConnection) -> None:
    if c.kind == "site_manager" and c.transport == "cloud":
        raise UnifiError("Site Manager connections are direct (or through an agent)", "bad_transport")
    if c.kind == "access" and c.transport == "cloud":
        raise UnifiError("UniFi Access is reached directly or through an agent", "bad_transport")
    if c.transport == "agent":
        n = await db.get(Node, c.agent_node_id) if c.agent_node_id else None
        if n is None:
            raise UnifiError("choose the agent (server or node) that can reach the console", "no_agent")
    if c.transport == "cloud":
        cloud = await db.get(UnifiConnection, c.cloud_connection_id) if c.cloud_connection_id else None
        if cloud is None or cloud.kind != "site_manager":
            raise UnifiError("choose the Site Manager connection to go through", "no_cloud")
        if not c.console_id:
            raise UnifiError("choose the console (host id) in Site Manager", "no_console")
    elif c.kind != "site_manager" and not c.base_url:
        raise UnifiError("enter the console address", "bad_url")
    if c.tls_mode == "pin":
        c.tls_fingerprint = normalize_fingerprint(c.tls_fingerprint)
        if not c.tls_fingerprint:
            raise UnifiError("pinning needs the certificate fingerprint", "bad_fingerprint")
    if c.transport != "cloud" and not c.api_key_enc:
        raise UnifiError("enter the API key", "no_key")


async def create_connection(db: AsyncSession, body: ConnectionIn) -> UnifiConnection:
    if await _by_name(db, body.name):
        raise UnifiError("a UniFi connection with this name exists", "duplicate")
    c = UnifiConnection(environment_id=body.environment_id, name=body.name.strip(), kind=body.kind,
                        transport=body.transport, agent_node_id=body.agent_node_id or None,
                        cloud_connection_id=body.cloud_connection_id or None, console_id=body.console_id or None,
                        api_key_enc=encrypt_secret((body.api_key or "").strip()), read_only=body.read_only,
                        tls_mode=body.tls_mode, tls_fingerprint=body.tls_fingerprint or None, enabled=body.enabled)
    if body.transport != "cloud":
        c.base_url = default_url(body.kind, body.base_url)
    await _validate(db, c)
    db.add(c)
    await db.flush()
    return c


async def update_connection(db: AsyncSession, c: UnifiConnection, body: ConnectionPatch) -> UnifiConnection:
    data = body.model_dump(exclude_unset=True)
    key = data.pop("api_key", None)
    if "name" in data and data["name"] and data["name"].lower() != c.name.lower() and await _by_name(db, data["name"]):
        raise UnifiError("a UniFi connection with this name exists", "duplicate")
    for k, v in data.items():
        if k == "transport" and v not in TRANSPORTS or k == "tls_mode" and v not in TLS_MODES:
            raise UnifiError(f"invalid {k}", "invalid")
        if k == "base_url":
            v = default_url(c.kind, v) if v else None
        if k == "environment_id" and v is None:
            continue
        setattr(c, k, v if v != "" else None)
    if key:
        c.api_key_enc = encrypt_secret(key.strip())
    await _validate(db, c)
    c.status = "unknown"
    await db.flush()
    return c


async def _by_name(db: AsyncSession, name: str) -> UnifiConnection | None:
    return (await db.execute(select(UnifiConnection).where(func.lower(UnifiConnection.name) == name.strip().lower()))
            ).scalars().first()


async def get_connection(db: AsyncSession, ref: str, kind: str | tuple[str, ...] | None = None) -> UnifiConnection:
    c = await db.get(UnifiConnection, ref) or await _by_name(db, ref)
    kinds = (kind,) if isinstance(kind, str) else kind
    if c is None or (kinds and c.kind not in kinds):
        raise UnifiError("UniFi connection not found", "not_found")
    return c


async def list_connections(db: AsyncSession, environment_id: str | None = None,
                           kind: str | None = None) -> list[UnifiConnection]:
    q = select(UnifiConnection).order_by(UnifiConnection.name)
    if environment_id:
        q = q.where(UnifiConnection.environment_id == environment_id)
    if kind:
        q = q.where(UnifiConnection.kind == kind)
    return list((await db.execute(q)).scalars())


def connection_out(c: UnifiConnection) -> dict[str, Any]:
    snap = c.snapshot or {}
    return {
        "id": c.id, "name": c.name, "kind": c.kind, "transport": c.transport, "base_url": c.base_url,
        "environment_id": c.environment_id, "environment_name": c.environment.name if c.environment else None,
        "agent_node_id": c.agent_node_id, "cloud_connection_id": c.cloud_connection_id, "console_id": c.console_id,
        "has_key": bool(c.api_key_enc), "read_only": c.read_only, "tls_mode": c.tls_mode,
        "tls_fingerprint": pretty_fingerprint(c.tls_fingerprint) if c.tls_fingerprint else None,
        "enabled": c.enabled, "status": c.status, "last_error": c.last_error, "last_ok_at": c.last_ok_at,
        "last_poll_at": c.last_poll_at, "version": (c.info or {}).get("version"),
        "sites": [{"id": s.get("id"), "name": s.get("name")} for s in (c.info or {}).get("sites") or []],
        "totals": snap.get("totals") or {},
    }


async def target(db: AsyncSession, c: UnifiConnection) -> Target:
    if c.transport == "cloud":
        cloud = await db.get(UnifiConnection, c.cloud_connection_id) if c.cloud_connection_id else None
        if cloud is None:
            raise UnifiError("the Site Manager connection used by this console is gone", "no_cloud")
        key = decrypt_secret(cloud.api_key_enc)
        if not key:
            raise UnifiError("the Site Manager connection has no API key", "no_key")
        base = f"{CLOUD_BASE}/v1/connector/consoles/{c.console_id}/network/integration"
        return Target(base_url=base, headers={"X-API-Key": key}, transport=cloud.transport,
                      agent_node_id=cloud.agent_node_id, label=c.name)
    key = decrypt_secret(c.api_key_enc)
    if not key:
        raise UnifiError("this connection has no (readable) API key; enter it again", "no_key")
    headers = {"Authorization": f"Bearer {key}"} if c.kind == "access" else {"X-API-KEY": key}
    return Target(base_url=c.base_url or CLOUD_BASE, headers=headers, transport=c.transport,
                  agent_node_id=c.agent_node_id, tls_mode=c.tls_mode, fingerprint=c.tls_fingerprint, label=c.name)


async def network_api(db: AsyncSession, c: UnifiConnection) -> NetworkApi:
    if c.kind != "network":
        raise UnifiError("this is not a UniFi Network connection", "wrong_kind")
    return NetworkApi(await target(db, c))


async def access_api(db: AsyncSession, c: UnifiConnection) -> AccessApi:
    if c.kind != "access":
        raise UnifiError("this is not a UniFi Access connection", "wrong_kind")
    return AccessApi(await target(db, c))


async def cloud_api(db: AsyncSession, c: UnifiConnection) -> SiteManagerApi:
    if c.kind != "site_manager":
        raise UnifiError("this is not a Site Manager connection", "wrong_kind")
    return SiteManagerApi(await target(db, c))


def require_writable(c: UnifiConnection) -> None:
    if c.read_only:
        raise UnifiError("this connection uses a read-only API key; actions are switched off", "read_only")


# ------------------------------------------------------------------ test
async def test_connection(db: AsyncSession, c: UnifiConnection) -> dict[str, Any]:
    started = time.monotonic()
    if c.kind == "network":
        api = await network_api(db, c)
        info = await api.info()
        sites = await api.sites()
        res = {"version": info.get("applicationVersion"),
               "sites": [{"id": s.get("id"), "name": s.get("name") or s.get("internalReference")} for s in sites]}
    elif c.kind == "site_manager":
        api = await cloud_api(db, c)
        hosts = await api.hosts()
        res = {"version": None, "hosts": [_host_out(h) for h in hosts][:200], "sites": []}
    else:
        api = await access_api(db, c)
        doors = await api.doors()
        res = {"version": None, "doors": len(doors), "sites": []}
    res["ms"] = int((time.monotonic() - started) * 1000)
    return res


def _host_out(h: dict) -> dict[str, Any]:
    rs = h.get("reportedState") or {}
    return {"id": h.get("id"), "name": rs.get("name") or rs.get("hostname") or h.get("id"),
            "type": h.get("type"), "ip": h.get("ipAddress"), "state": rs.get("state"),
            "version": rs.get("version"), "firmware": rs.get("firmware_version") or (rs.get("hardware") or {}).get(
                "firmwareVersion"), "blocked": bool(h.get("isBlocked")),
            "last_change": h.get("lastConnectionStateChange")}


# ------------------------------------------------------------------ polling
async def _gather(fns: list, limit: int = 6) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def one(fn):
        async with sem:
            try:
                return await fn()
            except UnifiError as e:
                return e

    return await asyncio.gather(*(one(f) for f in fns))


async def _optional(coro) -> Any:
    try:
        return await coro
    except UnifiError as e:
        if e.code in ("not_found", "forbidden"):
            return None
        raise


async def _network_snapshot(api: NetworkApi) -> dict[str, Any]:
    info = await api.info()
    sites = await api.sites()
    out_sites = []
    for s in sites:
        sid = s.get("id")
        sname = s.get("name") or s.get("internalReference") or sid
        devs = await api.devices(sid)
        details = await _gather([lambda d=d, sid=sid: api.device(sid, d["id"]) for d in devs])
        devices = [insights.norm_device(d, det if isinstance(det, dict) else None) for d, det in zip(devs, details, strict=True)]
        names = {d["id"]: d["name"] for d in devices}
        for d in devices:
            d["uplink_name"] = names.get(d["uplink_device_id"] or "")
        clients = await api.clients(sid)
        counts = {"total": len(clients), "wired": 0, "wireless": 0, "guest": 0, "vpn": 0}
        per_dev: dict[str, int] = {}
        for c in clients:
            t = str(c.get("type") or "").upper()
            counts["wireless" if t == "WIRELESS" else "wired" if t == "WIRED" else "vpn"] += 1
            if str((c.get("access") or {}).get("type") or "").upper() == "GUEST":
                counts["guest"] += 1
            if c.get("uplinkDeviceId"):
                per_dev[c["uplinkDeviceId"]] = per_dev.get(c["uplinkDeviceId"], 0) + 1
        for d in devices:
            d["clients"] = per_dev.get(d["id"], 0)
        nets_raw = await _optional(api.networks(sid))
        networks = [insights.norm_network(n) for n in nets_raw or []]
        nmap = {n["id"]: n for n in networks}
        wifi_raw = await _optional(api.wifi(sid))
        wifi = [insights.norm_wifi(w, nmap) for w in wifi_raw] if wifi_raw is not None else None
        wans_raw = await _optional(api.wans(sid))
        wans = [_wan_out(w) for w in wans_raw or []]
        out_sites.append({"id": sid, "name": sname, "devices": devices, "clients": counts, "networks": networks,
                          "wifi": wifi, "wans": wans})
    return {"version": info.get("applicationVersion"), "sites": out_sites}


def _wan_out(w: dict) -> dict[str, Any]:
    keep = {k: v for k, v in w.items() if not isinstance(v, (dict, list))}
    return {"id": w.get("id"), "name": w.get("name"),
            "status": {k: v for k, v in keep.items() if re.search(r"state|status|active|up|primary|failover", k, re.I)}}


async def _cloud_snapshot(api: SiteManagerApi) -> dict[str, Any]:
    hosts = await api.hosts()
    sites = await _optional(api.sites()) or []
    devices = await _optional(api.devices()) or []
    host_names = {h.get("id"): _host_out(h)["name"] for h in hosts}
    out_sites = []
    for s in sites:
        meta = s.get("meta") or {}
        stats = s.get("statistics") or {}
        counts = stats.get("counts") or {}
        out_sites.append({"id": s.get("siteId"), "host_id": s.get("hostId"), "host_name": host_names.get(s.get("hostId")),
                          "name": meta.get("desc") or meta.get("name") or s.get("siteId"),
                          "counts": counts, "isp": stats.get("ispInfo") or {},
                          "wan_uptime": (stats.get("percentages") or {}).get("wanUptime"),
                          "gateway": (stats.get("gateway") or {}).get("shortname")})
    devs = []
    for group in devices:
        for d in group.get("devices") or []:
            devs.append({"id": d.get("id"), "name": d.get("name"), "model": d.get("model") or d.get("shortname"),
                         "mac": d.get("mac"), "ip": d.get("ip"), "host_id": group.get("hostId"),
                         "host_name": group.get("hostName"), "state": str(d.get("status") or "").upper(),
                         "online": str(d.get("status") or "").lower() == "online", "firmware": d.get("version"),
                         "firmware_updatable": d.get("firmwareStatus") == "updateAvailable" or bool(d.get("updateAvailable")),
                         "product_line": d.get("productLine")})
    return {"hosts": [_host_out(h) for h in hosts], "sites": out_sites, "devices": devs}


async def _access_snapshot(api: AccessApi) -> dict[str, Any]:
    doors = await api.doors()
    return {"doors": [door_out(d) for d in doors]}


def door_out(d: dict) -> dict[str, Any]:
    return {"id": d.get("id"), "name": d.get("name"), "full_name": d.get("full_name"),
            "lock": d.get("door_lock_relay_status"), "position": d.get("door_position_status"),
            "has_hub": bool(d.get("is_bind_hub")), "floor_id": d.get("floor_id")}


def _totals(kind: str, snap: dict[str, Any]) -> dict[str, Any]:
    if kind == "network":
        devs = [d for s in snap.get("sites") or [] for d in s["devices"]]
        return {"sites": len(snap.get("sites") or []), "devices": len(devs),
                "offline": sum(1 for d in devs if not d["online"]),
                "firmware": sum(1 for d in devs if d["firmware_updatable"]),
                "clients": sum(s["clients"]["total"] for s in snap.get("sites") or []),
                "aps": sum(1 for d in devs if d["type"] == "ap"),
                "switches": sum(1 for d in devs if d["type"] == "switch"),
                "gateways": sum(1 for d in devs if d["type"] == "gateway")}
    if kind == "site_manager":
        devs = snap.get("devices") or []
        return {"hosts": len(snap.get("hosts") or []), "sites": len(snap.get("sites") or []), "devices": len(devs),
                "offline": sum(1 for d in devs if not d["online"]),
                "firmware": sum(1 for d in devs if d["firmware_updatable"])}
    doors = snap.get("doors") or []
    return {"doors": len(doors), "unlocked": sum(1 for d in doors if d["lock"] == "unlock"),
            "open": sum(1 for d in doors if d["position"] == "open")}


def _diff_events(c: UnifiConnection, old: dict[str, Any], new: dict[str, Any]) -> list[UnifiEvent]:
    ev: list[UnifiEvent] = []
    if not old:
        return ev
    if c.kind == "network":
        old_sites = {s["id"]: s for s in old.get("sites") or []}
        for s in new.get("sites") or []:
            prev = old_sites.get(s["id"])
            if not prev:
                continue
            pd = {d["id"]: d for d in prev["devices"]}
            for d in s["devices"]:
                p = pd.get(d["id"])
                if p is None:
                    ev.append(_ev(c, s, "device_added", "info", d, f"{d['name']} ({d['model']}) appeared"))
                    continue
                if p["online"] and not d["online"]:
                    sev = "critical" if d["type"] == "gateway" else "warning"
                    ev.append(_ev(c, s, "device_offline", sev, d, f"{d['name']} went {d['state'].lower()}"))
                elif not p["online"] and d["online"]:
                    ev.append(_ev(c, s, "device_online", "info", d, f"{d['name']} is back online"))
                if d["firmware_updatable"] and not p["firmware_updatable"]:
                    ev.append(_ev(c, s, "firmware_available", "info", d,
                                  f"firmware update available for {d['name']} (now {d['firmware']})"))
                if d["uplink_device_id"] and p["uplink_device_id"] and d["uplink_device_id"] != p["uplink_device_id"]:
                    ev.append(_ev(c, s, "uplink_changed", "warning", d,
                                  f"{d['name']} uplink moved from {p.get('uplink_name')} to {d.get('uplink_name')}"))
            pw = {w["id"]: w for w in prev.get("wans") or []}
            for w in s.get("wans") or []:
                if w["id"] in pw and w["status"] and pw[w["id"]]["status"] and w["status"] != pw[w["id"]]["status"]:
                    ev.append(_ev(c, s, "wan_change", "critical", None, f"WAN {w['name']}: {pw[w['id']]['status']} → "
                                                                        f"{w['status']} (failover?)",
                                  {"wan": w["name"], "before": pw[w["id"]]["status"], "after": w["status"]}))
    elif c.kind == "site_manager":
        ph = {h["id"]: h for h in old.get("hosts") or []}
        for h in new.get("hosts") or []:
            p = ph.get(h["id"])
            if p and p.get("state") != h.get("state") and (p.get("state") or h.get("state")):
                sev = "warning" if str(h.get("state")).lower() != "connected" else "info"
                ev.append(_ev(c, None, "console_state", sev, None,
                              f"console {h['name']}: {p.get('state')} → {h.get('state')}", {"host": h["id"]}))
        pd = {d["id"]: d for d in old.get("devices") or []}
        for d in new.get("devices") or []:
            p = pd.get(d["id"])
            if p and p["online"] and not d["online"]:
                ev.append(_ev(c, None, "device_offline", "warning", d, f"{d['name']} ({d.get('host_name')}) went offline"))
            elif p and not p["online"] and d["online"]:
                ev.append(_ev(c, None, "device_online", "info", d, f"{d['name']} ({d.get('host_name')}) is back online"))
    return ev


def _ev(c: UnifiConnection, site: dict | None, kind: str, severity: str, dev: dict | None, msg: str,
        detail: dict | None = None) -> UnifiEvent:
    d = dict(detail or {})
    if dev:
        d.update(device_id=dev["id"], model=dev.get("model"), mac=dev.get("mac"), type=dev.get("type"))
    return UnifiEvent(connection_id=c.id, site_id=site["id"] if site else None, site_name=site["name"] if site else None,
                      kind=kind, severity=severity, subject=dev["name"] if dev else None, message=msg[:500], detail=d)


async def poll(connection_id: str) -> dict[str, Any]:
    """Refresh the snapshot of one connection and record what changed."""
    lock = _locks.setdefault(connection_id, asyncio.Lock())
    async with lock, sessionmaker()() as db:
        c = await db.get(UnifiConnection, connection_id)
        if c is None:
            raise UnifiError("UniFi connection not found", "not_found")
        now = datetime.now(UTC)
        c.last_poll_at = now
        try:
            if c.kind == "network":
                snap = await _network_snapshot(await network_api(db, c))
                c.info = {"version": snap.pop("version", None),
                          "sites": [{"id": s["id"], "name": s["name"]} for s in snap["sites"]]}
            elif c.kind == "site_manager":
                snap = await _cloud_snapshot(await cloud_api(db, c))
                c.info = {"sites": [{"id": s["id"], "name": s["name"]} for s in snap["sites"]],
                          "hosts": [{"id": h["id"], "name": h["name"]} for h in snap["hosts"]]}
            else:
                snap = await _access_snapshot(await access_api(db, c))
        except UnifiError as e:
            if c.status != "error":
                db.add(UnifiEvent(connection_id=c.id, kind="connection_error", severity="warning",
                                  subject=c.name, message=f"cannot reach {c.name}: {e}"[:500]))
            c.status, c.last_error = "error", str(e)[:2000]
            await db.commit()
            raise
        snap["polled_at"] = now.isoformat()
        snap["totals"] = _totals(c.kind, snap)
        for e in _diff_events(c, c.snapshot or {}, snap):
            db.add(e)
        if c.status == "error":
            db.add(UnifiEvent(connection_id=c.id, kind="connection_restored", severity="info", subject=c.name,
                              message=f"{c.name} is reachable again"))
        c.snapshot, c.status, c.last_error, c.last_ok_at = snap, "ok", None, now
        await db.commit()
        return snap


async def poll_all() -> None:
    async with sessionmaker()() as db:
        ids = list((await db.execute(select(UnifiConnection.id).where(UnifiConnection.enabled.is_(True)))).scalars())
    for cid in ids:
        try:
            await poll(cid)
        except UnifiError as e:
            log.info("UniFi poll of %s failed: %s", cid, e)
        except Exception:  # noqa: BLE001
            log.exception("UniFi poll of %s failed", cid)


async def prune_events() -> None:
    async with sessionmaker()() as db:
        await db.execute(delete(UnifiEvent).where(UnifiEvent.at < datetime.now(UTC) - timedelta(days=EVENT_KEEP_DAYS)))
        await db.commit()


# ------------------------------------------------------------------ overviews (from the snapshots)
async def network_overview(db: AsyncSession, environment_id: str | None = None,
                           connection_id: str | None = None) -> dict[str, Any]:
    conns = [c for c in await list_connections(db, environment_id)
             if c.kind in ("network", "site_manager") and (not connection_id or c.id == connection_id)]
    devices, sites, wifi = [], [], []
    for c in conns:
        snap = c.snapshot or {}
        base = {"connection_id": c.id, "connection": c.name, "environment": c.environment.name if c.environment else None}
        if c.kind == "network":
            for s in snap.get("sites") or []:
                sites.append({**base, "site_id": s["id"], "site": s["name"], "clients": s["clients"],
                              "devices": len(s["devices"]), "offline": sum(1 for d in s["devices"] if not d["online"]),
                              "wans": s.get("wans") or []})
                devices += [{**base, "site_id": s["id"], "site": s["name"], **{k: v for k, v in d.items() if k != "ports"},
                             "source": "network"} for d in s["devices"]]
                wifi += [{**base, "site_id": s["id"], "site": s["name"], **w} for w in s.get("wifi") or []]
        else:
            for s in snap.get("sites") or []:
                cnt = s.get("counts") or {}
                sites.append({**base, "site_id": s["id"], "site": s["name"], "host": s.get("host_name"),
                              "clients": {"total": (cnt.get("wifiClient") or 0) + (cnt.get("wiredClient") or 0),
                                          "wireless": cnt.get("wifiClient"), "wired": cnt.get("wiredClient"),
                                          "guest": cnt.get("guestClient")},
                              "devices": cnt.get("totalDevice"), "offline": cnt.get("offlineDevice"),
                              "wan_uptime": s.get("wan_uptime"), "isp": (s.get("isp") or {}).get("name"),
                              "cloud": True})
            devices += [{**base, "site": d.get("host_name"), **d, "type": _cloud_type(d), "source": "cloud"}
                        for d in snap.get("devices") or [] if (d.get("product_line") or "network") == "network"]
    order = {"gateway": 0, "switch": 1, "ap": 2, "other": 3}
    devices.sort(key=lambda d: (d["online"], order.get(d["type"], 9), str(d.get("name")).lower()))
    q = select(UnifiEvent).order_by(UnifiEvent.at.desc()).limit(30)
    ids = [c.id for c in conns]
    events = [event_out(e, {c.id: c.name for c in conns}) for e in
              (await db.execute(q.where(UnifiEvent.connection_id.in_(ids)))).scalars()] if ids else []
    return {"connections": [connection_out(c) for c in conns], "sites": sites, "devices": devices, "wifi": wifi,
            "events": events,
            "totals": {"devices": len(devices), "offline": sum(1 for d in devices if not d["online"]),
                       "firmware": sum(1 for d in devices if d.get("firmware_updatable")),
                       "clients": sum((s["clients"] or {}).get("total") or 0 for s in sites),
                       "connections": len(conns), "errors": sum(1 for c in conns if c.status == "error")}}


def _cloud_type(d: dict) -> str:
    m = str(d.get("model") or "").upper().replace("-", "")
    if m.startswith(insights.GATEWAY_PREFIXES):
        return "gateway"
    if m.startswith(("US", "USW", "USL")):
        return "switch"
    if m.startswith(("U6", "U7", "UAP", "UAL", "E7", "AC", "NANOHD", "UWB", "UK")):
        return "ap"
    return "other"


def event_out(e: UnifiEvent, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": e.id, "at": e.at, "connection_id": e.connection_id, "connection": (names or {}).get(e.connection_id),
            "site": e.site_name, "kind": e.kind, "severity": e.severity, "subject": e.subject, "message": e.message,
            "detail": e.detail}


async def list_events(db: AsyncSession, connection_id: str | None = None, since_hours: int = 168,
                      kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    q = select(UnifiEvent).where(UnifiEvent.at >= datetime.now(UTC) - timedelta(hours=since_hours))
    if connection_id:
        q = q.where(UnifiEvent.connection_id == connection_id)
    if kind:
        q = q.where(UnifiEvent.kind == kind)
    rows = (await db.execute(q.order_by(UnifiEvent.at.desc()).limit(limit))).scalars().all()
    names = {c.id: c.name for c in await list_connections(db)}
    return [event_out(e, names) for e in rows]


# ------------------------------------------------------------------ live helpers for one site
def site_of(c: UnifiConnection, ref: str | None) -> dict[str, Any]:
    sites = (c.info or {}).get("sites") or []
    if not sites:
        raise UnifiError(f"{c.name} has no sites yet; test or refresh the connection first", "no_site")
    if not ref:
        if len(sites) == 1:
            return sites[0]
        raise UnifiError(f"{c.name} has several sites, name one: " + ", ".join(str(s['name']) for s in sites), "site")
    for s in sites:
        if ref in (s.get("id"), s.get("name")) or str(s.get("name") or "").lower() == ref.lower():
            return s
    raise UnifiError("site not found", "not_found")


def snapshot_site(c: UnifiConnection, site_id: str) -> dict[str, Any]:
    return next((s for s in (c.snapshot or {}).get("sites") or [] if s["id"] == site_id), {"devices": [], "wifi": None})


def find_device(c: UnifiConnection, site_id: str, ref: str) -> dict[str, Any]:
    devs = snapshot_site(c, site_id)["devices"]
    r = ref.strip().lower()
    for d in devs:
        if r in (str(d["id"]).lower(), str(d["name"]).lower()) or (len(insights.norm_mac(r)) == 12
                                                                    and insights.norm_mac(r) == insights.norm_mac(d["mac"])):
            return d
    cands = [d for d in devs if r in str(d["name"]).lower()]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise UnifiError("several devices match: " + ", ".join(d["name"] for d in cands[:10]), "ambiguous")
    raise UnifiError("device not found (refresh the connection if it is new)", "not_found")


async def device_detail(db: AsyncSession, c: UnifiConnection, site_id: str, device_id: str) -> dict[str, Any]:
    api = await network_api(db, c)
    det = await api.device(site_id, device_id)
    stats = await _optional(api.device_stats(site_id, device_id)) or {}
    d = insights.norm_device(det, None)
    names = {x["id"]: x["name"] for x in snapshot_site(c, site_id)["devices"]}
    d["uplink_name"] = names.get(d["uplink_device_id"] or "")
    up = stats.get("uplink") or {}
    d["stats"] = {"uptime_sec": stats.get("uptimeSec"), "cpu_pct": stats.get("cpuUtilizationPct"),
                  "mem_pct": stats.get("memoryUtilizationPct"), "load_1": stats.get("loadAverage1Min"),
                  "uplink_tx_bps": up.get("txRateBps"), "uplink_rx_bps": up.get("rxRateBps"),
                  "last_heartbeat": stats.get("lastHeartbeatAt"),
                  "radios": [{"frequency_ghz": r.get("frequencyGHz"), "tx_retries_pct": r.get("txRetriesPct")}
                             for r in (stats.get("interfaces") or {}).get("radios") or []]}
    clients = [insights.norm_client(x, names) for x in await api.clients(site_id, max_items=2000)
               if x.get("uplinkDeviceId") == device_id]
    d["client_list"] = clients
    d["clients"] = len(clients)
    return d


async def clients(db: AsyncSession, c: UnifiConnection, site_id: str, query: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
    api = await network_api(db, c)
    names = {x["id"]: x["name"] for x in snapshot_site(c, site_id)["devices"]}
    rows = [insights.norm_client(x, names) for x in await api.clients(site_id)]
    if query:
        rows = [r for r in rows if insights.matches_client(r, query)]
    return rows[:limit]


async def live_wifi(db: AsyncSession, c: UnifiConnection, site_id: str) -> list[dict[str, Any]] | None:
    api = await network_api(db, c)
    nets = await _optional(api.networks(site_id)) or []
    nmap = {n["id"]: n for n in (insights.norm_network(x) for x in nets)}
    raw = await _optional(api.wifi(site_id))
    return [insights.norm_wifi(w, nmap) for w in raw] if raw is not None else None


async def diagnose_client(db: AsyncSession, c: UnifiConnection, site_id: str, query: str) -> dict[str, Any]:
    api = await network_api(db, c)
    snap = snapshot_site(c, site_id)
    names = {x["id"]: x["name"] for x in snap["devices"]}
    cl = [insights.norm_client(x, names) for x in await api.clients(site_id)]
    wifi = await live_wifi(db, c, site_id)
    res = insights.diagnose_client(query, cl, wifi, snap["devices"])
    since = datetime.now(UTC) - timedelta(hours=24)
    evs = (await db.execute(select(UnifiEvent).where(UnifiEvent.connection_id == c.id, UnifiEvent.at >= since)
                            .order_by(UnifiEvent.at.desc()).limit(20))).scalars()
    res["recent_events"] = [event_out(e) for e in evs]
    return res


async def guest_isolation(db: AsyncSession, c: UnifiConnection, site_id: str) -> dict[str, Any]:
    api = await network_api(db, c)
    nets = await _optional(api.networks(site_id))
    if nets is None:
        raise UnifiError("networks are not available through this UniFi version's API (Network 9.3 or newer)",
                         "not_supported")
    nmap = {n["id"]: n for n in (insights.norm_network(x) for x in nets)}
    raw = await _optional(api.wifi(site_id))
    wifi = [insights.norm_wifi(w, nmap) for w in raw] if raw is not None else None
    zones = await _optional(api.firewall_zones(site_id))
    pols = await _optional(api.firewall_policies(site_id))
    res = insights.guest_isolation(nets, wifi, zones, pols)
    if zones is None:
        res["note"] += " Firewall zones are not available through this console's API (zone-based firewall needed)."
    return res


def firmware_report(conns: list[UnifiConnection], device_type: str | None = None) -> list[dict[str, Any]]:
    out = []
    for c in conns:
        snap = c.snapshot or {}
        if c.kind == "network":
            devs = [(s["name"], d) for s in snap.get("sites") or [] for d in s["devices"]]
        elif c.kind == "site_manager":
            devs = [(d.get("host_name"), {**d, "type": _cloud_type(d)}) for d in snap.get("devices") or []]
        else:
            continue
        for site, d in devs:
            if d.get("firmware_updatable") and (not device_type or d["type"] == device_type):
                out.append({"connection": c.name, "environment": c.environment.name if c.environment else None,
                            "site": site, "device": d["name"], "type": d["type"], "model": d.get("model"),
                            "firmware": d.get("firmware"), "online": d["online"]})
    return out


# ------------------------------------------------------------------ access
async def access_log(db: AsyncSession, c: UnifiConnection, *, hours: float = 24, since: datetime | None = None,
                     until: datetime | None = None, topic: str = "door_openings", door: str | None = None,
                     actor: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    api = await access_api(db, c)
    now = datetime.now(UTC)
    s = since or now - timedelta(hours=hours)
    u = until or now
    hits = await api.logs(topic, int(s.timestamp()), int(u.timestamp()), page_size=min(500, max(limit, 50)))
    rows = [log_out(h) for h in hits]
    if door:
        d = door.lower()
        rows = [r for r in rows if any(d in str(t.get("name") or "").lower() or d == str(t.get("id")).lower()
                                       for t in r["targets"])]
    if actor:
        a = actor.lower()
        rows = [r for r in rows if a in str(r.get("actor") or "").lower()]
    return rows[:limit]


def log_out(h: dict) -> dict[str, Any]:
    src = h.get("_source") or h
    actor = src.get("actor") or {}
    ev = src.get("event") or {}
    auth = src.get("authentication") or {}
    return {"at": h.get("@timestamp") or ev.get("published"), "actor": actor.get("display_name"),
            "actor_type": actor.get("type"), "event": ev.get("type"), "result": ev.get("result"),
            "message": ev.get("display_message"), "method": auth.get("credential_provider"),
            "targets": [{"type": t.get("type"), "id": t.get("id"), "name": t.get("display_name")}
                        for t in src.get("target") or []]}


async def find_door(db: AsyncSession, c: UnifiConnection, ref: str) -> dict[str, Any]:
    doors = [door_out(d) for d in await (await access_api(db, c)).doors()]
    r = ref.strip().lower()
    exact = [d for d in doors if r in (str(d["id"]).lower(), str(d["name"]).lower(), str(d["full_name"]).lower())]
    cands = exact or [d for d in doors if r in str(d["full_name"] or d["name"]).lower()]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise UnifiError("several doors match: " + ", ".join(str(d["name"]) for d in cands[:10]), "ambiguous")
    raise UnifiError("door not found", "not_found")


LOCK_RULES = ("keep_lock", "keep_unlock", "custom", "reset", "lock_early")
