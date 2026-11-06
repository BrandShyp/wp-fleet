"""Command line: python -m wp_fleet [--config sites.toml] {status,update,audit}"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, fleet
from .runner import SSHRunner


def main(argv=None, runner=None, out=None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="wp_fleet", description="Manage a fleet of WordPress sites over SSH and WP-CLI.")
    p.add_argument("--config", default="sites.toml", help="TOML file describing the sites (default sites.toml)")
    p.add_argument("--rollback-dir", default="rollback", help="where rollback records are written")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="core version and pending updates, one SSH exec per site")
    s.add_argument("--site")

    u = sub.add_parser("update", help="plan updates; --apply backs up, updates and verifies")
    u.add_argument("--site")
    u.add_argument("--apply", action="store_true", help="actually change the sites")

    a = sub.add_parser("audit", help="compare cache options with the baseline")
    a.add_argument("--site")
    a.add_argument("--apply", action="store_true", help="write the baseline (snapshot first)")
    a.add_argument("--restore", metavar="FILE", help="put back the values in a rollback snapshot")

    args = p.parse_args(argv)
    cfg = config.load(args.config)
    sites = cfg.select(args.site)
    runner = runner or SSHRunner()
    rb_dir = Path(args.rollback_dir)

    if args.cmd == "status":
        rows = [fleet.read_status(runner, site) for site in sites]
        fleet.print_status(rows, out)
        return 0 if all(r.ok for r in rows) else 1

    if args.cmd == "update":
        ok = [fleet.update_site(runner, site, apply=args.apply, rollback_dir=rb_dir, out=out) for site in sites]
        return 0 if all(ok) else 1

    if not cfg.audit.baseline:
        raise SystemExit("no [audit.baseline] in the config")
    if args.restore:
        if len(sites) != 1:
            raise SystemExit("--restore needs --site")
        return 0 if fleet.restore_site(runner, sites[0], cfg.audit, Path(args.restore), out) else 1
    ok = [fleet.audit_site(runner, site, cfg.audit, apply=args.apply, rollback_dir=rb_dir, out=out) for site in sites]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
