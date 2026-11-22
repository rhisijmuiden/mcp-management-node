"""Normalising UniFi API objects and the analyses behind the MCP tools: firmware status, why a client cannot
get on WiFi, and whether guests are isolated from the LAN."""

from __future__ import annotations

import re
from typing import Any

GATEWAY_PREFIXES = ("UDM", "UDR", "UDW", "UCG", "UXG", "USG", "UGW", "EFG", "UX")
GUEST_RE = re.compile(r"guest|gast|hotspot|visitor|bezoek", re.I)


def norm_mac(v: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (v or "").lower())


def _feature_keys(d: dict) -> set[str]:
    f = d.get("features")
    if isinstance(f, dict):
        return {k.lower() for k in f}
    if isinstance(f, list):
        return {str(k).lower() for k in f}
    return set()


def device_type(d: dict) -> str:
    keys = _feature_keys(d)
    model = str(d.get("model") or "").upper().replace("-", "")
    if "gateway" in keys or "routing" in keys or model.startswith(GATEWAY_PREFIXES):
        return "gateway"
    if "accesspoint" in keys:
        return "ap"
    if "switching" in keys:
        return "switch"
    return "other"


def norm_device(d: dict, detail: dict | None = None) -> dict[str, Any]:
    full = {**d, **(detail or {})}
    iface = full.get("interfaces") if isinstance(full.get("interfaces"), dict) else {}
    ports = []
    for p in iface.get("ports") or []:
        poe = p.get("poe") or {}
        ports.append({"idx": p.get("idx"), "state": p.get("state"), "connector": p.get("connector"),
                      "speed_mbps": p.get("speedMbps"), "max_speed_mbps": p.get("maxSpeedMbps"),
                      "poe": bool(poe) and poe.get("enabled", True), "poe_state": poe.get("state")})
    radios = [{"standard": r.get("wlanStandard"), "frequency_ghz": r.get("frequencyGHz"), "channel": r.get("channel"),
               "channel_width_mhz": r.get("channelWidthMHz")} for r in iface.get("radios") or []]
    up = full.get("uplink") or {}
    state = str(full.get("state") or "UNKNOWN").upper()
    return {
        "id": full.get("id"), "name": full.get("name") or full.get("macAddress") or "?", "model": full.get("model"),
        "mac": full.get("macAddress"), "ip": full.get("ipAddress"), "state": state, "online": state == "ONLINE",
        "type": device_type(full), "firmware": full.get("firmwareVersion"),
        "firmware_updatable": bool(full.get("firmwareUpdatable")),
        "uplink_device_id": up.get("deviceId") if isinstance(up, dict) else None,
        "uplink_port": up.get("portIdx") if isinstance(up, dict) else None,
        "ports": ports, "ports_up": sum(1 for p in ports if str(p["state"]).upper() == "UP"), "ports_total": len(ports),
        "radios": radios, "adopted_at": full.get("adoptedAt"), "clients": 0,
    }


def norm_client(c: dict, names: dict[str, str] | None = None) -> dict[str, Any]:
    access = c.get("access") or {}
    up = c.get("uplinkDeviceId")
    return {"id": c.get("id"), "name": c.get("name") or c.get("hostname") or c.get("macAddress"),
            "mac": c.get("macAddress"), "ip": c.get("ipAddress"), "type": str(c.get("type") or "").upper(),
            "connected_at": c.get("connectedAt"), "uplink_device_id": up,
            "uplink_name": (names or {}).get(up) if up else None,
            "access": str(access.get("type") or "DEFAULT").upper() if isinstance(access, dict) else str(access),
            "authorized": access.get("authorized") if isinstance(access, dict) else None}


def norm_network(n: dict) -> dict[str, Any]:
    return {"id": n.get("id"), "name": n.get("name"), "vlan": n.get("vlanId"), "enabled": n.get("enabled", True),
            "management": n.get("management"), "default": bool(n.get("default")),
            "origin": (n.get("metadata") or {}).get("origin")}


def _network_ref(w: dict) -> tuple[str | None, str | None]:
    net = w.get("network") or {}
    if not isinstance(net, dict):
        return None, None
    return net.get("type"), net.get("networkId") or net.get("id")


