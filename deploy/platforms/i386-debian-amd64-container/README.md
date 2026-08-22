# hp15bw Debian portable install (i386 host, amd64 container)

Deployment profile for the second collector host (`hp-15-bw036nr-d`,
Debian, external `pcc-data` drive). Kept separate from the main
`src/` pipeline rather than merged in, because the differences here are
about *how this specific host runs the app*, not about collector logic
that should be identical everywhere.

## Why this host needs its own profile

The host OS userspace on hp15bw is **i386 (32-bit)**, even though the
hardware is 64-bit-capable. Playwright's Firefox has no 32-bit Linux
build at all, so the collector pipeline cannot run directly on the host
-- it has to run inside an amd64 Debian container.

## Files

- **`Dockerfile.backfill`** -- `debian:bookworm-slim` built with
  `--platform linux/amd64`. Must be run with
  `--security-opt seccomp=unconfined`: the host's own i386 Docker
  default seccomp profile misclassifies amd64 syscall numbers and kills
  the process with `SIGSYS` otherwise.
- **`env.sh`** -- generic `installed`/`portable`/`development` mode env
  resolver (auto-detects `portable` mode here, since state lives on the
  external drive rather than an installed XDG path). Functionally the
  same role as the main repo's `lib/env.sh`, restructured as a
  standalone top-level file for this deployment.
- **`run_2025_backfill.sh`** -- standalone launcher for this host's
  assigned range (2025, full year). Unlike the main server's priority
  chain (`039-run-closed-backfill.sh`, which defers to two other
  collectors sharing the same machine), this host runs nothing else, so
  it just index-drains each group to completion then drains pending
  detail/cotizacion fetches in a plain loop -- no priority locking
  needed.

## Date-range split

- hp23 (main): 2026, full year
- hp15bw: 2025, full year

Same collector codebase and logic otherwise -- see the top-level
`src/20_pipeline/042-collect-closed-weekly-snapshot.py` for the one
script both hosts now run identically, parameterized only by device
name and date range.

## Known drift not captured here

hp15bw's own checkout currently has several *uncommitted* local edits
to shared pipeline files (`037-collect-closed-index.py`,
`039-run-closed-backfill.sh`, `common.py`, etc.) that go beyond this
deployment profile -- real logic changes, not just environment
differences. Those were deliberately left alone rather than merged in
here, since they're unreviewed and still driving hp15bw's live backfill
run. If that work is finished and meant to become the new shared
baseline, it needs its own review/merge separate from this profile.
