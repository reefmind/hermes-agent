
from hermes_cli import kanban_db_connect
"""Bounded, opt-in handoffs to an existing review child, owned by dispatch.

No tool gains cross-card authority. See docs/kanban-delivery-review.md for the
structured run-metadata contract. Free text, titles and PR URLs are not authority.
"""
import json
import re
import time

from hermes_cli import kanban_db as kb

# Humans who may post a merged-successor receipt (see _authorized_successor).
# Brett and Meghann are the only Kanban approvers.
HUMAN_APPROVERS = frozenset({"brett", "meghann"})


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _latest(conn, tid):
    return conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()


def _event(conn, tid, kind, key):
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
        "AND json_extract(payload, '$.transition')=? ORDER BY id DESC LIMIT 1",
        (tid, kind, key),
    ).fetchone()


def _emit_once(conn, tid, kind, key, payload, run_id):
    if _event(conn, tid, kind, key):
        return False
    kb._append_event(conn, tid, kind, {"transition": key, **payload}, run_id=run_id)
    return True


# A hold the worker itself can fix is returned to that worker with the exact
# missing item instead of parking the card silently (2026-09-29: eight cards sat
# on a hold for days). Bounded per card so a worker that keeps resubmitting the
# same gap ends on the original visible hold for a human. The gate is unchanged:
# a send-back never accepts anything.
MAX_AUTO_SENDBACKS = 3


def _auto_sendbacks(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='delivery_changes_requested'",
                        (tid,)).fetchall()
    return sum(1 for r in rows if _object(r["payload"]).get("auto_sendback") is True)


def _claimed(conn, run):
    return run is not None and conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
        (run["task_id"], run["id"]),
    ).fetchone() is not None


def _healthy(task):
    return (task["claim_lock"] is None and task["worker_pid"] is None
            and not task["consecutive_failures"] and not task["last_failure_error"])


def _resolve_superseded_timeouts(conn, task, run):
    """Resolve a proven old timeout streak, only for a validated owned phase.

    This is not crash recovery or a general success reset. Failure history stays
    immutable, and failures/holds recorded after the successful run's claim win.
    Called inside the controller transaction before publishing the new phase.
    """
    count = task["consecutive_failures"]
    if not count or task["claim_lock"] is not None or task["worker_pid"] is not None:
        return
    prior = conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? AND id<? ORDER BY id DESC LIMIT ?",
        (task["id"], run["id"], count),
    ).fetchall()
    if len(prior) != count or prior[0]["error"] != task["last_failure_error"] or not all(
        r["ended_at"] is not None and (r["outcome"] == "timed_out" or
        (r["outcome"] == "gave_up" and _object(r["metadata"]).get("trigger_outcome") == "timed_out"))
        for r in prior
    ):
        return
    claim = conn.execute(
        "SELECT id FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
        (task["id"], run["id"]),
    ).fetchone()
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND id>? "
        "AND kind IN ('blocked','gave_up','timed_out','crashed','spawn_failed','rate_limited') LIMIT 1",
        (task["id"], claim["id"]),
    ).fetchone():
        return
    conn.execute("UPDATE tasks SET consecutive_failures=0, last_failure_error=NULL WHERE id=?", (task["id"],))
    kb._append_event(conn, task["id"], "delivery_phase_failure_resolved",
                     {"superseded_runs": [r["id"] for r in prior], "failures": count,
                      "reason": "validated owned implementation phase supersedes timeout streak"}, run_id=run["id"])


def is_phase_submission(conn, task, metadata):
    """Classify owned review evidence, not final completion; never grant Ready.

    Both entrypoints still use complete_task's ownership/contract fences and the
    reconciliation transaction's exact-generation validation. Malformed evidence
    parks visibly; it cannot turn this classification into final acceptance.
    """
    if task is None or not isinstance(metadata, dict) or "delivery_review" not in metadata:
        return False
    requirement = kb._merged_review_requirement(task.review_requirement, metadata.get("review_requirement"))
    return requirement is not None or kb._canonical_review_parent(conn, task.id) is not None


