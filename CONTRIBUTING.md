# Contributing

Thank you for improving PanamaCompra Collector. This project favors small, reviewed changes with clear operational validation.

## Workflow

1. Open or select a GitHub issue with acceptance criteria.
2. Create a branch named `feature/<topic>`, `fix/<topic>`, or `docs/<topic>`.
3. Keep runtime data out of Git (`data/`, `records/`, `records_test/`).
4. Run the validation commands below before opening a pull request.
5. Fill out the pull request template, including risk and rollback notes when collector behavior changes.

## Required local checks

```bash
python -m compileall -q .
./scripts/validate_installation.sh --skip-browser
while IFS= read -r file; do bash -n "$file"; done < <(find . -maxdepth 2 -type f -name '*.sh' -not -path './.git/*')
```

## Review standards

- Preserve sequential, single-browser collection unless a design review explicitly approves otherwise.
- Never overwrite archived record payloads unless the script is explicitly documented as a repair/re-download tool.
- Update README or docs for every user-facing command, configuration, monitor, deployment, or notification change.
- Prefer environment-variable feature flags for risky operational changes.
