"""Routine findings return the observed artifact to its existing owner."""
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
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def handoff(conn):
    reviewer = kb.create_task(conn, title="existing independent review", assignee="reviewer")
    tid = kb.create_task(conn, title="canonical implementation", assignee="backend")
    kb.promote_task(conn, tid, actor="operator")
    assert kb.claim_task(conn, tid, claimer="implementation")
    run_id = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(conn, tid, expected_run_id=run_id, metadata={
        "head_sha": "a" * 40, "review_task_id": reviewer,
    })
    event = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    return tid, reviewer, event


def command(tid, reviewer, event, head="a" * 40):
    return (f'request-changes {tid} "CI failure on current artifact" '
            f'--expected-event-id {event} --head-sha {head} --review-task-id {reviewer}')


def test_routine_findings_preserve_pair_and_dispatch_only_one_worker(board):
    tid, reviewer, event = handoff(board)
    from hermes_cli import kanban_db_notify as notify
    origin = dict(task_id=tid, platform="discord", chat_id="fixture-origin", thread_id="fixture-thread")
    notify.add_notify_sub(board, **origin)
    before_reviewer = dict(board.execute("SELECT * FROM tasks WHERE id=?", (reviewer,)).fetchone())
    output = cli.run_slash(command(tid, reviewer, event))
    assert '"changes_requested": true' in output
    assert kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, tid).assignee == "backend"
    assert dict(board.execute("SELECT * FROM tasks WHERE id=?", (reviewer,)).fetchone()) == before_reviewer
    payload = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='changes_requested' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    assert payload["head_sha"] == "a" * 40
    assert payload["review_task_id"] == reviewer
    assert payload["ready"] is False
    notices = notify.claim_unseen_events_for_sub(board, **origin, kinds=("changes_requested",))[2]
    assert len(notices) == 1
    assert not notify.claim_unseen_events_for_sub(board, **origin, kinds=("changes_requested",))[2]
    assert '"changes_requested": true' not in cli.run_slash(command(tid, reviewer, event))
    barrier = Barrier(2)
    def claim(name):
        with kbc.connect_closing() as conn:
            barrier.wait(timeout=10)
            return kb.claim_task(conn, tid, claimer=name)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(bool(result) for result in pool.map(claim, ("one", "two"))) == 1


@pytest.mark.parametrize("hazard", ["head", "reviewer", "stale", "claim", "orphan", "worker", "missing_flags", "crash"])
def test_routine_findings_refuse_without_mutation(board, monkeypatch, hazard):
    tid, reviewer, event = handoff(board)
    head = "a" * 40
    if hazard == "head":
        head = "b" * 40
    if hazard == "reviewer":
        reviewer = tid
    if hazard == "stale":
        kb.add_comment(board, tid, "operator", "new evidence")
    if hazard == "claim":
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET worker_pid=42 WHERE id=?", (tid,))
    if hazard == "orphan":
        with kb.write_txn(board):
            board.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (tid,))
    if hazard == "crash":
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET consecutive_failures=1, last_failure_error='crashed' WHERE id=?", (tid,))
    if hazard == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", reviewer)
    before = list(board.iterdump())
    cmd = command(tid, reviewer, event, head)
    if hazard == "missing_flags":
        cmd = f'request-changes {tid} "finding" --head-sha {head}'
    output = cli.run_slash(cmd)
    assert '"changes_requested": true' not in output
    assert list(board.iterdump()) == before
