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

## Returning CI failures and review findings to the same card

Record the failed check or finding, exact head, and remaining scope on the original
implementation card. If it is still running, steer its current worker; do not reopen
or launch another. Once unowned in review/done, observe and explicitly reopen that
same ID using the command above. Preserve the existing independent review card and
ask its operator to review only the changed delta. Never create a continuation card
or treat a self-review as independent acceptance. Reuse evidence for unchanged code;
rerun invalidated checks and require actual hosted success for the new head.

The immediate transaction plus ordinary claim path grant only one worker ownership.
This is an explicit operator loop, not automatic CI-webhook recovery. After adoption,
verify one `canonical_reopened`, one `promoted_manual`, and one subsequent `claimed`
event/run for that authorization. Implementation completion must still require review.
A disposable composition probe is evidence of compatibility, not proof that the
installed gateway, dispatcher, and notification path have all adopted this workflow.

## Origin notification verification (operator-owned)

Use the actual originating gateway SessionSource, not the implementing worker's
profile or a channel ID guessed from a URL. The gateway that holds Discord credentials
must own the subscription; passive status requires `notify`, not an agent wake.

```sh
hermes kanban --board <board> notify-list <task-id> --json
hermes kanban --board <board> notify-subscribe <task-id> \
  --platform discord --chat-id <actual-chat-id> --chat-type <actual-chat-type> \
  --notifier-profile <gateway-profile> --delivery-mode notify
```

Include `--thread-id <actual-thread-id>` only when present in that SessionSource.
If both empty-thread and populated-thread routes target the same Discord location,
reconcile the source first and remove only the obsolete exact subscription with
`notify-unsubscribe` and its exact platform/chat/thread arguments. Do not blanket
unsubscribe or fabricate delivery receipts. Re-read `notify-list`; configuration and
cursor advancement are not observed delivery. Verify the actual Discord message ID,
channel, event/task, and content after a legitimate lifecycle event. No config edit
or gateway restart is part of this repair.

## Narrow adoption and rollback

Brett approval and operator adoption are separate from a technical review PASS.
Export a runtime-only patch from the reviewed base/head, restricted to
`hermes_cli/kanban.py`, `hermes_cli/kanban_parser.py`, and
`hermes_cli/kanban_reopen.py`. Repository CI changes belong in the integration branch,
not in the live runtime patch. Preserve the installation's current tracked/untracked
files before adoption; never reset the dirty installation to this PR's base.

```sh
git -C <candidate> diff --binary <reviewed-base> <reviewed-head> -- \
  hermes_cli/kanban.py hermes_cli/kanban_parser.py hermes_cli/kanban_reopen.py > runtime-reopen.patch
git -C <installation> apply --check <absolute-runtime-patch>
# Operator only, after preservation and approval:
git -C <installation> apply <absolute-runtime-patch>
hermes kanban --board <board> reopen --help
```

Run the focused tests against the adopted source with a disposable HERMES_HOME,
then use a fresh operator CLI to observe/dry-run the intended card. On authorized
execution, verify its audit, single claim/run, retained review requirement and origin
message. Never clear stale ownership just to pass the fence. Long-lived consumers
may still require separately authorized activation; none is implied here.

Rollback only these code changes, after checking no subsequent edit conflicts:

```sh
git -C <installation> apply --reverse --check <absolute-runtime-patch>
git -C <installation> apply --reverse <absolute-runtime-patch>
```

A code rollback does not undo task events or kill a legitimately dispatched worker.
Leave audit/history intact and reconcile operational state via supported commands.
For repository CI rollback, revert its isolated CI commit in a reviewed branch; do
not reset shared history or merge the installation baseline into upstream main.

Focused verification:

```sh
scripts/run_tests.sh tests/hermes_cli/test_kanban_canonical_reopen.py tests/hermes_cli/test_kanban_cli.py
git diff --check
```
