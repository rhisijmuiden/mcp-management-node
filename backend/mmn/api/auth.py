from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    client_ip,
    current_user,
    get_session,
    user_by_username,
)
from mmn.api.ratelimit import forgot_limiter, login_limiter
from mmn.api.schemas import (
    ChangePasswordIn,
    ForgotIn,
    LoginIn,
    PreferencesIn,
    ResetIn,
    SetupIn,
    UserOut,
)
from mmn.config import get_settings
from mmn.db import get_db
from mmn.i18n import t
from mmn.models import PasswordReset, Role, User, UserSession
from mmn.security import hash_password, hash_token, needs_rehash, new_token, verify_password
from mmn.services.audit import audit
from mmn.services.links import public_base_url
from mmn.services.mailer import MailContent, try_send_mail
from mmn.services.settings_store import get_setting

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Used to keep login timing equal for unknown users.
_DUMMY_HASH = hash_password("not-a-real-password-just-timing")


def setup_token_file():
    return get_settings().data_dir / "setup-token"


async def users_exist(db: AsyncSession) -> bool:
    return bool((await db.execute(select(func.count()).select_from(User))).scalar_one())


def _secure_cookie(request: Request) -> bool:
    if get_settings().insecure_cookies:
        return request.url.scheme == "https"
    return True


def set_session_cookies(response: Response, request: Request, token: str, max_age: int | None) -> None:
    s = get_settings()
    secure = _secure_cookie(request)
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=secure,
                        samesite="lax", path=s.cookie_path)
    response.set_cookie(CSRF_COOKIE, new_token(24), max_age=max_age, httponly=False, secure=secure,
                        samesite="strict", path=s.cookie_path)


def clear_session_cookies(response: Response) -> None:
    path = get_settings().cookie_path
    response.delete_cookie(SESSION_COOKIE, path=path)
    response.delete_cookie(CSRF_COOKIE, path=path)


async def start_session(db: AsyncSession, request: Request, response: Response, user: User, remember: bool):
    s = get_settings()
    token = new_token()
    lifetime = timedelta(days=s.remember_days) if remember else timedelta(hours=s.session_hours)
    db.add(UserSession(
        id=hash_token(token), user_id=user.id, expires_at=datetime.now(UTC) + lifetime, remember=remember,
        ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    user.last_login_at = datetime.now(UTC)
    set_session_cookies(response, request, token, int(lifetime.total_seconds()) if remember else None)


@router.get("/status")
async def auth_status(db: AsyncSession = Depends(get_db), sess: UserSession | None = Depends(get_session)):
    general = await get_setting(db, "general")
    return {
        "setup_required": not await users_exist(db),
        "authenticated": sess is not None,
        "app_name": general["app_name"],
        "default_language": general["default_language"],
        "mail_enabled": (await get_setting(db, "mail"))["enabled"],
    }


@router.post("/setup", response_model=UserOut)
async def setup(body: SetupIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if await users_exist(db):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already_set_up")
    f = setup_token_file()
    expected = f.read_text().strip() if f.exists() else ""
    if not expected or not hmac.compare_digest(expected, body.setup_token.strip()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid_setup_token")
    user = User(username=body.username, email=body.email, full_name=body.full_name, role=Role.admin,
                language=body.language, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()
    await start_session(db, request, response, user, remember=False)
    await audit(db, "auth.setup", user=user, ip=client_ip(request))
    await db.commit()
    f.unlink(missing_ok=True)
    return user


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request) or "?"
    key = f"{ip}:{body.username.lower()}"
    if not login_limiter.hit(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    user = await user_by_username(db, body.username)
    if user is None:
        verify_password(_DUMMY_HASH, body.password)
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        await audit(db, "auth.login", source="ui", target=body.username[:64], ip=ip, success=False)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    login_limiter.reset(key)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    await start_session(db, request, response, user, body.remember)
    await audit(db, "auth.login", user=user, ip=ip)
    await db.commit()
    return user


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db),
                 sess: UserSession | None = Depends(get_session)):
    if sess is not None:
        await db.delete(sess)
        await db.commit()
    clear_session_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(body: PreferencesIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(user, k, v)
    db.add(user)
    await db.commit()
    return user


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                          user: User = Depends(current_user)):
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="wrong_password")
    user.password_hash = hash_password(body.password)
    db.add(user)
    # Sign out other sessions.
    current = hash_token(request.cookies.get(SESSION_COOKIE, ""))
    others = (await db.execute(select(UserSession).where(UserSession.user_id == user.id,
                                                            UserSession.id != current))).scalars()
    for s in others:
        await db.delete(s)
    await audit(db, "auth.change_password", user=user, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.post("/forgot", status_code=202)
async def forgot(body: ForgotIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Always answers 202 so it can't be used to find out which accounts exist."""
    ip = client_ip(request) or "?"
    if not forgot_limiter.hit(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    q = body.username_or_email.strip().lower()
    user = (await db.execute(select(User).where(
        or_(User.username == q, func.lower(User.email) == q)))).scalar_one_or_none()
    if user and user.is_active and user.email:
        token = new_token()
        db.add(PasswordReset(id=hash_token(token), user_id=user.id,
                             expires_at=datetime.now(UTC) + timedelta(minutes=60)))
        await audit(db, "auth.forgot", user=user, ip=ip)
        await db.commit()
        base = await public_base_url(db, request)
        general = await get_setting(db, "general")
        lang = user.language
        app = general["app_name"]
        await try_send_mail(db, user.email, MailContent(
            subject=t(lang, "reset.subject", app=app), title=t(lang, "reset.title"),
            paragraphs=[t(lang, "reset.body", username=user.username)],
            muted=[t(lang, "reset.ignore")], button_url=f"{base}/reset-password?token={token}",
            button_label=t(lang, "reset.button")), lang)
    return {"status": "accepted"}


@router.post("/reset", status_code=204)
async def reset(body: ResetIn, request: Request, db: AsyncSession = Depends(get_db)):
    pr = await db.get(PasswordReset, hash_token(body.token))
    if pr is None or pr.used or pr.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    user = await db.get(User, pr.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    user.password_hash = hash_password(body.password)
    pr.used = True
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id).values(used=True))
    for s in (await db.execute(select(UserSession).where(UserSession.user_id == user.id))).scalars():
        await db.delete(s)
    await audit(db, "auth.reset_password", user=user, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)
