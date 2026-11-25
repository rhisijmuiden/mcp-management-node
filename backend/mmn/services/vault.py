"""The password vault per customer (phase 18).

Encryption at rest
------------------
Every vault item has its own random 256-bit data key. The secret part of the item (username,
password, notes and extra fields, as JSON) is encrypted with AES-256-GCM under that data key, with
the item id as associated data, so a ciphertext cannot be moved to another item. The data key itself
is wrapped (AES-256-GCM again) with the vault key, which lives in ``<data>/vault.key`` (mode 0600),
apart from the database and from the app's general ``secret.key``. A database dump alone therefore
reveals nothing. The vault key can be rotated: a new key is added to the key ring, every data key is
wrapped again and the old key is removed. Attachments of documents are encrypted the same way.

Who may read a secret
---------------------
* People: admins, in a session where they entered their password again ("unlock", 15 minutes by
  default). Every reveal and copy goes into the access log.
* The AI (MCP): only with the "vault" scope, only items with ai_access "approval", and only after an
  admin approved that one use through the AI gateway (phase 8). An approval makes a short-lived grant
  for that token and item (one read by default). Every request, approval, read and use is logged.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import string
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.models import (
    Document,
    DocumentAttachment,
    Environment,
    Node,
    VaultAccess,
    VaultGrant,
    VaultItem,
    VaultItemHistory,
    new_id,
)
from mmn.services.settings_store import get_setting

KINDS = ("login", "server", "network", "database", "api_key", "wifi", "certificate", "license", "note", "other")
AI_ACCESS = ("approval", "never")
SECRET_FIELDS = ("username", "password", "notes", "fields")
MAX_FIELDS = 30
BLOB_MAGIC = b"SPV1"


class VaultError(Exception):
    pass


# ------------------------------------------------------------------ the key ring
def key_file() -> Path:
    return get_settings().data_dir / "vault.key"


def _write_ring(ring: dict[str, Any]) -> None:
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(ring, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    reset_cache()


def _new_key_id(ring: dict[str, Any]) -> str:
    while True:
        kid = "k" + secrets.token_hex(4)
        if kid not in ring.get("keys", {}):
            return kid


@lru_cache
def _ring() -> dict[str, Any]:
    path = key_file()
    if path.exists():
        ring = json.loads(path.read_text())
        if not ring.get("keys") or ring.get("active") not in ring["keys"]:
            raise VaultError("the vault key file is damaged")
        return ring
    ring: dict[str, Any] = {"active": "", "keys": {}, "created_at": datetime.now(UTC).isoformat()}
    kid = _new_key_id(ring)
    ring.update(active=kid, keys={kid: base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()})
    _write_ring(ring)
    return ring


def reset_cache() -> None:
    _ring.cache_clear()


def ensure_key() -> None:
    _ring()


def _master(kid: str) -> AESGCM:
    keys = _ring()["keys"]
    if kid not in keys:
        raise VaultError(f"vault key {kid} is missing: restore vault.key from a backup")
    return AESGCM(base64.b64decode(keys[kid]))


def key_info() -> dict[str, Any]:
    ring = _ring()
    return {"active": ring["active"], "keys": len(ring["keys"]), "rotated_at": ring.get("rotated_at"),
            "created_at": ring.get("created_at"), "algorithm": "AES-256-GCM, per-item data keys"}


def _aad(kind: str, ident: str) -> bytes:
    return f"stackpilot:{kind}:{ident}".encode()


def _wrap(dek: bytes, aad: bytes, kid: str | None = None) -> tuple[str, bytes]:
    kid = kid or _ring()["active"]
    nonce = os.urandom(12)
    return kid, nonce + _master(kid).encrypt(nonce, dek, aad)


def _unwrap(kid: str, wrapped: bytes, aad: bytes) -> bytes:
    try:
        return _master(kid).decrypt(wrapped[:12], wrapped[12:], aad)
    except InvalidTag as e:
        raise VaultError("this secret could not be decrypted (wrong vault key?)") from e


def seal(kind: str, ident: str, plaintext: bytes) -> tuple[str, bytes, bytes]:
    """Encrypt ``plaintext`` under a fresh data key. Returns (key id, wrapped data key, ciphertext)."""
    aad = _aad(kind, ident)
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)
    kid, wrapped = _wrap(dek, aad)
    return kid, wrapped, ct


def unseal(kind: str, ident: str, kid: str, wrapped: bytes, ct: bytes) -> bytes:
    aad = _aad(kind, ident)
    dek = _unwrap(kid, wrapped, aad)
    try:
        return AESGCM(dek).decrypt(ct[:12], ct[12:], aad)
    except InvalidTag as e:
        raise VaultError("this secret could not be decrypted (damaged data)") from e


def seal_blob(kind: str, ident: str, data: bytes) -> bytes:
    """One self-contained encrypted blob (for files): magic, key id, wrapped key, ciphertext."""
    kid, wrapped, ct = seal(kind, ident, data)
    k = kid.encode()
    return BLOB_MAGIC + bytes([len(k)]) + k + len(wrapped).to_bytes(2, "big") + wrapped + ct


def _split_blob(blob: bytes) -> tuple[str, bytes, bytes]:
    if not blob.startswith(BLOB_MAGIC):
        raise VaultError("not an encrypted file")
    i = len(BLOB_MAGIC)
    kl = blob[i]
    kid = blob[i + 1:i + 1 + kl].decode()
    i += 1 + kl
    wl = int.from_bytes(blob[i:i + 2], "big")
    wrapped = blob[i + 2:i + 2 + wl]
    return kid, wrapped, blob[i + 2 + wl:]


def open_blob(kind: str, ident: str, blob: bytes) -> bytes:
    kid, wrapped, ct = _split_blob(blob)
    return unseal(kind, ident, kid, wrapped, ct)


def _rewrap_blob(kind: str, ident: str, blob: bytes, new_kid: str) -> bytes:
    kid, wrapped, ct = _split_blob(blob)
    aad = _aad(kind, ident)
    dek = _unwrap(kid, wrapped, aad)
    _, nw = _wrap(dek, aad, new_kid)
    k = new_kid.encode()
    return BLOB_MAGIC + bytes([len(k)]) + k + len(nw).to_bytes(2, "big") + nw + ct


# ------------------------------------------------------------------ items
def _clean_payload(data: dict[str, Any]) -> dict[str, Any]:
    fields = []
    for f in (data.get("fields") or [])[:MAX_FIELDS]:
        name = str(f.get("name") or "").strip()[:100]
        if name:
            fields.append({"name": name, "value": str(f.get("value") or "")[:10000], "secret": bool(f.get("secret", True))})
    return {"username": str(data.get("username") or "")[:255], "password": str(data.get("password") or "")[:10000],
            "notes": str(data.get("notes") or "")[:20000], "fields": fields}


def read_secret(item: VaultItem) -> dict[str, Any]:
    raw = unseal("item", item.id, item.key_id, item.wrapped_key, item.ciphertext)
    return _clean_payload(json.loads(raw))


def _write_secret(item: VaultItem, payload: dict[str, Any]) -> None:
    item.key_id, item.wrapped_key, item.ciphertext = seal("item", item.id, json.dumps(payload).encode())


def rotation_due(item: VaultItem, now: datetime | None = None) -> bool:
    if not item.rotate_days:
        return False
    return (now or datetime.now(UTC)) - item.secret_changed_at > timedelta(days=item.rotate_days)


def item_out(item: VaultItem, payload: dict[str, Any] | None = None, *, names: dict[str, str] | None = None,
             reveal: bool = False) -> dict[str, Any]:
    """Item without secrets (username and non-secret fields are shown); with ``reveal`` the whole secret."""
    p = payload if payload is not None else read_secret(item)
    out = {
        "id": item.id, "customer_id": item.customer_id, "customer_name": item.customer.name if item.customer else None,
        "name": item.name, "kind": item.kind, "url": item.url, "tags": item.tags or [],
        "machines": [{"id": n, "name": (names or {}).get(n)} for n in item.node_ids or []],
        "ai_access": item.ai_access, "rotate_days": item.rotate_days, "rotation_due": rotation_due(item),
        "created_at": item.created_at, "updated_at": item.updated_at, "secret_changed_at": item.secret_changed_at,
        "last_used_at": item.last_used_at, "created_by": item.created_by, "updated_by": item.updated_by,
        "username": p["username"], "has_password": bool(p["password"]), "has_notes": bool(p["notes"]),
        "fields": [{"name": f["name"], "secret": f["secret"], "value": None if f["secret"] and not reveal else f["value"]}
                   for f in p["fields"]],
    }
    if reveal:
        out.update(password=p["password"], notes=p["notes"])
    return out


async def machine_names(db: AsyncSession) -> dict[str, str]:
    return {i: n for i, n in (await db.execute(select(Node.id, Node.name)))}


async def clean_nodes(db: AsyncSession, customer_id: str, node_ids: list[str] | None) -> list[str]:
    if not node_ids:
        return []
    rows = (await db.execute(select(Node.id).where(Node.id.in_(node_ids), Node.environment_id == customer_id))).scalars()
    found = set(rows)
    if found != set(node_ids):
        raise VaultError("a linked machine does not belong to this customer")
    return list(dict.fromkeys(node_ids))


def _tags(tags: list[str] | None) -> list[str]:
    return sorted({t.strip().lower()[:40] for t in (tags or []) if t and t.strip()})[:20]


async def create_item(db: AsyncSession, *, customer_id: str, name: str, kind: str, url: str | None,
                      payload: dict[str, Any], tags: list[str] | None = None, node_ids: list[str] | None = None,
                      ai_access: str = "approval", rotate_days: int | None = None, by: str | None = None) -> VaultItem:
    if kind not in KINDS:
        raise VaultError(f"unknown kind {kind}")
    if ai_access not in AI_ACCESS:
        raise VaultError("ai_access must be approval or never")
    if await db.get(Environment, customer_id) is None:
        raise VaultError("customer not found")
    item = VaultItem(id=new_id(), customer_id=customer_id, name=name.strip()[:200], kind=kind,
                     url=(url or "").strip()[:500] or None, tags=_tags(tags),
                     node_ids=await clean_nodes(db, customer_id, node_ids), ai_access=ai_access,
                     rotate_days=rotate_days or None, created_by=by, updated_by=by)
    _write_secret(item, _clean_payload(payload))
    db.add(item)
    await db.flush()
    await db.refresh(item, ["customer"])
    return item


async def update_item(db: AsyncSession, item: VaultItem, data: dict[str, Any], *, by: str | None) -> list[str]:
    """Apply changes; returns which fields changed. A changed password (or username) keeps the old secret in
    the history."""
    changed: list[str] = []
    for k in ("name", "kind", "url", "ai_access", "rotate_days"):
        if k in data:
            v = data[k]
            if k == "kind" and v not in KINDS:
                raise VaultError(f"unknown kind {v}")
            if k == "ai_access" and v not in AI_ACCESS:
                raise VaultError("ai_access must be approval or never")
            if k in ("name", "url"):
                v = (v or "").strip()[: 200 if k == "name" else 500] or None
                if k == "name" and not v:
                    raise VaultError("name is required")
            if getattr(item, k) != v:
                setattr(item, k, v)
                changed.append(k)
    if "tags" in data:
        item.tags = _tags(data["tags"])
        changed.append("tags")
    if "node_ids" in data:
        item.node_ids = await clean_nodes(db, item.customer_id, data["node_ids"])
        changed.append("machines")
    if any(k in data for k in SECRET_FIELDS):
        old = read_secret(item)
        new = _clean_payload({**old, **{k: data[k] for k in SECRET_FIELDS if k in data and data[k] is not None}})
        if new != old:
            secret_changed = new["password"] != old["password"] or new["username"] != old["username"]
            if secret_changed and (old["password"] or old["username"]):
                h = VaultItemHistory(id=new_id(), item_id=item.id, customer_id=item.customer_id, created_by=by,
                                     key_id="", wrapped_key=b"", ciphertext=b"")
                h.key_id, h.wrapped_key, h.ciphertext = seal("history", h.id, json.dumps(old).encode())
                db.add(h)
                await db.flush()
                await _trim_history(db, item.id)
                item.secret_changed_at = datetime.now(UTC)
            _write_secret(item, new)
            changed += [k for k in SECRET_FIELDS if new[k] != old[k]]
    if changed:
        item.updated_at, item.updated_by = datetime.now(UTC), by
    return changed


async def _trim_history(db: AsyncSession, item_id: str) -> None:
    keep = int((await get_setting(db, "vault")).get("history_keep") or 10)
    ids = (await db.execute(select(VaultItemHistory.id).where(VaultItemHistory.item_id == item_id)
                            .order_by(VaultItemHistory.created_at.desc()).offset(keep))).scalars().all()
    if ids:
        await db.execute(delete(VaultItemHistory).where(VaultItemHistory.id.in_(ids)))


def history_secret(h: VaultItemHistory) -> dict[str, Any]:
    return _clean_payload(json.loads(unseal("history", h.id, h.key_id, h.wrapped_key, h.ciphertext)))


async def find_item(db: AsyncSession, ref: str, customer_id: str | None = None) -> VaultItem:
    """By id, or by name (case-insensitive) within the customer; a name that several customers use needs the
    customer."""
    ref = (ref or "").strip()
    item = await db.get(VaultItem, ref) if len(ref) == 32 else None
    if item is not None and (customer_id is None or item.customer_id == customer_id):
        return item
    q = select(VaultItem).where(func.lower(VaultItem.name) == ref.lower())
    if customer_id:
        q = q.where(VaultItem.customer_id == customer_id)
    rows = list((await db.execute(q)).scalars())
    if not rows:
        raise VaultError(f"credential {ref!r} not found")
    if len(rows) > 1:
        raise VaultError(f"more than one credential is called {ref!r}: name the customer or use the id")
    return rows[0]


async def search_items(db: AsyncSession, *, customer_id: str | None = None, q: str | None = None,
                       kind: str | None = None, node_id: str | None = None, limit: int = 500) -> list[VaultItem]:
    stmt = select(VaultItem).order_by(func.lower(VaultItem.name)).limit(limit)
    if customer_id:
        stmt = stmt.where(VaultItem.customer_id == customer_id)
    if kind:
        stmt = stmt.where(VaultItem.kind == kind)
    rows = list((await db.execute(stmt)).scalars())
    if node_id:
        rows = [r for r in rows if node_id in (r.node_ids or [])]
    if q:
        needle = q.strip().lower()
        out = []
        for r in rows:
            hay = " ".join([r.name, r.url or "", " ".join(r.tags or []), r.kind]).lower()
            if needle in hay or needle in read_secret(r)["username"].lower():
                out.append(r)
        rows = out
    return rows


# ------------------------------------------------------------------ access log
async def log_access(db: AsyncSession, item: VaultItem | None, action: str, *, actor_kind: str = "user",
                     username: str | None = None, token_id: str | None = None, token_name: str | None = None,
                     approval_id: str | None = None, reason: str | None = None, ip: str | None = None,
                     success: bool = True, customer_id: str | None = None, item_name: str | None = None,
                     **detail: Any) -> None:
    db.add(VaultAccess(item_id=item.id if item else None, item_name=item.name if item else item_name,
                       customer_id=item.customer_id if item else customer_id, action=action, actor_kind=actor_kind,
                       username=username, token_id=token_id, token_name=token_name, approval_id=approval_id,
                       reason=(reason or "").strip()[:2000] or None, ip=ip, success=success,
                       detail={k: v for k, v in detail.items() if v is not None}))
    if item is not None and success and action in ("reveal", "copy", "ai_reveal", "ai_use"):
        item.last_used_at = datetime.now(UTC)
    await db.flush()


def access_out(a: VaultAccess) -> dict[str, Any]:
    return {"id": a.id, "at": a.at, "item_id": a.item_id, "item_name": a.item_name, "customer_id": a.customer_id,
            "action": a.action, "actor_kind": a.actor_kind, "username": a.username, "token_name": a.token_name,
            "approval_id": a.approval_id, "reason": a.reason, "ip": a.ip, "success": a.success, "detail": a.detail or {}}


async def access_log(db: AsyncSession, *, item_id: str | None = None, customer_id: str | None = None,
                     actor_kind: str | None = None, limit: int = 200) -> list[VaultAccess]:
    q = select(VaultAccess).order_by(VaultAccess.at.desc()).limit(max(1, min(limit, 2000)))
    if item_id:
        q = q.where(VaultAccess.item_id == item_id)
    if customer_id:
        q = q.where(VaultAccess.customer_id == customer_id)
    if actor_kind:
        q = q.where(VaultAccess.actor_kind == actor_kind)
    return list((await db.execute(q)).scalars())


async def announce_ai_use(db: AsyncSession, data: dict[str, Any]) -> None:
    """A notification in the app for the admins: the AI read or used a secret."""
    from mmn.services import events

    if (await get_setting(db, "vault")).get("notify_ai_use", True):
        events.publish("vault.ai_use", data)


# ------------------------------------------------------------------ AI grants
async def create_grant(db: AsyncSession, item: VaultItem, *, token_id: str | None, approval_id: str | None,
                       approved_by: str | None) -> VaultGrant:
    cfg = await get_setting(db, "vault")
    minutes = max(1, min(int(cfg.get("grant_minutes") or 15), 24 * 60))
    g = VaultGrant(item_id=item.id, customer_id=item.customer_id, token_id=token_id, approval_id=approval_id,
                   approved_by=approved_by, expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
                   uses_left=max(1, min(int(cfg.get("grant_uses") or 1), 20)))
    db.add(g)
    await db.flush()
    return g


async def consume_grant(db: AsyncSession, item: VaultItem, token_id: str | None) -> VaultGrant | None:
    """Use up one read of a valid grant for this token and item; None when there is none."""
    now = datetime.now(UTC)
    await db.execute(delete(VaultGrant).where(or_(VaultGrant.expires_at < now, VaultGrant.uses_left <= 0)))
    g = (await db.execute(select(VaultGrant).where(VaultGrant.item_id == item.id, VaultGrant.token_id == token_id)
                          .order_by(VaultGrant.created_at).limit(1).with_for_update())).scalar_one_or_none()
    if g is None:
        return None
    g.uses_left -= 1
    await db.flush()
    return g


async def revoke_grants(db: AsyncSession, item_id: str | None = None) -> int:
    q = delete(VaultGrant)
    if item_id:
        q = q.where(VaultGrant.item_id == item_id)
    res = await db.execute(q)
    return res.rowcount or 0


async def active_grants(db: AsyncSession) -> list[VaultGrant]:
    now = datetime.now(UTC)
    return list((await db.execute(select(VaultGrant).where(VaultGrant.expires_at >= now, VaultGrant.uses_left > 0)
                                  .order_by(VaultGrant.created_at.desc()))).scalars())


# ------------------------------------------------------------------ key rotation
async def rotate_key(db: AsyncSession) -> dict[str, Any]:
    """Wrap every data key (items, history, attachments) with a new vault key, then drop the old keys.
    The new key is added to the key file before anything is re-wrapped, so a crash leaves every secret
    readable; the old keys are removed only after the database commit."""
    from mmn.services import tenancy

    ring = json.loads(json.dumps(_ring()))
    new_kid = _new_key_id(ring)
    ring["keys"][new_kid] = base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()
    _write_ring(ring)  # old keys still present
    n_items = n_hist = n_files = 0
    with tenancy.unrestricted():
        for item in (await db.execute(select(VaultItem))).scalars():
            aad = _aad("item", item.id)
            item.key_id, item.wrapped_key = _wrap(_unwrap(item.key_id, item.wrapped_key, aad), aad, new_kid)
            n_items += 1
        for h in (await db.execute(select(VaultItemHistory))).scalars():
            aad = _aad("history", h.id)
            h.key_id, h.wrapped_key = _wrap(_unwrap(h.key_id, h.wrapped_key, aad), aad, new_kid)
            n_hist += 1
        atts = list((await db.execute(select(DocumentAttachment))).scalars())
    rewritten: list[tuple[Path, bytes]] = []
    for a in atts:
        p = attachment_path(a.id)
        if p.exists():
            rewritten.append((p, _rewrap_blob("attachment", a.id, p.read_bytes(), new_kid)))
    for p, blob in rewritten:
        tmp = p.with_suffix(".rotate")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        n_files += 1
    await db.flush()
    await db.commit()
    ring["active"] = new_kid
    ring["keys"] = {new_kid: ring["keys"][new_kid]}
    ring["rotated_at"] = datetime.now(UTC).isoformat()
    _write_ring(ring)
    return {"key_id": new_kid, "items": n_items, "history": n_hist, "attachments": n_files}


# ------------------------------------------------------------------ attachments
def attachment_path(att_id: str) -> Path:
    return get_settings().files_dir / "docs" / att_id[:2] / att_id


def write_attachment(att_id: str, data: bytes) -> None:
    p = attachment_path(att_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(seal_blob("attachment", att_id, data))


def read_attachment(att_id: str) -> bytes:
    p = attachment_path(att_id)
    if not p.exists():
        raise VaultError("the file of this attachment is missing")
    return open_blob("attachment", att_id, p.read_bytes())


def remove_attachment_file(att_id: str) -> None:
    try:
        attachment_path(att_id).unlink()
    except FileNotFoundError:
        pass


def scrub(text: str, secrets_: list[str] | None) -> str:
    """Replace every secret in ``text`` with ***."""
    if not text or not secrets_:
        return text or ""
    for v in sorted(set(secrets_), key=len, reverse=True):
        if v:
            text = text.replace(v, "***")
    return text


# ------------------------------------------------------------------ password generator
AMBIGUOUS = set("O0Il1|`'\"")
WORDS = ("anchor", "banjo", "cactus", "dolphin", "ember", "falcon", "glacier", "harbor", "island", "jasmine",
         "kettle", "lantern", "meadow", "nebula", "orchid", "pepper", "quartz", "rocket", "saffron", "tundra",
         "umbra", "velvet", "walnut", "xenon", "yonder", "zephyr", "amber", "breeze", "copper", "delta", "echo",
         "fjord", "granite", "horizon", "indigo", "juniper", "koala", "lagoon", "marble", "nectar", "olive",
         "pixel", "quiver", "raven", "summit", "timber", "ultra", "violet", "willow", "yellow", "zenith",
         "beacon", "canyon", "dune", "fable", "grove", "hazel", "ivory", "jungle", "kayak", "lotus", "maple")


def generate_password(length: int = 24, *, symbols: bool = True, digits: bool = True, avoid_ambiguous: bool = True,
                      words: int = 0) -> str:
    """A random password from the OS CSPRNG, with at least one of each chosen character class; with ``words``
    a passphrase of that many words."""
    if words:
        n = max(3, min(words, 12))
        parts = [secrets.choice(WORDS).capitalize() if i == 0 else secrets.choice(WORDS) for i in range(n)]
        return "-".join(parts) + "-" + str(secrets.randbelow(90) + 10)
    length = max(8, min(length, 128))
    classes = [string.ascii_lowercase, string.ascii_uppercase]
    if digits:
        classes.append(string.digits)
    if symbols:
        classes.append("!#$%&()*+,-./:;<=>?@[]^_{}~")
    if avoid_ambiguous:
        classes = ["".join(c for c in cls if c not in AMBIGUOUS) for cls in classes]
    alphabet = "".join(classes)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(ch in cls for ch in pw) for cls in classes):
            return pw


# ------------------------------------------------------------------ documents
CATEGORIES = ("network", "procedure", "notes", "contacts", "howto", "hardware", "software", "security", "other")


async def counts(db: AsyncSession, customer_id: str | None = None) -> dict[str, Any]:
    dq = select(func.count()).select_from(Document)
    vq = select(VaultItem)
    if customer_id:
        dq = dq.where(Document.customer_id == customer_id)
        vq = vq.where(VaultItem.customer_id == customer_id)
    items = list((await db.execute(vq)).scalars())
    now = datetime.now(UTC)
    return {"documents": (await db.execute(dq)).scalar_one(), "credentials": len(items),
            "rotation_due": sum(1 for i in items if rotation_due(i, now)),
            "ai_never": sum(1 for i in items if i.ai_access == "never")}
