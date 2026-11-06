"""Site configuration, loaded from a TOML file (stdlib tomllib)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


@dataclass(frozen=True)
class Site:
    name: str
    host: str
    user: str
    wp_path: str
    url: str = ""
    port: int = 22
    key: str = ""
    # Shell commands run (from wp_path) after a successful update.
    purge: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Audit:
    get: str = "wp litespeed-option get {key}"
    set: str = "wp litespeed-option set {key} {value}"
    baseline: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Fleet:
    sites: List[Site]
    audit: Audit

    def select(self, name: str | None) -> List[Site]:
        if not name:
            return list(self.sites)
        picked = [s for s in self.sites if s.name == name]
        if not picked:
            known = ", ".join(s.name for s in self.sites)
            raise SystemExit(f"unknown site {name!r} (configured: {known})")
        return picked


def load(path: str | Path) -> Fleet:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    sites = []
    for name, cfg in raw.get("sites", {}).items():
        missing = [k for k in ("host", "user", "wp_path") if k not in cfg]
        if missing:
            raise SystemExit(f"site {name!r} is missing: {', '.join(missing)}")
        sites.append(
            Site(
                name=name,
                host=cfg["host"],
                user=cfg["user"],
                wp_path=cfg["wp_path"],
                url=cfg.get("url", ""),
                port=int(cfg.get("port", 22)),
                key=cfg.get("key", ""),
                purge=list(cfg.get("purge", [])),
            )
        )
    a = raw.get("audit", {})
    audit = Audit(
        get=a.get("get", Audit.get),
        set=a.get("set", Audit.set),
        baseline={k: str(v) for k, v in a.get("baseline", {}).items()},
    )
    return Fleet(sites, audit)
