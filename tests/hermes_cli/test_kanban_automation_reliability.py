"""Actual SQLite lifecycle tests for local unattended Kanban automation."""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from scripts import kanban_recovery as recovery
from scripts import kanban_pr_reconcile as reconcile


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / 'kanban.db')
    yield conn
    conn.close()


def stopped(conn, kind='transient'):
    task = kb.create_task(conn, title='Ship change PR #123', assignee='builder')
    run = kb.claim_task(conn, task, claimer='test')
    assert run
    assert kb.block_task(conn, task, kind=kind, reason='Awaiting merge of PR #123', expected_run_id=run.current_run_id)
    kb.add_comment(conn, task, author='builder', body='Awaiting merge of PR #123')
    return task


def age(conn, task):
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET created_at=? WHERE task_id=?', (int(time.time())-4000, task))


def test_recovery_preserves_budget_and_stops_after_two(conn):
    task = stopped(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET consecutive_failures=2, last_failure_error=? WHERE id=?', ('temporary upstream outage',task))
    for attempt in range(2):
        age(conn,task)
        assert recovery.recover_task(conn,task)=='ready'
        assert kb.get_task(conn,task).consecutive_failures==2
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task,))
    age(conn,task)
    assert recovery.recover_task(conn,task) is None
    assert kb.get_task(conn,task).status=='blocked'
    assert len([e for e in kb.list_events(conn,task) if e.kind=='auto_recovered'])==2


def test_recovery_waits_and_dry_run_does_not_write(conn):
    task=stopped(conn)
    assert recovery.recover_task(conn,task) is None
    age(conn,task)
    before=conn.total_changes
    assert recovery.recover_task(conn,task,dry_run=True)=='ready'
    assert conn.total_changes==before
    assert kb.get_task(conn,task).status=='blocked'


@pytest.mark.parametrize('kind', ['needs_input','capability'])
def test_structural_blocks_remain(conn,kind):
    task=stopped(conn,kind)
    age(conn,task)
    assert recovery.recover_task(conn,task) is None


def test_recovery_regates_parent_and_restores_review(conn):
    parent=kb.create_task(conn,title='upstream',assignee='builder')
    task=stopped(conn)
    kb.link_tasks(conn,parent,task)
    age(conn,task)
    assert recovery.recover_task(conn,task)=='todo'
    review=kb.create_task(conn,title='review recovery',assignee='builder')
    run=kb.claim_task(conn,review)
    assert kb.request_review(conn,review,reviewer='reviewer',expected_run_id=run.current_run_id)
    run=kb.claim_review_task(conn,review)
    assert kb.block_task(conn,review,kind='transient',expected_run_id=run.current_run_id)
    age(conn,review)
    assert recovery.recover_task(conn,review)=='review'


def test_claimed_task_never_recovered(conn):
    task=stopped(conn)
    age(conn,task)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=12345 WHERE id=?',(task,))
    assert recovery.recover_task(conn,task) is None


def test_two_recovery_processes_only_transition_once(conn):
    task=stopped(conn); age(conn,task)
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    def act(_):
        c=sqlite3.connect(path,isolation_level=None,timeout=5); c.row_factory=sqlite3.Row
        try: return recovery.recover_task(c,task)
        finally: c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(act,range(2)))
    assert outcomes.count('ready')==1
    assert outcomes.count(None)==1


@pytest.mark.parametrize('state,merged', [('OPEN',False),('CLOSED',False),('MERGED',False)])
def test_unmerged_never_completed(conn,state,merged):
    task=stopped(conn,'needs_input'); row=reconcile.observation(conn,task)
    assert not reconcile.apply_observation(conn,row,'owner/repo',{123:{'state':state,'mergedAt':merged}})
    assert kb.get_task(conn,task).status=='blocked'


def test_every_pr_must_be_known_and_merged(conn):
    task=stopped(conn,'needs_input')
    kb.add_comment(conn,task,author='builder',body='Awaiting merge of PR #123 and PR #456')
    row=reconcile.observation(conn,task)
    assert not reconcile.apply_observation(conn,row,'owner/repo',{123:{'state':'MERGED','mergedAt':'today'}})


def test_unrelated_block_not_completed(conn):
    task=stopped(conn,'needs_input')
    kb.add_comment(conn,task,author='builder',body='Need device access; PR #123 is ready')
    row=reconcile.observation(conn,task)
    assert not reconcile.apply_observation(conn,row,'owner/repo',{123:{'state':'MERGED','mergedAt':'today'}})


