import io
import json
import tempfile
import unittest
from pathlib import Path

from wp_fleet import config, fleet
from wp_fleet.__main__ import main
from wp_fleet.config import Audit, Site

from tests.fake import FakeRunner, FakeServer

ROOT = Path(__file__).resolve().parents[1]
BLOG = Site("blog", "blog.example.com", "deploy", "~/public_html")
SHOP = Site("shop", "shop.example.org", "deploy", "/var/www/shop",
            purge=["wp litespeed-purge all", "wp litespeed-purge url https://shop.example.org/"])
AUDIT = Audit(baseline={"optm-js_defer": "1", "optm-css_async": "0"})


def shop_server():
    return FakeServer(plugins={"woocommerce": ["9.8.2", "10.3.0"], "akismet": ["5.4", "5.4"]},
                      themes={"storefront": ["4.6.0", "4.6.1"]})


class Parsing(unittest.TestCase):
    def test_split_and_wp_json(self):
        out = "@@wp-fleet:a\n6.9.4\n@@wp-fleet:b\nSuccess: WordPress is at the latest version.\n"
        sec = fleet.split(out)
        self.assertEqual(sec["a"], "6.9.4")
        self.assertEqual(fleet.wp_json(sec["b"]), [])
        self.assertEqual(fleet.wp_json('[{"name":"x"}]'), [{"name": "x"}])
        self.assertEqual(fleet.wp_json("Warning: noise\n[]"), [])

    def test_tilde_path_still_expands(self):
        self.assertEqual(fleet.qpath("~/public html"), "~/'public html'")
        self.assertTrue(fleet.batch(BLOG, {"x": "true"}).startswith("cd ~/public_html"))

    def test_example_config_loads(self):
        cfg = config.load(ROOT / "sites.example.toml")
        self.assertEqual([s.name for s in cfg.sites], ["blog", "shop"])
        self.assertEqual(cfg.audit.baseline["optm-css_async"], "0")


class Status(unittest.TestCase):
    def test_one_exec_per_site(self):
        r = FakeRunner({"blog": FakeServer(core="6.9.4"), "shop": shop_server()})
        rows = [fleet.read_status(r, s) for s in (BLOG, SHOP)]
        self.assertEqual([c[0] for c in r.calls], ["blog", "shop"])
        self.assertEqual(rows[0].pending, 0)
        self.assertEqual(rows[1].core_update, "6.9.4")
        self.assertEqual([p.name for p in rows[1].plugins], ["woocommerce"])
        self.assertEqual(rows[1].pending, 3)

    def test_unreachable_site_is_reported_not_fatal(self):
        r = FakeRunner({})
        row = fleet.read_status(r, BLOG)
        self.assertFalse(row.ok)
        self.assertIn("Connection refused", row.error)


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_dry_run_never_writes(self):
        server = shop_server()
        r = FakeRunner({"shop": server})
        ok = fleet.update_site(r, SHOP, apply=False, rollback_dir=self.tmp, out=io.StringIO())
        self.assertTrue(ok)
        self.assertEqual(len(r.calls), 1)  # status only
        self.assertEqual(server.backups, [])
        self.assertFalse(any(self.tmp.iterdir()))

    def test_apply_backs_up_first_and_verifies(self):
        server = shop_server()
        r = FakeRunner({"shop": server})
        out = io.StringIO()
        ok = fleet.update_site(r, SHOP, apply=True, rollback_dir=self.tmp, out=out, now=0)
        self.assertTrue(ok, out.getvalue())
        rb = self.tmp / "update-shop-19700101-000000.json"
        write_script = r.calls[1][1]
        self.assertLess(write_script.index("wp db export"), write_script.index("wp core update"))
        self.assertRegex(write_script, r"wp db export \S+ && ")  # no backup, no update
        self.assertIn("&& wp litespeed-purge url https://shop.example.org/", write_script)
        self.assertEqual(json.loads(rb.read_text())["plugins"], {"woocommerce": "9.8.2"})
        self.assertEqual(len(r.calls), 3)  # status, write, verify
        self.assertIn("verified on the live site", out.getvalue())

    def test_rollback_file_exists_before_the_write(self):
        r = FakeRunner({"shop": shop_server()})
        rb = self.tmp / "update-shop-19700101-000000.json"
        r.watch = [rb]
        fleet.update_site(r, SHOP, apply=True, rollback_dir=self.tmp, out=io.StringIO(), now=0)
        status_call, write_call = r.calls[0], r.calls[1]
        self.assertFalse(status_call[2][str(rb)])
        self.assertTrue(write_call[2][str(rb)])

    def test_failed_backup_means_no_update(self):
        server = shop_server()
        server.backup_fails = True
        r = FakeRunner({"shop": server})
        out = io.StringIO()
        ok = fleet.update_site(r, SHOP, apply=True, rollback_dir=self.tmp, out=out, now=0)
        self.assertFalse(ok)
        self.assertEqual(server.core, "6.8.1")
        self.assertIn("NOT VERIFIED", out.getvalue())

    def test_exit_zero_is_not_trusted(self):
        server = shop_server()
        server.updates_are_noops = True  # wp-cli says Success, nothing moved
        r = FakeRunner({"shop": server})
        out = io.StringIO()
        ok = fleet.update_site(r, SHOP, apply=True, rollback_dir=self.tmp, out=out, now=0)
        self.assertFalse(ok)
        self.assertIn("woocommerce still 9.8.2", out.getvalue())


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_report_only_by_default(self):
        server = FakeServer(options={"optm-js_defer": "2", "optm-css_async": "0"})
        r = FakeRunner({"blog": server})
        out = io.StringIO()
        fleet.audit_site(r, BLOG, AUDIT, apply=False, rollback_dir=self.tmp, out=out)
        self.assertIn("optm-js_defer: 2 -> 1", out.getvalue())
        self.assertEqual(server.options["optm-js_defer"], "2")
        self.assertEqual(len(r.calls), 1)

    def test_apply_snapshots_then_restore_puts_it_back(self):
        server = FakeServer(options={"optm-js_defer": "2", "optm-css_async": "0"})
        r = FakeRunner({"blog": server})
        out = io.StringIO()
        self.assertTrue(fleet.audit_site(r, BLOG, AUDIT, apply=True, rollback_dir=self.tmp, out=out, now=0))
        self.assertEqual(server.options["optm-js_defer"], "1")
        snap = self.tmp / "audit-blog-19700101-000000.json"
        self.assertEqual(json.loads(snap.read_text())["options"], {"optm-js_defer": "2"})
        self.assertTrue(fleet.restore_site(r, BLOG, AUDIT, snap, io.StringIO()))
        self.assertEqual(server.options["optm-js_defer"], "2")


class Cli(unittest.TestCase):
    def test_status_command(self):
        r = FakeRunner({"blog": FakeServer(core="6.9.4", core_latest="6.9.4"), "shop": shop_server()})
        out = io.StringIO()
        code = main(["--config", str(ROOT / "sites.example.toml"), "status"], runner=r, out=out)
        self.assertEqual(code, 0)
        self.assertIn("3 pending update(s) across 2 site(s).", out.getvalue())


if __name__ == "__main__":
    unittest.main()
