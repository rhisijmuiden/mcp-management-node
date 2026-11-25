"""Thin wrappers around the three official UniFi APIs.

* UniFi Network Integration API (``/proxy/network/integration/v1``, header ``X-API-KEY``).
  Lists use ``offset``/``limit`` (max 200) and return ``{offset, limit, count, totalCount, data}``.
* UniFi Site Manager API (``https://api.ui.com/v1``, header ``X-API-Key``). Lists return
  ``{data, httpStatusCode, traceId, nextToken}``.
* UniFi Access developer API (``https://<console>:12445/api/v1/developer``, ``Authorization: Bearer``).
  Answers are ``{code: "SUCCESS", msg, data}``.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from mmn.services.unifi.transport import Target, UnifiError, request

PAGE = 200
MAX_PAGES = 25


def _q(v: str) -> str:
    return quote(str(v), safe="")


class NetworkApi:
    def __init__(self, target: Target):
        self.t = target

    async def _get(self, path: str, **params: Any) -> Any:
        return await request(self.t, "GET", path, params=params or None)

    async def _list(self, path: str, filter: str | None = None, max_items: int = PAGE * MAX_PAGES) -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page = await request(self.t, "GET", path, params={"offset": offset, "limit": PAGE, "filter": filter})
            if isinstance(page, list):  # tolerate an unwrapped list
                return page[:max_items]
            data = (page or {}).get("data") or []
            out.extend(data)
            total = (page or {}).get("totalCount")
            offset += len(data)
            if not data or len(out) >= max_items or (total is not None and offset >= int(total)):
                break
        return out[:max_items]

    async def info(self) -> dict:
        return await self._get("v1/info") or {}

    async def sites(self) -> list[dict]:
        return await self._list("v1/sites")

    async def devices(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/devices")

    async def device(self, site: str, device_id: str) -> dict:
        return await self._get(f"v1/sites/{_q(site)}/devices/{_q(device_id)}") or {}

    async def device_stats(self, site: str, device_id: str) -> dict:
        return await self._get(f"v1/sites/{_q(site)}/devices/{_q(device_id)}/statistics/latest") or {}

    async def clients(self, site: str, filter: str | None = None, max_items: int = PAGE * MAX_PAGES) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/clients", filter, max_items)

    async def client(self, site: str, client_id: str) -> dict:
        return await self._get(f"v1/sites/{_q(site)}/clients/{_q(client_id)}") or {}

    async def networks(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/networks")

    async def wifi(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/wifi/broadcasts")

    async def wifi_one(self, site: str, wifi_id: str) -> dict:
        return await self._get(f"v1/sites/{_q(site)}/wifi/broadcasts/{_q(wifi_id)}") or {}

    async def firewall_zones(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/firewall/zones")

    async def firewall_policies(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/firewall/policies")

    async def wans(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/wans")

    # Phase 20: VPN (Network 9.3+ / 10; older consoles answer 404 and are skipped).
    async def vpn_tunnels(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/vpn/site-to-site-tunnels")

    async def vpn_servers(self, site: str) -> list[dict]:
        return await self._list(f"v1/sites/{_q(site)}/vpn/servers")

    # actions
    async def device_action(self, site: str, device_id: str, action: str) -> Any:
        return await request(self.t, "POST", f"v1/sites/{_q(site)}/devices/{_q(device_id)}/actions",
                             json_body={"action": action})

    async def port_action(self, site: str, device_id: str, port_idx: int, action: str) -> Any:
        return await request(self.t, "POST",
                             f"v1/sites/{_q(site)}/devices/{_q(device_id)}/interfaces/ports/{int(port_idx)}/actions",
                             json_body={"action": action})

    async def client_action(self, site: str, client_id: str, action: str) -> Any:
        return await request(self.t, "POST", f"v1/sites/{_q(site)}/clients/{_q(client_id)}/actions",
                             json_body={"action": action})

    async def update_wifi(self, site: str, wifi_id: str, body: dict) -> Any:
        return await request(self.t, "PUT", f"v1/sites/{_q(site)}/wifi/broadcasts/{_q(wifi_id)}", json_body=body)


class SiteManagerApi:
    def __init__(self, target: Target):
        self.t = target

    async def _list(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        token = None
        for _ in range(MAX_PAGES):
            page = await request(self.t, "GET", path, params={**(params or {}), "pageSize": 500, "nextToken": token})
            out.extend((page or {}).get("data") or [])
            token = (page or {}).get("nextToken")
            if not token:
                break
        return out

    async def hosts(self) -> list[dict]:
        return await self._list("v1/hosts")

    async def sites(self) -> list[dict]:
        return await self._list("v1/sites")

    async def devices(self, host_ids: list[str] | None = None) -> list[dict]:
        return await self._list("v1/devices", {"hostIds[]": host_ids} if host_ids else None)

    async def isp_metrics(self, kind: str = "1h", duration: str = "7d") -> list[dict]:
        page = await request(self.t, "GET", f"v1/isp-metrics/{_q(kind)}", params={"duration": duration})
        return (page or {}).get("data") or []


class AccessApi:
    def __init__(self, target: Target):
        self.t = target

    async def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        res = await request(self.t, method, path, params=params, json_body=body)
        if isinstance(res, dict) and "code" in res:
            if str(res.get("code")).upper() != "SUCCESS":
                msg = res.get("msg") or res.get("code")
                code = "forbidden" if "PERMISSION" in str(res.get("code")).upper() else "access_error"
                raise UnifiError(f"UniFi Access: {msg}", code)
            return res.get("data")
        return res

    async def doors(self) -> list[dict]:
        return await self._call("GET", "doors") or []

    async def door(self, door_id: str) -> dict:
        return await self._call("GET", f"doors/{_q(door_id)}") or {}

    async def lock_rule(self, door_id: str) -> dict:
        return await self._call("GET", f"doors/{_q(door_id)}/lock_rule") or {}

    async def unlock(self, door_id: str, actor_name: str | None = None) -> Any:
        body = {"actor_id": "mmn", "actor_name": actor_name[:60]} if actor_name else None
        return await self._call("PUT", f"doors/{_q(door_id)}/unlock", body=body)

    async def set_lock_rule(self, door_id: str, rule: str, interval: int | None = None) -> Any:
        body: dict[str, Any] = {"type": rule}
        if interval:
            body["interval"] = int(interval)
        return await self._call("PUT", f"doors/{_q(door_id)}/lock_rule", body=body)

    async def emergency(self) -> dict:
        return await self._call("GET", "doors/settings/emergency") or {}

    async def set_emergency(self, lockdown: bool, evacuation: bool) -> Any:
        return await self._call("PUT", "doors/settings/emergency",
                                body={"lockdown": bool(lockdown), "evacuation": bool(evacuation)})

    async def schedules(self) -> list[dict]:
        return await self._call("GET", "access_policies/schedules") or []

    async def policies(self) -> list[dict]:
        return await self._call("GET", "access_policies") or []

    async def devices(self) -> Any:
        return await self._call("GET", "devices") or []

    async def logs(self, topic: str = "door_openings", since: int | None = None, until: int | None = None,
                   page_size: int = 100, actor_id: str | None = None) -> list[dict]:
        now = int(time.time())
        body: dict[str, Any] = {"topic": topic, "since": since or now - 86400, "until": until or now}
        if actor_id:
            body["actor_id"] = actor_id
        data = await self._call("POST", "system/logs", params={"page_num": 1, "page_size": max(1, min(page_size, 500))},
                                body=body)
        return (data or {}).get("hits") or [] if isinstance(data, dict) else (data or [])
