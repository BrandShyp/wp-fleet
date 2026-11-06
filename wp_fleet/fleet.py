"""Status, update and audit for a fleet of WordPress sites.

The rules this module is built around:

1. One SSH exec per site per step. Commands are batched into one script and the
   output is split on marker lines.
2. Never update without a backup. The database export and the updates run in one
   script joined with `&&`, so an update cannot start if the export failed.
3. Record prior state before writing. The rollback file is on local disk before the
   first write command leaves the machine.
4. Verify on the live result, not the exit code. After every write, the state is read
   back from the server and compared with what was asked for. wp-cli can exit 0 and
   still leave a plugin where it was.
5. A cache "purge all" can miss a static front page. Purge commands are per site and
   explicit, so the front page URL can be purged by name.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from .config import Audit, Site
from .runner import Result, Runner

MARK = "@@wp-fleet:"


# --------------------------------------------------------------------------- helpers

def qpath(path: str) -> str:
    """Shell-quote a path but leave a leading ~/ outside the quotes so it expands."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def batch(site: Site, steps: Dict[str, str], joiner: str = "; ") -> str:
    """One script: cd into the WordPress root, then each step behind a marker line."""
    parts = [f"cd {qpath(site.wp_path)}"]
    for name, cmd in steps.items():
        parts.append(f"echo {shlex.quote(MARK + name)}")
        parts.append(cmd)
    return joiner.join(parts)


