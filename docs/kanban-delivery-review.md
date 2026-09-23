# Exact-head delivery continuation

## One contract, not two competing routes

`review_requirement` is the persisted, immutable pair ownership contract from the
peer's source. `delivery_review` contains only generation evidence. No task IDs,
PRs, branches, or reviewer ownership are inferred from titles or prose.

An implementation worker calls its existing `kanban_complete` on its OWN card:

```json
{
  "review_requirement": {
    "required": true,
    "owner": "orchestrator",
    "review_task_id": "<existing sole review child>"
  },
  "delivery_review": {
    "head": "<full 40-character lowercase git SHA>",
    "evidence": {
      "artifact": "<stable PR URL or repository/branch identity>",
      "checks": [{"command": "<focused command>", "result": "passed"}],
      "ci_head": "<same SHA>", "ci": "success", "draft": false,
      "proof_head": "<same SHA>", "proof": "passed"
    }
  }
}
```

For immutable local snapshots, supply `evidence.artifact_identity` as the stable
logical repository/branch identifier and `evidence.artifact` as the per-generation
snapshot location. Without `artifact_identity`, legacy `artifact` remains the
identity as before. Within an artifact its identity cannot change. To
continue a legacy handoff that used a snapshot path as identity, explicitly retain
that exact original value in `artifact_identity` and put the new snapshot in
`artifact`; do not delete history, infer identity from path prefixes, or rebind to
a different repository. The exact head and subsequent owned reviewer claim still
bind each generation; an older PASS never approves a new snapshot head.

A verified merged PR may have an explicitly authorized successor on the SAME
canonical pair. The persisted `review_requirement.owner` posts a JSON-only comment
on the implementation (owner must differ from both workers):

```json
{"delivery_artifact_successor": {
  "implementation_task": "<same implementation>", "review_task": "<same reviewer>",
  "previous_artifact": "<latest phase's logical identity>",
  "previous_head": "<latest phase's full head>",
  "merged": true, "merge_commit": "<verified full merge SHA>",
  "merge_verified_at": "<verification timestamp>",
  "merge_source": "<latest phase's artifact URL>",
  "artifact": "<authorized successor logical identity>", "head": "<new full head>"
}}
```

The owner must verify the merge against the forge and the user's successor
permission BEFORE posting; this is an auditable owner attestation, not an automatic
forge query or authority inferred from prose. The implementation supplies that
comment's integer ID in `evidence.successor_authorization_comment`. A foreign-card,
worker-authored, mismatched, unmerged or previously consumed-head receipt fails
closed. The latest enqueued phase becomes the identity anchor, preserving all
historical phases; ordinary subsequent heads on that artifact need no new receipt.
Enqueue still requires focused checks, normal safety/ownership gates and fresh
independent review. It never accepts the old artifact's PASS for the successor.

A reviewer interrupted by supported manual reclaim OR by this canonical parent's
reopen can resume for a NEW generation after a later supported operator
`promote --force`. A failed claim due to unfinished parent does not consume that
promotion. Phase completion and reviewer enqueue remain atomic; no user marks the
parent finally complete to break the dependency. For an actual crashed run, a
later supported operator `unblock` acknowledgment followed by `promote --force`
is additionally required. A forced promotion alone does not acknowledge a crash,
and an unblock alone does not dispatch a review. These operations retain the
original crash run/outcome and failure events. A new hold, live claim, timeout or
exhaustion remains closed. The controller never performs unblock/promotion itself
and never treats a failure as permission for an automatic retry.

The owned run closes into a parked handoff, not final delivery completion. The
registered tool and CLI entrypoints classify structured generation submissions
before the whole-goal judge: pending independent review cannot veto scheduling
that review. `kanban_request_review` with the same metadata uses this same
canonical-pair path; without it the existing same-card review path remains a
phase handoff, not acceptance. Ordinary final completion still uses the goal
judge. A judge's BLOCKED verdict in the worker loop is a request for the worker
to identify the next action, not authority to synthesize a human decision;
genuine blockers still use the structured block boundary and budgets stay bounded.

On the
next EXISTING dispatcher tick the controller validates the run, pair, exact SHA,
artifact identity and focused evidence. In one SQLite IMMEDIATE transaction it
records implementation PHASE completion and enqueues the existing reviewer. This
releases the dependency without asking the worker to mutate another card or
waiting for the human to complete/reopen the parent. Normal capacity/auth/quota,
claim and process-ownership gates still govern launch. No second reviewer exists.

The reviewer calls `kanban_complete` on its OWN review card with:

```json
{"delivery_review": {"head": "<exact candidate SHA>", "verdict": "BLOCK", "findings": "<actionable findings>"}}
```

