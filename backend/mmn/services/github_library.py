"""Software library (v0.27.0): GitHub tokens and repositories, with rights per customer.

Several GitHub tokens can sit next to each other (your own account and, for example, an organisation).
Repositories are managed at the top level, in the library, and are linked to customers:

* ``product``: your own software, deployable to every customer it is linked to;
* ``customer``: made for one customer (``owner_customer_id``), only that customer can use it;
* ``tooling``: scripts and tools of your own, linked like a product.

Per linked customer: ``can_deploy`` (the repository may be deployed onto that customer's containers or
servers) and ``ai_read`` (an AI token limited to that customer may read the repository).

A repository that is deployed but not yet in the library is added on the fly, linked to the customer
of the target, so existing deployments keep working.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Deployment, Environment, GithubAccount, Repo, RepoAccess
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services import tenancy
from mmn.services.settings_store import get_setting, set_setting

API_BASE = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
EXPIRY_WARN_DAYS = 14
KINDS = ("product", "customer", "tooling")


class GitHubError(Exception):
    pass


# ------------------------------------------------------------------ HTTP
async def call(method: str, path: str, token: str | None, *, params: dict[str, Any] | None = None,
               json: Any = None) -> tuple[Any, httpx.Headers]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "stackpilot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=API_BASE, timeout=20) as c:
        r = await c.request(method, path, headers=headers, params=params, json=json)
    if r.status_code == 401:
        raise GitHubError("GitHub refused the token (401): it is wrong, revoked or expired")
    if r.status_code == 403:
        raise GitHubError(f"the token may not do this (403): {_msg(r)}")
    if r.status_code == 404:
        raise GitHubError("not found on GitHub (or the token has no access)")
    if r.status_code == 422:
        raise GitHubError(f"GitHub did not accept this: {_msg(r)}")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub answered HTTP {r.status_code}: {_msg(r)}")
    return (r.json() if r.content else None), r.headers


def _msg(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:200]
    errs = "; ".join(str(e.get("message") or e) for e in data.get("errors") or [] if isinstance(e, dict))
    return (data.get("message") or "") + (f" ({errs})" if errs else "")


def token_kind(token: str) -> str:
    return "fine_grained" if token.startswith("github_pat_") else "classic"


def _expiry(headers: httpx.Headers) -> datetime | None:
    raw = headers.get("github-authentication-token-expiration")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ accounts
def account_out(a: GithubAccount) -> dict[str, Any]:
    days = None
    if a.expires_at:
        days = (a.expires_at - datetime.now(UTC)).days
    return {"id": a.id, "name": a.name, "login": a.login, "token_kind": a.token_kind, "scopes": a.scopes or [],
            "expires_at": a.expires_at.isoformat() if a.expires_at else None, "expires_in_days": days,
            "expiring": days is not None and days <= EXPIRY_WARN_DAYS, "status": a.status, "last_error": a.last_error,
            "checked_at": a.checked_at.isoformat() if a.checked_at else None, "created_at": a.created_at.isoformat()}


async def ensure_legacy(db: AsyncSession) -> None:
    """Move the single token of v0.26 and older (setting "github") into an account row, once."""
    cfg = await get_setting(db, "github")
    if not cfg.get("token_enc"):
        return
    with tenancy.unrestricted():
        have = (await db.execute(select(GithubAccount.id).limit(1))).scalar()
    if have is None:
        token = decrypt_secret(cfg.get("token_enc")) or ""
        db.add(GithubAccount(name=cfg.get("login") or "GitHub", token_enc=cfg["token_enc"], login=cfg.get("login"),
                             token_kind=token_kind(token), checked_at=datetime.now(UTC)))
    await set_setting(db, "github", {"token_enc": None, "login": None, "checked_at": None, "migrated": True})
    await db.flush()


async def list_accounts(db: AsyncSession) -> list[GithubAccount]:
    await ensure_legacy(db)
    return list((await db.execute(select(GithubAccount).order_by(GithubAccount.created_at))).scalars())


async def get_account(db: AsyncSession, ref: str | None) -> GithubAccount:
    accounts = await list_accounts(db)
    if not accounts:
        raise GitHubError("connect GitHub first: Integrations → GitHub, with a personal access token")
    if not ref:
        return accounts[0]
    for a in accounts:
        if ref in (a.id, a.name, a.login):
            return a
    raise GitHubError(f"GitHub account {ref!r} not found")


async def _check(a: GithubAccount, token: str) -> None:
    me, headers = await call("GET", "/user", token)
    a.login = (me or {}).get("login")
    a.token_kind = token_kind(token)
    a.scopes = [s.strip() for s in (headers.get("x-oauth-scopes") or "").split(",") if s.strip()]
    a.expires_at = _expiry(headers)
    a.status, a.last_error, a.checked_at = "ok", None, datetime.now(UTC)


async def add_account(db: AsyncSession, name: str | None, token: str) -> GithubAccount:
    token = token.strip()
    if not token:
        raise GitHubError("paste the token")
    a = GithubAccount(name="", token_enc=encrypt_secret(token))
    await _check(a, token)
    a.name = (name or "").strip() or a.login or "GitHub"
    db.add(a)
    await db.flush()
    return a


async def replace_token(db: AsyncSession, a: GithubAccount, token: str) -> GithubAccount:
    await _check(a, token.strip())
    a.token_enc = encrypt_secret(token.strip())
    await db.flush()
    return a


async def check_account(db: AsyncSession, a: GithubAccount) -> GithubAccount:
    try:
        await _check(a, decrypt_secret(a.token_enc) or "")
    except (GitHubError, httpx.HTTPError) as e:
        a.status, a.last_error, a.checked_at = "error", str(e), datetime.now(UTC)
    await db.flush()
    return a


async def owners(db: AsyncSession, a: GithubAccount) -> list[dict[str, Any]]:
    """Where the token can create a repository: the user itself and its organisations."""
    token = decrypt_secret(a.token_enc)
    out = [{"login": a.login, "type": "user"}]
    try:
        orgs, _ = await call("GET", "/user/orgs", token, params={"per_page": 100})
        out += [{"login": o.get("login"), "type": "org"} for o in orgs or []]
    except GitHubError:
        pass  # a fine-grained token without organisation access cannot list them
    return out


async def token_for(db: AsyncSession, repo: str | None = None) -> str | None:
    """The token to use for a repository: the account it is stored under, else the first account."""
    accounts = await list_accounts(db)
    if repo:
        with tenancy.unrestricted():
            r = (await db.execute(select(Repo).where(Repo.full_name.ilike(repo)))).scalars().first()
        if r is not None and r.account_id:
            for a in accounts:
                if a.id == r.account_id:
                    return decrypt_secret(a.token_enc)
        owner = repo.split("/", 1)[0].lower()
        for a in accounts:
            if (a.login or "").lower() == owner:
                return decrypt_secret(a.token_enc)
    return decrypt_secret(accounts[0].token_enc) if accounts else None


# ------------------------------------------------------------------ remote repositories
async def remote_repos(db: AsyncSession, account: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    accounts = [await get_account(db, account)] if account else await list_accounts(db)
    if not accounts:
        raise GitHubError("connect GitHub first: Integrations → GitHub, with a personal access token")
    with tenancy.unrestricted():
        known = {r.full_name.lower() for r in (await db.execute(select(Repo))).scalars()}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in accounts:
        token = decrypt_secret(a.token_enc)
        for page in range(1, 6):
            items, _ = await call("GET", "/user/repos", token, params={
                "per_page": 100, "page": page, "sort": "pushed",
                "affiliation": "owner,collaborator,organization_member"})
            for r in items or []:
                fn = r.get("full_name") or ""
                if fn.lower() in seen:
                    continue
                seen.add(fn.lower())
                out.append({"full_name": fn, "private": bool(r.get("private")),
                            "default_branch": r.get("default_branch"), "description": r.get("description") or "",
                            "pushed_at": r.get("pushed_at"), "html_url": r.get("html_url"),
                            "archived": bool(r.get("archived")), "account_id": a.id,
                            "in_library": fn.lower() in known})
            if len(items or []) < 100:
                break
    if query:
        q = query.lower()
        out = [r for r in out if q in r["full_name"].lower() or q in r["description"].lower()]
    return out


# ------------------------------------------------------------------ library
class AccessIn(BaseModel):
    customer_id: str
    can_deploy: bool = True
    ai_read: bool = False


class RepoIn(BaseModel):
    kind: Literal["product", "customer", "tooling"] = "product"
    owner_customer_id: str | None = None
    notes: str = Field(default="", max_length=5000)


class ImportIn(RepoIn):
    full_names: list[str] = Field(min_length=1, max_length=100)
    account_id: str | None = None
    customers: list[AccessIn] = Field(default_factory=list)


class CreateIn(RepoIn):
    account_id: str | None = None
    owner: str | None = None  # user login or organisation; empty = the token's own user
    name: str = Field(pattern=NAME_RE.pattern)
    description: str = Field(default="", max_length=350)
    private: bool = True
    auto_init: bool = True
    gitignore: str | None = Field(default=None, max_length=50)
    test_branch: bool = True
    customers: list[AccessIn] = Field(default_factory=list)


def repo_out(r: Repo, deployments: int = 0, names: dict[str, str] | None = None) -> dict[str, Any]:
    names = names or {}
    ids = tenancy.allowed()
    access = [a for a in r.access if ids is None or a.customer_id in ids]
    return {"id": r.id, "full_name": r.full_name, "account_id": r.account_id, "kind": r.kind,
            "owner_customer_id": r.owner_customer_id, "owner_customer": names.get(r.owner_customer_id or ""),
            "description": r.description, "private": r.private, "default_branch": r.default_branch,
            "html_url": r.html_url or f"https://github.com/{r.full_name}", "notes": r.notes,
            "access": [{"customer_id": a.customer_id, "customer": names.get(a.customer_id), "can_deploy": a.can_deploy,
                        "ai_read": a.ai_read} for a in access],
            "deployments": deployments, "created_by": r.created_by, "created_at": r.created_at.isoformat()}


async def _names(db: AsyncSession) -> dict[str, str]:
    return {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}


async def library(db: AsyncSession) -> list[dict[str, Any]]:
    repos = list((await db.execute(select(Repo).order_by(Repo.full_name))).scalars())
    deps = list((await db.execute(select(Deployment.repo))).scalars())
    count: dict[str, int] = {}
    for fn in deps:
        count[fn.lower()] = count.get(fn.lower(), 0) + 1
    names = await _names(db)
    return [repo_out(r, count.get(r.full_name.lower(), 0), names) for r in repos]


async def repo_view(db: AsyncSession, r: Repo) -> dict[str, Any]:
    deps = list((await db.execute(select(Deployment.id).where(Deployment.repo.ilike(r.full_name)))).scalars())
    return repo_out(r, len(deps), await _names(db))


async def get_repo(db: AsyncSession, ref: str) -> Repo:
    r = await db.get(Repo, ref)
    if r is None:
        r = (await db.execute(select(Repo).where(Repo.full_name.ilike(ref)))).scalars().first()
    if r is None:
        raise GitHubError("repository not in the software library")
    return r


async def _validate(db: AsyncSession, body: RepoIn, customers: list[AccessIn]) -> None:
    if body.kind == "customer" and not body.owner_customer_id:
        raise GitHubError("a customer repository needs the customer it belongs to")
    ids = {c.customer_id for c in customers} | ({body.owner_customer_id} if body.owner_customer_id else set())
    for cid in ids:
        if await db.get(Environment, cid) is None:
            raise GitHubError(f"customer {cid} not found")
    if body.kind == "customer" and any(c.customer_id != body.owner_customer_id for c in customers):
        raise GitHubError("a customer repository can only be linked to its own customer")


def _set_access(r: Repo, customers: list[AccessIn]) -> None:
    want = {c.customer_id: c for c in customers}
    if r.kind == "customer" and r.owner_customer_id and r.owner_customer_id not in want:
        want[r.owner_customer_id] = AccessIn(customer_id=r.owner_customer_id)
    keep = []
    for a in list(r.access):
        c = want.pop(a.customer_id, None)
        if c is None:
            continue
        a.can_deploy, a.ai_read = c.can_deploy, c.ai_read
        keep.append(a)
    for c in want.values():
        keep.append(RepoAccess(customer_id=c.customer_id, can_deploy=c.can_deploy, ai_read=c.ai_read))
    r.access = keep


async def import_repos(db: AsyncSession, body: ImportIn, user: str | None = None) -> list[Repo]:
    await _validate(db, body, body.customers)
    remote = {x["full_name"].lower(): x for x in await remote_repos(db, body.account_id)}
    out = []
    for fn in body.full_names:
        info = remote.get(fn.lower())
        if info is None:
            raise GitHubError(f"{fn} is not visible to the connected token")
        with tenancy.unrestricted():
            r = (await db.execute(select(Repo).where(Repo.full_name.ilike(fn)))).scalars().first()
        if r is None:
            r = Repo(full_name=info["full_name"], created_by=user, access=[])
            db.add(r)
        r.account_id = info["account_id"]
        r.description, r.private = info["description"], info["private"]
        r.default_branch, r.html_url = info["default_branch"] or "main", info["html_url"]
        r.kind, r.owner_customer_id, r.notes = body.kind, body.owner_customer_id, body.notes
        _set_access(r, body.customers)
        out.append(r)
    await db.flush()
    return out


async def create_repo(db: AsyncSession, body: CreateIn, user: str | None = None) -> Repo:
    """Create a new repository on GitHub and add it to the library."""
    await _validate(db, body, body.customers)
    a = await get_account(db, body.account_id)
    token = decrypt_secret(a.token_enc)
    payload: dict[str, Any] = {"name": body.name, "description": body.description, "private": body.private,
                               "auto_init": body.auto_init}
    if body.gitignore:
        payload["gitignore_template"] = body.gitignore
    owner = (body.owner or "").strip()
    path = "/user/repos" if not owner or owner.lower() == (a.login or "").lower() else f"/orgs/{owner}/repos"
    data, _ = await call("POST", path, token, json=payload)
    fn = data.get("full_name") or f"{owner or a.login}/{body.name}"
    default = data.get("default_branch") or "main"
    if body.test_branch and body.auto_init:
        try:
            ref, _ = await call("GET", f"/repos/{fn}/git/ref/heads/{default}", token)
            await call("POST", f"/repos/{fn}/git/refs", token,
                       json={"ref": "refs/heads/test", "sha": ((ref or {}).get("object") or {}).get("sha")})
        except GitHubError:
            pass  # the repository exists; the test branch can be made later
    r = Repo(full_name=fn, account_id=a.id, kind=body.kind, owner_customer_id=body.owner_customer_id,
             description=body.description, private=bool(data.get("private", body.private)), default_branch=default,
             html_url=data.get("html_url"), notes=body.notes, created_by=user, access=[])
    _set_access(r, body.customers)
    db.add(r)
    await db.flush()
    return r


async def update_repo(db: AsyncSession, r: Repo, body: RepoIn) -> Repo:
    await _validate(db, body, [])
    if body.kind == "customer":
        # a customer repository belongs to one customer: the links to other customers go
        r.access = [a for a in r.access if a.customer_id == body.owner_customer_id]
    r.kind, r.owner_customer_id, r.notes = body.kind, body.owner_customer_id, body.notes
    _set_access(r, [AccessIn(customer_id=a.customer_id, can_deploy=a.can_deploy, ai_read=a.ai_read)
                    for a in r.access])
    await db.flush()
    return r


async def set_access(db: AsyncSession, r: Repo, customers: list[AccessIn]) -> Repo:
    body = RepoIn(kind=r.kind, owner_customer_id=r.owner_customer_id)  # type: ignore[arg-type]
    await _validate(db, body, customers)
    _set_access(r, customers)
    await db.flush()
    return r


async def link(db: AsyncSession, r: Repo, customer_id: str, *, can_deploy: bool = True, ai_read: bool = False,
               remove: bool = False) -> Repo:
    current = [AccessIn(customer_id=a.customer_id, can_deploy=a.can_deploy, ai_read=a.ai_read) for a in r.access
               if a.customer_id != customer_id]
    if not remove:
        current.append(AccessIn(customer_id=customer_id, can_deploy=can_deploy, ai_read=ai_read))
    return await set_access(db, r, current)


# ------------------------------------------------------------------ rights on deploy
async def check_deploy(db: AsyncSession, repo: str, customer_id: str | None, *, user: str | None = None) -> None:
    """May ``repo`` be deployed onto a machine of ``customer_id``? Adds an unknown repository to the library,
    linked to that customer, so deployments made before v0.27.0 keep working."""
    with tenancy.unrestricted():
        r = (await db.execute(select(Repo).where(Repo.full_name.ilike(repo)))).scalars().first()
    if r is None:
        if customer_id is None:
            return
        r = Repo(full_name=repo, kind="product", created_by=user,
                 access=[RepoAccess(customer_id=customer_id, can_deploy=True)])
        with tenancy.unrestricted():
            db.add(r)
            await db.flush()
        return
    if customer_id is None:
        return
    if r.kind == "customer" and r.owner_customer_id and r.owner_customer_id != customer_id:
        raise GitHubError(f"{r.full_name} is a repository of another customer and cannot be deployed here")
    a = next((x for x in r.access if x.customer_id == customer_id), None)
    if a is None or not a.can_deploy:
        raise GitHubError(f"{r.full_name} is not released for this customer: link it in Software → Repositories")
