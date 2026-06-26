# Lightweight Agile/Scrum Operating Model

This repository uses a pragmatic Scrum-inspired process suitable for a small operations team.

## Cadence

- **Sprint length:** 2 weeks.
- **Sprint planning:** select issues with explicit acceptance criteria and a clear priority label.
- **Daily check-in:** blockers, collection failures, and deployment risks.
- **Review/demo:** show monitor, installer, data integrity, and notification behavior changed in the sprint.
- **Retrospective:** record one process improvement and one technical debt item.

## Definition of Ready

A backlog item is ready when it has:

1. User/operations problem statement.
2. Acceptance criteria that can be tested by command or UI observation.
3. Risk/rollback notes for collector, archive, or notification changes.
4. Priority label (`P0`-`P3`) and owner.

## Definition of Done

A change is done only when:

1. CI passes.
2. Installation validation passes or documented environment limitations are explained.
3. README/docs are updated for user-facing behavior.
4. Runtime data (`data/`, `records/`, `records_test/`) is not committed.
5. Pull request includes validation evidence and linked issue/backlog item.

## Branching and release flow

- Branches: `feature/<short-topic>`, `fix/<short-topic>`, `docs/<short-topic>`.
- Merge by pull request after CI and review.
- Use semantic version tags for operator-facing releases.
- Keep rollback simple: collector scripts must remain runnable from the previous tagged release.