or `verdict: "PASS"`. The reviewer must independently verify the candidate and
submitted evidence; metadata is not a substitute for GitHub/CI/browser evidence.
A BLOCK requires nonempty findings and returns the same implementation for ONE
claim. A corrected, NEW SHA returns to the same reviewer, including a reviewer
whose last run is done. Repeating the same pair/SHA cannot replenish permission.
A reviewer crash/timeout/exhaustion or actual blocked card never grants rework.
An already-claimed canonical reviewer may record its structured verdict if the
implementation resumed meanwhile. This exception requires the exact active run,
its claimed event, an independent reviewer and no other dependency. It only closes
the review run; the controller still rejects stale heads and independently checks
all delivery gates. It does not grant scheduling or crash-retry permission.
Scientific/authorization blockers remain parked for operator disposition.

PASS alone does not mean Ready. `delivery_accepted` requires a real claimed
independent reviewer run after this generation was enqueued, exact head match,
CI success, non-Draft, and exact-head proof. An exact PASS with incomplete delivery
gates returns the canonical implementation once per head through
`delivery_changes_requested`, not a blocked-event operator wake. A same-head
CI rerun may reuse that exact review; a source change requires a new generation
and review. Repeated incomplete evidence for the same head produces a visible
hold rather than an automatic spin. Local-commit-only work can be reviewed but does not claim
hosted CI/nonDraft/browser acceptance: do not invent those fields to get Ready.
No PR is marked nonDraft, merged, deployed, or otherwise changed by this module.
Before remediation the worker must reconcile the existing PR's live state and
head; already merged work must not be repaired by opening another PR.

Review titles do not bypass dependencies. Creation, readiness, promotion and
claims remain gated until the explicit implementation phase is released. Missing
reviewer bindings park unassigned with an actionable owner handoff, not on the
implementation worker's review lane.

## Canonical events for the notifier owner

All controller events are durable and emitted at most once per transition. The
`transition` key is `<implementation task>:<review task>:<SHA>` (hold keys append
a reason). Existing origin subscriptions/cursors are left intact.

- `delivery_phase_completed`: implementation_task, review_task, head, transition,
  acceptance="pending", evidence. This is Ready=NO.
- `delivery_changes_requested`: same pair/head fields, status="ready", review_run,
  findings. Ready here means work queue, NOT product acceptance.
- `delivery_accepted`: same fields, review_run, acceptance="ready", evidence.
- `delivery_review_hold`: same fields, reason, deduped reason-specific transition.
- Internal reviewer events: `delivery_review_enqueued` and
  `delivery_review_candidate`. The latter carries exact evidence and instructions
  above the stale opening body in `build_worker_context`.
- An ordinary `completed` event from the generation reviewer carries
  completion_kind="delivery_review_result", ready=false. It MUST NOT be rendered
  as review_approved or final Ready; the parent controller evaluates acceptance.

## Explicit delivery roles and artifact preservation

Optional `metadata.delivery_dispatch` carries `assignee`, `role`, `phase`, and
`validation_target: {kind, commit}`. Supported role/phase pairs are
`open_pr_remediation` / `pr_head` and `post_merge_validation` / `merged_checkout`.
The commit must be an exact lowercase 40-character SHA. An incompatible explicit
contract produces a visible delivery hold before claiming a worker. Untyped
legacy work retains existing behavior; prose is not interpreted as permission.
Acceptance evidence with `validation_target` must name the candidate `pr_head`;
merged-checkout or deployed claims cannot satisfy that gate.

Ancestor reopen preserves a completed PASS only when its latest completed run
names both an exact SHA and explicit artifact/repository identity. This retains
historical proof, not approval for a new head. Such descendants park with a
needs-input hold rather than scheduling another unchanged review. An operator
can resume the same reviewer for a materially changed target; downstream gates
remain unsatisfied until that review completes. Other descendants retain normal
invalidation semantics; a new generation still requires exact-head review.

A dead worker, including a clean exit without a lifecycle handoff, parks for
operator disposition on its first exit. It never grants another automatic run.
The existing protocol-violation budget still applies to explicitly resumed
attempts; provider quota cooldown remains a separate intentional requeue path.

## Durable notifications and verification

The existing notifier uses a fenced SQLite delivery ledger. Canonical phase,
changes, hold and acceptance notices use the existing origin subscription; phase
completion and reviewer verdicts say Ready=no. Only gated delivery acceptance
produces the Ready=yes origin ping. Acceptance is not evidence that the ping has
arrived: its durable acknowledgement/cursor is the transport receipt.
Ambiguous transport acceptance is reconciled against provider history,
or parked for an operator if it cannot be proven. Retrying a tick does not create
another review or resend an acknowledged phase notice. Stop notices do not wake
a new worker merely because a dependency, crash, or operator decision is pending.

Focused tests exercise actual isolated SQLite lifecycle, dispatch, completion,
notifier and CLI paths. Positive CI/proof values in fixtures are synthetic inputs,
not claims of hosted CI or deployment. Runtime activation is a separate operator
step; source tests never establish what code an already-running gateway loaded.
