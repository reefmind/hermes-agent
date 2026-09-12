"""Explicit operator reopening of unfinished canonical work, without reclaiming workers."""
from __future__ import annotations

import contextlib
import os
import sqlite3

from hermes_cli import kanban_db as kb


def reopen_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: str,
    assignee: str, expected_status: str, expected_event_id: int, dry_run: bool = False,
) -> dict:
    """Reopen only the observed, unowned card; retain history and review contracts.

    This is not stale-run recovery or descendant invalidation. Ambiguous ownership
    and downstream work require separate operator reconciliation, never a force flag.
    """
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_RUN_ID"):
        raise ValueError("cannot reopen from a dispatched worker; operator action required")
    if expected_status not in {"review", "done"} or expected_event_id < 1:
        raise ValueError("cannot reopen without a review/done observation and event id")
    if not actor.strip() or not reason.strip() or not assignee.strip():
        raise ValueError("cannot reopen without actor, reason, and explicit assignee")
    reason = str(kb.redact_review_value(reason.strip()))
    canonical_assignee = kb._canonical_assignee(assignee)
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        observed = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (task_id,),
        ).fetchone()[0]
        if row is None or row["status"] != expected_status or observed != expected_event_id:
            raise ValueError("cannot reopen: observation changed; read the card again")
        if any(row[key] is not None for key in (
            "current_run_id", "claim_lock", "claim_expires", "worker_pid",
        )) or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND "
            "(ended_at IS NULL OR claim_lock IS NOT NULL OR worker_pid IS NOT NULL "
            "OR claim_expires IS NOT NULL) LIMIT 1", (task_id,),
        ).fetchone():
            raise ValueError("cannot reopen: active or stale ownership requires reconciliation")
        # Refuse rather than retract completed review evidence, kill descendants,
        # or allow an already-dispatchable child to race the reopened prerequisite.
        if conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? LIMIT 1", (task_id,),
        ).fetchone():
            raise ValueError("cannot reopen: downstream tasks require explicit reconciliation")
        status = kb._landing_status_after_parents(conn, task_id)
        payload = {
            "actor": actor, "reason": reason, "source_status": expected_status,
            "status": status, "previous_assignee": row["assignee"], "assignee": canonical_assignee,
            "expected_event_id": expected_event_id, "operation": "canonical_reopen",
        }
        if not dry_run:
            conn.execute(
                "UPDATE tasks SET status = ?, assignee = ?, completed_at = NULL WHERE id = ?",
                (status, canonical_assignee, task_id),
            )
            kb._append_event(conn, task_id, "canonical_reopened", payload)
            # Reuse the dispatcher's existing one-claim authorization, including
            # installations with the newer event-ID continuation fence.
            kb._append_event(conn, task_id, "promoted_manual", payload)
        return {"task_id": task_id, "reopened": not dry_run, "dry_run": dry_run, **payload}


def cmd_reopen(args) -> int:
    from hermes_cli import kanban as cli
    from hermes_cli.sqlite_safe_read import connect_tracked

    try:
        # Existing DB only: no schema migration, run recovery, or board creation.
        uri = kb.kanban_db_path().resolve().as_uri() + "?mode=rw"
        with contextlib.closing(connect_tracked(uri, uri=True, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            result = reopen_task(
                conn, args.task_id, actor=cli._profile_author(), reason=args.reason,
                assignee=args.assignee, expected_status=args.expected_status,
                expected_event_id=args.expected_event_id, dry_run=args.dry_run,
            )
    except (ValueError, sqlite3.Error) as exc:
        cli._print_json({"task_id": args.task_id, "reopened": False, "error": str(exc)})
        return 1
    cli._print_json(result)
    return 0
