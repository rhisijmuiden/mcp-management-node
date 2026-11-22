"""Runtime configuration, read from the environment (``/etc/mcp-management-node/mmn.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CURRENT_REPO = "rhisijmuiden/stackpilot"
# Former names of the repository (GitHub redirects them, but we use the current one).
LEGACY_REPOS = {"rhisijmuiden/mcp-management-node", "rhisijmuiden/proxmox-management"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MMN_", env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://mmn:mmn@127.0.0.1:5432/mmn"
    data_dir: Path = Path("/var/lib/mcp-management-node")
    install_dir: Path = Path("/opt/mcp-management-node")
    # Path the app is served under, e.g. "/" or "/proxmox". Nginx strips it before proxying the API.
    base_path: str = "/"
    # GitHub repository releases are fetched from.
    github_repo: str = CURRENT_REPO
    github_token: str | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 8740
    # Set to true only for local development without HTTPS.
    insecure_cookies: bool = False
    run_scheduler: bool = True
    log_level: str = "info"
    # Folder with the agent builds (default: <install_dir>/current/agent from the release tarball).
    agent_dist: Path | None = None
    session_hours: int = Field(default=12, ge=1)
    remember_days: int = Field(default=30, ge=1)

    @field_validator("github_repo")
    @classmethod
    def _current_repo(cls, v: str) -> str:
        # Installs from before the rename to Stackpilot still name the old repository in mmn.env.
        return CURRENT_REPO if v.strip().lower() in LEGACY_REPOS else v.strip()

    @field_validator("base_path")
    @classmethod
    def _norm_base(cls, v: str) -> str:
        v = "/" + v.strip().strip("/")
        return "/" if v == "/" else v

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def agent_tls_dir(self) -> Path:
        """Certificates Nginx uses for the agent endpoint (written by the app, read by Nginx)."""
        return self.data_dir / "agent-tls"

    @property
    def agent_dist_dir(self) -> Path:
        return self.agent_dist or (self.install_dir / "current" / "agent")

    @property
    def cookie_path(self) -> str:
        return self.base_path

    @property
    def sync_database_url(self) -> str:
        """libpq-style URL for pg_dump / pg_restore."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
