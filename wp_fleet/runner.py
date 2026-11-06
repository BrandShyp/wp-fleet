"""How commands reach a server.

Everything in wp-fleet goes through a Runner, so the logic can be tested with a fake
one and no network. The real one shells out to the system `ssh` binary: it already
knows your keys, agent, known_hosts and ProxyJump settings, and there is no extra
dependency to install.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from .config import Site


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


class Runner(Protocol):
    def run(self, site: Site, script: str) -> Result: ...


class SSHRunner:
    """Runs one shell script per call over one SSH connection.

    Rule: one exec per site per step. Shared hosts throttle rapid exec channels, and a
    tool that opens a connection per wp-cli call gets slow, then gets refused. Every
    step in wp-fleet batches its commands into a single script for this reason.
    """

    def __init__(self, timeout: int = 900):
        self.timeout = timeout

    def run(self, site: Site, script: str) -> Result:
        cmd = [
            "ssh",
            "-p", str(site.port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
        ]
        if site.key:
            cmd += ["-i", site.key]
        cmd += [f"{site.user}@{site.host}", script]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {self.timeout}s")
        return Result(p.returncode, p.stdout, p.stderr)
