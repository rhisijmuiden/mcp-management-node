"""AI gateway: guardrails, approvals and the kill switch for AI (MCP) tool calls.

Every MCP tool has a risk level (read, operate, change, destructive). Policies map a risk level, or
one tool, to allow, approve or block, in three layers:

1. the defaults (one "global" policy);
2. per customer: replaces the defaults for calls on that customer's machines (may be looser or
   stricter), plus "outside the maintenance window" handling;
3. per token: can only make it stricter.

A call that touches more than one customer gets the strictest outcome of those customers. "approve"
stores the call as an approval request; an admin approves it in the app (mail and app notifications)
and the app then runs it with the token's own rights. "Pause all AI actions" refuses every call
that is not read-only, for the whole app or for one customer or token.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import AiApproval, AiPolicy, ApiToken, Environment
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services import events
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.gateway")

RISKS = ("read", "operate", "change", "destructive")
ACTIONS = ("allow", "approve", "block")
_ORDER = {"allow": 0, "approve": 1, "block": 2}
DEFAULT_RISKS = {"read": "allow", "operate": "allow", "change": "allow", "destructive": "approve"}
DEFAULT_OUTSIDE_WINDOW = "approve"
WINDOW_RISKS = ("change", "destructive")

# Tools that change something but are routine and easy to undo: they count as "operate"
# instead of the "change" their MCP annotation would give.
OPERATE_TOOLS = {
    "refresh_node_updates", "refresh_unifi", "create_snapshot", "guest_power", "run_backup", "run_backup_job",
    "send_test_mail", "send_node_test_notification", "create_pairing_code", "sync_templates",
    "run_scheduled_task", "run_deployment", "update_node_agent", "set_cpu_governor", "manage_service",
    "restart_unifi_device", "refresh_mcp_server",
}

FINAL = {"executed", "failed", "rejected", "expired"}


# ------------------------------------------------------------------ tool registry
@dataclass
class ToolSpec:
    name: str
    scope: str
    risk: str
    all_customers: bool
    description: str
    call: Callable[..., Awaitable[Any]] | None = None  # the audited wrapper
    source: str | None = None  # None = built in, "hub" = a tool of another MCP server (MCP hub)
    customer_id: str | None = None  # hub tools: the customer the MCP server belongs to
    upstream_id: str | None = None


REGISTRY: dict[str, ToolSpec] = {}


def risk_from_annotations(name: str, read_only: bool, destructive: bool) -> str:
    if read_only:
        return "read"
    if name in OPERATE_TOOLS:
        return "operate"
    return "destructive" if destructive else "change"


def register(spec: ToolSpec) -> None:
    REGISTRY[spec.name] = spec


def catalog() -> list[dict[str, Any]]:
    return [{"name": s.name, "risk": s.risk, "scope": s.scope, "description": s.description,
             "source": s.source or "stackpilot", "upstream_id": s.upstream_id, "customer_id": s.customer_id,
             "all_customers": s.all_customers}
            for s in sorted(REGISTRY.values(), key=lambda s: (RISKS.index(s.risk), s.name))]


HUB_SEP = "__"


def namespace_of(tool: str) -> str | None:
    """The MCP hub prefix of a tool name ("github" for "github__create_issue"), None for built-in tools."""
    if HUB_SEP not in tool:
        return None
    return tool.split(HUB_SEP, 1)[0] or None


def valid_rule_key(key: str) -> bool:
    """Policy rules name a tool, a hub tool (also while its server is offline) or a whole hub server
    ("github__*")."""
    if key in REGISTRY:
        return True
    ns = namespace_of(key)
    return bool(ns) and re.fullmatch(r"[a-z][a-z0-9_]{0,23}__(\*|[A-Za-z0-9_-]{1,64})", key) is not None


# Set while the app runs an approved call, so the policy check is skipped for exactly that call.
approved_call: contextvars.ContextVar[str | None] = contextvars.ContextVar("mmn_approved_call", default=None)


# ------------------------------------------------------------------ settings
async def gateway_settings(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "ai_gateway")


# ------------------------------------------------------------------ maintenance windows
_DAYS = {
    "mon": 0, "monday": 0, "ma": 0, "maandag": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "di": 1, "dinsdag": 1,
    "wed": 2, "wednesday": 2, "wo": 2, "woensdag": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "do": 3, "donderdag": 3,
    "fri": 4, "friday": 4, "vr": 4, "vrijdag": 4,
    "sat": 5, "saturday": 5, "za": 5, "zaterdag": 5,
    "sun": 6, "sunday": 6, "zo": 6, "zondag": 6,
}
_GROUPS = {
    "daily": set(range(7)), "everyday": set(range(7)), "dagelijks": set(range(7)), "elkedag": set(range(7)),
    "iederedag": set(range(7)), "weekdays": set(range(5)), "werkdagen": set(range(5)),
    "weekend": {5, 6}, "weekends": {5, 6},
}
_TIME = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*(?:-|to|tot|until|t/m)\s*(\d{1,2})(?:[:.](\d{2}))?")


@dataclass
class Window:
    days: set[int]
    start: int  # minutes after midnight
    end: int


def parse_window(text: str | None) -> list[Window] | None:
    """Parse "Tue 22:00–02:00", "Mon-Fri 18:00-07:00", "Sat, Sun 00:00-06:00", "daily 02:00-04:00",
    "za 22:00-02:00" (Dutch day names too), several separated by ";". None when it can't be read."""
    if not text or not text.strip():
        return None
    out: list[Window] = []
    norm = text.lower().replace("–", "-").replace("—", "-").replace("−", "-")
    for seg in re.split(r";|\n|\band\b|\ben\b|\|", norm):
        seg = seg.strip()
        if not seg:
            continue
        m = _TIME.search(seg)
        if not m:
            return None
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
        if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
            return None
        days = _parse_days(seg[:m.start()] + " " + seg[m.end():])
        if days is None:
            return None
        out.append(Window(days=days, start=(h1 % 24) * 60 + m1, end=(h2 % 24) * 60 + m2))
    return out or None


