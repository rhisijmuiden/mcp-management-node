"""Deploy own GitHub repositories natively into LXC containers (no Docker).

A deployment ties a repository to a container: the production or test branch is cloned into a
directory in the container and the deploy commands run there (as root, with ``pct exec`` from the
node). Optional: a snapshot first with roll back on failure, a systemd service to restart, and
automatic deploys on push (GitHub webhook) or by polling the branch every few minutes.

The GitHub token is stored encrypted and reaches the container only through stdin into a file
that is removed when the deploy ends; it never appears in a command line or in the job log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import shlex
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Deployment, DeploymentRun, Node, NodeJob
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services import agent_hub, events, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.deploy")

API_BASE = "https://api.github.com"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,200}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@._-]{1,100}$")
FORBIDDEN_PATHS = {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys",
                   "/usr", "/var", "/opt", "/srv", "/home", "/tmp"}
KEEP_SNAPSHOTS = 3

PRESETS: dict[str, dict[str, str]] = {
    "script": {"setup": "", "deploy": "bash ./deploy/install.sh"},
    "python": {"setup": "apt-get update && apt-get install -y python3-venv python3-pip",
               "deploy": "python3 -m venv .venv\n.venv/bin/pip install --upgrade pip\n"
                         "if [ -f requirements.txt ]; then .venv/bin/pip install -r requirements.txt; "
                         "else .venv/bin/pip install .; fi"},
    "node": {"setup": "apt-get update && apt-get install -y nodejs npm", "deploy": "npm ci\nnpm run build"},
    "static": {"setup": "apt-get update && apt-get install -y nginx rsync",
               "deploy": "rsync -a --delete --exclude=.git ./ /var/www/html/"},
    "custom": {"setup": "", "deploy": ""},
}


class GitHubError(Exception):
    pass


# ------------------------------------------------------------------ GitHub API
async def _token(db: AsyncSession) -> str | None:
    cfg = await get_setting(db, "github")
    return decrypt_secret(cfg.get("token_enc"))


async def github_get(path: str, token: str | None, params: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "mcp-management-node"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=API_BASE, timeout=20) as c:
        r = await c.get(path, headers=headers, params=params)
    if r.status_code == 401:
        raise GitHubError("GitHub refused the token (401)")
    if r.status_code == 404:
        raise GitHubError("not found on GitHub (or the token has no access)")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub answered HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


async def github_status(db: AsyncSession) -> dict[str, Any]:
    cfg = await get_setting(db, "github")
    return {"configured": bool(cfg.get("token_enc")), "login": cfg.get("login"), "checked_at": cfg.get("checked_at")}


async def set_github_token(db: AsyncSession, token: str | None) -> dict[str, Any]:
    if not token:
        await set_setting(db, "github", {"token_enc": None, "login": None, "checked_at": None})
        return await github_status(db)
    me = await github_get("/user", token)
    await set_setting(db, "github", {"token_enc": encrypt_secret(token), "login": me.get("login"),
                                     "checked_at": datetime.now(UTC).isoformat()})
    return await github_status(db)


async def list_repos(db: AsyncSession, query: str | None = None) -> list[dict[str, Any]]:
    token = await _token(db)
    if not token:
        raise GitHubError("connect GitHub first (a personal access token with read access to your repositories)")
    out: list[dict[str, Any]] = []
    for page in range(1, 6):
        items = await github_get("/user/repos", token, {"per_page": 100, "page": page, "sort": "pushed",
                                                         "affiliation": "owner,collaborator,organization_member"})
        for r in items or []:
            out.append({"full_name": r.get("full_name"), "private": bool(r.get("private")),
                        "default_branch": r.get("default_branch"), "description": r.get("description") or "",
                        "pushed_at": r.get("pushed_at"), "html_url": r.get("html_url"),
                        "archived": bool(r.get("archived"))})
        if len(items or []) < 100:
            break
    if query:
        q = query.lower()
        out = [r for r in out if q in (r["full_name"] or "").lower() or q in r["description"].lower()]
    return out


async def list_branches(db: AsyncSession, repo: str) -> list[str]:
    if not REPO_RE.match(repo):
        raise GitHubError("repository looks like owner/name")
    items = await github_get(f"/repos/{repo}/branches", await _token(db), {"per_page": 100})
    return [b.get("name") for b in items or []]


async def branch_head(db: AsyncSession, repo: str, branch: str) -> str | None:
    data = await github_get(f"/repos/{repo}/branches/{branch}", await _token(db))
    return ((data or {}).get("commit") or {}).get("sha")


async def detect(db: AsyncSession, repo: str, branch: str) -> dict[str, Any]:
    """Suggest a preset from the files in the repository root."""
    if not REPO_RE.match(repo) or not BRANCH_RE.match(branch) or ".." in branch:
        raise GitHubError("invalid repository or branch")
    tree = await github_get(f"/repos/{repo}/git/trees/{branch}", await _token(db))
    names = {e.get("path") for e in (tree or {}).get("tree") or []}
    preset = "custom"
    deploy_cmd = None
    if "deploy" in names:
        sub = await github_get(f"/repos/{repo}/contents/deploy", await _token(db), {"ref": branch})
        files = {e.get("name") for e in sub or [] if isinstance(e, dict)}
        if "install.sh" in files:
            preset, deploy_cmd = "script", "bash ./deploy/install.sh"
        elif "deploy.sh" in files:
            preset, deploy_cmd = "script", "bash ./deploy/deploy.sh"
    if preset == "custom":
        if "install.sh" in names:
            preset, deploy_cmd = "script", "bash ./install.sh"
        elif "package.json" in names:
            preset = "node"
        elif names & {"pyproject.toml", "requirements.txt", "setup.py"}:
            preset = "python"
        elif "index.html" in names:
            preset = "static"
    p = PRESETS[preset]
    return {"preset": preset, "setup_cmd": p["setup"], "deploy_cmd": deploy_cmd or p["deploy"],
            "files": sorted(n for n in names if n)[:100]}


# ------------------------------------------------------------------ deployments
class DeploymentIn(BaseModel):
    name: str = Field(pattern=NAME_RE.pattern)
    repo: str = Field(pattern=REPO_RE.pattern)
    channel: Literal["prod", "test"] = "prod"
    branch_prod: str = Field(default="main", pattern=BRANCH_RE.pattern)
    branch_test: str = Field(default="test", pattern=BRANCH_RE.pattern)
    node_id: str
    vmid: int = Field(ge=100, le=999_999_999)
    path: str | None = Field(default=None, pattern=PATH_RE.pattern)
    preset: Literal["script", "python", "node", "static", "custom"] = "script"
    setup_cmd: str = Field(default="", max_length=20000)
    deploy_cmd: str = Field(default="", max_length=20000)
    service: str | None = Field(default=None, pattern=SERVICE_RE.pattern)
    env: str | None = Field(default=None, max_length=50000)  # KEY=value lines; None keeps the stored value
    auto_deploy: Literal["off", "webhook", "poll"] = "off"
    snapshot: bool = True
    rollback: bool = True
    mail_to: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("branch_prod", "branch_test")
    @classmethod
    def _branch(cls, v: str) -> str:
        if ".." in v or v.startswith(("/", "-")) or v.endswith("/"):
            raise ValueError("invalid branch name")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if ".." in v:
            raise ValueError("the directory may not contain '..'")
        v = v.rstrip("/") or "/"
        if v in FORBIDDEN_PATHS:
            raise ValueError("choose a directory of its own, e.g. /opt/<name>")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: str | None) -> str | None:
        if v is None:
            return v
        for line in v.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", s):
                raise ValueError(f"environment lines look like KEY=value (got {s[:40]!r})")
        return v


def branch_of(d: Deployment) -> str:
    return d.branch_test if d.channel == "test" else d.branch_prod


def deployment_out(d: Deployment, base_url: str = "") -> dict[str, Any]:
    return {"id": d.id, "name": d.name, "repo": d.repo, "channel": d.channel, "branch_prod": d.branch_prod,
            "branch_test": d.branch_test, "branch": branch_of(d), "node_id": d.node_id, "vmid": d.vmid,
            "path": d.path, "preset": d.preset, "setup_cmd": d.setup_cmd, "deploy_cmd": d.deploy_cmd,
            "service": d.service, "has_env": bool(d.env_enc), "auto_deploy": d.auto_deploy, "snapshot": d.snapshot,
            "rollback": d.rollback, "mail_to": d.mail_to or [], "last_sha": d.last_sha, "last_status": d.last_status,
            "last_run_at": d.last_run_at.isoformat() if d.last_run_at else None,
            "webhook_url": f"{base_url.rstrip('/')}/api/hooks/github/{d.id}" if base_url else f"/api/hooks/github/{d.id}",
            "created_by": d.created_by, "created_at": d.created_at.isoformat()}


def run_out(r: DeploymentRun) -> dict[str, Any]:
    return {"id": r.id, "deployment_id": r.deployment_id, "status": r.status, "trigger": r.trigger,
            "branch": r.branch, "sha": r.sha, "previous_sha": r.previous_sha, "requested_by": r.requested_by,
            "job_id": r.job_id, "snapshot": r.snapshot, "rolled_back": r.rolled_back, "error": r.error,
            "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None}


async def get_deployment(db: AsyncSession, ref: str) -> Deployment:
    d = await db.get(Deployment, ref)
    if d is None:
        d = (await db.execute(select(Deployment).where(Deployment.name == ref.lower()))).scalars().first()
    if d is None:
        raise NodeError("deployment not found")
    return d


async def list_deployments(db: AsyncSession) -> list[Deployment]:
    return list((await db.execute(select(Deployment).order_by(Deployment.name))).scalars())


async def _check_target(db: AsyncSession, node_id: str, vmid: int) -> None:
    node = await nodes.get_node(db, node_id)
    try:
        _, g = await guests.find_guest(db, vmid, node.id)
    except NodeError as e:
        raise NodeError(f"container {vmid} not found on {node.name}") from e
    if g["type"] != "lxc":
        raise NodeError("deployments go into LXC containers (a VM is not supported)")


async def save_deployment(db: AsyncSession, body: DeploymentIn, d: Deployment | None = None,
                          user: str | None = None, *, check_target: bool = True) -> Deployment:
    if check_target:
        await _check_target(db, body.node_id, body.vmid)
    clash = (await db.execute(select(Deployment).where(Deployment.name == body.name))).scalars().first()
    if clash is not None and (d is None or clash.id != d.id):
        raise NodeError("a deployment with this name already exists")
    if not body.deploy_cmd.strip() and not body.service:
        raise NodeError("give a deploy command (or at least a service to restart)")
    node = await nodes.get_node(db, body.node_id)
    if d is None:
        d = Deployment(name=body.name, repo=body.repo, vmid=body.vmid, path=body.path or f"/opt/{body.name}",
                       created_by=user, webhook_secret_enc=encrypt_secret(secrets.token_hex(24)))
        db.add(d)
    d.name, d.repo, d.channel = body.name, body.repo, body.channel
    d.branch_prod, d.branch_test = body.branch_prod, body.branch_test
    d.node_id, d.vmid, d.path = node.id, body.vmid, body.path or d.path or f"/opt/{body.name}"
    d.preset, d.setup_cmd, d.deploy_cmd = body.preset, body.setup_cmd.strip(), body.deploy_cmd.strip()
    d.service = body.service.removesuffix(".service") if body.service else None
    if body.env is not None:
        d.env_enc = encrypt_secret(body.env) if body.env.strip() else None
    d.auto_deploy, d.snapshot, d.rollback, d.mail_to = body.auto_deploy, body.snapshot, body.rollback, body.mail_to
    await db.flush()
    return d


def webhook_secret(d: Deployment) -> str:
    return decrypt_secret(d.webhook_secret_enc) or ""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header.removeprefix("sha256="))


# ------------------------------------------------------------------ the deploy script
def container_script(d: Deployment, branch: str, *, run_setup: bool) -> str:
    """Runs inside the container (bash)."""
    q = shlex.quote
    svc = d.service
    lines = [
        "set -euo pipefail",
        "trap 'rm -f /root/.mmn-git-token' EXIT",
        "export DEBIAN_FRONTEND=noninteractive HOME=/root",
        f"REPO={q(d.repo)}; BRANCH={q(branch)}; DIR={q(d.path)}; NAME={q(d.name)}",
        'if ! command -v git >/dev/null 2>&1; then echo "Installing git"; apt-get update -qq; '
        "apt-get install -y -qq git ca-certificates >/dev/null; fi",
        "CRED='!f() { test \"$1\" = get || exit 0; echo username=x-access-token; "
        "echo \"password=$(cat /root/.mmn-git-token)\"; }; f'",
        'G() { if [ -s /root/.mmn-git-token ]; then git -c credential.helper= -c "credential.helper=$CRED" "$@"; '
        'else git "$@"; fi; }',
        "FIRST=0",
        'if [ -d "$DIR/.git" ]; then',
        '  echo "Fetching $REPO ($BRANCH)"',
        '  G -C "$DIR" remote set-url origin "https://github.com/$REPO.git"',
        '  G -C "$DIR" fetch --prune origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"',
        "else",
        '  if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then echo "$DIR exists and is not a git '
        'checkout; empty it or choose another directory"; exit 1; fi',
        '  FIRST=1; mkdir -p "$(dirname "$DIR")"',
        '  echo "Cloning $REPO ($BRANCH) into $DIR"',
        '  G clone --branch "$BRANCH" "https://github.com/$REPO.git" "$DIR"',
        "fi",
        'PREV=$(git -C "$DIR" rev-parse -q --verify HEAD 2>/dev/null || true)',
        'echo "MMN_PREV=$PREV"',
        'git -C "$DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"',
        'git -C "$DIR" reset -q --hard "origin/$BRANCH"',
        'SHA=$(git -C "$DIR" rev-parse HEAD)',
        'echo "MMN_SHA=$SHA"',
        "git -C \"$DIR\" log -1 --format='Commit %h: %s (%an, %cr)'",
        'cd "$DIR"',
        f'export MMN_DEPLOY="$NAME" MMN_BRANCH="$BRANCH" MMN_CHANNEL={q(d.channel)} MMN_SHA="$SHA" '
        'MMN_FIRST_DEPLOY="$FIRST"',
        'ENVF="/etc/mmn-deploy/$NAME.env"',
        'if [ -f "$ENVF" ]; then set -a; . "$ENVF"; set +a; fi',
    ]
    if d.setup_cmd:
        cond = "true" if run_setup else '[ "$FIRST" = 1 ]'
        lines += [f"if {cond}; then", '  echo "== setup"', f"  bash -euo pipefail -c {q(d.setup_cmd)}", "fi"]
    if d.deploy_cmd:
        lines += ['echo "== deploy"', f"bash -euo pipefail -c {q(d.deploy_cmd)}"]
    if svc:
        lines += [
            f'echo "== restarting {svc}"',
            "systemctl daemon-reload",
            f"systemctl restart {q(svc)}",
            "sleep 3",
            f"if ! systemctl is-active --quiet {q(svc)}; then systemctl status --no-pager {q(svc)} || true; "
            f"journalctl -u {q(svc)} -n 40 --no-pager || true; echo 'the service did not start'; exit 1; fi",
        ]
    lines.append('echo "Deployed $SHA"')
    return "\n".join(lines) + "\n"


def node_script(d: Deployment, branch: str, *, run_setup: bool) -> str:
    """Runs on the Proxmox node: hands token and environment to the container through stdin and
    runs the container script."""
    vmid = int(d.vmid)
    inner = container_script(d, branch, run_setup=run_setup)
    name = shlex.quote(d.name)
    return f"""set -euo pipefail
