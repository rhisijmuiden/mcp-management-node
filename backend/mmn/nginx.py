"""Nginx configuration for the app. Pure functions: used by the installer and the root helper.

Two modes:
* ``direct``: Nginx terminates TLS itself (port 443, HTTP redirects to HTTPS).
* ``proxy``:  behind a reverse proxy such as Zoraxy. Nginx listens on plain HTTP only and trusts
  ``X-Forwarded-For`` / ``X-Forwarded-Proto`` solely from the configured proxy addresses.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SERVER_NAME_RE = re.compile(r"^(_|[A-Za-z0-9*.-]{1,253})( [A-Za-z0-9*.-]{1,253})*$")
BASE_PATH_RE = re.compile(r"^/([A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*)?$")
CERT_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")

UPSTREAM = "127.0.0.1:8740"


class ProxyIn(BaseModel):
    mode: Literal["direct", "proxy"] = "direct"
    trusted_proxies: list[str] = Field(default_factory=list, max_length=20)
    restrict_to_proxy: bool = False
    server_name: str = "_"
    http_port: int = Field(default=80, ge=1, le=65535)
    https_port: int = Field(default=443, ge=1, le=65535)
    base_path: str = "/"
    ssl_certificate: str = "/etc/ssl/certs/ssl-cert-snakeoil.pem"
    ssl_certificate_key: str = "/etc/ssl/private/ssl-cert-snakeoil.key"
    listen_ipv6: bool = True
    # Port for agents (mutual TLS, terminated by Nginx itself, also behind Zoraxy: use a TCP stream rule).
    agent_port: int = Field(default=8443, ge=1, le=65535)

    @field_validator("trusted_proxies")
    @classmethod
    def _cidrs(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            item = item.strip()
            if not item:
                continue
            out.append(str(ipaddress.ip_network(item, strict=False)))
        return out

    @field_validator("server_name")
    @classmethod
    def _sn(cls, v: str) -> str:
        v = " ".join(v.split()) or "_"
        if not SERVER_NAME_RE.match(v):
            raise ValueError("invalid server name")
        return v

    @field_validator("base_path")
    @classmethod
    def _bp(cls, v: str) -> str:
        v = "/" + v.strip().strip("/")
        if v != "/" and not BASE_PATH_RE.match(v):
            raise ValueError("invalid base path")
        return v

    @field_validator("ssl_certificate", "ssl_certificate_key")
    @classmethod
    def _cert(cls, v: str) -> str:
        if not CERT_PATH_RE.match(v) or ".." in v:
            raise ValueError("invalid certificate path")
        return v

    def check(self) -> None:
        if self.mode == "proxy" and not self.trusted_proxies:
            raise ValueError("behind a reverse proxy you must enter the proxy's IP address")
        used = {self.http_port} | ({self.https_port} if self.mode == "direct" else set())
        if self.agent_port in used:
            raise ValueError("the agent port must differ from the web ports")


SECURITY_HEADERS = """
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'self'; base-uri 'self'; form-action 'self'" always;"""


def _locations(cfg: ProxyIn, frontend: str, hsts: bool) -> str:
    bp = cfg.base_path
    prefix = "" if bp == "/" else bp
    headers = SECURITY_HEADERS + ('\n    add_header Strict-Transport-Security "max-age=31536000" always;'
                                  if hsts else "")
    proxy = """
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $mmn_proto;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;"""
    index_rewrite = "" if bp == "/" else f"""
        sub_filter '<base href="/" />' '<base href="{bp}/" />';
        sub_filter_once on;"""
    wk = "" if bp == "/" else f"""
    location ^~ {prefix}/.well-known/ {{
        proxy_pass http://mmn_backend/.well-known/;{proxy}
    }}
"""
    redirect = "" if bp == "/" else f"""
    location = {bp} {{
        return 301 {bp}/;
    }}
"""
    return f"""{headers}
    client_max_body_size 64m;
    {redirect}
    location ^~ {prefix}/api/ {{
        proxy_pass http://mmn_backend/api/;{proxy}
    }}

    location = {prefix}/api/templates/upload {{
        proxy_pass http://mmn_backend/api/templates/upload;{proxy}
        client_max_body_size 8g;
        proxy_request_buffering off;
    }}

    location ^~ {prefix}/mcp {{
        proxy_pass http://mmn_backend/mcp/;{proxy}
        proxy_buffering off;
        proxy_cache off;
    }}

    location ^~ {prefix}/oauth/ {{
        proxy_pass http://mmn_backend/oauth/;{proxy}
    }}

    location ^~ /.well-known/oauth- {{
        proxy_pass http://mmn_backend;{proxy}
    }}
{wk}
    location ^~ {prefix}/assets/ {{
        alias {frontend}/assets/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }}

    location ^~ {prefix}/ {{
        alias {frontend}/;
        try_files $uri $uri/ {prefix}/index.html;
        add_header Cache-Control "no-cache";{headers.replace(chr(10) + "    ", chr(10) + "        ")}{index_rewrite}
    }}
"""


