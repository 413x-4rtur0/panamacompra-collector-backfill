# Script organization and ordered task names

The repository keeps the historical `pc_*.sh` entrypoints in the root for
backward compatibility, but new operational entrypoints should live under
`scripts/` and be reachable from `bin/pcc`.

## Naming methodology

Use ordered names when the filename should communicate workflow hierarchy:

```text
NNN[-letter]-short-description.sh
```

Examples:

- `001a-setup-development.sh` — first bootstrap path for development/portable use.
- `001b-install-update-monitor-launcher.sh` — alternate/conditional first-time desktop step.
- `020-start-collector.sh` — normal run step after setup/update.
- `090-uninstall-or-purge.sh` — late/destructive lifecycle step.

Rules:

1. `NNN` is the lifecycle phase. Lower numbers happen earlier.
2. Optional letters (`001a`, `001b`) are mutually exclusive or conditional
   variants inside the same phase.
3. The description is lowercase kebab-case and starts with a verb when possible.
4. Numbered task files should be thin wrappers; implementation belongs in
   reusable scripts or the `bin/pcc` command dispatcher.
5. Do not rename root-level legacy scripts until all README, desktop, systemd,
   and user workflows have migrated. Add ordered wrappers first, then deprecate.

## Current lifecycle map

| Ordered wrapper | Canonical command | Purpose |
| --- | --- | --- |
| `tasks/000-review-legacy-and-new-names.sh` | `./bin/pcc review-names` | Review old root names against ordered wrappers. |
| `tasks/000b-fresh-install-doctor.sh` | `./bin/pcc doctor` | Evaluate a fresh clone/install before running. |
| `tasks/001a-setup-development.sh` | `./setup.sh` | Bootstrap dependencies for a checkout. |
| `tasks/001b-install-update-monitor-launcher.sh` | `./bin/pcc launcher install` | Install the Update + Monitor desktop launcher. |
| `tasks/002-install-user-services.sh` | `./bin/pcc service install --enable` | Install user systemd services. |
| `tasks/010-update-local-copy.sh` | `./update_local_copy.sh` | Update code/dependencies before monitor use. |
| `tasks/020-start-collector.sh` | `./bin/pcc start` | Queue/start a collector run. |
| `tasks/021-open-monitor.sh` | `./bin/pcc monitor` | Open the monitor independently. |
| `tasks/030-status.sh` | `./bin/pcc status` | Inspect queues, progress, logs, and process state. |
| `tasks/040-stop-all.sh` | `./bin/pcc stop` | Stop host processes. |
| `tasks/090-uninstall-or-purge.sh` | `./bin/pcc uninstall` | Stop services/containers and optionally purge state. |

The authoritative old-to-new map lives in `config/script-name-map.tsv`. This gives the visible hierarchy you asked for without breaking existing users
or systemd/desktop integrations that still call the historical filenames.


## Full script reorder surface

For a broader reviewed/sorted surface, `scripts/ordered/` contains ordered aliases
for every shell entrypoint that still exists in the repository. The source map is
`config/ordered-script-map.tsv`; run `./bin/pcc review-names` to validate both the
phase task wrappers and the full ordered alias set. Run `./bin/pcc doctor` before
redoing a clone/install from zero to verify required files, syntax, and runtime
path resolution. Run `./bin/pcc self-contained` to verify the repository is self-contained while secrets/runtime data remain excluded and Docker Compose ports/project names are safe.
