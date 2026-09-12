# Reopening unfinished canonical work

`hermes kanban --board <board> reopen <task-id>` is an explicit operator action,
not worker recovery or automatic retry. It is separate from legacy `reopen-review`:
that command can be disabled by local workflow policy and does not reopen done work.

Read the card with `hermes kanban --board <board> show <task-id> --json`.
Use its current status and maximum event `id` as the observation fence:

```sh
hermes kanban --board <board> reopen <task-id> \
  --expected-status done --expected-event-id <latest-event-id> \
  --assignee <implementation-profile> \
  --reason 'The original scope remains unfinished' --dry-run
```

Inspect the JSON dry-run result. Repeat without `--dry-run` only after authorizing
the remaining scope. For a review card use `--expected-status review`. A changed
event or status invalidates the authorization; read again, do not force it.
Read the exact card back afterward to verify the status and `canonical_reopened`
event. Dispatch may already have claimed it, so `running` with the new claim after
that event is also a valid readback.

## Safety contract

- Existing database only; this command performs no initialization or migrations.
- A dispatched worker context is refused. This is a workflow guard, not an OS-level
  security boundary: an operator with database access already controls the board.
- Any task ownership field, open run, or uncleared run ownership causes refusal.
  Expired leases are not proof that a worker is dead. Reconcile them separately.
- Cards with children are refused, including historical review children. This
  bounded command never kills workers, invalidates evidence, or rewires reviews.
- Unsatisfied parents land the card in `todo`, not `ready`.
- All guards and the update share one immediate write transaction. The explicit
  status and latest-event fence prevents duplicate/stale reopen requests.
- Only status, assignee, and completed timestamp change. Runs, result, attachments,
  comments, review contracts, failure counters and other task fields are retained.
- `canonical_reopened` records actor, reason, prior owner/status and observation.
  A `promoted_manual` event grants the dispatcher's existing one-claim continuation;
  claiming consumes it. Reopening is not successful completion or review approval.

## Adoption

Apply only this change to a compatible, preserved runtime checkout after review.
Do not replace a dirty installation with this branch: local lifecycle repairs may
not be present in its committed base. There are no configuration or schema changes.

A newly launched CLI loads the adopted files without a gateway restart. Existing
long-lived gateway/slash handlers retain imported code until operator-controlled
activation; this change does not restart or reconfigure any service. Use a fresh
operator terminal CLI for reopening, then verify the existing dispatcher consumes
its standard promotion event. A successful dry run is not permission to restart,
deploy customer software, merge a PR, or mutate production.

Focused verification:

```sh
scripts/run_tests.sh tests/hermes_cli/test_kanban_canonical_reopen.py tests/hermes_cli/test_kanban_cli.py
git diff --check
```