def norm_wifi(w: dict, networks: dict[str, dict] | None = None) -> dict[str, Any]:
    ntype, nid = _network_ref(w)
    net = (networks or {}).get(nid or "") or {}
    if ntype == "NATIVE" and not net:
        net = next((n for n in (networks or {}).values() if n.get("default")), {})
    sec = w.get("securityConfiguration") or {}
    filt = w.get("clientFilteringPolicy") or {}
    blackout = (w.get("blackoutScheduleConfiguration") or {}).get("days") or []
    devf = w.get("broadcastingDeviceFilter") or {}
    name = w.get("name") or ""
    return {
        "id": w.get("id"), "name": name, "enabled": bool(w.get("enabled", True)), "type": w.get("type"),
        "security": sec.get("type") if isinstance(sec, dict) else None, "network_id": net.get("id") or nid,
        "network_name": net.get("name"), "vlan": net.get("vlan"), "hidden": bool(w.get("hideName")),
        "client_isolation": bool(w.get("clientIsolationEnabled")),
        "mac_filter": {"action": filt.get("action"), "macs": [norm_mac(m) for m in filt.get("macAddressFilter") or []]}
        if filt else None,
        "blackout_days": len(blackout), "device_filter": devf.get("type") if isinstance(devf, dict) else None,
        "guest": bool(GUEST_RE.search(name) or GUEST_RE.search(str(net.get("name") or ""))),
    }


