"""Fenced routine CI/review findings on the existing implementation card.

This does not dispatch or approve reviews. Independent evidence and human
approval remain separate from authorizing one implementation attempt.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_reopen import reopen_task


def request_changes(conn, task_id, *, actor, reason, expected_event_id, head_sha, review_task_id):
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("request-changes requires an exact lowercase head SHA")
    if not expected_event_id or not review_task_id:
        raise ValueError("request-changes requires event id and existing review task id")
    with kb.write_txn(conn, allow_nested=True):
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("implementation task not found")
        if task["consecutive_failures"] or task["last_failure_error"]:
            raise ValueError("unresolved worker failure requires operator recovery, not routine findings")
        # Use the latest run, never search back for a matching stale artifact.
        run = kb.latest_run(conn, task_id)
        metadata = run.metadata if run else None
        if not isinstance(metadata, dict) or metadata.get("head_sha") != head_sha:
            raise ValueError("head SHA does not match latest implementation evidence")
        requirement = kb._json_dict(task["review_requirement"]) if "review_requirement" in task.keys() else {}
        bound_review = requirement.get("review_task_id") or metadata.get("review_task_id")
        if bound_review != review_task_id or task_id == review_task_id:
            raise ValueError("existing canonical independent review must match handoff evidence")
        reviewer = conn.execute("SELECT assignee FROM tasks WHERE id=?", (review_task_id,)).fetchone()
        if reviewer is None or not reviewer["assignee"] or reviewer["assignee"] == task["assignee"]:
            raise ValueError("existing review must have an independent assignee")
        # Keep the proven ownership/event/dependency and one-claim fences. In
        # particular, routine findings cannot grant crash recovery authorization.
        result = reopen_task(
            conn, task_id, actor=actor, reason=reason, assignee=task["assignee"] or "",
            expected_status="review", expected_event_id=expected_event_id,
        )
        payload = {
            "reason": str(kb.redact_review_value(reason)), "implementer": task["assignee"],
            "reviewer": reviewer["assignee"], "review_task_id": review_task_id,
            "head_sha": head_sha, "status": result["status"], "ready": False,
            "actor": actor, "expected_event_id": expected_event_id,
        }
        kb._append_event(conn, task_id, "changes_requested", payload)
        return {"task_id": task_id, "changes_requested": True, **payload}


def _unowned(conn, task):
    return task is not None and not any(task[key] is not None for key in (
        "current_run_id", "claim_lock", "claim_expires", "worker_pid",
    )) and not task["consecutive_failures"] and not task["last_failure_error"] and not conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND (ended_at IS NULL OR claim_lock IS NOT NULL "
        "OR claim_expires IS NOT NULL OR worker_pid IS NOT NULL) LIMIT 1", (task["id"],),
    ).fetchone()


def _owned_handoff(conn, task):
    run = kb.latest_run(conn, task["id"])
    if (run is None or run.outcome not in {"completed", "review_requested"}
            or run.profile != task["assignee"] or not conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
                (task["id"], run.id),
            ).fetchone()):
        return None
    return run


def reconcile_delivery(conn):
    """Consume opt-in independent-card findings in the existing dispatcher tick.

    No cards, dependencies, reviewer dispatches, approvals, or subscriptions are
    created. Stored structured receipts are evidence supplied by the owning runs,
    not a substitute for actually running CI. Missing/approval gates stay parked.
    """
    managed = set()
    with kb.write_txn(conn):
        for task in conn.execute("SELECT * FROM tasks WHERE status='review'").fetchall():
            latest = kb.latest_run(conn, task["id"])
            latest_metadata = latest.metadata if latest and isinstance(latest.metadata, dict) else {}
            if "canonical_delivery" not in latest_metadata:
                continue
            managed.add(task["id"])
            if not _unowned(conn, task):
                continue
            run = _owned_handoff(conn, task)
            metadata = run.metadata if run and isinstance(run.metadata, dict) else {}
            spec = metadata.get("canonical_delivery")
            if not isinstance(spec, dict) or "delivery_review" in metadata:
                continue  # never compete with the existing linked-review controller
            head, reviewer_id = metadata.get("head_sha"), metadata.get("review_task_id")
            if (not isinstance(reviewer_id, str) or not reviewer_id
                    or not isinstance(spec.get("artifact"), str) or not spec["artifact"].strip()):
                continue
            requirement = kb._json_dict(task["review_requirement"]) if "review_requirement" in task.keys() else {}
            if requirement.get("review_task_id", reviewer_id) != reviewer_id:
                continue
            if not kb._parents_satisfied(conn, task["id"]) or conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id=? LIMIT 1", (task["id"],),
            ).fetchone():
                continue
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
                continue
            reviewer = conn.execute("SELECT * FROM tasks WHERE id=?", (reviewer_id,)).fetchone()
            if (not _unowned(conn, reviewer) or reviewer["status"] != "done"
                    or not reviewer["assignee"] or reviewer["assignee"] == task["assignee"]
                    or reviewer["tenant"] != task["tenant"]):
                continue
            reviewed = _owned_handoff(conn, reviewer)
            verdict = reviewed.metadata if reviewed and isinstance(reviewed.metadata, dict) else {}
            if (verdict.get("head_sha") != head or verdict.get("artifact") != spec.get("artifact")):
                continue
            # All authorizations for a head are consumed once, even if a worker
            # resubmits identical failed metadata in a later normally-ended run.
            consumed = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                "('canonical_delivery_returned','canonical_delivery_ready') "
                "AND json_valid(payload) AND json_extract(payload,'$.head_sha')=?",
                (task["id"], head),
            ).fetchone()
            if consumed:
                continue
            ci, proof = spec.get("ci"), spec.get("proof")
            if not isinstance(ci, dict) or ci.get("head") != head:
                continue
            reason = None
            if verdict.get("verdict") == "BLOCK":
                reason = kb._nonblank_str(verdict.get("findings"))
            elif ci.get("status") == "failure" and ci.get("action") == "rework":
                reason = "Recorded code CI failure on " + head
            payload = {"head_sha": head, "review_task_id": reviewer_id, "review_run_id": reviewed.id}
            if reason:
                event_id = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (task["id"],)).fetchone()[0]
                try:
                    request_changes(conn, task["id"], actor="dispatcher", reason=reason,
                                    expected_event_id=event_id, head_sha=head, review_task_id=reviewer_id)
                except ValueError:
                    continue  # topology/ownership refusal is not recovery authority
                kb._append_event(conn, task["id"], "canonical_delivery_returned", payload, run_id=run.id)
            elif (verdict.get("verdict") == "PASS" and ci.get("status") == "success"
                  and isinstance(proof, dict) and proof.get("head") == head
                  and proof.get("status") == "passed" and spec.get("draft") is False
                  and isinstance(spec.get("artifact"), str) and spec["artifact"].strip()):
                kb._append_event(conn, task["id"], "canonical_delivery_ready",
                                 {**payload, "artifact": spec["artifact"], "ready": True,
                                  "approval": "pending human", "installed": False}, run_id=run.id)
    return managed


def cmd_request_changes(args):
    from hermes_cli import kanban as cli
    from hermes_cli.sqlite_safe_read import connect_tracked

    try:
        # No initialization/migration or recovery may precede the refusal gates.
        uri = kb.kanban_db_path().resolve().as_uri() + "?mode=rw"
        with contextlib.closing(connect_tracked(uri, uri=True, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            result = request_changes(
                conn, args.task_id, actor=cli._profile_author(), reason=" ".join(args.reason),
                expected_event_id=args.expected_event_id, head_sha=args.head_sha,
                review_task_id=args.review_task_id,
            )
    except (ValueError, sqlite3.Error) as exc:
        cli._print_json({"task_id": args.task_id, "changes_requested": False, "error": str(exc)})
        return 1
    cli._print_json(result)
    return 0
