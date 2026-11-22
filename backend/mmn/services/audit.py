from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import AuditLog, User


async def audit(
    db: AsyncSession,
    action: str,
    *,
    user: User | None = None,
    source: str = "ui",
    target: str | None = None,
    ip: str | None = None,
    success: bool = True,
    **detail: Any,
) -> None:
    db.add(
        AuditLog(
            action=action,
            user_id=user.id if user else None,
            username=user.username if user else None,
            source=source,
            target=target,
            ip=ip,
            success=success,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )
    await db.flush()
