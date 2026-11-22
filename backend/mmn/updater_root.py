"""Privileged helper, run as root by ``mcp-management-node-updater.service``.

It is started by a systemd path unit when the (unprivileged) app drops a request file in
``/var/lib/mcp-management-node/run``. It never trusts more than the request's *intent*:
the release is fetched again from the repository configured in the root-owned env file and
its SHA-256 is verified; web-server settings are validated again before they are written.

Usage::

    python -m mmn.updater_root run              # process pending requests (systemd)
    python -m mmn.updater_root render-nginx     # print nginx config from /etc/.../proxy.json
    python -m mmn.updater_root apply-nginx      # write + test + reload nginx from proxy.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP = "mcp-management-node"
INSTALL_DIR = Path(os.environ.get("MMN_INSTALL_DIR", f"/opt/{APP}"))
ETC_DIR = Path(f"/etc/{APP}")
ENV_FILE = ETC_DIR / "mmn.env"
PROXY_FILE = ETC_DIR / "proxy.json"
NGINX_SITE = Path(f"/etc/nginx/sites-available/{APP}.conf")
NGINX_LINK = Path(f"/etc/nginx/sites-enabled/{APP}.conf")
SERVICE = f"{APP}.service"
APP_USER = "mmn"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(-test\.\d+)?$")
KEEP_RELEASES = 3
MAX_REQUEST = 64 * 1024


# ------------------------------------------------------------------ utils
def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def set_env_value(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    shutil.copymode(ENV_FILE, tmp) if ENV_FILE.exists() else os.chmod(tmp, 0o640)
    if ENV_FILE.exists():
        st = ENV_FILE.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, ENV_FILE)


def data_dir() -> Path:
    return Path(read_env().get("MMN_DATA_DIR", f"/var/lib/{APP}"))


def run_dir() -> Path:
    return data_dir() / "run"


def _app_ids() -> tuple[int, int]:
    import pwd

    pw = pwd.getpwnam(APP_USER)
    return pw.pw_uid, pw.pw_gid


def write_status(name: str, data: dict[str, Any]) -> None:
    """Write a status file into the app-owned run dir without following symlinks."""
    d = run_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.{os.getpid()}.tmp"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    try:
        uid, gid = _app_ids()
        os.chown(tmp, uid, gid)
    except (KeyError, PermissionError):
        pass
    os.replace(tmp, d / name)


def read_request(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        raw = f.read(MAX_REQUEST + 1)
    path.unlink(missing_ok=True)
    if len(raw) > MAX_REQUEST:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def sh(cmd: list[str], *, env: dict[str, str] | None = None, user: str | None = None, check: bool = True,
       timeout: int = 1800) -> subprocess.CompletedProcess:
    if user:
        cmd = ["runuser", "-u", user, "--", *cmd]
    full_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    if env:
        full_env.update(env)
    res = subprocess.run(cmd, env=full_env, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {(res.stderr or res.stdout).strip()[-1500:]}")
    return res


def pg_env(env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    from urllib.parse import unquote, urlparse

    u = urlparse(env.get("MMN_DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://"))
    extra = {"PGPASSWORD": unquote(u.password or "")}
    args = ["-h", u.hostname or "127.0.0.1", "-p", str(u.port or 5432), "-U", unquote(u.username or "mmn"),
            "-d", (u.path or "/mmn").lstrip("/")]
    return args, extra


# ------------------------------------------------------------------ update
class Progress:
    def __init__(self, tag: str, channel: str) -> None:
        self.data: dict[str, Any] = {"state": "running", "tag": tag, "channel": channel,
                                     "started_at": datetime.now(UTC).isoformat(), "steps": []}
        self.flush()

    def step(self, msg: str) -> None:
        self.data["steps"].append({"at": datetime.now(UTC).isoformat(), "msg": msg})
        self.flush()

    def done(self, state: str, error: str | None = None, **extra: Any) -> None:
        self.data.update(state=state, error=error, finished_at=datetime.now(UTC).isoformat(), **extra)
        self.flush()

    def flush(self) -> None:
        write_status("update-status.json", self.data)


def _github(url: str, token: str | None, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP}-updater",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept != "application/octet-stream" or not token:
        return urllib.request.urlopen(req, timeout=60)  # noqa: S310 - fixed https host
    # Asset downloads redirect to storage with a signed URL; that host rejects our token header,
    # so follow the redirect ourselves without it.
    try:
        return _NO_REDIRECT.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not location or not location.startswith("https://"):
            raise
        plain = urllib.request.Request(location, headers={"User-Agent": f"{APP}-updater"})
        return urllib.request.urlopen(plain, timeout=60)  # noqa: S310


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def download_release(tag: str, dest: Path, env: dict[str, str]) -> Path:
    repo = env.get("MMN_GITHUB_REPO", "rhisijmuiden/mcp-management-node")
    token = env.get("MMN_GITHUB_TOKEN") or None
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        raise RuntimeError("invalid MMN_GITHUB_REPO")
    with _github(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token) as r:
        rel = json.load(r)
    name = f"{APP}-{tag}.tar.gz"
    assets = {a["name"]: a for a in rel.get("assets", [])}
    if name not in assets or f"{name}.sha256" not in assets:
        raise RuntimeError(f"release {tag} has no {name} (+ .sha256)")
    with _github(assets[f"{name}.sha256"]["url"], token, "application/octet-stream") as r:
        expected = r.read().decode().split()[0].strip().lower()
    out = dest / name
    h = hashlib.sha256()
    with _github(assets[name]["url"], token, "application/octet-stream") as r, out.open("wb") as f:
        for chunk in iter(lambda: r.read(1024 * 1024), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != expected:
        raise RuntimeError("checksum mismatch: download rejected")
    return out


def health_ok(timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8740/api/health", timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(2)
    return False


def install_release(tarball: Path, tag: str, p: Progress | None = None) -> Path:
    releases = INSTALL_DIR / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / f"{tag}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=releases))
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(tmp, filter="data")
    # The tarball contains one top-level folder.
    entries = [e for e in tmp.iterdir()]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
    root.rename(target)
    shutil.rmtree(tmp, ignore_errors=True)
    if p:
        p.step("installing Python packages")
    sh([sys.executable if Path(sys.executable).name.startswith("python3") else "python3", "-m", "venv",
        str(target / "venv")])
    pip = [str(target / "venv/bin/pip"), "install", "--no-cache-dir", "--disable-pip-version-check"]
    wheels = target / "wheels"
    if wheels.is_dir() and any(wheels.iterdir()):
        res = sh([*pip, "--no-index", "--find-links", str(wheels), str(target / "backend")], check=False)
        if res.returncode != 0:
            sh([*pip, str(target / "backend")])
    else:
        sh([*pip, str(target / "backend")])
    sh(["chown", "-R", "root:root", str(target)])
    sh(["chmod", "-R", "go-w", str(target)])
    return target


def switch_current(target: Path) -> Path | None:
    current = INSTALL_DIR / "current"
    previous = current.resolve() if current.exists() else None
    tmp = INSTALL_DIR / ".current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, current)
    return previous


def sync_system_files(release: Path) -> None:
    """Install systemd units shipped with the release and re-render nginx."""
    units = release / "deploy" / "systemd"
    changed = False
    if units.is_dir():
        for unit in units.iterdir():
            dst = Path("/etc/systemd/system") / unit.name
            if not dst.exists() or dst.read_bytes() != unit.read_bytes():
                shutil.copy2(unit, dst)
                changed = True
    if changed:
        sh(["systemctl", "daemon-reload"])
    if PROXY_FILE.exists():
        apply_nginx(json.loads(PROXY_FILE.read_text()), reload=True)


def prune_releases() -> None:
    releases = INSTALL_DIR / "releases"
    current = (INSTALL_DIR / "current").resolve()
    dirs = sorted((d for d in releases.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_RELEASES:]:
        if d != current:
            shutil.rmtree(d, ignore_errors=True)


def do_update(tag: str, channel: str) -> None:
    if not TAG_RE.match(tag):
        raise RuntimeError("invalid tag")
    p = Progress(tag, channel)
    env = read_env()
    work = Path(tempfile.mkdtemp(prefix="mmn-update-"))
    previous: Path | None = None
    dump: Path | None = None
    migrated = False
    try:
        p.step("downloading release")
        tarball = download_release(tag, work, env)
        p.step("checksum verified, unpacking")
        target = install_release(tarball, tag, p)
        p.step("backing up the database")
        dumps = data_dir() / "pre-update"
        dumps.mkdir(parents=True, exist_ok=True)
        uid, gid = _app_ids()
        os.chown(dumps, uid, gid)
        dump = dumps / f"{tag}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.dump"
        args, pg_extra = pg_env(env)
        sh(["pg_dump", "-Fc", "-f", str(dump), *args], env=pg_extra, user=APP_USER)
        for old in sorted(dumps.glob("*.dump"), key=lambda f: f.stat().st_mtime, reverse=True)[3:]:
            old.unlink(missing_ok=True)
        p.step("migrating the database")
        sh([str(target / "venv/bin/mmn"), "migrate"], env=env, user=APP_USER)
        migrated = True
        p.step("switching to the new version")
        previous = switch_current(target)
        sync_system_files(target)
        p.step("restarting")
        sh(["systemctl", "restart", SERVICE])
        if not health_ok():
            raise RuntimeError("the new version did not start (health check failed)")
        prune_releases()
        p.done("success", previous=previous.name if previous else None)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        try:
            if previous is not None:
                p.step("rolling back to the previous version")
                switch_current(previous)
            if migrated and dump is not None:
                p.step("restoring the database from before the update")
                args, pg_extra = pg_env(env)
                sh(["psql", "-v", "ON_ERROR_STOP=1", *args, "-c",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"], env=pg_extra, user=APP_USER)
                sh(["pg_restore", "--no-owner", "--single-transaction", *args, str(dump)], env=pg_extra,
                   user=APP_USER)
            if previous is not None or migrated:
                sh(["systemctl", "restart", SERVICE], check=False)
        except Exception as rollback_error:  # noqa: BLE001
            msg += f" | rollback problem: {rollback_error}"
        p.done("failed", error=msg[-3000:])
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ nginx / reconfigure
def _proxy_in(cfg: dict[str, Any]):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mmn.nginx import ProxyIn

    return ProxyIn(**cfg)


def render(cfg: dict[str, Any]) -> str:
    from mmn.nginx import render_nginx

    return render_nginx(_proxy_in(cfg), frontend=str(INSTALL_DIR / "current" / "frontend"))


def apply_nginx(cfg: dict[str, Any], reload: bool = True) -> None:
    if not Path("/proc/net/if_inet6").exists():
        cfg = {**cfg, "listen_ipv6": False}  # IPv6 disabled on this host
    text = render(cfg)
    backup = NGINX_SITE.read_text() if NGINX_SITE.exists() else None
    NGINX_SITE.parent.mkdir(parents=True, exist_ok=True)
    NGINX_SITE.write_text(text)
    if not NGINX_LINK.exists():
        NGINX_LINK.symlink_to(NGINX_SITE)
    default = Path("/etc/nginx/sites-enabled/default")
    if default.is_symlink():
        default.unlink()
    res = sh(["nginx", "-t"], check=False)
    if res.returncode != 0:
        if backup is not None:
            NGINX_SITE.write_text(backup)
        else:
            NGINX_SITE.unlink(missing_ok=True)
            NGINX_LINK.unlink(missing_ok=True)
        raise RuntimeError(f"nginx rejected the configuration: {res.stderr.strip()[-800:]}")
    if reload:
        sh(["systemctl", "reload-or-restart", "nginx"])


def do_reconfigure(cfg: dict[str, Any]) -> None:
    write_status("system-status.json", {"state": "running", "at": datetime.now(UTC).isoformat()})
    try:
        model = _proxy_in(cfg)
        model.check()
        clean = model.model_dump()
        apply_nginx(clean)
        old_base = read_env().get("MMN_BASE_PATH", "/")
        PROXY_FILE.write_text(json.dumps(clean, indent=1))
        if old_base != clean["base_path"]:
            set_env_value("MMN_BASE_PATH", clean["base_path"])
            sh(["systemctl", "restart", SERVICE])
        write_status("system-status.json", {"state": "success", "at": datetime.now(UTC).isoformat()})
    except Exception as e:  # noqa: BLE001
        write_status("system-status.json", {"state": "failed", "error": str(e)[-2000:],
                                            "at": datetime.now(UTC).isoformat()})


def run_pending() -> None:
    d = run_dir()
    req = read_request(d / "system-request.json")
    if req and req.get("action") == "reconfigure" and isinstance(req.get("config"), dict):
        do_reconfigure(req["config"])
    req = read_request(d / "update-request.json")
    if req and req.get("action") == "update":
        do_update(str(req.get("tag", "")), str(req.get("channel", "production")))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "run"
    if os.geteuid() != 0 and cmd != "render-nginx":
        print("must run as root", file=sys.stderr)
        return 1
    if cmd == "run":
        run_pending()
    elif cmd == "render-nginx":
        cfg = json.loads(PROXY_FILE.read_text()) if PROXY_FILE.exists() else {}
        print(render(cfg))
    elif cmd == "apply-nginx":
        cfg = json.loads(Path(argv[1]).read_text()) if len(argv) > 1 else (
            json.loads(PROXY_FILE.read_text()) if PROXY_FILE.exists() else {})
        cfg = _proxy_in(cfg).model_dump()
        apply_nginx(cfg)
        PROXY_FILE.write_text(json.dumps(cfg, indent=1))
    elif cmd == "install-tarball":
        # Used by the installer: install-tarball <file> <tag>
        target = install_release(Path(argv[1]), argv[2])
        switch_current(target)
        print(target)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
