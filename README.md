# wp-fleet

[![tests](https://github.com/BrandShyp/wp-fleet/actions/workflows/tests.yml/badge.svg)](https://github.com/BrandShyp/wp-fleet/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Status, updates and cache-setting audits for many WordPress sites at once, over SSH and
WP-CLI. Every update is planned first, backed up before it runs, and checked on the live
site afterwards. Standard library only; it uses the `ssh` already on your machine.

```
$ python -m wp_fleet status
site  core     core update  plugins  themes
blog  6.9.4    -            0        0
shop  6.8.1    6.9.4        2        1
  shop: plugin woocommerce 9.8.2 -> 10.3.0
  shop: plugin wordpress-seo 25.1 -> 26.4
  shop: theme storefront 4.6.0 -> 4.6.1

4 pending update(s) across 2 site(s).
```

## The rules it follows

Each of these is in the code, with a test behind it.

| Rule | Why |
|---|---|
| **One SSH exec per site per step** | Shared hosts throttle rapid exec channels. A tool that opens a connection per wp-cli call gets slow, then gets refused. Every step batches its commands into one script and splits the output on marker lines. |
| **Dry run by default** | `update` and `audit` only report until you pass `--apply`. |
| **No backup, no update** | `wp db export` and the updates run in one script joined with `&&`, so an update cannot start if the export failed. |
| **Record prior state before writing** | The rollback file (prior core, plugin and theme versions, the backup path) is on local disk before the first write command is sent. |
| **Verify the live result, not the exit code** | After every write the state is read back and compared with what was asked for. wp-cli can report success and leave a plugin where it was; wp-fleet reports that as `NOT VERIFIED`. |
| **Purge the front page by name** | A cache plugin's "purge all" can skip a static front page, so visitors keep seeing the old one while every check passes. Purge commands are per site, so the home URL can be listed explicitly. |

## Set it up

```bash
git clone https://github.com/BrandShyp/wp-fleet && cd wp-fleet
cp sites.example.toml sites.toml      # sites.toml is gitignored
```

Each site needs `host`, `user` and `wp_path`; `port`, `key`, `url` and `purge` are
optional. Your SSH key must already work for that user (`ssh user@host` with no prompt),
and WP-CLI must be on the server's PATH.

## Use it

```bash
python -m wp_fleet status                      # every site, one exec each
python -m wp_fleet update                      # plan only
python -m wp_fleet update --site shop --apply  # back up, update, purge, verify
python -m wp_fleet audit                       # compare cache options with the baseline
python -m wp_fleet audit --site blog --apply   # snapshot, write the baseline, verify
python -m wp_fleet audit --site blog --restore rollback/audit-blog-<stamp>.json
```

A planned update:

```
$ python -m wp_fleet update --site shop
shop: 4 update(s)
  core 6.8.1 -> 6.9.4
  plugin woocommerce 9.8.2 -> 10.3.0
  plugin wordpress-seo 25.1 -> 26.4
  theme storefront 4.6.0 -> 4.6.1
  dry run: nothing changed (use --apply)
```

An audit against the baseline in `sites.toml`:

```
$ python -m wp_fleet audit --site blog
blog: 2 option(s) differ from baseline
  optm-js_defer: 2 -> 1
  optm-css_async: 1 -> 0
```

The audit reads and writes LiteSpeed Cache options by default. Change `[audit] get` and
`set` to point it at another plugin's options.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The tests run against a fake server, so they need no network. They cover parsing wp-cli
output, one exec per site, a dry run that never writes, the rollback file existing before
the write, a failed backup stopping the update, a "successful" update that changed
nothing being caught, and an audit snapshot that restores cleanly.

This is version 0.1: a clean rewrite of the tooling we use on our own sites, tested
against the fake server only. Run it on a staging site before you point it at production.

## About

Built by [BrandShyp](https://brandshyp.org), a software firm in San Antonio. MIT licensed.
