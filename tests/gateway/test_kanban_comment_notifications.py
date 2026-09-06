"""Opted-in progress comments reach the subscriber without waking more work."""

import asyncio

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class Transport:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, chat_id, text, metadata=None):
        if self.fail:
            raise RuntimeError("temporary transport outage")
        self.sent.append((chat_id, text, metadata))

    async def handle_message(self, event):
        pytest.fail("progress must not wake an agent")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress.db"))
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = Transport()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    conn = kbc.connect()
    tid = kb.create_task(conn, title="repair CI", assignee="worker")
    yield conn, tid, runner, adapter
    conn.close()


def subscribe(conn, tid, *, mode="notify+wake", enabled=True):
    kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="parent", thread_id="thread",
                       chat_type="thread", delivery_mode=mode, delivery_metadata={"notify_comments": enabled})


def tick(runner):
    deliveries = _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)
    for delivery in deliveries:
        asyncio.run(_KanbanNotification(runner, delivery, platform_cls=Platform,
                                       sub_fail_counts=runner._kanban_sub_fail_counts).deliver())
    return deliveries


def test_exact_comment_delivery_redaction_and_dedup(setup, monkeypatch):
    conn, tid, runner, adapter = setup
    subscribe(conn, tid)
    monkeypatch.setattr(kb.time, "time", lambda: 1700000000)
    first = kb.add_comment(conn, tid, "worker", "CI failed; logs in /Users/person/private.log")
    second = kb.add_comment(conn, tid, "worker", "CI repaired; run https://github.com/org/repo/actions/runs/123 passed")
    other = kb.create_task(conn, title="unrelated")
    unrelated = kb.add_comment(conn, other, "worker", "private unrelated task")
    assert kb.get_comment(conn, tid, unrelated) is None
    deliveries = tick(runner)
    assert [ev.payload["comment_id"] for ev in deliveries[0]["events"]] == [first, second]
    assert len(adapter.sent) == 2
    assert "CI failed" in adapter.sent[0][1]
    assert "/Users/person" not in adapter.sent[0][1]
    assert "CI repaired" in adapter.sent[1][1]
    assert "https://github.com/org/repo/actions/runs/123" in adapter.sent[1][1]
    assert all(message[2]["thread_id"] == "thread" for message in adapter.sent)
    assert tick(runner) == []


@pytest.mark.parametrize("mode,enabled", [("wake", True), ("notify", False)])
def test_comment_subscription_preferences_are_preserved(setup, mode, enabled):
    conn, tid, runner, adapter = setup
    subscribe(conn, tid, mode=mode, enabled=enabled)
    kb.add_comment(conn, tid, "worker", "progress")
    assert tick(runner) == []
    assert adapter.sent == []


def test_failed_comment_delivery_retries_without_losing_the_event(setup):
    conn, tid, runner, adapter = setup
    subscribe(conn, tid)
    cursor = kbn.list_notify_subs(conn, tid)[0]["last_event_id"]
    kb.add_comment(conn, tid, "worker", "CI needs attention")
    adapter.fail = True
    tick(runner)
    assert kbn.list_notify_subs(conn, tid)[0]["last_event_id"] == cursor
    adapter.fail = False
    tick(runner)
    assert len(adapter.sent) == 1
    assert tick(runner) == []


def test_legacy_comment_event_does_not_guess_a_body(setup):
    conn, tid, runner, adapter = setup
    subscribe(conn, tid)
    kb.add_comment(conn, tid, "worker", "must not be guessed")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload = '{}' WHERE task_id = ? AND kind = 'commented'", (tid,))
    tick(runner)
    assert adapter.sent == []