if ! pct status {vmid} | grep -q 'status: running'; then echo "Container {vmid} is not running"; exit 1; fi
if [ -n "${{MMN_GIT_TOKEN:-}}" ]; then printf '%s' "$MMN_GIT_TOKEN" | pct exec {vmid} -- sh -c 'umask 077; cat > /root/.mmn-git-token'; fi
if [ -n "${{MMN_ENV_B64:-}}" ]; then printf '%s' "$MMN_ENV_B64" | pct exec {vmid} -- sh -c 'umask 077; mkdir -p /etc/mmn-deploy; base64 -d > /etc/mmn-deploy/'{name}'.env'; fi
pct exec {vmid} -- bash -c {shlex.quote(inner)}
"""


# ------------------------------------------------------------------ running
async def start_run(deployment_id: str, *, trigger: str = "manual", requested_by: str | None = None,
                    source: str = "ui", run_setup: bool = False) -> DeploymentRun:
    async with sessionmaker()() as db:
        d = await get_deployment(db, deployment_id)
        if not d.node_id:
            raise NodeError("the node of this deployment was removed; choose a container again")
        agent_hub.get(d.node_id)
        running = (await db.execute(select(DeploymentRun).where(DeploymentRun.deployment_id == d.id,
                                                                DeploymentRun.status == "running"))).scalars().first()
        if running is not None:
            raise NodeError("a deployment of this app is still running")
        branch = branch_of(d)
        job = await nodes.create_job(d.node_id, "deploy.run", {"deployment": d.name, "repo": d.repo, "branch": branch,
                                                               "vmid": d.vmid}, source=source,
                                     requested_by=requested_by, exclusive=False, busy_key=f"guest:{d.vmid}")
        run = DeploymentRun(deployment_id=d.id, trigger=trigger, branch=branch, requested_by=requested_by,
                            job_id=job.id, previous_sha=d.last_sha)
        db.add(run)
        d.last_status, d.last_run_at = "running", datetime.now(UTC)
        await db.commit()
        run_id = run.id
    agent_hub.spawn(_pipeline(run_id, run_setup, source, requested_by))
    events.publish("deploy.run", {"run_id": run_id, "deployment_id": deployment_id, "started": True})
    return run


async def _pipeline(run_id: str, run_setup: bool, source: str, requested_by: str | None) -> None:
    async with sessionmaker()() as db:
        run = await db.get(DeploymentRun, run_id)
        d = await db.get(Deployment, run.deployment_id)
        node_id, job_id, branch = d.node_id, run.job_id, run.branch
        token = await _token(db)
        env_text = decrypt_secret(d.env_enc) or ""
        script = node_script(d, branch, run_setup=run_setup)
        do_snap, do_rb = d.snapshot, d.rollback
    say = lambda line: agent_hub.add_line(node_id, job_id, line)  # noqa: E731
    who = {"source": source, "requested_by": requested_by}
    snap, sha, error, rolled_back = None, None, None, False
    node = g = None
    try:
        async with sessionmaker()() as db:
            node, g = await guests.find_guest(db, d.vmid, node_id)
        say(f"Deploying {d.repo} ({branch}) into container {d.vmid} {g['name']} on {node.name}")
        if do_snap:
            snap = guests.snapshot_name("mmn-deploy")
            say(f"Taking snapshot {snap}")
            sj = await guests.create_snapshot(node, g, snap, f"Before deploying {d.name} ({branch})", parent=job_id,
                                              **who)
            status, _, err = await guests.run_and_wait(sj, 1800)
            if status != "success":
                raise NodeError(f"snapshot failed: {err}")
        env = {}
        if token:
            env["MMN_GIT_TOKEN"] = token
        if env_text.strip():
            env["MMN_ENV_B64"] = base64.b64encode(env_text.encode()).decode()
        sub = await guests.script(node.id, "deploy.script", script, env=env, timeout=3600, parent=job_id, **who)
        status, _, err = await guests.run_and_wait(sub, 3900)
        async with sessionmaker()() as db:
            out = (await db.get(NodeJob, sub.id)).output or ""
        m = re.findall(r"^MMN_SHA=([0-9a-f]{7,64})$", out, re.M)
        sha = m[-1] if m else None
        if status != "success":
            raise NodeError(err or "the deploy script failed")
    except (NodeError, AgentError) as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("deploy run %s", run_id)
        error = f"{type(e).__name__}: {e}"
    if error:
        say(f"ERROR: {error}")
        if snap and do_rb and node is not None:
            say(f"Rolling the container back to {snap}")
            try:
                rb = await guests.rollback_snapshot(node, g, snap, start=True, parent=job_id, **who)
                rstatus, _, rerr = await guests.run_and_wait(rb, 1800)
                rolled_back = rstatus == "success"
                if not rolled_back:
                    say(f"Roll back failed: {rerr}")
            except (NodeError, AgentError) as e:
                say(f"Roll back failed: {e}")
    elif snap and node is not None:
        from mmn.services.tasks import _prune_snapshots

        await _prune_snapshots(node, g, "mmn-deploy", KEEP_SNAPSHOTS, who)
    status = "failed" if error else "success"
    async with sessionmaker()() as db:
        run = await db.get(DeploymentRun, run_id)
        dep = await db.get(Deployment, run.deployment_id)
        run.status, run.sha, run.snapshot, run.rolled_back = status, sha, snap, rolled_back
        run.error, run.finished_at = error, datetime.now(UTC)
        dep.last_status = status
        if status == "success" and sha:
            dep.last_sha = sha
        await db.commit()
        mail_to = list(dep.mail_to or [])
    await agent_hub.finish_job(job_id, status, {"sha": sha, "snapshot": snap, "rolled_back": rolled_back}, error)
    events.publish("deploy.run", {"run_id": run_id, "done": True, "status": status})
    if mail_to:
        await _mail(run_id, mail_to)


async def _mail(run_id: str, to: list[str]) -> None:
    from mmn.i18n import t
    from mmn.services.mailer import MailContent, try_send_mail

    async with sessionmaker()() as db:
        run = await db.get(DeploymentRun, run_id)
        d = await db.get(Deployment, run.deployment_id)
        node = await db.get(Node, d.node_id) if d.node_id else None
        general = await get_setting(db, "general")
        lang = general["default_language"]
        base = (general.get("base_url") or "").rstrip("/")
        paras = [t(lang, "deploy.body", repo=d.repo, branch=run.branch, vmid=d.vmid, node=node.name if node else "-",
                   status=run.status)]
        if run.error:
            paras.append(run.error[:500])
        if run.rolled_back:
            paras.append(t(lang, "deploy.rolled_back"))
        await try_send_mail(db, to, MailContent(
            subject=t(lang, f"deploy.subject_{run.status}", name=d.name, branch=run.branch, sha=(run.sha or "")[:7]),
            title=t(lang, "deploy.title", name=d.name), paragraphs=paras, muted=[],
            button_url=f"{base}/deployments/{d.id}" if base else None,
            button_label=t(lang, "deploy.button") if base else None), lang)


async def list_runs(db: AsyncSession, deployment_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    q = select(DeploymentRun).order_by(DeploymentRun.started_at.desc()).limit(min(limit, 200))
    if deployment_id:
        q = q.where(DeploymentRun.deployment_id == deployment_id)
    return [run_out(r) for r in (await db.execute(q)).scalars()]


# ------------------------------------------------------------------ automatic deploys
async def handle_webhook(deployment_id: str, event: str | None, signature: str | None, body: bytes,
                         payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with sessionmaker()() as db:
        d = await db.get(Deployment, deployment_id)
        if d is None:
            return 404, {"detail": "unknown deployment"}
        if not verify_signature(webhook_secret(d), body, signature):
            return 401, {"detail": "bad signature"}
        if event == "ping":
            return 200, {"ok": True, "pong": True}
        if event != "push":
            return 202, {"ignored": f"event {event}"}
        if d.auto_deploy != "webhook":
            return 202, {"ignored": "automatic deploys are off for this deployment"}
        branch = branch_of(d)
        if payload.get("ref") != f"refs/heads/{branch}":
            return 202, {"ignored": f"push to {payload.get('ref')}, deploying {branch}"}
        if payload.get("deleted"):
            return 202, {"ignored": "branch deleted"}
        if (payload.get("repository") or {}).get("full_name", d.repo).lower() != d.repo.lower():
            return 400, {"detail": "repository does not match"}
        dep_id = d.id
    try:
        run = await start_run(dep_id, trigger="webhook", requested_by="github", source="system")
    except (NodeError, AgentError) as e:
        return 409, {"detail": str(e)}
    return 202, {"run_id": run.id}


async def poll_all() -> None:
    """Deploy when the branch of a 'poll' deployment has a new commit."""
    async with sessionmaker()() as db:
        items = [d for d in await list_deployments(db) if d.auto_deploy == "poll"]
        for d in items:
            if not d.node_id or not agent_hub.is_online(d.node_id) or d.last_status == "running":
                continue
            try:
                sha = await branch_head(db, d.repo, branch_of(d))
            except (GitHubError, httpx.HTTPError) as e:
                log.info("poll %s: %s", d.name, e)
                continue
            if sha and sha != d.last_sha:
                if d.last_status == "failed":
                    last = (await db.execute(select(DeploymentRun).where(DeploymentRun.deployment_id == d.id)
                                             .order_by(DeploymentRun.started_at.desc()))).scalars().first()
                    if last is not None and last.sha == sha:
                        continue  # this commit failed already; wait for the next one
                try:
                    await start_run(d.id, trigger="poll", requested_by="poll", source="system")
                except (NodeError, AgentError) as e:
                    log.info("poll %s: %s", d.name, e)


# ------------------------------------------------------------------ new container + deployment
async def create_with_container(db: AsyncSession, dep: DeploymentIn, ct: guests.CreateCtIn, *, source: str = "ui",
                                requested_by: str | None = None) -> tuple[Deployment, NodeJob]:
    """Create a container and, once it runs, the first deployment into it."""
    if not ct.start:
        ct.start = True
    job = await guests.create_ct(db, ct, source=source, requested_by=requested_by)
    vmid = int(job.params.get("vmid"))
    body = dep.model_copy(update={"node_id": ct.node_id, "vmid": vmid})
    d = await save_deployment(db, body, user=requested_by, check_target=False)
    await db.commit()
    agent_hub.spawn(_after_create(job.id, d.id, source, requested_by))
    return d, job


async def _after_create(job_id: str, deployment_id: str, source: str, requested_by: str | None) -> None:
    import asyncio

    status, _, _ = await agent_hub.wait_job(job_id, 3600)
    if status != "success":
        return
    await asyncio.sleep(5)  # let the container's network come up
    try:
        await start_run(deployment_id, trigger="create", requested_by=requested_by, source=source, run_setup=True)
    except (NodeError, AgentError) as e:
        log.warning("first deployment of %s: %s", deployment_id, e)
