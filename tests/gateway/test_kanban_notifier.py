import asyncio
import json
from types import SimpleNamespace


from gateway.config import Platform
from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _release_singleton_lock,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert {k: v for k, v in adapter.sent[0]["metadata"].items() if not k.startswith("delivery_")} == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kbn.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "stopped unexpectedly" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "stopped unexpectedly" in adapter.sent[1]["text"].lower()


def test_notifier_subscription_survives_done_reopen_until_archive(
    tmp_path, monkeypatch,
):
    """Done is reversible; archive alone ends notification ownership."""
    db_path = tmp_path / "done-reopen-archive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.sent[0]["metadata"]["thread_id"] == "origin-thread"
    assert adapter.handled[0].source.thread_id == "origin-thread"
    assert adapter.handled[0].source.profile == "reviewer"

    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1, "completion must retain the origin subscription"
        first_cursor = subs[0]["last_event_id"]
    finally:
        conn.close()

    # A quiet tick proves the completed event cannot replay after its cursor
    # was advanced, even though the subscription now remains present.
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="corrected completion")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The reopen status and second completion each deliver once, while only
    # completion wakes the exact original session/thread.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    assert all(item["chat_id"] == "origin-chat" for item in adapter.sent)
    assert adapter.handled[-1].source.thread_id == "origin-thread"
    assert adapter.handled[-1].source.profile == "reviewer"

    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["last_event_id"] > first_cursor
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Archive itself is intentionally silent, but consumes its event and
    # removes the subscription so no later historical event can replay.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    conn = kbc.connect()
    try:
        assert kbn.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def _unseen_terminal_events_for(tid, chat_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kbc.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kbn.list_notify_deliveries

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kbn, "list_notify_deliveries", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kbn.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kbn, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kbc.connect()
    try:
        _, remaining = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []


# ---------------------------------------------------------------------------
# #111125 — a repeated-block circuit breaker establishes that orchestration
# attention is needed, NOT that a human decision exists. The formatter must
# use neutral wording unless the block was typed as a genuine owner-input
# request (`needs_input`, the only kind that carries a concrete question).
# ---------------------------------------------------------------------------


class _StubEvent:
    def __init__(self, payload):
        self.payload = payload


class _StubNotif:
    head = "H123"


def _fmt_block_loop(payload):
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    msg, _, _ = _EVENT_FORMATTERS["block_loop_detected"](_StubEvent(payload), _StubNotif())
    return msg


def test_block_loop_technical_kind_uses_neutral_orchestration_wording():
    """A repeated technical block (transient/capability/untyped) routed to
    triage is an orchestration handoff with no question for the owner, so the
    ping must not claim a human decision (#111125)."""
    payload = {"reason": "waiting on upstream", "kind": "transient", "recurrences": 2}
    msg = _fmt_block_loop(payload)
    assert "for orchestration attention" in msg
    assert "human decision" not in msg
    # Circuit-breaker visibility is preserved.
    assert "TRIAGE" in msg
    assert "waiting on upstream" in msg


def test_block_loop_owner_input_keeps_decision_wording():
    """A `needs_input` block carries a concrete question for the owner, so the
    owner-decision wording is correct and must be retained (#111125)."""
    payload = {
        "reason": "Which API key should this use?",
        "kind": "needs_input",
        "recurrences": 2,
        "limit": kb.BLOCK_RECURRENCE_LIMIT,
    }
    msg = _fmt_block_loop(payload)
    assert "needs a human decision" in msg
    assert "for orchestration attention" not in msg
    assert "Which API key should this use?" in msg


# ---------------------------------------------------------------------------
# Handoffs that hand a decision back to the origin must wake it, not only ping
# it: `review_requested` (implementation done, waiting for a reviewer) and
# `block_loop_detected` (routed to triage) are terminal kinds just like
# `blocked`.
# ---------------------------------------------------------------------------


def _wake_text(adapter):
    """Text of the single synthetic wake turn injected into the adapter."""
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


def _review_handoff_task(
    *,
    delivery_mode="notify+wake",
    summary="PR ready: https://example.invalid/pr/7\nfull details below",
):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="implement the thing",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary=summary, expected_run_id=run_id,
        ) is True
        return tid
    finally:
        conn.close()


def test_review_requested_wakes_the_origin_session(tmp_path, monkeypatch):
    """A review handoff wakes the origin and carries the worker's summary."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-wake.db"))
    kb.init_db()
    tid = _review_handoff_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "the passive review ping is unchanged"

    wake = _wake_text(adapter)
    assert tid in wake
    assert "PR ready: https://example.invalid/pr/7" in wake, (
        "the worker's handoff must ride the wake turn like it does for "
        "`completed`, otherwise the woken reviewer has to re-read the board"
    )


def test_block_loop_detected_notifies_without_waking_the_agent(tmp_path, monkeypatch):
    """Current operator policy pages a human for triage without agent wake."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage-wake.db"))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="loops forever",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert adapter.handled == []