def matches_client(c: dict, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    qm = norm_mac(q)
    if len(qm) >= 6 and qm in norm_mac(c.get("mac")):
        return True
    return q in str(c.get("name") or "").lower() or q == str(c.get("ip") or "") or q in str(c.get("ip") or "")


# ------------------------------------------------------------------ WiFi troubleshooting
def diagnose_client(query: str, clients: list[dict], wifi: list[dict] | None, devices: list[dict]) -> dict[str, Any]:
    """Collect the facts Claude needs for "why can't this laptop get on WiFi"."""
    findings: list[dict[str, str]] = []
    matches = [c for c in clients if matches_client(c, query)]
    qmac = norm_mac(query) if len(norm_mac(query)) == 12 else None
    if not qmac and matches and matches[0].get("mac"):
        qmac = norm_mac(matches[0]["mac"])
    by_id = {d["id"]: d for d in devices}
    aps = [d for d in devices if d["type"] == "ap"]
    aps_off = [d["name"] for d in aps if not d["online"]]
    if aps_off:
        findings.append({"severity": "warning", "text": f"{len(aps_off)} of {len(aps)} access points are not online: "
                                                        + ", ".join(aps_off[:10])})
    if not aps:
        findings.append({"severity": "warning", "text": "this site has no access points"})
    for c in matches[:10]:
        ap = by_id.get(c.get("uplink_device_id") or "")
        where = f" via {ap['name']}" if ap else ""
        if c.get("type") == "WIRELESS":
            findings.append({"severity": "info", "text": f"{c['name']} is connected to WiFi now{where} since "
                                                         f"{c.get('connected_at') or '?'}"})
        else:
            findings.append({"severity": "info", "text": f"{c['name']} is connected ({c.get('type') or '?'}){where}"})
        if not c.get("ip"):
            findings.append({"severity": "warning", "text": f"{c['name']} has no IP address: check DHCP on its network"})
        if c.get("access") == "GUEST" and c.get("authorized") is False:
            findings.append({"severity": "warning", "text": f"{c['name']} is on the guest portal but not authorized"})
        if ap and not ap["online"]:
            findings.append({"severity": "warning", "text": f"its access point {ap['name']} is {ap['state']}"})
    if not matches:
        findings.append({"severity": "warning", "text": f"no connected client matches '{query}': the device is not "
                                                        "associated right now (wrong password, blocked, out of range, "
                                                        "or the SSID is not offered)"})
    ssids = []
    for w in wifi or []:
        notes = []
        if not w["enabled"]:
            notes.append("disabled")
        if w["hidden"]:
            notes.append("hidden SSID: the device must enter the name manually")
        sec = str(w.get("security") or "")
        if sec in ("WPA3_PERSONAL", "WPA3_ENTERPRISE"):
            notes.append("WPA3 only: older laptops and IoT devices cannot join")
        if "ENTERPRISE" in sec:
            notes.append("802.1X/RADIUS: the user needs valid credentials and the RADIUS server must answer")
        mf = w.get("mac_filter")
        if mf and qmac:
            listed = qmac in mf["macs"]
            if mf.get("action") == "ALLOW" and not listed:
                notes.append("MAC filter in allow mode and this device is NOT on the list: it is refused")
            elif mf.get("action") == "BLOCK" and listed:
                notes.append("this device is on the MAC block list: it is refused")
        elif mf and mf.get("action") == "ALLOW":
            notes.append(f"MAC allow list active ({len(mf['macs'])} devices)")
        if w.get("blackout_days"):
            notes.append(f"blackout schedule on {w['blackout_days']} day(s): the SSID is off part of the time")
        if w.get("device_filter"):
            notes.append("only broadcast by selected access points")
        ssids.append({**{k: w.get(k) for k in ("name", "enabled", "security", "vlan", "network_name", "guest")},
                      "notes": notes})
        for n in notes:
            if "refused" in n or n == "disabled":
                findings.append({"severity": "critical" if "refused" in n else "warning", "text": f"SSID {w['name']}: {n}"})
    if wifi is None:
        findings.append({"severity": "info", "text": "WiFi settings are not available through this UniFi version's API"})
    return {"query": query, "matches": matches[:10], "findings": findings, "ssids": ssids,
            "aps": [{"name": d["name"], "state": d["state"], "clients": d.get("clients", 0)} for d in aps]}


# ------------------------------------------------------------------ guest isolation
def _filters(side: dict) -> Any:
    return (side or {}).get("trafficFilter")


def guest_isolation(networks: list[dict], wifi: list[dict] | None, zones: list[dict] | None,
                    policies: list[dict] | None) -> dict[str, Any]:
    nets = [norm_network(n) for n in networks]
    zones = zones or []
    zone_of = {nid: z for z in zones for nid in z.get("networkIds") or []}
    guest_ids = {n["id"] for n in nets if GUEST_RE.search(str(n["name"] or ""))}
    for z in zones:
        if GUEST_RE.search(str(z.get("name") or "")):
            guest_ids |= set(z.get("networkIds") or [])
    for w in wifi or []:
        if w.get("guest") and w.get("network_id"):
            guest_ids.add(w["network_id"])
    internal = [n for n in nets if n["id"] not in guest_ids]
    out_nets = []
    overall = "isolated"
    for g in [n for n in nets if n["id"] in guest_ids]:
        gz = zone_of.get(g["id"])
        res: dict[str, Any] = {"network": g["name"], "vlan": g["vlan"], "zone": gz.get("name") if gz else None,
                               "to": []}
        for n in internal:
            iz = zone_of.get(n["id"])
            row: dict[str, Any] = {"network": n["name"], "vlan": n["vlan"], "zone": iz.get("name") if iz else None}
            if not gz or not iz:
                row.update(verdict="unknown", why="zone assignment not available through the API")
            elif gz.get("id") == iz.get("id"):
                row.update(verdict="open", why=f"both networks are in zone {gz.get('name')}: traffic is allowed")
            else:
                pol = sorted([p for p in policies or [] if p.get("enabled", True)
                              and (p.get("source") or {}).get("zoneId") == gz.get("id")
                              and (p.get("destination") or {}).get("zoneId") == iz.get("id")],
                             key=lambda p: p.get("index") if p.get("index") is not None else 10**6)
                exceptions = []
                verdict, why = "unknown", "no firewall policies returned for this zone pair"
                for p in pol:
                    action = str((p.get("action") or {}).get("type") or "").upper()
                    broad = not _filters(p.get("source")) and not _filters(p.get("destination"))
                    if broad:
                        verdict = "isolated" if action in ("BLOCK", "REJECT") else "open"
                        why = f"policy '{p.get('name')}' {action.lower()}s all traffic {gz.get('name')} → {iz.get('name')}"
                        break
                    if action == "ALLOW":
                        exceptions.append(p.get("name"))
                if verdict == "isolated" and exceptions:
                    verdict = "partial"
                    why += "; but these earlier policies allow some traffic: " + ", ".join(map(str, exceptions))
                row.update(verdict=verdict, why=why)
            res["to"].append(row)
            rank = {"isolated": 0, "unknown": 1, "partial": 2, "open": 3}
            if rank[row["verdict"]] > rank[overall]:
                overall = row["verdict"]
        ssids = [w for w in wifi or [] if w.get("network_id") == g["id"]]
        res["ssids"] = [{"name": w["name"], "client_isolation": w["client_isolation"]} for w in ssids]
        out_nets.append(res)
    if not out_nets:
        overall = "no_guest_network"
    return {"verdict": overall, "guest_networks": out_nets,
            "zones": [{"name": z.get("name"), "networks": [n["name"] for n in nets if n["id"] in (z.get("networkIds") or [])]}
                      for z in zones],
            "note": "verdict per guest network and LAN network from the zone-based firewall policies "
                    "(first matching policy wins). 'client_isolation' stops guests from reaching each other."}
