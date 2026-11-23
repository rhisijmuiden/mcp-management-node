from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mmn.api import (
    agent,
    auth,
    backups,
    cluster,
    customers,
    deploy,
    gateway,
    guests,
    hub,
    library,
    monitoring,
    network,
    nightly,
    oauth,
    protection,
    proxmox,
    reports,
    servers,
    settings,
    system,
    tokens,
    unifi,
    users,
    workstations,
    ws,
)
from mmn.config import get_settings
from mmn.db import dispose_engine
from mmn.mcp_server import build_mcp_app, mcp
from mmn.security import load_or_create_secret_key
from mmn.services import tenancy
from mmn.services.backup.engine import maintenance
from mmn.version import get_build_info

log = logging.getLogger("mmn")


async def _announce_version() -> None:
    """Send the 'updated' mail once after a new version starts."""
    from mmn.db import sessionmaker
    from mmn.i18n import t
    from mmn.services.mailer import MailContent, try_send_mail
    from mmn.services.settings_store import get_setting, set_setting

    b = get_build_info()
    async with sessionmaker()() as db:
        state = await get_setting(db, "runtime")
        old = state.get("last_tag")
        if old == b.tag:
            return
        await set_setting(db, "runtime", {"last_tag": b.tag})
        await db.commit()
        notif = await get_setting(db, "notifications")
        if old and notif.get("update_to"):
            general = await get_setting(db, "general")
            lang, app = general["default_language"], general["app_name"]
            channel = (await get_setting(db, "updates"))["channel"]
            await try_send_mail(db, notif["update_to"], MailContent(
                subject=t(lang, "update.subject", app=app, version=b.version), title=t(lang, "update.title"),
                paragraphs=[t(lang, "update.body", app=app, old=old, version=b.tag, channel=channel)], muted=[]),
                lang)


async def _prepare_agent_tls() -> None:
    """Write the agent endpoint certificates for Nginx; ask the root helper to reload Nginx when
    they changed (first start, or after a restore with another CA)."""
    import json
    import os

    from mmn.db import sessionmaker
    from mmn.services.agent_ca import ensure_tls_files
    from mmn.services.updates import updater_available

    async with sessionmaker()() as db:
        changed = await ensure_tls_files(db)
    if changed and updater_available():
        run = get_settings().run_dir
        tmp = run / "nginx-reload-request.tmp"
        tmp.write_text(json.dumps({"action": "reload-nginx"}))
        os.replace(tmp, run / "nginx-reload-request.json")


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp_app = build_mcp_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.run_dir.mkdir(parents=True, exist_ok=True)
        load_or_create_secret_key()
        if s.run_scheduler:
            from mmn.services import scheduler

            await scheduler.start()
        try:
            await _prepare_agent_tls()
        except Exception:  # noqa: BLE001
            log.exception("agent TLS files could not be written")
        try:
            from mmn.services import hub as hub_service

            await hub_service.sync()
        except Exception:  # noqa: BLE001
            log.exception("MCP hub tools could not be loaded")
        try:
            await _announce_version()
        except Exception:  # noqa: BLE001
            log.exception("version announcement failed")
        async with mcp.session_manager.run():
            yield
        if s.run_scheduler:
            from mmn.services import scheduler

            scheduler.stop()
        await dispose_engine()

    app = FastAPI(title="Stackpilot", version=get_build_info().version, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.middleware("http")
    async def maintenance_guard(request: Request, call_next):
        if request.scope["path"] == "/mcp":
            request.scope["path"] = "/mcp/"  # avoid a redirect that would lose the base path
        if maintenance["active"] and request.url.path.startswith(("/api/", "/mcp", "/oauth/")) \
                and request.url.path not in ("/api/health", "/api/system/maintenance"):
            return JSONResponse({"detail": "maintenance", "reason": maintenance["reason"]}, status_code=503)
        return await call_next(request)

    @app.middleware("http")
    async def customer_filter(request: Request, call_next):
        """The customer picked in the switcher of the web interface limits every query of the request."""
        cid = request.headers.get(tenancy.HEADER, "").strip()
        path = request.url.path
        # Customer pages name their customer in the path, and the switcher needs the full list.
        if not cid or not path.startswith("/api/") or path.startswith((
                "/api/customers", "/api/environments", "/api/auth/", "/api/agent/", "/api/tokens", "/api/oauth/",
                "/api/users", "/api/system/")):
            return await call_next(request)
        tok = tenancy.set_allowed([cid[:64]])
        try:
            return await call_next(request)
        finally:
            tenancy.reset(tok)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": "validation_error", "errors": errors}, status_code=422)

    @app.get("/api/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "version": get_build_info().tag}

    @app.get("/api/system/maintenance", include_in_schema=False)
    async def maintenance_state():
        return maintenance

    for r in (auth.router, users.router, tokens.router, settings.router, backups.router, system.router,
              oauth.router, ws.router, agent.router, proxmox.router, guests.router, library.router, network.router,
              cluster.router, monitoring.router, deploy.router, servers.router, unifi.router, customers.router,
              gateway.router, hub.router, workstations.router, reports.router, protection.router, nightly.router):
        app.include_router(r)
    app.mount("/mcp", mcp_app)
    return app


app = create_app()