def test_review_requested_does_not_wake_a_notify_only_subscription(
    tmp_path, monkeypatch,
):
    """delivery_mode still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-notify.db"))
    kb.init_db()
    _review_handoff_task(delivery_mode="notify")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "notify-only subscriptions must not be woken by a review handoff"
    )


# ---------------------------------------------------------------------------
# Meghann, 2026-09-25: never tag the retired @pr-reviewer, and ask once when a
# card's Discord thread is unknown.
# ---------------------------------------------------------------------------


def test_delivery_hold_never_tags_a_retired_owner_and_mentions_the_humans(monkeypatch):
    from hermes_cli import config as hermes_config
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    monkeypatch.setattr(hermes_config, "load_config_readonly",
                        lambda: {"kanban": {"retired_assignees": {"pr-reviewer": "integrator"}}})
    monkeypatch.setenv("REEFMIND_DISCORD_INTAKE_NOTIFY_USERS", "923685524860981310,143529998664335360")
    ev = SimpleNamespace(kind="delivery_review_hold", payload={"head": "abc123", "reason": "reviewer parked or owned; operator decision required"})
    n = SimpleNamespace(task=SimpleNamespace(assignee="backend", review_requirement={"owner": "pr-reviewer"}),
                        sub={"task_id": "t_hold"}, d={}, title="hold card", board_tag="")

    msg, _, _ = _EVENT_FORMATTERS["delivery_review_hold"](ev, n)

    assert "pr-reviewer" not in msg
    assert "Who: <@923685524860981310> or <@143529998664335360> — " in msg


def test_card_without_a_thread_asks_for_one_exactly_once(tmp_path, monkeypatch):
    from hermes_cli import config as hermes_config
    from gateway import kanban_watchers_notifier as notifier

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "no-thread.db"))
    monkeypatch.setattr(hermes_config, "load_config_readonly",
                        lambda: {"kanban": {"default_notify_target": {"reefmind": "chan-kanban"}}})
    monkeypatch.setattr(notifier, "_triage_thread_for", lambda *a, **k: None)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="orphan card", assignee="backend")
        collector = notifier._Collector.__new__(notifier._Collector)
        collector.notifier_profile = None
        for _ in range(2):   # a second tick must not ask again
            collector._ensure_default_subs(conn, "reefmind")
        conn.execute("DELETE FROM kanban_notify_subs")   # nor after the sub is gone
        collector._ensure_default_subs(conn, "reefmind")
        asks = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='thread_unknown'",
                            (tid,)).fetchall()
    finally:
        conn.close()

    assert len(asks) == 1
    ev = SimpleNamespace(kind="thread_unknown", payload=json.loads(asks[0]["payload"]))
    msg, _, _ = notifier._EVENT_FORMATTERS["thread_unknown"](ev, SimpleNamespace(task=None, title="orphan card"))
    assert (f"hermes kanban --board reefmind notify-subscribe {tid} --platform discord "
            "--chat-id <forum> --thread-id <thread> --chat-type thread") in msg


# ---------------------------------------------------------------------------
# Meghann, 2026-09-26: stop notices are at most three plain lines (plus the PR
# url). No SHAs, card ids, board tags or role handles in the rendered text.
# ---------------------------------------------------------------------------

_SHA = "a1efddf0" * 5
_PR = "https://github.com/millermindsolutions-com/reefmind-project/pull/3627"


def _plain_n(status="blocked", pending=()):
    task = SimpleNamespace(id="t_15cc7fda", assignee="backend", status=status, result="", branch_name=None,
                           title="INT-3623 implementation · Automatic asset content fingerprints (replace ?v= pins)",
                           review_requirement={"owner": "pr-reviewer"})
    return SimpleNamespace(task=task, sub={"task_id": "t_15cc7fda"}, d={"pending_parents": list(pending), "pr_urls": []},
                           title=task.title, board_tag="[reefmind] ", board_slug="reefmind", task_id="t_15cc7fda")


def test_delivery_hold_renders_the_plain_worked_example(monkeypatch):
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    monkeypatch.setenv("REEFMIND_DISCORD_INTAKE_NOTIFY_USERS", "923685524860981310,143529998664335360")
    ev = SimpleNamespace(kind="delivery_review_hold", payload={
        "head": _SHA, "implementation_task": "t_15cc7fda", "review_task": "t_27a96b9f",
        "reason": "reviewer parked or owned; operator decision required", "evidence": {"artifact": _PR}})

    msg, _, _ = _EVENT_FORMATTERS["delivery_review_hold"](ev, _plain_n())

    assert msg == ("⏸ INT-3623 Automatic asset content fingerprints — stuck\n"
                   "Why: The reviewer stopped before finishing.\n"
                   "Who: <@923685524860981310> or <@143529998664335360> — say retry or drop.\n"
                   f"{_PR}")


def test_every_stop_notice_is_short_and_free_of_ids_shas_and_roles(monkeypatch):
    import re
    from gateway import kanban_watchers_notifier as notifier

    monkeypatch.setenv("REEFMIND_DISCORD_INTAKE_NOTIFY_USERS", "923685524860981310,143529998664335360")
    noisy = {"head": _SHA, "implementation_task": "t_15cc7fda", "review_task": "t_27a96b9f",
             "reason": f"full exact commit SHA required for {_SHA} @integrator t_27a96b9f",
             "error": f"@backend crashed at {_SHA}", "summary": f"t_15cc7fda done at {_SHA} @orchestrator",
             "findings": "exact head evidence stale", "reviewer": "pr-reviewer", "owner": "pr-reviewer",
             "implementer": "backend", "limit_seconds": 3600, "recurrences": 3}
    cases = [
        (notifier._notice_completed, "completed", {"completion_kind": "delivery_review_result", "review_handoff": {}}),
        (notifier._notice_completed, "completed", {"completion_kind": "implementation_ready_for_review",
                                                   "review_handoff": {"review_task_id": "t_27a96b9f"}}),
        (notifier._notice_delivery_phase_completed, "delivery_phase_completed", {"evidence": {"artifact": "x"}}),
        (notifier._notice_delivery_phase_completed, "delivery_changes_requested", {}),
        (notifier._notice_delivery_phase_completed, "delivery_accepted", {"acceptance": "ready"}),
        (notifier._notice_delivery_phase_completed, "delivery_accepted", {}),
        (notifier._notice_delivery_phase_completed, "delivery_review_hold", {}),
        (notifier._notice_blocked, "dependency_wait", {}),
        (notifier._notice_blocked, "blocked", {"kind": "needs_input"}),
        (notifier._notice_gave_up, "gave_up", {}),
        (notifier._notice_crashed, "crashed", {}),
        (notifier._notice_timed_out, "timed_out", {}),
        (notifier._notice_review_requested, "review_handoff_required", {}),
        (notifier._notice_review_requested, "review_requested", {}),
        (notifier._notice_block_loop_detected, "block_loop_detected", {}),
    ]
    for fmt, kind, extra in cases:
        ev = SimpleNamespace(kind=kind, payload={**noisy, **extra})
        pending = [{"id": "t_0badc0de", "title": "parent", "status": "running"}] if kind == "dependency_wait" else ()
        msg, _, _ = fmt(ev, _plain_n(pending=pending))
        lines = msg.splitlines()
        assert len(lines) <= 4 and lines[1].startswith("Why: ") and lines[2].startswith("Who: "), (kind, msg)
        assert not re.search(r"\b[0-9a-f]{40}\b|\bt_[0-9a-f]{6,}|(?<!<)@[A-Za-z]|\[reefmind\]", msg), (kind, msg)
        assert not re.search(r"canonical pair|artifact identity|exact head|evidence|Ready:", msg), (kind, msg)


def test_every_known_hold_reason_maps_to_a_plain_sentence():
    from gateway.kanban_watchers_notifier_plain import UNKNOWN_REASON, plain_reason

    reasons = [
        "reviewer parked or owned; operator decision required",
        "reviewer parked or failed; operator decision required",
        "reviewer did not finish normally; operator decision required",
        "artifact identity changed; continue the same PR/branch or cite the owner",
        "review has no exact PASS or actionable BLOCK",
        "handoff must name the existing sole independent review child",
        "legacy review handoff lacks an exact head; implementer must resubmit delivery_review",
        "canonical review_requirement required",
        "review child has additional dependencies",
        "full exact commit SHA required",
        "stable PR or repository/branch artifact identity required",
        "review head missing or stale",
        "review passed; delivery gates still incomplete: CI",
        "exact-head proof gates: nonDraft",
        "focused check evidence required",
        "implementation has unresolved failure or claim",
    ]
    for reason in reasons:
        assert plain_reason(reason)[0] != UNKNOWN_REASON, reason
    assert plain_reason("something new")[0] == UNKNOWN_REASON
