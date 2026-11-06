"""A fake server for tests: answers the scripts wp-fleet sends, records every call,
and can be told to misbehave (fail the backup, exit 0 without updating)."""

import json
import re

from wp_fleet.fleet import MARK
from wp_fleet.runner import Result


class FakeServer:
    def __init__(self, core="6.8.1", core_latest="6.9.4", plugins=None, themes=None, options=None):
        self.core = core
        self.core_latest = core_latest
        # name -> [installed, available]
        self.plugins = plugins if plugins is not None else {}
        self.themes = themes if themes is not None else {}
        self.options = dict(options or {})
        self.backup_fails = False
        self.updates_are_noops = False
        self.backups = []


class FakeRunner:
    def __init__(self, servers):
        self.servers = servers  # site name -> FakeServer
        self.calls = []  # (site name, script, snapshot of files that existed)
        self.watch = []  # paths whose existence is recorded at each call

    def run(self, site, script):
        self.calls.append((site.name, script, {str(p): p.exists() for p in self.watch}))
        s = self.servers.get(site.name)
        if s is None:
            return Result(255, "", "ssh: connect to host port 22: Connection refused")
        if "wp core version" in script:
            return Result(0, self._status(s))
        if " get " in script and MARK in script:
            out = []
            for key in re.findall(MARK + r"([^'\s;]+)", script):
                out += [MARK + key, s.options.get(key, "")]
            return Result(0, "\n".join(out) + "\n")
        if "wp db export" in script:
            if s.backup_fails:
                return Result(1, "", "Error: Failed to get database connection")
            s.backups.append(script)
            if not s.updates_are_noops:
                if "wp core update" in script:
                    s.core = s.core_latest
                for table in (s.plugins, s.themes):
                    for name, v in table.items():
                        if f" {name}" in script:
                            v[0] = v[1]
            return Result(0, "Success: done.\n")
        if " set " in script:
            for key, value in re.findall(r"set (\S+) (\S+?)(?: &&|$)", script):
                s.options[key.strip("'")] = value.strip("'")
            return Result(0, "")
        return Result(127, "", f"unexpected script: {script}")

    @staticmethod
    def _rows(table):
        return json.dumps([{"name": n, "version": v[0], "update_version": v[1]}
                           for n, v in table.items() if v[0] != v[1]])

    def _status(self, s):
        if s.core != s.core_latest:
            core_updates = json.dumps([{"version": s.core_latest, "update_type": "minor"}])
        else:
            core_updates = "Success: WordPress is at the latest version."
        return "\n".join([
            MARK + "core", s.core,
            MARK + "core_updates", core_updates,
            MARK + "plugins", self._rows(s.plugins),
            MARK + "themes", self._rows(s.themes),
        ]) + "\n"