def is_owned_review_result(conn, task_id, metadata, expected_run_id):
    """Record an already claimed review even if its parent resumed meanwhile.

    This closes only the review run, not its parent's delivery gates. Scheduling
    still requires satisfied dependencies; unrelated dependencies remain fences.
    """
    review = _object(_object(metadata).get("delivery_review"))
    if (not isinstance(review.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", review["head"])
            or review.get("verdict") not in {"PASS", "BLOCK"}):
        return False
    parent = kb._canonical_review_parent(conn, task_id)
    task = kb.get_task(conn, task_id)
    run = _latest(conn, task_id)
    return bool(parent and task and task.status == "running"
                and expected_run_id is not None and task.current_run_id == expected_run_id
                and run and run["id"] == expected_run_id and run["ended_at"] is None
                and run["profile"] == task.assignee and _claimed(conn, run)
                and task.assignee != parent[2]
                and conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id=?",
                                 (task_id,)).fetchone()[0] == 1)


def _authorized_successor(conn, parent, child, previous, evidence, identity, head):
    """Consume the pair owner's explicit, verified merge receipt, never worker prose.

    Merge verification is performed by the owner against the forge before posting
    this receipt; like CI evidence it is not a network call inside the DB lock.
    """
    comment_id = evidence.get("successor_authorization_comment")
    if (type(comment_id) is not int or conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='delivery_phase_completed' "
            "AND json_valid(payload) AND json_extract(payload, '$.head')=? LIMIT 1",
            (parent["id"], head)).fetchone()):
        return False
    comment = conn.execute("SELECT author, body FROM task_comments WHERE task_id=? AND id=?",
                           (parent["id"], comment_id)).fetchone()
    owner = _object(parent["review_requirement"]).get("owner")
    workers = {parent["assignee"], child["assignee"]}
    # When the pair owner IS the reviewer profile (the common pr-reviewer setup)
    # no owner receipt can ever be independent of both workers, so the gate was
    # unsatisfiable. A named human approver is the independent authority then.
    author = (comment["author"] or "").strip().lower() if comment else ""
    allowed = ({owner} - workers) | (HUMAN_APPROVERS - workers)
    if not comment or author not in allowed:
        return False
    receipt = _object(_object(comment["body"]).get("delivery_artifact_successor"))
    prior = _object(previous.get("evidence"))
    expected = {"implementation_task": parent["id"], "review_task": child["id"],
                "previous_artifact": prior.get("artifact_identity", prior.get("artifact")),
                "previous_head": previous.get("head"), "artifact": identity, "head": head}
    return (all(receipt.get(k) == v for k, v in expected.items())
            and receipt.get("merged") is True
            and receipt.get("merge_source") == prior.get("artifact")
            and isinstance(receipt.get("merge_verified_at"), str) and bool(receipt["merge_verified_at"].strip())
            and isinstance(receipt.get("merge_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", receipt["merge_commit"]) is not None)


def _authorized_reclaimed_review(conn, parent, child, run):
    """Explicit release is distinct from automatic retry; retain failure history."""
    if run["outcome"] not in {"reclaimed", "crashed"}:
        return False
    reclaimed = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind IN ('reclaimed','descendant_invalidated','crashed') ORDER BY id DESC LIMIT 1",
        (child["id"], run["id"])).fetchone()
    provenance = _object(reclaimed["payload"]) if reclaimed else {}
    if not reclaimed or not (
            (reclaimed["kind"] == "reclaimed" and provenance.get("manual") is True)
            or (reclaimed["kind"] == "descendant_invalidated"
                and provenance.get("ancestor") == parent["id"]
                and provenance.get("prior_status") == "running")
            or (run["outcome"] == "crashed" and reclaimed["kind"] == "crashed")):
        return False
    if run["outcome"] == "crashed":
        # A forced promotion alone is not crash acknowledgment. The supported
        # unblock must follow the failure/hold; a newer hold wins.
        release = conn.execute(
            "SELECT id, kind FROM task_events WHERE task_id=? AND id>? "
            "AND kind IN ('unblocked','blocked','crashed','gave_up','timed_out') ORDER BY id DESC LIMIT 1",
            (child["id"], reclaimed["id"])).fetchone()
        if not release or release["kind"] != "unblocked":
            return False
        reclaimed = release  # promotion must follow acknowledgment, not precede it
    last = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? AND id>? "
        "AND kind IN ('promoted_manual','blocked','crashed','gave_up','timed_out','claimed') ORDER BY id DESC LIMIT 1",
        (child["id"], reclaimed["id"])).fetchone()
    promotion = _object(last["payload"]) if last else {}
    return bool(last and last["kind"] == "promoted_manual" and promotion.get("forced") is True
                and promotion.get("actor") and promotion["actor"] != child["assignee"])


