from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.db import get_db
from mmn.models import Role, User, UserSession
from mmn.security import hash_token

SESSION_COOKIE = "mmn_session"
CSRF_COOKIE = "mmn_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    # uvicorn's ProxyHeadersMiddleware already resolved X-Forwarded-For from trusted proxies only.
    return request.client.host if request.client else None


async def get_session(request: Request, db: AsyncSession = Depends(get_db)) -> UserSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = await db.get(UserSession, hash_token(token))
    now = datetime.now(UTC)
    if sess is None or sess.expires_at < now or not sess.user.is_active:
        return None
    # Sliding expiry for normal sessions, refreshed at most every 5 minutes.
    if not sess.remember:
        new_exp = now + timedelta(hours=get_settings().session_hours)
        if new_exp - sess.expires_at > timedelta(minutes=5):
            sess.expires_at = new_exp
            await db.commit()
    return sess


def check_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not hmac.compare_digest(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf_failed")


async def current_user(
    request: Request, sess: UserSession | None = Depends(get_session)
) -> User:
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    check_csrf(request)
    return sess.user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_required")
    return user


async def user_by_username(db: AsyncSession, username: str) -> User | None:
    return (await db.execute(select(User).where(User.username == username.lower()))).scalar_one_or_none()
