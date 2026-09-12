"""Canonical reopen uses a fresh observation, never steals a worker's claim."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def fixture_task(conn, status):
    tid = kb.create_task(conn, title="unfinished canonical work", assignee="backend")
    # Fixture-only legacy states; production reopening must never use raw SQL.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?, completed_at=123, result='historical artifact' WHERE id=?", (status, tid))
    event = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    return tid, event


@pytest.mark.parametrize("status", ["review", "done"])
@pytest.mark.parametrize("parent_pending", [False, True])
def test_reopen_cli_preserves_history_and_regates_once(board, status, parent_pending):
    tid, event = fixture_task(board, status)
    if parent_pending:
        parent = kb.create_task(board, title="unfinished prerequisite", assignee="backend")
        kb.link_tasks(board, parent_id=parent, child_id=tid)
        event = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    shown = json.loads(cli.run_slash(f"show {tid} --json"))
    assert max(item["id"] for item in shown["events"]) == event
    command = f'reopen {tid} --expected-status {status} --expected-event-id {event} --assignee backend --reason "Original scope remains"'
    before = dict(board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    dry = cli.run_slash(command + " --dry-run")
    assert '"status": "' + ("todo" if parent_pending else "ready") + '"' in dry
    assert dict(board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    output = cli.run_slash(command)
    assert '"reopened": true' in output
    task = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert task["status"] == ("todo" if parent_pending else "ready")
    assert task["result"] == "historical artifact"
    assert task["completed_at"] is None
    events = board.execute("SELECT kind,payload FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    audit = json.loads(next(e["payload"] for e in events if e["kind"] == "canonical_reopened"))
    assert audit["reason"] == "Original scope remains"
    assert audit["source_status"] == status
    assert audit["expected_event_id"] == event
    assert audit["actor"]
    from hermes_cli.kanban_db_dispatch import _has_unclaimed_continuation
    assert _has_unclaimed_continuation(board, tid)
    assert '"reopened": true' not in cli.run_slash(command)
    if not parent_pending:
        barrier = Barrier(2)

        def claim(worker):
            with kbc.connect_closing() as conn:
                barrier.wait(timeout=10)
                return kb.claim_task(conn, tid, claimer=worker)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, ("worker-one", "worker-two")))
        assert sum(bool(result) for result in claims) == 1
        assert not _has_unclaimed_continuation(board, tid)


@pytest.mark.parametrize("hazard", ["claim_lock", "claim_expires", "worker_pid", "current_run_id", "orphan_run", "stale_event", "running", "descendant", "worker_context"])
def test_reopen_refuses_ownership_or_stale_observation_without_writes(board, monkeypatch, hazard):
    tid, event = fixture_task(board, "done")
    with kb.write_txn(board):
        if hazard in {"claim_lock", "claim_expires", "worker_pid", "current_run_id"}:
            board.execute(f"UPDATE tasks SET {hazard}=? WHERE id=?", (1, tid))
        elif hazard == "orphan_run":
            board.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (tid,))
        elif hazard == "running":
            board.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    if hazard == "stale_event":
        kb.add_comment(board, tid, "operator", "New observation invalidates old authorization")
    if hazard == "descendant":
        child = kb.create_task(board, title="downstream evidence", assignee="reviewer")
        kb.link_tasks(board, parent_id=tid, child_id=child)
        event = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    if hazard == "worker_context":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "another-task")
    before = list(board.iterdump())
    output = cli.run_slash(f'reopen {tid} --expected-status done --expected-event-id {event} --assignee backend --reason "Original scope remains"')
    assert '"reopened": true' not in output
    assert "cannot reopen" in output.lower()
    assert list(board.iterdump()) == before