def _create_review_child(conn, parent, requirement, head, rid):
    """Create and bind the one review child a required handoff never named.

    Only reached with zero linked children, so a rerun (child now linked and
    bound) creates nothing; the idempotency key also dedupes on (task, head).
    More than one child stays ambiguous and keeps the visible hold.
    """
    tid, owner = parent["id"], requirement["owner"]
    child_id = kb.create_task(
        conn, title=f"Independent review: {parent['title']}"[:200],
        body=(f"Independent exact-head review of {tid} at {head}. Return delivery_review "
              "metadata with head, verdict PASS/BLOCK, and findings."),
        assignee=owner, created_by="delivery-review", parents=[tid], tenant=parent["tenant"],
        workspace_kind=parent["workspace_kind"], workspace_path=parent["workspace_path"],
        idempotency_key=f"delivery-review:{tid}:{head}")
    bound = {**requirement, "review_task_id": child_id}
    conn.execute("UPDATE tasks SET review_requirement=? WHERE id=?",
                 (json.dumps(bound, sort_keys=True), tid))
    _emit_once(conn, tid, "delivery_review_sent", f"{tid}:{child_id}:{head}",
               {"head": head, "review_task": child_id, "implementation_task": tid,
                "owner": owner, "created": True}, rid)
    return child_id


