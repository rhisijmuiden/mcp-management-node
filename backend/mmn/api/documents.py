"""Customer documentation (phase 18): pages per customer with versions and attachments."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Document, DocumentAttachment, DocumentRevision, User
from mmn.services import docs, events, vault
from mmn.services.audit import audit

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _who(u: User) -> str:
    return u.full_name or u.username


async def _doc(db: AsyncSession, did: str) -> Document:
    d = await db.get(Document, did)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return d


class DocIn(BaseModel):
    customer_id: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=docs.MAX_BODY)
    category: str = "notes"
    tags: list[str] = Field(default_factory=list, max_length=20)
    node_ids: list[str] = Field(default_factory=list, max_length=200)
    pinned: bool = False
    ai_visible: bool = True


class DocUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=docs.MAX_BODY)
    category: str | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    node_ids: list[str] | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    ai_visible: bool | None = None
    # The version the editor started from: a newer version saved in the meantime is a conflict.
    base_version: int | None = None


@router.get("")
async def list_documents(customer_id: str | None = None, q: str | None = None, category: str | None = None,
                         tag: str | None = None, node_id: str | None = None, db: AsyncSession = Depends(get_db),
                         _: User = Depends(current_user)):
    rows = await docs.search(db, customer_id=customer_id, q=q, category=category, tag=tag, node_id=node_id)
    names = await vault.machine_names(db)
    out = []
    for d in rows:
        item = docs.doc_out(d, names=names, body=False)
        if q:
            item["snippets"] = docs.snippets(d.body, q)
        out.append(item)
    return {"documents": out, "counts": await vault.counts(db, customer_id), "categories": list(docs.CATEGORIES)}


@router.get("/templates")
async def templates(lang: str = "en", _: User = Depends(current_user)):
    lang = "nl" if lang.startswith("nl") else "en"
    return [{"key": k, "category": k if k in docs.CATEGORIES else "notes", **v[lang]} for k, v in docs.TEMPLATES.items()]


@router.post("", status_code=201)
async def create_document(body: DocIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    try:
        d = await docs.create(db, customer_id=body.customer_id, title=body.title, body=body.body, category=body.category,
                              tags=body.tags, node_ids=body.node_ids, pinned=body.pinned, ai_visible=body.ai_visible,
                              by=_who(admin))
    except docs.DocError as e:
        raise _bad(e) from e
    await audit(db, "docs.create", user=admin, target=d.title, ip=client_ip(request), customer=d.customer_id,
                document_id=d.id)
    await db.commit()
    events.publish("docs.changed", {"id": d.id})
    return docs.doc_out(d, names=await vault.machine_names(db), attachments=[])


@router.get("/{did}")
async def get_document(did: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    d = await _doc(db, did)
    return {**docs.doc_out(d, names=await vault.machine_names(db), attachments=await docs.attachments_of(db, d.id)),
            "revisions": [docs.revision_out(r) for r in await docs.revisions(db, d.id)]}


@router.patch("/{did}")
async def update_document(did: str, body: DocUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    d = await _doc(db, did)
    data = body.model_dump(exclude_unset=True)
    base = data.pop("base_version", None)
    if base is not None and base != d.version and ("body" in data or "title" in data):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="doc_conflict")
    try:
        changed = await docs.update(db, d, data, by=_who(admin))
    except docs.DocError as e:
        raise _bad(e) from e
    if changed:
        await audit(db, "docs.update", user=admin, target=d.title, ip=client_ip(request), customer=d.customer_id,
                    document_id=d.id, changed=changed, version=d.version)
    await db.commit()
    events.publish("docs.changed", {"id": d.id})
    return docs.doc_out(d, names=await vault.machine_names(db), attachments=await docs.attachments_of(db, d.id))


@router.delete("/{did}", status_code=204)
async def delete_document(did: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    d = await _doc(db, did)
    title, cid = d.title, d.customer_id
    await docs.delete_document(db, d)
    await audit(db, "docs.delete", user=admin, target=title, ip=client_ip(request), customer=cid, document_id=did)
    await db.commit()
    events.publish("docs.changed", {"id": did})
    return Response(status_code=204)


@router.get("/{did}/revisions/{rid}")
async def get_revision(did: str, rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    await _doc(db, did)
    r = await db.get(DocumentRevision, rid)
    if r is None or r.document_id != did:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return docs.revision_out(r, body=True)


@router.post("/{did}/revisions/{rid}/restore")
async def restore_revision(did: str, rid: str, request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    d = await _doc(db, did)
    r = await db.get(DocumentRevision, rid)
    if r is None or r.document_id != did:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    await docs.update(db, d, {"title": r.title, "body": r.body}, by=_who(admin))
    await audit(db, "docs.restore_revision", user=admin, target=d.title, ip=client_ip(request), customer=d.customer_id,
                document_id=d.id, restored_version=r.version, version=d.version)
    await db.commit()
    events.publish("docs.changed", {"id": d.id})
    return docs.doc_out(d, names=await vault.machine_names(db), attachments=await docs.attachments_of(db, d.id))


# ------------------------------------------------------------------ attachments
@router.post("/{did}/attachments", status_code=201)
async def upload_attachment(did: str, request: Request, file: UploadFile = File(...),
                            name: str | None = Form(default=None), db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    d = await _doc(db, did)
    data = await file.read(docs.MAX_ATTACHMENT + 1)
    try:
        a = await docs.add_attachment(db, d, filename=name or file.filename or "file", content_type=file.content_type,
                                      data=data, by=_who(admin))
    except docs.DocError as e:
        raise _bad(e) from e
    await audit(db, "docs.attachment_add", user=admin, target=f"{d.title}: {a.filename}"[:255], ip=client_ip(request),
                customer=d.customer_id, document_id=d.id, size=a.size)
    await db.commit()
    events.publish("docs.changed", {"id": d.id})
    return docs.attachment_out(a)


async def _attachment(db: AsyncSession, did: str, aid: str) -> DocumentAttachment:
    a = await db.get(DocumentAttachment, aid)
    if a is None or a.document_id != did:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return a


@router.get("/{did}/attachments/{aid}")
async def download_attachment(did: str, aid: str, inline: bool = False, db: AsyncSession = Depends(get_db),
                              _: User = Depends(current_user)):
    a = await _attachment(db, did, aid)
    try:
        data = vault.read_attachment(a.id)
    except vault.VaultError as e:
        raise HTTPException(status.HTTP_410_GONE, detail=str(e)) from e
    # Only images and PDFs are shown inline; everything else is a download (no HTML from uploads).
    safe_inline = inline and (a.content_type.startswith("image/") and a.content_type != "image/svg+xml"
                              or a.content_type == "application/pdf")
    disp = "inline" if safe_inline else "attachment"
    quoted = urllib.parse.quote(a.filename)
    return Response(content=data, media_type=a.content_type if safe_inline else "application/octet-stream",
                    headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quoted}",
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                             "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})


@router.delete("/{did}/attachments/{aid}", status_code=204)
async def delete_attachment(did: str, aid: str, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    d = await _doc(db, did)
    a = await _attachment(db, did, aid)
    vault.remove_attachment_file(a.id)
    await db.delete(a)
    await audit(db, "docs.attachment_delete", user=admin, target=f"{d.title}: {a.filename}"[:255],
                ip=client_ip(request), customer=d.customer_id, document_id=d.id)
    await db.commit()
    events.publish("docs.changed", {"id": d.id})
    return Response(status_code=204)
