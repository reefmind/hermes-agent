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

## Routine findings and durable dispatcher reconciliation

The supported idle-handoff form no longer calls the legacy active-review-only
`request_changes` function (which installations may disable). It bypasses database
initialization and retains all reopening fences, while deriving the implementation
owner from the card and checking its latest exact-head handoff and existing reviewer:

```sh
hermes kanban --board <board> request-changes <implementation-id> 'Concrete finding' \
  --expected-event-id <latest-event-id> --head-sha <40-char-sha> \
  --review-task-id <existing-review-id>
```

All three flags are required together. This operator command is not crash recovery:
worker failure markers, active/stale ownership and downstream dependencies refuse it.
The original flagless active-review command is unchanged. No worker acquires authority
to mutate another card, and no reviewer card is created or automatically dispatched.

For routine automation, the existing dispatcher tick consumes opt-in structured
`canonical_delivery` metadata from the latest normally ended, claimed implementation
run while the same card is in `review`. This is not a new daemon, supervisor, CI poller
or webhook. The implementation handoff uses the existing completion metadata channel:

```json
{
  "head_sha": "<40-char-lowercase-sha>",
  "review_task_id": "<existing-review-id>",
  "canonical_delivery": {
    "artifact": "<same-PR-URL>",
    "draft": false,
    "ci": {"head": "<same-sha>", "status": "success", "action": "hold"},
    "proof": {"head": "<same-sha>", "status": "passed"}
  }
}
```

The independent reviewer's own normally completed claimed run must acknowledge the
same artifact and head in its metadata:

```json
{"head_sha":"<same-sha>","artifact":"<same-PR-URL>","verdict":"PASS","findings":""}
```

Use `verdict: BLOCK` with concrete nonblank findings to request code changes. A recorded
CI code failure uses `ci.status: failure` and explicit `ci.action: rework`; approval,
credential, runner-availability or other operator gates use `action: hold` (the default).
Never classify missing `ci-reviewed` approval as a code failure. `success` means the
actual aggregate of all required hosted checks, including required approval gates;
never manufacture a success receipt from partial green jobs. Receipts are supplied by
owning runs, not fetched or verified against GitHub by this controller.

With matching completed independent evidence, actionable findings/CI rework return the
SAME implementation ID and owner to the standard ready queue. Each exact head is consumed
once, so repeated ticks or identical resubmissions cannot replenish attempts. Changed
code needs a new head and only the invalidated evidence. Manual reopening remains the
explicit operator escape for legacy done work or deliberate same-head recovery.
Missing/stale/malformed evidence stays parked, including with spare dispatch capacity;
legacy same-card review dispatch cannot accidentally claim an opt-in handoff. Existing
linked `delivery_review` contracts are not handled by this path or silently converted.

An exact matching independent PASS, required-CI success, non-Draft PR and proof receipt
emit one `canonical_delivery_ready` event, not a task completion/approval or merge. The
existing originating subscription renders `Ready PR (not approved or installed)` with
head and artifact. No subscription is synthesized and no live transport delivery is
claimed by a disposable notification test. The card remains `review` for Brett.

After operator adoption, verify the actual tick emits one authorization/claim, preserves
the independent card and contract, and delivers the correct origin notification. A
fresh CLI smoke alone does NOT prove the long-lived dispatcher/notifier adopted code.

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
`hermes_cli/kanban.py`, `hermes_cli/kanban_parser.py`, `hermes_cli/kanban_reopen.py`,
`hermes_cli/kanban_findings.py`, `hermes_cli/kanban_db_dispatch.py`, and
`gateway/kanban_watchers_notifier.py`. Repository CI changes belong in the integration branch,
not in the live runtime patch. Preserve the installation's current tracked/untracked
files before adoption; never reset the dirty installation to this PR's base.

```sh
git -C <candidate> diff --binary <reviewed-base> <reviewed-head> -- \
  hermes_cli/kanban.py hermes_cli/kanban_parser.py hermes_cli/kanban_reopen.py \
  hermes_cli/kanban_findings.py hermes_cli/kanban_db_dispatch.py \
  gateway/kanban_watchers_notifier.py > runtime-reopen.patch
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