def test_merge_completion_releases_child_and_is_idempotent(conn):
    task=stopped(conn,'needs_input')
    child=kb.create_task(conn,title='downstream',assignee='builder',parents=[task])
    row=reconcile.observation(conn,task)
    states={123:{'state':'MERGED','mergedAt':'today'}}
    assert reconcile.apply_observation(conn,row,'owner/repo',states)
    assert kb.get_task(conn,task).status=='done'
    assert kb.get_task(conn,child).status=='ready'
    assert kb.latest_run(conn,task).outcome=='completed'
    assert any(e.kind=='completed' for e in kb.list_events(conn,task))
    assert not reconcile.apply_observation(conn,row,'owner/repo',states)


def test_stale_observation_cannot_complete_reworked_task(conn):
    task=stopped(conn,'needs_input'); row=reconcile.observation(conn,task)
    assert kb.unblock_task(conn,task)
    assert kb.claim_task(conn,task)
    assert not reconcile.apply_observation(conn,row,'owner/repo',{123:{'state':'MERGED','mergedAt':'today'}})
    assert kb.get_task(conn,task).status=='running'


def test_completion_guard_rechecks_inside_transaction(conn):
    task=stopped(conn,'needs_input'); row=reconcile.observation(conn,task)
    kb.add_comment(conn,task,author='builder',body='New requirement before completion')
    assert not kb.complete_task(conn,task,expected_status=row['status'],expected_event_id=row['event_id'])
    assert not kb.complete_task(conn,task,expected_status='review')
    assert kb.get_task(conn,task).status=='blocked'


def test_pr_references_respect_repository():
    assert reconcile.referenced_prs('issue #123','owner/repo')==set()
    assert reconcile.referenced_prs('PR #123 https://github.com/elsewhere/repo/pull/456','owner/repo')==set()
    assert reconcile.referenced_prs('https://github.com/owner/repo/pull/123','owner/repo')=={123}


def test_empty_board_is_skipped(tmp_path):
    empty=tmp_path/'kanban/boards/default/kanban.db'; empty.parent.mkdir(parents=True)
    sqlite3.connect(empty).close()
    real=tmp_path/'kanban.db'; c=connect(real); c.close()
    assert list(recovery.board_paths(tmp_path))==[real]


def test_completion_guard_catches_event_during_staging(conn,monkeypatch):
    task=stopped(conn,'needs_input'); row=reconcile.observation(conn,task)
    original=kb._merge_completion_prose_artifacts
    def changed(*args,**kwargs):
        kb.add_comment(conn,task,author='builder',body='New requirement during staging')
        return original(*args,**kwargs)
    monkeypatch.setattr(kb,'_merge_completion_prose_artifacts',changed)
    assert not kb.complete_task(conn,task,expected_status=row['status'],expected_event_id=row['event_id'])
    assert kb.get_task(conn,task).status=='blocked'


def test_maintenance_pins_each_board_without_switching(tmp_path):
    import os
    import subprocess
    home=tmp_path/'home'; root=home/'.hermes'; (root/'logs').mkdir(parents=True)
    binary=root/'hermes-agent/venv/bin/hermes'; binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/bash\nprintf "%s|%s\\n" "$HERMES_KANBAN_BOARD" "$*" >> "$HOME/calls"\n')
    binary.chmod(0o755)
    for board in ['alpha','beta']:
        path=root/'kanban/boards'/board/'kanban.db'; path.parent.mkdir(parents=True)
        c=sqlite3.connect(path); c.execute('CREATE TABLE tasks (id TEXT,status TEXT,completed_at INTEGER)')
        c.execute("INSERT INTO tasks VALUES (?, 'done',1)",(board+'_task',)); c.commit(); c.close()
    selected=root/'kanban/current_board'; selected.write_text('interactive-board')
    fakebin=tmp_path/'bin'; fakebin.mkdir()
    (fakebin/'ps').write_text('#!/bin/sh\nexit 0\n'); (fakebin/'ps').chmod(0o755)
    script=Path(__file__).resolve().parents[2]/'scripts/kanban_maintenance.sh'
    subprocess.run(['bash',str(script)],env={**os.environ,'HOME':str(home),'PATH':str(fakebin)+':'+os.environ['PATH']},check=True,timeout=20)
    calls=(home/'calls').read_text().splitlines()
    assert len(calls)==4
    for board in ['alpha','beta']:
        assert f'{board}|kanban archive {board}_task' in calls
        assert any(line.startswith(f'{board}|kanban gc ') for line in calls)
    assert selected.read_text()=='interactive-board'


def test_dry_run_readonly_connection(conn):
    task=stopped(conn); age(conn,task)
    path=Path(conn.execute('PRAGMA database_list').fetchone()[2])
    read=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)
    read.row_factory=sqlite3.Row
    try:
        assert recovery.recover_task(read,task,dry_run=True)=='ready'
    finally:
        read.close()
    assert kb.get_task(conn,task).status=='blocked'
