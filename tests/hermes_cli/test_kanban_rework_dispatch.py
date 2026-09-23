"""A review finding permits one implementation run, even with an open PR."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_db_dispatch as dispatch

@pytest.fixture
def conn(tmp_path):
    c=connect(tmp_path/'kanban.db')
    yield c
    c.close()

def ready_rework(conn):
    task=kb.create_task(conn,title='Rework existing PR',assignee='builder')
    run=kb.claim_task(conn,task)
    kb.add_comment(conn,task,author='builder',body='https://github.com/example/repo/pull/123')
    assert kb.request_review(conn,task,reviewer='reviewer',expected_run_id=run.current_run_id)
    review=kb.claim_review_task(conn,task)
    assert kb.request_changes(conn,task,reason='Fix failing check',expected_run_id=review.current_run_id)[0]
    return task

def test_review_changes_allow_existing_pr_continuation_once(conn):
    task=ready_rework(conn)
    assert dispatch.check_respawn_guard(conn,task) is None
    run=kb.claim_task(conn,task)
    assert run
    with kb.write_txn(conn):
        kb._end_run(conn,task,outcome='crashed',status='crashed')
        conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,claim_expires=NULL WHERE id=?",(task,))
    assert dispatch.check_respawn_guard(conn,task)=='active_pr'

def test_operator_can_authorize_one_ready_continuation(conn):
    task=kb.create_task(conn,title='Existing PR',assignee='builder')
    kb.add_comment(conn,task,author='builder',body='https://github.com/example/repo/pull/123')
    assert dispatch.check_respawn_guard(conn,task)=='active_pr'
    assert kb.promote_task(conn,task,actor='operator',reason='Resume the existing work')[0]
    assert dispatch.check_respawn_guard(conn,task) is None
    assert kb.claim_task(conn,task)
    assert dispatch.check_respawn_guard(conn,task)=='active_pr'

def test_continuation_never_bypasses_auth(conn):
    task=ready_rework(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?',('authentication failed: 401',task))
    assert dispatch.check_respawn_guard(conn,task)=='blocker_auth'

def test_repeated_ticks_do_not_consume_permission(conn):
    task=ready_rework(conn)
    for _ in range(3):assert dispatch.check_respawn_guard(conn,task) is None
    assert kb.get_task(conn,task).status=='ready'