def split(out: str) -> Dict[str, str]:
    """Split batched output back into {step: text} on the marker lines."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in out.splitlines():
        if line.startswith(MARK):
            current = line[len(MARK):].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def wp_json(text: str) -> list:
    """Parse wp-cli --format=json output. wp-cli prints a plain 'Success: ...' line
    instead of [] when there is nothing to report, so anything that is not a JSON
    array counts as empty."""
    text = text.strip()
    start = text.find("[")
    if start == -1:
        return []
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- status

@dataclass
class Pending:
    name: str
    version: str
    update_version: str


@dataclass
class SiteStatus:
    site: str
    ok: bool
    core: str = ""
    core_update: str = ""
    plugins: List[Pending] = field(default_factory=list)
    themes: List[Pending] = field(default_factory=list)
    error: str = ""

    @property
    def pending(self) -> int:
        return (1 if self.core_update else 0) + len(self.plugins) + len(self.themes)


STATUS_STEPS = {
    "core": "wp core version",
    "core_updates": "wp core check-update --format=json",
    "plugins": "wp plugin list --update=available --fields=name,version,update_version --format=json",
    "themes": "wp theme list --update=available --fields=name,version,update_version --format=json",
}


def _pending(rows: list) -> List[Pending]:
    return [Pending(r["name"], str(r.get("version", "")), str(r.get("update_version", ""))) for r in rows]


def read_status(runner: Runner, site: Site) -> SiteStatus:
    res = runner.run(site, batch(site, STATUS_STEPS))
    sec = split(res.out)
    if "core" not in sec or not sec["core"]:
        return SiteStatus(site.name, False, error=(res.err.strip() or "no output")[:200])
    updates = wp_json(sec.get("core_updates", ""))
    newest = max((u.get("version", "") for u in updates), default="", key=_vkey)
    return SiteStatus(
        site.name,
        True,
        core=sec["core"].splitlines()[-1].strip(),
        core_update=newest,
        plugins=_pending(wp_json(sec.get("plugins", ""))),
        themes=_pending(wp_json(sec.get("themes", ""))),
    )


def _vkey(v: str):
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]


def print_status(rows: List[SiteStatus], out: TextIO) -> None:
    w = max([len(r.site) for r in rows] + [4]) + 2
    out.write(f"{'site':<{w}}{'core':<9}{'core update':<13}{'plugins':<9}themes\n")
    for r in rows:
        if not r.ok:
            out.write(f"{r.site:<{w}}unreachable: {r.error}\n")
            continue
        out.write(
            f"{r.site:<{w}}{r.core:<9}{(r.core_update or '-'):<13}"
            f"{len(r.plugins):<9}{len(r.themes)}\n"
        )
    for r in rows:
        for kind, items in (("plugin", r.plugins), ("theme", r.themes)):
            for p in items:
                out.write(f"  {r.site}: {kind} {p.name} {p.version} -> {p.update_version}\n")
    total = sum(r.pending for r in rows if r.ok)
    out.write(f"\n{total} pending update(s) across {len(rows)} site(s).\n")


# --------------------------------------------------------------------------- update

def update_site(
    runner: Runner,
    site: Site,
    *,
    apply: bool,
    rollback_dir: Path,
    out: TextIO,
    now: Optional[float] = None,
) -> bool:
    """Plan (and with apply=True, perform) every pending update on one site."""
    before = read_status(runner, site)
    if not before.ok:
        out.write(f"{site.name}: unreachable, skipped ({before.error})\n")
        return False
    if before.pending == 0:
        out.write(f"{site.name}: up to date\n")
        return True

    plan = []
    if before.core_update:
        plan.append(f"core {before.core} -> {before.core_update}")
    plan += [f"plugin {p.name} {p.version} -> {p.update_version}" for p in before.plugins]
    plan += [f"theme {p.name} {p.version} -> {p.update_version}" for p in before.themes]
    out.write(f"{site.name}: {len(plan)} update(s)\n" + "".join(f"  {p}\n" for p in plan))
    if not apply:
        out.write("  dry run: nothing changed (use --apply)\n")
        return True

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    backup = f"~/wp-fleet-backups/{site.name}-{stamp}.sql"
    qbackup = qpath(backup)

    # Rule 3: the rollback record exists locally before anything is written remotely.
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rb = rollback_dir / f"update-{site.name}-{stamp}.json"
    rb.write_text(json.dumps({
        "site": site.name,
        "taken_at": stamp,
        "db_backup": backup,
        "core": before.core,
        "plugins": {p.name: p.version for p in before.plugins},
        "themes": {p.name: p.version for p in before.themes},
    }, indent=2))

    # Rule 2: backup and updates in one script, joined with && so no backup, no update.
    cmds = ["mkdir -p ~/wp-fleet-backups", f"wp db export {qbackup}"]
    if before.core_update:
        cmds += ["wp core update", "wp core update-db"]
    if before.plugins:
        cmds.append("wp plugin update " + " ".join(shlex.quote(p.name) for p in before.plugins))
    if before.themes:
        cmds.append("wp theme update " + " ".join(shlex.quote(p.name) for p in before.themes))
    cmds += site.purge
    res = runner.run(site, f"cd {qpath(site.wp_path)} && " + " && ".join(cmds))

    # Rule 4: read the live state back; the exit code is not the verdict.
    after = read_status(runner, site)
    stuck = []
    if not after.ok:
        stuck.append("site did not answer after the update")
    else:
        if before.core_update and after.core != before.core_update:
            stuck.append(f"core is {after.core}, expected {before.core_update}")
        left = {p.name for p in after.plugins} | {p.name for p in after.themes}
        stuck += [f"{p.name} still {p.version}" for p in before.plugins + before.themes if p.name in left]
    if stuck or not res.ok:
        out.write(f"  NOT VERIFIED (exit {res.code}): {'; '.join(stuck) or res.err.strip()[:200]}\n")
        out.write(f"  rollback record: {rb}\n")
        return False
    out.write(f"  verified on the live site; db backup {backup}; rollback record {rb}\n")
    return True


# --------------------------------------------------------------------------- audit

def read_options(runner: Runner, site: Site, audit: Audit) -> Dict[str, str]:
    steps = {k: audit.get.format(key=shlex.quote(k)) + " 2>/dev/null" for k in audit.baseline}
    sec = split(runner.run(site, batch(site, steps)).out)
    return {k: sec.get(k, "").strip() for k in audit.baseline}


def drift(current: Dict[str, str], wanted: Dict[str, str]) -> Dict[str, tuple]:
    return {k: (current.get(k, ""), v) for k, v in wanted.items() if current.get(k, "") != v}


def _write_options(runner: Runner, site: Site, audit: Audit, values: Dict[str, str]) -> Result:
    cmds = [audit.set.format(key=shlex.quote(k), value=shlex.quote(v)) for k, v in values.items()]
    return runner.run(site, f"cd {qpath(site.wp_path)} && " + " && ".join(cmds))


def audit_site(
    runner: Runner,
    site: Site,
    audit: Audit,
    *,
    apply: bool,
    rollback_dir: Path,
    out: TextIO,
    now: Optional[float] = None,
) -> bool:
    current = read_options(runner, site, audit)
    diff = drift(current, audit.baseline)
    if not diff:
        out.write(f"{site.name}: matches baseline ({len(audit.baseline)} options)\n")
        return True
    out.write(f"{site.name}: {len(diff)} option(s) differ from baseline\n")
    for k, (have, want) in diff.items():
        out.write(f"  {k}: {have or '(unset)'} -> {want}\n")
    if not apply:
        return True

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rb = rollback_dir / f"audit-{site.name}-{stamp}.json"
    rb.write_text(json.dumps({"site": site.name, "taken_at": stamp,
                              "options": {k: have for k, (have, _) in diff.items()}}, indent=2))
    wanted = {k: want for k, (_, want) in diff.items()}
    _write_options(runner, site, audit, wanted)
    left = drift(read_options(runner, site, audit), audit.baseline)
    if left:
        out.write(f"  NOT VERIFIED: {', '.join(left)} still differ; rollback {rb}\n")
        return False
    out.write(f"  applied and verified; rollback snapshot {rb}\n")
    return True


def restore_site(runner: Runner, site: Site, audit: Audit, snapshot: Path, out: TextIO) -> bool:
    data = json.loads(snapshot.read_text())
    if data.get("site") != site.name:
        raise SystemExit(f"{snapshot} belongs to {data.get('site')!r}, not {site.name!r}")
    values = {k: v for k, v in data["options"].items() if v != ""}
    _write_options(runner, site, audit, values)
    now_vals = read_options(runner, site, audit)
    bad = [k for k, v in values.items() if now_vals.get(k) != v]
    out.write(f"{site.name}: restored {len(values) - len(bad)} of {len(values)} option(s)\n")
    if bad:
        out.write(f"  NOT VERIFIED: {', '.join(bad)}\n")
    return not bad