def reconcile(conn):
    """One atomic controller pass. Return externally-managed review parents.

    A submitted implementation phase may be released without the worker calling
    complete on its parent (which otherwise deadlocks its review child). This is
    NOT delivery acceptance: only delivery_accepted denotes exact gate PASS.
    No retry is generated from a crash, timeout, exhaustion, or blocked verdict.
    """
    managed = set()
    holds = []
    with kanban_db_connect.write_txn(conn):
        rows = conn.execute(
            "SELECT t.*, r.metadata AS handoff_metadata, r.id AS handoff_run "
            "FROM tasks t JOIN task_runs r ON r.id=(SELECT MAX(id) FROM task_runs WHERE task_id=t.id) "
            "WHERE t.status IN ('review', 'done') AND r.ended_at IS NOT NULL "
            "AND r.outcome IN ('completed', 'review_requested')"
        ).fetchall()
        for parent in rows:
            metadata = _object(parent["handoff_metadata"])
            if "delivery_review" not in metadata:
                continue
            spec = _object(metadata["delivery_review"])
            # Verdict-only child metadata is not an implementation handoff.
            if "verdict" in spec and not parent["review_requirement"]:
                continue
            tid, rid = parent["id"], parent["handoff_run"]
            managed.add(tid)  # malformed external handoffs fail closed too
            requirement = _object(parent["review_requirement"])
            head, child_id = spec.get("head"), requirement.get("review_task_id")
            key = f"{tid}:{child_id}:{head}"
            payload = {"head": head, "review_task": child_id, "implementation_task": tid}

            def hold(reason):
                holds.append((tid, reason))
                _emit_once(conn, tid, "delivery_review_hold", key + ":" + reason,
                           {**payload, "reason": reason}, rid)

            def send_back(reason, findings, target=None, run_id=None):
                """Return *target* (default: the implementer) to ready with *findings*."""
                target, run_id = target or tid, run_id or rid
                if (_auto_sendbacks(conn, target) < MAX_AUTO_SENDBACKS
                        and _emit_once(conn, target, "delivery_changes_requested",
                                       f"{key}:sendback:{run_id}:{reason}",
                                       {**payload, "status": "ready", "auto_sendback": True,
                                        "reason": reason, "findings": findings}, run_id)):
                    conn.execute("UPDATE tasks SET status='ready', completed_at=NULL, result=NULL WHERE id=?",
                                 (target,))
                else:
                    hold(reason)

            if requirement.get("required") is not True or not requirement.get("owner"):
                # A finished card with no review requirement has nothing left to
                # gate; re-reporting it every tick only buried the real holds.
                if parent["status"] != "done":
                    hold("canonical review_requirement required")
                continue
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
                send_back("full exact commit SHA required",
                          "delivery_review.head must be the full 40-character commit SHA of the PR head "
                          f"you want reviewed (got {head!r}). Resubmit with the exact SHA.")
                continue
            children = conn.execute(
                "SELECT t.* FROM tasks t JOIN task_links l ON l.child_id=t.id WHERE l.parent_id=?",
                (tid,),
            ).fetchall()
            if child_id is None and not children and requirement["owner"] != parent["assignee"]:
                child_id = _create_review_child(conn, parent, requirement, head, rid)
                key = f"{tid}:{child_id}:{head}"
                payload["review_task"] = child_id
                children = conn.execute("SELECT * FROM tasks WHERE id=?", (child_id,)).fetchall()
            if (len(children) != 1 or children[0]["id"] != child_id
                    or not children[0]["assignee"] or children[0]["assignee"] == parent["assignee"]
                    or children[0]["tenant"] != parent["tenant"]):
                hold("handoff must name the existing sole independent review child")
                continue
            child = children[0]
            if conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id=?", (child_id,)).fetchone()[0] != 1:
                hold("review child has additional dependencies")
                continue
            run = _latest(conn, tid)
            if not _claimed(conn, run) or run["id"] != rid or run["profile"] != parent["assignee"]:
                hold("owned implementation run required; do not reassign parent to reviewer")
                continue
            evidence = _object(spec.get("evidence"))
            target = evidence.get("validation_target")
            if target is not None and (not isinstance(target, dict)
                    or target.get("kind") != "pr_head" or target.get("commit") != head):
                send_back("PR-head acceptance requires matching pr_head validation, not merged-checkout or deployed claims",
                          "evidence.validation_target must be {\"kind\": \"pr_head\", \"commit\": \"" + head + "\"} "
                          "for the PR head under review; merged-checkout or deployed claims do not count.")
                continue
            artifact = evidence.get("artifact")
            if not isinstance(artifact, str) or not artifact.strip():
                send_back("stable PR or repository/branch artifact identity required",
                          "delivery_review.evidence.artifact is missing. Set it to the PR URL "
                          "(https://github.com/<org>/<repo>/pull/<n>) whose head is " + head + ", then resubmit.")
                continue
            # Snapshot locations may change without changing logical identity.
            # A new artifact needs the pair owner's verified merged-successor
            # receipt; after enqueue, that latest phase becomes the stable anchor.
            identity = evidence.get("artifact_identity", artifact)
            if not isinstance(identity, str) or not identity.strip():
                send_back("nonempty stable artifact_identity required",
                          "delivery_review.evidence.artifact_identity is empty. Omit it or set it to the PR URL "
                          f"{artifact}.")
                continue
            previous = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='delivery_phase_completed' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            prior_phase = _object(previous["payload"]) if previous else {}
            prior_evidence = _object(prior_phase.get("evidence"))
            if (previous and prior_evidence.get("artifact_identity", prior_evidence.get("artifact")) != identity
                    and not _authorized_successor(conn, parent, child, prior_phase, evidence, identity, head)):
                prior_identity = prior_evidence.get("artifact_identity", prior_evidence.get("artifact"))
                send_back("artifact identity changed; continue the same PR/branch or supply the owner's verified merged-successor receipt",
                          f"You submitted {identity} but this card's reviewed artifact is {prior_identity}. "
                          f"Do not open a new PR: push your fix to the branch of {prior_identity} and resubmit "
                          f"with artifact {prior_identity}. If {prior_identity} was merged and {identity} is its "
                          "successor, the owner must record a verified merged-successor receipt instead.")
                continue
            checks = evidence.get("checks")
            if not isinstance(checks, list) or not checks or not all(
                isinstance(c, dict) and c.get("command") and c.get("result") == "passed" for c in checks
            ):
                send_back("focused check evidence required",
                          "delivery_review.evidence.checks must list every focused check you ran on " + head +
                          " as {\"command\": ..., \"result\": \"passed\"}: at least one, all passed. "
                          "Fix failing checks first; do not report a failing check as passed.")
                continue
            if parent["claim_lock"] is not None or parent["worker_pid"] is not None:
                hold("implementation has unresolved failure or claim")
                continue
            sent = _event(conn, tid, "delivery_phase_completed", key)
            if sent is None:
                if child["status"] not in {"todo", "ready", "done"} or not _healthy(child):
                    hold("reviewer parked or owned; operator decision required")
                    continue
                last_child_run = _latest(conn, child_id)
                if (last_child_run is not None and last_child_run["outcome"] not in {"completed", "review_requested"}
                        and not _authorized_reclaimed_review(conn, parent, child, last_child_run)):
                    hold("reviewer did not finish normally; operator decision required")
                    continue
                _resolve_superseded_timeouts(conn, parent, run)
                refreshed = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if not _healthy(refreshed):
                    hold("implementation has unresolved failure or claim")
                    continue
                # Phase completion and exact-head review enqueue are one commit.
                # Only proven superseded timeout accounting is resolved; no final
                # completion event, new card or subscription is synthesized.
                conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), tid))
                conn.execute("UPDATE tasks SET status='ready', completed_at=NULL, result=NULL WHERE id=?", (child_id,))
                _emit_once(conn, tid, "delivery_phase_completed", key,
                           {**payload, "acceptance": "pending", "evidence": evidence}, rid)
                _emit_once(conn, child_id, "delivery_review_enqueued", key,
                           {**payload, "implementation_run": rid, "status": "ready"}, rid)
                # Durable exact candidate takes precedence over stale reviewer
                # body text; retain original IDs and past generations for audit.
                kb._append_event(conn, child_id, "delivery_review_candidate", {
                    **payload, "transition": key, "evidence": evidence,
                    "instruction": "Review this exact head, not the superseded head in the opening body. "
                                   "Return delivery_review metadata with head, verdict PASS/BLOCK, and findings.",
                }, run_id=rid)
                continue
            if not _healthy(parent):
                hold("implementation has unresolved failure or claim")
                continue
            gate_rework = _event(conn, tid, "delivery_changes_requested", key + ":gates")
            if sent["run_id"] != rid and gate_rework is None:
                send_back("this exact head was already consumed; submit a new generation",
                          f"Head {head} was already reviewed. Push a new commit to the same PR that addresses "
                          "the review findings, then resubmit with the new head SHA.")
                continue
            if child["status"] != "done" or not _healthy(child):
                if child["status"] in {"blocked", "triage"} or not _healthy(child) and child["claim_lock"] is None:
                    hold("reviewer parked or failed; operator decision required")
                continue
            reviewed = _latest(conn, child_id)
            if not _claimed(conn, reviewed) or reviewed["outcome"] != "completed" or reviewed["profile"] != child["assignee"]:
                continue
            claim = conn.execute("SELECT id FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
                                 (child_id, reviewed["id"])).fetchone()
            if claim["id"] <= sent["id"]:
                continue  # a prior review is never evidence for a new candidate
            review = _object(_object(reviewed["metadata"]).get("delivery_review"))
            if review.get("head") != head:
                send_back("review head missing or stale",
                          f"Review the exact head {head} and return delivery_review metadata with "
                          "head, verdict PASS or BLOCK, and findings.", target=child_id, run_id=reviewed["id"])
                continue
            if review.get("verdict") == "BLOCK" and isinstance(review.get("findings"), str) and review["findings"].strip():
                if _emit_once(conn, tid, "delivery_changes_requested", key,
                              {**payload, "status": "ready", "review_run": reviewed["id"],
                               "findings": kb.redact_review_value(review["findings"])}, rid):
                    conn.execute("UPDATE tasks SET status='ready', completed_at=NULL, result=NULL WHERE id=?", (tid,))
                continue
            if review.get("verdict") != "PASS":
                send_back("review has no exact PASS or actionable BLOCK",
                          f"Your review of {head} returned no usable verdict. Return delivery_review metadata "
                          f"with head {head}, verdict PASS or BLOCK, and (for BLOCK) concrete findings.",
                          target=child_id, run_id=reviewed["id"])
                continue
            if not (evidence.get("ci_head") == head and evidence.get("ci") == "success"
                    and evidence.get("draft") is False and evidence.get("proof_head") == head
                    and evidence.get("proof") == "passed"):
                reason = "Review passed; continue the same PR to finish required CI, non-Draft and exact-head proof gates"
                if _emit_once(conn, tid, "delivery_changes_requested", key + ":gates",
                              {**payload, "status": "ready", "review_run": reviewed["id"],
                               "findings": reason}, rid):
                    conn.execute("UPDATE tasks SET status='ready', completed_at=NULL, result=NULL WHERE id=?", (tid,))
                else:
                    send_back("review passed; CI, nonDraft or proof gate missing/failed/stale",
                              f"Review passed for {head}. On the same PR: get CI green on {head}, mark it "
                              f"non-Draft, record exact-head proof, then resubmit evidence with ci_head={head}, "
                              f"ci=success, draft=false, proof_head={head}, proof=passed.")
                continue
            if _emit_once(conn, tid, "delivery_accepted", key,
                          {**payload, "review_run": reviewed["id"], "acceptance": "ready",
                           "evidence": evidence}, rid):
                conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), tid))
    return managed, holds
