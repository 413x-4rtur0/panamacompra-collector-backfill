# Ordered script aliases

This folder is the reviewed, sorted, human-readable script surface for the
project. It intentionally keeps the historical root `pc_*.sh` files in place for
compatibility, while exposing every shell entrypoint with a lifecycle-ordered
name.

Use this folder when you want the filename itself to explain the order:

- `000-*` review and diagnostics
- `001-*` to `012-*` setup and installation
- `020-*` migrations
- `030-*` updates
- `040-*` run-all worker/queue lifecycle
- `050-*` monitor/status
- `060-*` webhook operations
- `070-*` stop operations
- `080-*` wrapper/review helpers
- `090-*` uninstall/purge

The authoritative mapping is `config/ordered-script-map.tsv`. Legacy names remain
as compatibility targets until all desktop, systemd, README, and user workflows
are migrated to `bin/pcc` or these ordered aliases.
