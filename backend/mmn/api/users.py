from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, require_admin
from mmn.api.schemas import UserCreate, UserOut, UserUpdate
from mmn.db import get_db
from mmn.models import Role, User, UserSession
from mmn.security import hash_password
from mmn.services.audit import audit

router = APIRouter(prefix="/api/users", tags=["users"])


async def _admin_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User).where(
        User.role == Role.admin, User.is_active.is_(True)))).scalar_one()


@router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return (await db.execute(select(User).order_by(User.username))).scalars().all()


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    if (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="username_taken")
    if body.email and (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email_taken")
    user = User(username=body.username, email=body.email, full_name=body.full_name, role=Role(body.role),
                language=body.language, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()
    await audit(db, "user.create", user=admin, target=user.username, ip=client_ip(request))
    await db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: str, body: UserUpdate, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    data = body.model_dump(exclude_unset=True)
    demoting = (data.get("role") == "viewer" or data.get("is_active") is False) and user.role == Role.admin
    if demoting and await _admin_count(db) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="last_admin")
    if "password" in data:
        pw = data.pop("password")
        if pw:
            user.password_hash = hash_password(pw)
    if "role" in data:
        data["role"] = Role(data["role"])
    for k, v in data.items():
        setattr(user, k, v)
    if data.get("is_active") is False:
        for s in (await db.execute(select(UserSession).where(UserSession.user_id == user.id))).scalars():
            await db.delete(s)
    await audit(db, "user.update", user=admin, target=user.username, ip=client_ip(request),
                fields=sorted(body.model_dump(exclude_unset=True).keys()))
    await db.commit()
    return user


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="cannot_delete_self")
    if user.role == Role.admin and await _admin_count(db) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="last_admin")
    await db.delete(user)
    await audit(db, "user.delete", user=admin, target=user.username, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)
