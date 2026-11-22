"""Installed version and build information.

A release tarball contains ``VERSION`` and ``BUILD.json`` (written by CI) at its root.
In a development checkout only ``VERSION`` exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from packaging.version import InvalidVersion, Version

PKG = Path(__file__).resolve().parent
# Release builds put VERSION and BUILD.json inside the package; a dev checkout has VERSION at the repo root.
SEARCH = (PKG, PKG.parent.parent)

TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)(?:-test\.(\d+))?$")


@dataclass(frozen=True)
class BuildInfo:
    version: str
    tag: str
    channel: str
    commit: str
    built_at: str | None
    dev: bool


@lru_cache
def get_build_info() -> BuildInfo:
    version = "0.0.0"
    for root in SEARCH:
        if (root / "VERSION").exists():
            version = (root / "VERSION").read_text().strip() or version
            break
    bf = PKG / "BUILD.json"
    if bf.exists():
        data = json.loads(bf.read_text())
        return BuildInfo(
            version=data.get("version", version),
            tag=data.get("tag", f"v{version}"),
            channel=data.get("channel", "production"),
            commit=data.get("commit", "unknown"),
            built_at=data.get("built_at"),
            dev=False,
        )
    return BuildInfo(version=version, tag=f"v{version}", channel="production", commit="dev",
                     built_at=None, dev=True)


def parse_tag(tag: str) -> Version | None:
    """Turn a release tag into a comparable version.

    ``v1.2.0`` is a production release, ``v1.2.0-test.7`` is test build 7 on the way to 1.2.0,
    which sorts below 1.2.0 itself.
    """
    m = TAG_RE.match(tag.strip())
    if not m:
        return None
    base, test_n = m.groups()
    try:
        return Version(f"{base}.dev{test_n}" if test_n else base)
    except InvalidVersion:
        return None


def channel_of_tag(tag: str) -> str:
    return "test" if "-test." in tag else "production"
