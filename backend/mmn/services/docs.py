"""Customer documentation (phase 18): Markdown pages per customer (network plans, procedures, notes,
contacts...) with linked machines, tags, attachments and every earlier version."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Document, DocumentAttachment, DocumentRevision, Environment, new_id
from mmn.services import vault
from mmn.services.vault import CATEGORIES, clean_nodes

MAX_BODY = 500_000
MAX_ATTACHMENT = 25 * 1024 * 1024
KEEP_REVISIONS = 100


class DocError(Exception):
    pass


def _tags(tags: list[str] | None) -> list[str]:
    return sorted({t.strip().lower()[:40] for t in (tags or []) if t and t.strip()})[:20]


def excerpt(body: str, n: int = 220) -> str:
    text = re.sub(r"[#>*_`|\[\]()-]+", " ", body or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def doc_out(d: Document, *, names: dict[str, str] | None = None, body: bool = True,
            attachments: list[DocumentAttachment] | None = None) -> dict[str, Any]:
    out = {
        "id": d.id, "customer_id": d.customer_id, "customer_name": d.customer.name if d.customer else None,
        "title": d.title, "category": d.category, "tags": d.tags or [], "pinned": d.pinned, "ai_visible": d.ai_visible,
        "machines": [{"id": n, "name": (names or {}).get(n)} for n in d.node_ids or []], "version": d.version,
        "created_at": d.created_at, "updated_at": d.updated_at, "created_by": d.created_by, "updated_by": d.updated_by,
        "excerpt": excerpt(d.body),
    }
    if body:
        out["body"] = d.body
    if attachments is not None:
        out["attachments"] = [attachment_out(a) for a in attachments]
    return out


def attachment_out(a: DocumentAttachment) -> dict[str, Any]:
    return {"id": a.id, "document_id": a.document_id, "filename": a.filename, "content_type": a.content_type,
            "size": a.size, "sha256": a.sha256, "created_at": a.created_at, "created_by": a.created_by}


def revision_out(r: DocumentRevision, body: bool = False) -> dict[str, Any]:
    out = {"id": r.id, "version": r.version, "title": r.title, "created_at": r.created_at, "created_by": r.created_by}
    if body:
        out["body"] = r.body
    return out


async def attachments_of(db: AsyncSession, doc_id: str) -> list[DocumentAttachment]:
    return list((await db.execute(select(DocumentAttachment).where(DocumentAttachment.document_id == doc_id)
                                  .order_by(DocumentAttachment.created_at))).scalars())


async def create(db: AsyncSession, *, customer_id: str, title: str, body: str = "", category: str = "notes",
                 tags: list[str] | None = None, node_ids: list[str] | None = None, pinned: bool = False,
                 ai_visible: bool = True, by: str | None = None) -> Document:
    if category not in CATEGORIES:
        raise DocError(f"unknown category {category}")
    if await db.get(Environment, customer_id) is None:
        raise DocError("customer not found")
    title = title.strip()[:200]
    if not title:
        raise DocError("title is required")
    if len(body) > MAX_BODY:
        raise DocError("the text is too long")
    try:
        nodes = await clean_nodes(db, customer_id, node_ids)
    except vault.VaultError as e:
        raise DocError(str(e)) from e
    d = Document(id=new_id(), customer_id=customer_id, title=title, body=body, category=category, tags=_tags(tags),
                 node_ids=nodes, pinned=pinned, ai_visible=ai_visible, created_by=by, updated_by=by)
    db.add(d)
    await db.flush()
    await db.refresh(d, ["customer"])
    return d


async def update(db: AsyncSession, d: Document, data: dict[str, Any], *, by: str | None) -> list[str]:
    """Apply changes. A change of title or text saves the previous version as a revision."""
    changed: list[str] = []
    new_title = (data.get("title") or "").strip()[:200] if "title" in data else d.title
    if "title" in data and not new_title:
        raise DocError("title is required")
    new_body = data["body"] if data.get("body") is not None else d.body
    if len(new_body) > MAX_BODY:
        raise DocError("the text is too long")
    if new_title != d.title or new_body != d.body:
        db.add(DocumentRevision(document_id=d.id, customer_id=d.customer_id, version=d.version, title=d.title,
                                body=d.body, created_at=d.updated_at, created_by=d.updated_by))
        if new_title != d.title:
            changed.append("title")
        if new_body != d.body:
            changed.append("body")
        d.title, d.body, d.version = new_title, new_body, d.version + 1
        await _trim(db, d.id)
    if "category" in data and data["category"] is not None and data["category"] != d.category:
        if data["category"] not in CATEGORIES:
            raise DocError(f"unknown category {data['category']}")
        d.category = data["category"]
        changed.append("category")
    for k in ("pinned", "ai_visible"):
        if k in data and data[k] is not None and getattr(d, k) != data[k]:
            setattr(d, k, bool(data[k]))
            changed.append(k)
    if "tags" in data and data["tags"] is not None:
        d.tags = _tags(data["tags"])
        changed.append("tags")
    if "node_ids" in data and data["node_ids"] is not None:
        try:
            d.node_ids = await clean_nodes(db, d.customer_id, data["node_ids"])
        except vault.VaultError as e:
            raise DocError(str(e)) from e
        changed.append("machines")
    if changed:
        d.updated_at, d.updated_by = datetime.now(UTC), by
    await db.flush()
    return changed


async def _trim(db: AsyncSession, doc_id: str) -> None:
    ids = (await db.execute(select(DocumentRevision.id).where(DocumentRevision.document_id == doc_id)
                            .order_by(DocumentRevision.version.desc()).offset(KEEP_REVISIONS))).scalars().all()
    for rid in ids:
        r = await db.get(DocumentRevision, rid)
        if r is not None:
            await db.delete(r)


async def revisions(db: AsyncSession, doc_id: str) -> list[DocumentRevision]:
    return list((await db.execute(select(DocumentRevision).where(DocumentRevision.document_id == doc_id)
                                  .order_by(DocumentRevision.version.desc()))).scalars())


async def find(db: AsyncSession, ref: str, customer_id: str | None = None) -> Document:
    ref = (ref or "").strip()
    d = await db.get(Document, ref) if len(ref) == 32 else None
    if d is not None and (customer_id is None or d.customer_id == customer_id):
        return d
    q = select(Document).where(func.lower(Document.title) == ref.lower())
    if customer_id:
        q = q.where(Document.customer_id == customer_id)
    rows = list((await db.execute(q)).scalars())
    if not rows:
        raise DocError(f"document {ref!r} not found")
    if len(rows) > 1:
        raise DocError(f"more than one document is called {ref!r}: name the customer or use the id")
    return rows[0]


async def search(db: AsyncSession, *, customer_id: str | None = None, q: str | None = None,
                 category: str | None = None, tag: str | None = None, node_id: str | None = None,
                 ai_only: bool = False, limit: int = 500) -> list[Document]:
    stmt = select(Document).order_by(Document.pinned.desc(), Document.updated_at.desc()).limit(limit)
    if customer_id:
        stmt = stmt.where(Document.customer_id == customer_id)
    if category:
        stmt = stmt.where(Document.category == category)
    if ai_only:
        stmt = stmt.where(Document.ai_visible.is_(True))
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Document.title).like(like), func.lower(Document.body).like(like)))
    rows = list((await db.execute(stmt)).scalars())
    if tag:
        rows = [r for r in rows if tag.strip().lower() in (r.tags or [])]
    if node_id:
        rows = [r for r in rows if node_id in (r.node_ids or [])]
    return rows


def snippets(body: str, q: str, n: int = 3, width: int = 90) -> list[str]:
    """Lines around the matches of ``q`` (for search results)."""
    if not q:
        return []
    out = []
    low = body.lower()
    needle = q.strip().lower()
    start = 0
    while len(out) < n:
        i = low.find(needle, start)
        if i < 0:
            break
        a, b = max(0, i - width // 2), min(len(body), i + len(needle) + width // 2)
        out.append(("…" if a else "") + re.sub(r"\s+", " ", body[a:b]).strip() + ("…" if b < len(body) else ""))
        start = i + len(needle)
    return out


async def add_attachment(db: AsyncSession, d: Document, *, filename: str, content_type: str | None, data: bytes,
                         by: str | None) -> DocumentAttachment:
    if len(data) > MAX_ATTACHMENT:
        raise DocError("the file is larger than 25 MB")
    name = re.sub(r"[\\/\x00-\x1f]+", "_", (filename or "file").strip())[:255] or "file"
    a = DocumentAttachment(id=new_id(), document_id=d.id, customer_id=d.customer_id, filename=name,
                           content_type=(content_type or "application/octet-stream")[:128], size=len(data),
                           sha256=hashlib.sha256(data).hexdigest(), created_by=by)
    vault.write_attachment(a.id, data)
    db.add(a)
    d.updated_at, d.updated_by = datetime.now(UTC), by
    await db.flush()
    return a


async def delete_document(db: AsyncSession, d: Document) -> None:
    for a in await attachments_of(db, d.id):
        vault.remove_attachment_file(a.id)
    await db.delete(d)
    await db.flush()


TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    "network": {
        "en": {"title": "Network plan", "body": "# Network plan\n\n## Internet\n| Item | Value |\n|---|---|\n| Provider | |\n| Public IP | |\n| Gateway | |\n\n## VLANs\n| VLAN | Name | Subnet | Gateway | DHCP |\n|---|---|---|---|---|\n| 1 | LAN | 192.168.1.0/24 | 192.168.1.1 | yes |\n\n## Devices\n| Name | Type | IP | Location |\n|---|---|---|---|\n\n## WiFi\n| SSID | VLAN | Security |\n|---|---|---|\n\n## VPN\n"},
        "nl": {"title": "Netwerkplan", "body": "# Netwerkplan\n\n## Internet\n| Onderdeel | Waarde |\n|---|---|\n| Provider | |\n| Publiek IP | |\n| Gateway | |\n\n## VLAN's\n| VLAN | Naam | Subnet | Gateway | DHCP |\n|---|---|---|---|---|\n| 1 | LAN | 192.168.1.0/24 | 192.168.1.1 | ja |\n\n## Apparaten\n| Naam | Type | IP | Locatie |\n|---|---|---|---|\n\n## WiFi\n| SSID | VLAN | Beveiliging |\n|---|---|---|\n\n## VPN\n"},
    },
    "procedure": {
        "en": {"title": "Procedure: ", "body": "# Procedure\n\n**When:** \n**Who:** \n**Takes:** \n\n## Before you start\n- \n\n## Steps\n1. \n2. \n3. \n\n## Check afterwards\n- \n\n## Roll back\n- \n"},
        "nl": {"title": "Procedure: ", "body": "# Procedure\n\n**Wanneer:** \n**Wie:** \n**Duur:** \n\n## Voordat je begint\n- \n\n## Stappen\n1. \n2. \n3. \n\n## Controle achteraf\n- \n\n## Terugdraaien\n- \n"},
    },
    "contacts": {
        "en": {"title": "Contacts and suppliers", "body": "# Contacts and suppliers\n\n| Name | Company | Role | Phone | Email |\n|---|---|---|---|---|\n\n## Support contracts\n| Supplier | Product | Contract | Ends | Phone |\n|---|---|---|---|---|\n"},
        "nl": {"title": "Contactpersonen en leveranciers", "body": "# Contactpersonen en leveranciers\n\n| Naam | Bedrijf | Rol | Telefoon | E-mail |\n|---|---|---|---|---|\n\n## Supportcontracten\n| Leverancier | Product | Contract | Loopt af | Telefoon |\n|---|---|---|---|---|\n"},
    },
    "hardware": {
        "en": {"title": "Hardware inventory", "body": "# Hardware inventory\n\n| Device | Model | Serial | Purchased | Warranty until | Location |\n|---|---|---|---|---|---|\n"},
        "nl": {"title": "Hardware-overzicht", "body": "# Hardware-overzicht\n\n| Apparaat | Model | Serienummer | Gekocht | Garantie tot | Locatie |\n|---|---|---|---|---|---|\n"},
    },
}