def _parse_days(text: str) -> set[int] | None:
    text = re.sub(r"\b(every|each|elke|iedere|op|on|from|van|uur|h)\b", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    tokens = [t for t in re.split(r"[\s,/&+]+", text) if t]
    if not tokens:
        return set(range(7))
    days: set[int] = set()
    joined = "".join(tokens)
    if joined in _GROUPS:
        return set(_GROUPS[joined])
    for tok in tokens:
        tok = tok.strip(".")
        if tok in _GROUPS:
            days |= _GROUPS[tok]
        elif "-" in tok:
            a, _, b = tok.partition("-")
            if a not in _DAYS or b not in _DAYS:
                return None
            i, j = _DAYS[a], _DAYS[b]
            while True:
                days.add(i)
                if i == j:
                    break
                i = (i + 1) % 7
        elif tok in _DAYS:
            days.add(_DAYS[tok])
        elif tok in {"day", "dag", "days", "dagen"}:
            continue
        else:
            return None
    return days


def in_window(windows: list[Window], now: datetime) -> bool:
    day, t = now.weekday(), now.hour * 60 + now.minute
    for w in windows:
        if w.start == w.end:  # whole day
            if day in w.days:
                return True
        elif w.start < w.end:
            if day in w.days and w.start <= t < w.end:
                return True
        elif (day in w.days and t >= w.start) or ((day - 1) % 7 in w.days and t < w.end):
            return True
    return False


async def local_now(db: AsyncSession) -> datetime:
    tz = (await get_setting(db, "schedules")).get("timezone") or "Europe/Amsterdam"
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        return datetime.now(UTC)


# ------------------------------------------------------------------ which customers a call touches
async def customers_of(db: AsyncSession, args: dict[str, Any], token_customers: frozenset[str] | None,
                       extra: list[str] | None = None) -> list[Environment]:
    from mmn.services import nodes

    env_ids: set[str] = set(extra or [])

    async def by_node(ref: Any) -> None:
        if not ref:
            return
        try:
            env_ids.add((await nodes.get_node(db, str(ref))).environment_id)
        except Exception:  # noqa: BLE001 - unknown target: the tool itself reports it
            pass

    if args.get("customer"):
        try:
            env_ids.add((await nodes.get_environment(db, str(args["customer"]))).id)
        except Exception:  # noqa: BLE001
            pass
    for key in ("node", "server", "source", "target", "target_node"):
        if isinstance(args.get(key), str):
            await by_node(args[key])
    if isinstance(args.get("servers"), list):
        for s in args["servers"][:50]:
            await by_node(s)
    if args.get("guest") and not args.get("node"):
        try:
            from mmn.services import guests

            n, _ = await guests.find_guest(db, args["guest"], None)
            env_ids.add(n.environment_id)
        except Exception:  # noqa: BLE001
            pass
    if args.get("connection"):
        try:
            from mmn.services.unifi import service as uf

            env_ids.add((await uf.get_connection(db, str(args["connection"]), None)).environment_id)
        except Exception:  # noqa: BLE001
            pass
    if args.get("deployment"):
        try:
            from mmn.models import Deployment, Node

            d = await db.get(Deployment, str(args["deployment"]))
            if d is not None and (n := await db.get(Node, d.node_id)) is not None:
                env_ids.add(n.environment_id)
        except Exception:  # noqa: BLE001
            pass
    if not env_ids and token_customers is not None and len(token_customers) == 1:
        env_ids = set(token_customers)
    env_ids.discard(None)  # type: ignore[arg-type]
    if not env_ids:
        return []
    return list((await db.execute(select(Environment).where(Environment.id.in_(sorted(env_ids)))
                                  .order_by(Environment.name))).scalars())


# ------------------------------------------------------------------ policies
async def get_policy(db: AsyncSession, scope: str, scope_id: str | None = None) -> AiPolicy | None:
    q = select(AiPolicy).where(AiPolicy.scope == scope)
    q = q.where(AiPolicy.scope_id.is_(None)) if scope_id is None else q.where(AiPolicy.scope_id == scope_id)
    return (await db.execute(q)).scalars().first()


def policy_out(p: AiPolicy | None, scope: str, scope_id: str | None = None, **extra: Any) -> dict[str, Any]:
    if p is None:
        base = {"scope": scope, "scope_id": scope_id, "risks": {}, "tools": {}, "outside_window": "inherit",
                "paused": False, "updated_at": None, "updated_by": None, "exists": False}
        if scope == "global":
            base.update(risks=dict(DEFAULT_RISKS), outside_window=DEFAULT_OUTSIDE_WINDOW)
        return {**base, **extra}
    risks = dict(p.risks or {})
    if scope == "global":
        risks = {**DEFAULT_RISKS, **{k: v for k, v in risks.items() if v in ACTIONS}}
    return {"scope": p.scope, "scope_id": p.scope_id, "risks": risks, "tools": dict(p.tools or {}),
            "outside_window": p.outside_window if not (scope == "global" and p.outside_window == "inherit")
            else DEFAULT_OUTSIDE_WINDOW,
            "paused": p.paused, "updated_at": p.updated_at, "updated_by": p.updated_by, "exists": True, **extra}


def clean_policy(scope: str, risks: dict[str, str], tools: dict[str, str], outside_window: str) -> tuple[dict, dict, str]:
    allowed = set(ACTIONS) if scope == "global" else {*ACTIONS, "inherit"}
    r = {k: v for k, v in (risks or {}).items() if k in RISKS and v in allowed and v != "inherit"}
    if scope == "global":
        r = {**DEFAULT_RISKS, **r}
    t = {k: v for k, v in (tools or {}).items() if valid_rule_key(k) and v in ACTIONS}
    ow_allowed = {"ignore", "approve", "block"} | (set() if scope == "global" else {"inherit"})
    ow = outside_window if outside_window in ow_allowed else ("approve" if scope == "global" else "inherit")
    if scope == "token":
        ow = "inherit"
    return r, t, ow


async def save_policy(db: AsyncSession, scope: str, scope_id: str | None, *, risks: dict[str, str],
                      tools: dict[str, str], outside_window: str, paused: bool, by: str | None) -> AiPolicy:
    r, t, ow = clean_policy(scope, risks, tools, outside_window)
    p = await get_policy(db, scope, scope_id)
    if p is None:
        p = AiPolicy(scope=scope, scope_id=scope_id)
        db.add(p)
    p.risks, p.tools, p.outside_window, p.paused, p.updated_by = r, t, ow, bool(paused), by
    p.updated_at = datetime.now(UTC)
    await db.flush()
    return p


def _strictest(*actions: str | None) -> str:
    vals = [a for a in actions if a in _ORDER]
    return max(vals, key=lambda a: _ORDER[a]) if vals else "allow"


def _layer(p: AiPolicy | None, risk: str, tool: str) -> tuple[str | None, str | None]:
    """(action, "tool"|"risk") of one policy layer, or (None, None) to inherit."""
    if p is None:
        return None, None
    rules = p.tools or {}
    v = rules.get(tool)
    if v in ACTIONS:
        return v, "tool"
    ns = namespace_of(tool)
    if ns and rules.get(f"{ns}{HUB_SEP}*") in ACTIONS:
        return rules[f"{ns}{HUB_SEP}*"], "tool"
    v = (p.risks or {}).get(risk)
    if v in ACTIONS:
        return v, "risk"
    return None, None


@dataclass
class Decision:
    action: str  # allow | approve | block | paused
    risk: str
    reasons: list[dict[str, Any]] = field(default_factory=list)
    customers: list[tuple[str, str]] = field(default_factory=list)  # (id, name)


def reason_text(r: dict[str, Any]) -> str:
    c = r.get("code")
    p = r.get("params", {})
    if c == "paused":
        return "All AI actions are paused by an admin" + (f": {p['note']}" if p.get("note") else "")
    if c == "paused_customer":
        return f"AI actions are paused for customer {p.get('customer')}"
    if c == "paused_token":
        return "AI actions are paused for this token"
    if c == "risk":
        where = f"customer {p['customer']}" if p.get("customer") else "the default policy"
        return f"{p.get('risk')} tools: {p.get('action')} ({where})"
    if c == "tool":
        where = (f"customer {p['customer']}" if p.get("customer") else "this token" if p.get("layer") == "token"
                 else "the default policy")
        return f"rule for {p.get('tool')}: {p.get('action')} ({where})"
    if c == "outside_window":
        return f"outside the maintenance window of {p.get('customer')} ({p.get('window')})"
    return c or ""


async def decide(db: AsyncSession, *, tool: str, risk: str, args: dict[str, Any], token_id: str | None,
                 token_customers: frozenset[str] | None) -> Decision:
    cfg = await gateway_settings(db)
    spec = REGISTRY.get(tool)
    if spec is not None and spec.source == "hub":
        # A hub tool works on its MCP server, not on Stackpilot's machines: its arguments say nothing
        # about customers; the server's own customer does.
        envs = await customers_of(db, {}, token_customers, [spec.customer_id] if spec.customer_id else None)
    else:
        envs = await customers_of(db, args, token_customers)
    d = Decision(action="allow", risk=risk, customers=[(e.id, e.name) for e in envs])
    gpol = await get_policy(db, "global")
    tpol = await get_policy(db, "token", token_id) if token_id else None
    cpols = {}
    if envs:
        rows = (await db.execute(select(AiPolicy).where(AiPolicy.scope == "customer",
                                                        AiPolicy.scope_id.in_([e.id for e in envs])))).scalars()
        cpols = {p.scope_id: p for p in rows}

    if risk != "read" or cfg.get("pause_reads"):
        if cfg.get("paused"):
            d.action = "paused"
            d.reasons.append({"code": "paused", "params": {"note": cfg.get("pause_note") or ""}})
            return d
        for e in envs:
            if cpols.get(e.id) is not None and cpols[e.id].paused:
                d.action = "paused"
                d.reasons.append({"code": "paused_customer", "params": {"customer": e.name}})
                return d
        if tpol is not None and tpol.paused:
            d.action = "paused"
            d.reasons.append({"code": "paused_token", "params": {}})
            return d

    g_action, g_kind = _layer(gpol, risk, tool)
    if g_action is None:
        g_action, g_kind = DEFAULT_RISKS.get(risk, "approve"), "risk"
    g_ow = gpol.outside_window if gpol is not None and gpol.outside_window != "inherit" else DEFAULT_OUTSIDE_WINDOW

    results: list[tuple[str, dict[str, Any]]] = []
    now = None
    for e in envs:
        cp = cpols.get(e.id)
        a, kind = _layer(cp, risk, tool)
        if a is None:
            a, kind = g_action, g_kind
            reason = {"code": kind, "params": {"risk": risk, "tool": tool, "action": a}}
        else:
            reason = {"code": kind, "params": {"risk": risk, "tool": tool, "action": a, "customer": e.name,
                                               "layer": "customer"}}
        results.append((a, reason))
        ow = cp.outside_window if cp is not None and cp.outside_window != "inherit" else g_ow
        if risk in WINDOW_RISKS and ow in ("approve", "block") and kind != "tool":
            windows = parse_window(e.maintenance_window)
            if windows:
                now = now or await local_now(db)
                if not in_window(windows, now):
                    results.append((ow, {"code": "outside_window",
                                         "params": {"customer": e.name, "window": e.maintenance_window,
                                                    "action": ow}}))
    if not envs:
        results.append((g_action, {"code": g_kind, "params": {"risk": risk, "tool": tool, "action": g_action}}))

    t_action, t_kind = _layer(tpol, risk, tool)
    if t_action is not None:
        results.append((t_action, {"code": t_kind, "params": {"risk": risk, "tool": tool, "action": t_action,
                                                              "layer": "token"}}))

    d.action = _strictest(*(a for a, _ in results))
    if d.action != "allow":
        seen = set()
        for a, r in results:
            key = json.dumps(r, sort_keys=True)
            if a == d.action and key not in seen:
                seen.add(key)
                d.reasons.append(r)
    return d


async def effective_for(db: AsyncSession, customer_id: str | None, token_id: str | None) -> dict[str, Any]:
    """The outcome per risk level (no specific tool) for a customer and/or token, for the interface
    and the get_guardrails tool."""
    env = await db.get(Environment, customer_id) if customer_id else None
    out: dict[str, Any] = {}
    for risk in RISKS:
        args = {"customer": customer_id} if customer_id else {}
        d = await decide(db, tool="__risk__", risk=risk, args=args, token_id=token_id, token_customers=None)
        out[risk] = {"action": d.action, "reasons": [reason_text(r) for r in d.reasons]}
    cfg = await gateway_settings(db)
    windows = parse_window(env.maintenance_window) if env else None
    return {"risks": out, "paused": bool(cfg.get("paused")),
            "maintenance_window": env.maintenance_window if env else None,
            "maintenance_window_understood": bool(windows) if env and env.maintenance_window else None,
            "in_maintenance_window": in_window(windows, await local_now(db)) if windows else None}


# ------------------------------------------------------------------ approvals
def fingerprint(token_id: str | None, tool: str, args: dict[str, Any]) -> str:
    raw = json.dumps({"t": token_id, "n": tool, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def approval_out(a: AiApproval, *, full: bool = True) -> dict[str, Any]:
    out = {
        "id": a.id, "created_at": a.created_at, "expires_at": a.expires_at, "status": a.status, "tool": a.tool,
        "risk": a.risk, "target": a.target, "customer_ids": a.customer_ids or [],
        "customer_names": a.customer_names or [], "reasons": a.reasons or [],
        "reason_texts": [reason_text(r) for r in (a.reasons or [])], "ai_reason": a.ai_reason,
        "token_id": a.token_id, "token_name": a.token_name, "username": a.username, "client": a.client or {},
        "decided_at": a.decided_at, "decided_by": a.decided_by, "decision_note": a.decision_note,
        "executed_at": a.executed_at, "error": a.error,
    }
    if full:
        out.update(args=a.args_view or {}, result=a.result)
        spec = REGISTRY.get(a.tool)
        out["description"] = spec.description if spec else None
    return out


async def expire_old(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    rows = (await db.execute(select(AiApproval).where(AiApproval.status == "pending",
                                                      AiApproval.expires_at < now))).scalars().all()
    for a in rows:
        a.status = "expired"
        a.args_enc = None
    if rows:
        await db.flush()
        events.publish("approval.changed", {"count": len(rows)})
    return len(rows)


async def create_approval(db: AsyncSession, *, tool: str, risk: str, args: dict[str, Any], args_view: dict[str, Any],
                          target: str | None, decision: Decision, token_id: str | None, token_name: str | None,
                          user_id: str | None, username: str | None, client: dict[str, Any]) -> tuple[AiApproval, bool]:
    """Store a call that waits for approval. Returns (approval, created); the same call (same token,
    tool and arguments) that is still pending is returned instead of a duplicate."""
    await expire_old(db)
    fp = fingerprint(token_id, tool, args)
    existing = (await db.execute(select(AiApproval).where(AiApproval.fingerprint == fp,
                                                          AiApproval.status == "pending"))).scalars().first()
    if existing is not None:
        return existing, False
    cfg = await gateway_settings(db)
    hours = max(1, min(int(cfg.get("approval_expiry_hours") or 24), 24 * 14))
    reason = args.get("reason") if isinstance(args.get("reason"), str) else None
    a = AiApproval(
        expires_at=datetime.now(UTC) + timedelta(hours=hours), status="pending", tool=tool, risk=risk,
        args_enc=encrypt_secret(json.dumps(args, default=str)), args_view=json.loads(json.dumps(args_view, default=str)),
        fingerprint=fp, target=target, customer_ids=[c[0] for c in decision.customers],
        customer_names=[c[1] for c in decision.customers], reasons=decision.reasons, ai_reason=reason,
        token_id=token_id, token_name=token_name, user_id=user_id, username=username,
        client={k: v for k, v in (client or {}).items() if k in ("name", "version", "user_agent")},
    )
    db.add(a)
    await db.flush()
    return a, True


async def wait_for(approval_id: str, seconds: float) -> AiApproval | None:
    """Wait until the approval is final (or the time is up). Returns the row in its last state."""
    deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
    while True:
        async with sessionmaker()() as db:
            a = await db.get(AiApproval, approval_id)
            if a is None or a.status in FINAL or asyncio.get_running_loop().time() >= deadline:
                return a
        await asyncio.sleep(0.5)


class ApprovalError(Exception):
    pass


_running: set[asyncio.Task] = set()


async def decide_approval(db: AsyncSession, a: AiApproval, *, approve: bool, by: str, note: str | None) -> AiApproval:
    await expire_old(db)
    await db.refresh(a)
    if a.status != "pending":
        raise ApprovalError(f"already {a.status}")
    if approve:
        cfg = await gateway_settings(db)
        if cfg.get("paused"):
            raise ApprovalError("AI actions are paused; resume them first")
    a.status = "approved" if approve else "rejected"
    a.decided_at, a.decided_by, a.decision_note = datetime.now(UTC), by, (note or "").strip()[:2000] or None
    if not approve:
        a.args_enc = None
    await db.flush()
    events.publish("approval.changed", {"id": a.id, "status": a.status})
    return a


def start_execution(approval_id: str) -> None:
    task = asyncio.create_task(execute(approval_id))
    _running.add(task)
    task.add_done_callback(_running.discard)


def _store_result(result: Any) -> Any:
    data = json.loads(json.dumps(result, default=str))
    raw = json.dumps(data)
    if len(raw) > 200_000:
        return {"truncated": True, "preview": raw[:200_000]}
    return data


async def execute(approval_id: str) -> None:
    """Run an approved call with the token's own rights (scopes and customers) and store the result."""
    from mmn.api.tokens import effective_scopes
    from mmn.mcp_server import Principal, _principal
    from mmn.services import tenancy

    async with sessionmaker()() as db:
        a = await db.get(AiApproval, approval_id)
        if a is None or a.status != "approved":
            return
        a.status = "running"
        await db.commit()
        spec = REGISTRY.get(a.tool)
        tok = await db.get(ApiToken, a.token_id) if a.token_id else None
        now = datetime.now(UTC)
        problem = None
        if spec is None or spec.call is None:
            problem = "this tool no longer exists"
        elif tok is None or tok.revoked or (tok.expires_at and tok.expires_at < now) or not tok.user.is_active:
            problem = "the token that asked for this was revoked or expired"
        try:
            args = json.loads(decrypt_secret(a.args_enc) or "null")
        except ValueError:
            args = None
        if args is None and problem is None:
            problem = "the stored arguments could not be read"
        if problem:
            a.status, a.error, a.executed_at, a.args_enc = "failed", problem, now, None
            await db.commit()
            events.publish("approval.changed", {"id": a.id, "status": a.status})
            return
        principal = Principal(user_id=tok.user_id, username=tok.user.username, token_id=tok.id, token_name=tok.name,
                              token_kind=tok.kind, scopes=effective_scopes(tok), ip=None,
                              client={**(tok.last_client or {}), "approved_by": a.decided_by},
                              customers=frozenset(tok.customer_ids) if tok.customer_ids is not None else None)

    ptok = _principal.set(principal)
    ttok = tenancy.set_allowed(principal.customers)
    atok = approved_call.set(approval_id)
    status, result, error = "executed", None, None
    try:
        result = await spec.call(**args)
    except Exception as e:  # noqa: BLE001 - stored and shown to the admin and the AI
        status, error = "failed", (str(e) or type(e).__name__)[:4000]
    finally:
        approved_call.reset(atok)
        tenancy.reset(ttok)
        _principal.reset(ptok)

    async with sessionmaker()() as db:
        a = await db.get(AiApproval, approval_id)
        a.status, a.executed_at, a.error, a.args_enc = status, datetime.now(UTC), error, None
        a.result = _store_result(result) if status == "executed" else None
        await db.commit()
    events.publish("approval.changed", {"id": approval_id, "status": status})


def pending_response(a: AiApproval, url: str | None) -> dict[str, Any]:
    reasons = "; ".join(reason_text(r) for r in (a.reasons or []))
    return {
        "status": "pending_approval", "approval_id": a.id, "tool": a.tool, "expires_at": a.expires_at.isoformat(),
        "reasons": [reason_text(r) for r in (a.reasons or [])], "approve_url": url,
        "message": (f"This call has NOT run yet: it needs approval from an admin in Stackpilot ({reasons}). "
                    "Tell the user it is waiting for approval (they get a notification). Once approved, the app "
                    "runs it itself; check the outcome with get_approval(approval_id). Do not retry the call."),
    }


# ------------------------------------------------------------------ AI sessions (for the command center)
def sessions_from_audit(rows: list[Any], gap_minutes: int = 30) -> list[dict[str, Any]]:
    """Group MCP audit entries (newest first) into sessions: same token, no gap above gap_minutes."""
    sessions: list[dict[str, Any]] = []
    open_by_token: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: r.at):
        d = r.detail or {}
        key = d.get("token_id") or r.username or "?"
        s = open_by_token.get(key)
        if s is None or (r.at - s["_last"]).total_seconds() > gap_minutes * 60:
            s = {"token_id": d.get("token_id"), "token_name": d.get("token"), "username": r.username,
                 "client": (d.get("client") or {}).get("name") or None, "started_at": r.at, "_last": r.at,
                 "calls": 0, "failed": 0, "pending": 0, "tools": {}, "targets": set()}
            sessions.append(s)
            open_by_token[key] = s
        s["_last"] = r.at
        s["calls"] += 1
        s["failed"] += 0 if r.success else 1
        if (d.get("result") or {}).get("status") == "pending_approval":
            s["pending"] += 1
        tool = d.get("tool") or r.action.removeprefix("mcp.")
        s["tools"][tool] = s["tools"].get(tool, 0) + 1
        if r.target:
            s["targets"].add(r.target)
    out = []
    for s in reversed(sessions):
        last = s.pop("_last")
        out.append({**s, "last_at": last, "targets": sorted(s["targets"])[:10],
                    "tools": sorted(s["tools"].items(), key=lambda kv: -kv[1])[:8]})
    return out
