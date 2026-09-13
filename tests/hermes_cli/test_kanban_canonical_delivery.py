"""Opt-in dispatch reconciliation without creating/dispatching reviewer cards."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import profiles, kanban_db_dispatch
    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    monkeypatch.setattr(kanban_db_dispatch, "_memory_pressure_level", lambda: "normal")
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def submit(conn, reviewer, head, ci="success", action="hold"):
    tid = kb.create_task(conn, title="implementation", assignee="backend")
    kb.promote_task(conn, tid, actor="operator")
    run = kb.claim_task(conn, tid, claimer="implementation")
    assert kb.request_review(conn, tid, expected_run_id=run.current_run_id, metadata={
        "head_sha": head, "review_task_id": reviewer,
        "canonical_delivery": {"artifact": "https://github.com/example/repo/pull/1", "draft": False,
            "ci": {"head": head, "status": ci, "action": action},
            "proof": {"head": head, "status": "passed"}},
    })
    return tid


def review(conn, head, verdict):
    tid = kb.create_task(conn, title="existing review", assignee="reviewer")
    kb.promote_task(conn, tid, actor="operator")
    run = kb.claim_task(conn, tid, claimer="review")
    assert kb.complete_task(conn, tid, expected_run_id=run.current_run_id, metadata={
        "head_sha": head, "verdict": verdict, "findings": "Fix ordering",
        "artifact": "https://github.com/example/repo/pull/1",
    }, fire_lifecycle_hook=False)
    return tid


@pytest.mark.parametrize("source", ["ci", "review"])
def test_dispatch_reconciles_routine_failure_once_on_same_card(board, source):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    def reconcile_delivery(conn):
        return dispatch_once(conn, max_spawn=0, reconcile_orphans=False)
    from hermes_cli import kanban_db_notify as notify
    head = "a" * 40
    reviewer = review(board, head, "BLOCK" if source == "review" else "PASS")
    tid = submit(board, reviewer, head, "failure" if source == "ci" else "success", "rework")
    origin = dict(task_id=tid, platform="discord", chat_id="fixture", thread_id="origin")
    notify.add_notify_sub(board, **origin)
    for _ in range(3):
        reconcile_delivery(board)
    assert kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, reviewer).status == "done"
    assert len(kb.list_tasks(board)) == 2
    events = [e for e in kb.list_events(board, tid) if e.kind == "changes_requested"]
    assert len(events) == 1 and events[0].payload["ready"] is False
    assert len(notify.claim_unseen_events_for_sub(board, **origin, kinds=("changes_requested",))[2]) == 1
    assert kb.claim_task(board, tid, claimer="one")
    assert not kb.claim_task(board, tid, claimer="two")


@pytest.mark.parametrize("gate", ["pass", "ci_approval", "stale_review", "stale_ci", "draft", "crash", "wrong_artifact"])
def test_exact_head_ready_gate_never_approves_or_requeues_human_holds(board, gate):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    def reconcile_delivery(conn):
        return dispatch_once(conn, max_spawn=0, reconcile_orphans=False)
    head = "a" * 40
    reviewer = review(board, "b" * 40 if gate == "stale_review" else head, "PASS")
    tid = submit(board, reviewer, head, "failure" if gate == "ci_approval" else "success")
    from hermes_cli import kanban_db_notify as notify
    origin = dict(task_id=tid, platform="discord", chat_id="fixture", thread_id="origin")
    notify.add_notify_sub(board, **origin)
    with kb.write_txn(board):
        if gate == "wrong_artifact":
            reviewed = kb.latest_run(board, reviewer)
            metadata = reviewed.metadata
            metadata["artifact"] = "https://github.com/example/repo/pull/2"
            board.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), reviewed.id))
        if gate == "crash":
            board.execute("UPDATE tasks SET consecutive_failures=1, last_failure_error='crashed' WHERE id=?", (tid,))
        if gate in {"stale_ci", "draft"}:
            run = kb.latest_run(board, tid)
            metadata = run.metadata
            if gate == "draft":
                metadata["canonical_delivery"]["draft"] = True
            else:
                metadata["canonical_delivery"]["ci"]["head"] = "b" * 40
            board.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id))
    for _ in range(2):
        reconcile_delivery(board)
    events = [e for e in kb.list_events(board, tid) if e.kind == "canonical_delivery_ready"]
    assert len(events) == (1 if gate == "pass" else 0)
    if events:
        from gateway.kanban_watchers_notifier import _KanbanNotification, TERMINAL_KINDS
        notices = notify.claim_unseen_events_for_sub(board, **origin, kinds=TERMINAL_KINDS)[2]
        assert any(e.kind == "canonical_delivery_ready" for e in notices)
        notification = _KanbanNotification(None, {"sub": origin, "task": kb.get_task(board, tid)},
                                           platform_cls=None, sub_fail_counts={})
        text = notification.format_event(events[0])
        assert "Ready PR" in text and "not approved or installed" in text and head in text
    assert not dispatch_once(board, max_spawn=2, reconcile_orphans=False, spawn_fn=lambda *args: None).spawned
    assert kb.get_task(board, tid).status == "review"
    assert kb.get_task(board, reviewer).status == "done"
