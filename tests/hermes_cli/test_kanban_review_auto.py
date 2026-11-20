"""Auto exact-head review, bounded budget auto-continue, and merged/closed PR cleanup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_review_auto as auto

PR = "https://github.com/acme/widgets/pull/7"
HEAD = "a" * 40


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(auto, "_PR_CACHE", {})
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _pr(state="OPEN", draft=False, head=HEAD, ci="success"):
    return lambda url: {"state": state, "isDraft": draft, "headRefOid": head, "ci": ci}


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _writer_in_review(conn, title="Build widget"):
    tid = kb.create_task(conn, title=title, assignee="backend")
    run = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary=f"Opened {PR}", expected_run_id=run.current_run_id)
    return tid


def _finish_review(conn, child, verdict, head=HEAD, findings=""):
    run = kb.claim_task(conn, child)
    meta = {"delivery_review": {"head": head, "verdict": verdict, "findings": findings}}
    assert kb.complete_task(conn, child, summary=f"{verdict} at {head}", metadata=meta,
                            expected_run_id=run.current_run_id)


# --- (a) one independent exact-head review card per PR+head ---------------------------


def test_handoff_creates_one_independent_review_card_per_pr_head(conn):
    writer = _writer_in_review(conn)
    first = auto.on_handoff(conn, writer, pr_lookup=_pr())
    second = auto.on_handoff(conn, writer, pr_lookup=_pr())
    review = kb.get_task(conn, first)
    assert (first == second, review.assignee, review.status) == (True, "pr-reviewer", "ready")
    assert HEAD in review.body and PR in review.body
    assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ?", (first,)).fetchone()[0] == 0


def test_new_head_gets_its_own_review_card(conn):
    writer = _writer_in_review(conn)
    first = auto.on_handoff(conn, writer, pr_lookup=_pr())
    assert auto.on_handoff(conn, writer, pr_lookup=_pr(head="b" * 40)) != first


def test_handoff_without_a_pr_creates_nothing(conn):
    tid = kb.create_task(conn, title="no pr", assignee="backend")
    run = kb.claim_task(conn, tid)
    kb.request_review(conn, tid, summary="done, no PR", expected_run_id=run.current_run_id)
    assert auto.on_handoff(conn, tid, pr_lookup=_pr()) is None


def test_pass_on_green_pr_marks_writer_done_with_ready_ping_event(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "PASS")
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    accepted = _events(conn, writer, "delivery_accepted")
    assert kb.get_task(conn, writer).status == "done"
    assert [(e["acceptance"], e["head"], e["pr"]) for e in accepted] == [("ready", HEAD, PR)]


def test_pass_is_routed_once(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "PASS")
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    assert len(_events(conn, writer, "delivery_accepted")) == 1


def test_block_reopens_writer_to_implementer_with_findings(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "BLOCK", findings="null deref in parse()")
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    task = kb.get_task(conn, writer)
    comments = [c.body for c in kb.list_comments(conn, writer)]
    assert (task.status, task.assignee) == ("ready", "backend")
    assert any("null deref in parse()" in body for body in comments)
    assert _events(conn, writer, "delivery_changes_requested")[0]["findings"] == "null deref in parse()"


def test_pass_on_draft_pr_reopens_writer_instead_of_ready(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "PASS")
    auto.reconcile_verdicts(conn, pr_lookup=_pr(draft=True))
    assert (kb.get_task(conn, writer).status, _events(conn, writer, "delivery_accepted")) == ("ready", [])


def test_pass_for_a_different_head_holds_instead_of_accepting(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "PASS", head="c" * 40)
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    assert (kb.get_task(conn, writer).status, len(_events(conn, writer, "delivery_review_hold"))) == ("review", 1)


def test_request_review_tool_creates_the_review_card(conn, monkeypatch):
    from tools import kanban_tools
    tid = kb.create_task(conn, title="tool handoff", assignee="backend")
    run = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(auto, "gh_pr_view", _pr())
    out = json.loads(kanban_tools._handle_request_review({"summary": f"ready: {PR}"}))
    assert kb.get_task(conn, out["review_task"]).assignee == "pr-reviewer"


# --- (b) one bounded auto-continue on step-budget exhaustion -----------------------------


def _running(conn):
    tid = kb.create_task(conn, title="long job", assignee="backend")
    kb.claim_task(conn, tid)
    return tid


def test_budget_exhaustion_with_saved_progress_continues_once(conn):
    tid = _running(conn)
    kb.add_comment(conn, tid, "backend", "handoff: step 3 of 5 done, commit abc")
    assert kbd.continue_after_budget(conn, tid, used=90, maximum=90) is True
    task = kb.get_task(conn, tid)
    assert (task.status, task.consecutive_failures, kb.latest_run(conn, tid).outcome) == (
        "ready", 0, "budget_continued")


def test_second_budget_exhaustion_is_not_continued(conn):
    tid = _running(conn)
    kb.add_comment(conn, tid, "backend", "handoff 1")
    kbd.continue_after_budget(conn, tid, used=90, maximum=90)
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "backend", "handoff 2")
    assert kbd.continue_after_budget(conn, tid, used=90, maximum=90) is False


def test_budget_exhaustion_without_worker_comment_is_not_continued(conn):
    tid = _running(conn)
    kb.add_comment(conn, tid, "brett", "operator note")
    assert kbd.continue_after_budget(conn, tid, used=90, maximum=90) is False


def test_budget_auto_continue_authorizes_one_respawn_past_active_pr_guard(conn):
    tid = _running(conn)
    kb.add_comment(conn, tid, "backend", f"progress on {PR}")
    kbd.continue_after_budget(conn, tid, used=90, maximum=90)
    assert kbd.check_respawn_guard(conn, tid) is None


def test_finalizer_records_failure_when_not_continued(conn):
    from agent import turn_finalizer
    import logging
    tid = _running(conn)
    turn_finalizer._record_kanban_budget_exhausted(tid, 90, 90, logging.getLogger("t"))
    assert kb.latest_run(conn, tid).outcome == "timed_out"


# --- (c) merged / closed PR cleanup -------------------------------------------------------


def test_merged_pr_completes_writer_and_archives_pending_review(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    auto._PR_CACHE.clear()  # the PR merged after the handoff
    auto.cleanup_closed_prs(conn, pr_lookup=_pr(state="MERGED"))
    assert (kb.get_task(conn, writer).status, kb.get_task(conn, child).status) == ("done", "archived")
    assert any("merged" in c.body for c in kb.list_comments(conn, writer))


def test_closed_pr_archives_writer(conn):
    writer = _writer_in_review(conn)
    auto.cleanup_closed_prs(conn, pr_lookup=_pr(state="CLOSED"))
    assert kb.get_task(conn, writer).status == "archived"


def test_open_pr_and_running_review_are_left_alone(conn):
    writer = _writer_in_review(conn)
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    kb.claim_task(conn, child)
    auto.cleanup_closed_prs(conn, pr_lookup=_pr(state="OPEN"))
    assert kb.get_task(conn, writer).status == "review"
    auto._PR_CACHE.clear()
    auto.cleanup_closed_prs(conn, pr_lookup=_pr(state="MERGED"))
    assert kb.get_task(conn, child).status == "running"


def test_existing_stuck_review_card_is_cleaned_on_first_tick(conn):
    """Adoption: a legacy card that never had an auto review is closed too."""
    tid = kb.create_task(conn, title="legacy", assignee="backend")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "backend", f"opened {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review', current_run_id = NULL WHERE id = ?", (tid,))
    auto.tick(conn, pr_lookup=_pr(state="MERGED"))
    assert kb.get_task(conn, tid).status == "done"


def test_tick_github_lookups_are_bounded(conn):
    calls = []
    for i in range(6):
        tid = kb.create_task(conn, title=f"c{i}", assignee="backend")
        kb.add_comment(conn, tid, "backend", f"https://github.com/acme/widgets/pull/{i}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    auto.tick(conn, pr_lookup=lambda url: calls.append(url) or {"state": "OPEN"}, max_lookups=3)
    assert len(calls) == 3


def test_body_reference_to_another_pr_does_not_close_the_card(conn):
    tid = kb.create_task(conn, title="stacked", body=f"stack on {PR}", assignee="backend")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    auto.cleanup_closed_prs(conn, pr_lookup=_pr(state="MERGED"))
    assert kb.get_task(conn, tid).status == "review"


def test_block_reopen_lets_the_implementer_respawn_past_active_pr_guard(conn):
    writer = _writer_in_review(conn)
    kb.add_comment(conn, writer, "backend", f"opened {PR}")
    child = auto.on_handoff(conn, writer, pr_lookup=_pr())
    _finish_review(conn, child, "BLOCK", findings="fix the race")
    auto.reconcile_verdicts(conn, pr_lookup=_pr())
    assert kbd.check_respawn_guard(conn, writer) is None