def _agent_server(cfg: ProxyIn, tls_dir: str, secret: str) -> str:
    v6 = f"    listen [::]:{cfg.agent_port} ssl;\n" if cfg.listen_ipv6 else ""
    proxy = f"""
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-MMN-Agent-Secret "{secret}";
        proxy_set_header X-MMN-Client-Verify $ssl_client_verify;
        proxy_set_header X-MMN-Client-Cert $ssl_client_escaped_cert;"""
    return f"""# Agent endpoint: mutual TLS with the app's own agent CA. Agents connect here (outgoing from the node).
server {{
    listen {cfg.agent_port} ssl;
{v6}    server_name _;
    ssl_certificate {tls_dir}/server.crt;
    ssl_certificate_key {tls_dir}/server.key;
    ssl_client_certificate {tls_dir}/ca.crt;
    ssl_verify_client optional;
    ssl_verify_depth 1;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:mmn_agent_ssl:5m;
    client_max_body_size 1m;

    location = /agent/enroll {{
        proxy_pass http://mmn_backend/agent/enroll;{proxy}
    }}

    location = /agent/ws {{
        if ($ssl_client_verify != SUCCESS) {{ return 403; }}
        proxy_pass http://mmn_backend/agent/ws;{proxy}
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }}

    location ^~ /agent/download/ {{
        if ($ssl_client_verify != SUCCESS) {{ return 403; }}
        proxy_pass http://mmn_backend/agent/download/;{proxy}
    }}

    location ^~ /agent/templates/ {{
        if ($ssl_client_verify != SUCCESS) {{ return 403; }}
        proxy_pass http://mmn_backend/agent/templates/;{proxy}
        proxy_buffering off;
        proxy_read_timeout 1h;
    }}

    location ^~ /agent/transfer/ {{
        # Data copies between servers: streamed, any size (the app enforces its own limit).
        if ($ssl_client_verify != SUCCESS) {{ return 403; }}
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 6h;
        proxy_send_timeout 6h;
        proxy_pass http://mmn_backend/agent/transfer/;{proxy}
    }}

    location ^~ /agent/stream/ {{
        if ($ssl_client_verify != SUCCESS) {{ return 403; }}
        proxy_pass http://mmn_backend/agent/stream/;{proxy}
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 12h;
        proxy_send_timeout 12h;
    }}

    location / {{
        return 404;
    }}
}}"""


SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def render_nginx(cfg: ProxyIn, frontend: str = "/opt/mcp-management-node/current/frontend",
                 agent_tls: str | None = None, agent_secret: str | None = None) -> str:
    """``agent_tls`` is the folder with ca.crt/server.crt/server.key; without it (or the secret) the
    agent endpoint is left out."""
    cfg.check()

    def v6(port: int, extra: str = "") -> str:
        return f"    listen [::]:{port}{extra};\n" if cfg.listen_ipv6 else ""

    lines = ["# Managed by MCP Management Node. Change it in the app (Administration > Web server & proxy); edits here are lost.",
             f"upstream mmn_backend {{\n    server {UPSTREAM};\n    keepalive 16;\n}}",
             "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    ''      close;\n}"]
    if cfg.mode == "proxy":
        geo = "\n".join(f"    {c} 1;" for c in cfg.trusted_proxies)
        lines.append(f"geo $realip_remote_addr $mmn_from_proxy {{\n    default 0;\n{geo}\n}}")
        lines.append('map "$mmn_from_proxy:$http_x_forwarded_proto" $mmn_proto {\n'
                     '    "1:https" https;\n    "1:http"  http;\n    default $scheme;\n}')
        real_ip = "\n".join(f"    set_real_ip_from {c};" for c in cfg.trusted_proxies)
        access = ""
        if cfg.restrict_to_proxy:
            access = "\n".join(f"    allow {c};" for c in cfg.trusted_proxies) + "\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;"
        lines.append(f"""server {{
    listen {cfg.http_port};
{v6(cfg.http_port)}    server_name {cfg.server_name};
{real_ip}
    real_ip_header X-Forwarded-For;
    real_ip_recursive on;
{access}
{_locations(cfg, frontend, hsts=False)}
}}""")
    else:
        lines.append("map $scheme $mmn_proto {\n    default $scheme;\n}")
        lines.append(f"""server {{
    listen {cfg.http_port};
{v6(cfg.http_port)}    server_name {cfg.server_name};
    location /.well-known/acme-challenge/ {{
        root /var/www/html;
    }}
    location / {{
        return 301 https://$host{'' if cfg.https_port == 443 else ':' + str(cfg.https_port)}$request_uri;
    }}
}}""")
        lines.append(f"""server {{
    listen {cfg.https_port} ssl;
{v6(cfg.https_port, ' ssl')}    http2 on;
    server_name {cfg.server_name};
    ssl_certificate {cfg.ssl_certificate};
    ssl_certificate_key {cfg.ssl_certificate_key};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:mmn_ssl:10m;
    ssl_session_timeout 1d;
{_locations(cfg, frontend, hsts=True)}
}}""")
    if agent_tls and agent_secret:
        if not CERT_PATH_RE.match(agent_tls) or not SECRET_RE.match(agent_secret):
            raise ValueError("invalid agent TLS settings")
        lines.append(_agent_server(cfg, agent_tls, agent_secret))
    return "\n\n".join(lines) + "\n"


def proxy_settings_from(d: dict[str, Any], base_path: str) -> ProxyIn:
    data = {k: v for k, v in d.items() if k in ProxyIn.model_fields}
    data["base_path"] = base_path
    return ProxyIn(**data)
