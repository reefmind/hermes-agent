#!/usr/bin/env python3
"""Bounded, transactional recovery for the existing Hermes LaunchAgent.

The gateway owns dispatch. This helper only recovers eligible stopped tasks;
it never starts workers, sends messages, or completes work.
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

# The LaunchAgent uses the Hermes venv but runs outside its source directory.
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT if (ROOT / 'hermes_cli').is_dir() else ROOT / 'hermes-agent'
sys.path.insert(0, str(SOURCE))
from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

HARD_FAILURE = re.compile(
    r'usage limit|extra usage|429|quota|billing|auth|credential|permission|'
    r'not alive|spawn_failed|worktree add failed|not a valid branch name', re.I)
RECOVERY_LIMIT = 2
RECOVERY_WINDOW = 86400
RECOVERY_DELAY = 900
TRIAGE_RUN_CAP = 8
TRIAGE_QUIET = 21600


def recover_task(conn, task_id, *, now=None, dry_run=False):
    """Recheck policy and change state under ONE lock; retries retain history."""
    now = int(time.time()) if now is None else now
    with kb.write_txn(conn):
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['status'] not in ('blocked', 'triage'):
            return None
        # Never steal a worker, including a leaked pointer requiring reconciliation.
        if row['current_run_id'] is not None or row['worker_pid'] is not None or row['claim_lock']:
            return None
        runs = conn.execute(
            'SELECT count(*), MAX(started_at) FROM task_runs WHERE task_id=? AND started_at>?',
            (task_id, now - RECOVERY_WINDOW)).fetchone()
        if runs[0] >= TRIAGE_RUN_CAP:
            return None
        if row['status'] == 'triage':
            # Existing explicit intake can flow; old repeated failures need quiet time.
            if row['block_recurrences'] and runs[1] and runs[1] > now - TRIAGE_QUIET:
                return None
            if HARD_FAILURE.search(row['last_failure_error'] or ''):
                return None
            target, kind = 'todo', 'specified'
        else:
            if row['block_kind'] not in (None, 'transient'):
                return None
            if row['block_kind'] is None and not row['last_failure_error']:
                return None  # Untyped human block is not proven transient.
            if HARD_FAILURE.search(row['last_failure_error'] or ''):
                return None
            attempts = conn.execute(
                "SELECT count(*) FROM task_events WHERE task_id=? AND kind='auto_recovered' AND created_at>?",
                (task_id, now - RECOVERY_WINDOW)).fetchone()[0]
            if attempts >= RECOVERY_LIMIT:
                return None
            last = conn.execute(
                "SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind IN ('blocked','gave_up','auto_recovered','specified')", (task_id,)).fetchone()[0]
            delay = RECOVERY_DELAY * (2 ** attempts)
            if now - int(last or row['created_at']) < delay:
                return None
            target = kb._landing_status_after_parents(conn, task_id)
            if target == 'ready' and kb._resume_status_from_events(conn, task_id) == 'review':
                target = 'review'
            kind = 'auto_recovered'
        if dry_run:
            return target
        conn.execute(
            'UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL WHERE id=?',
            (target, task_id))
        # Intentionally preserve consecutive_failures, last_failure_error,
        # block_kind, and block_recurrences. A restart is not a success.
        kb._append_event(conn, task_id, kind, {
            'actor': 'kanban-autorecover', 'status': target,
            'previous_status': row['status'], 'failure_history_preserved': True,
        })
    return target


def board_paths(home):
    paths = [home / 'kanban.db', *sorted((home / 'kanban' / 'boards').glob('*/kanban.db'))]
    for path in paths:
        if not path.is_file():
            continue
        # A leftover empty default DB must not abort the other boards.
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as probe:
            if not probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                continue
        yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    failures = 0
    for path in board_paths(get_hermes_home()):
        try:
            # Dry-run is genuinely read-only: no migrations or event writes.
            conn = sqlite3.connect(path.as_uri() + ('?mode=ro' if args.dry_run else '?mode=rw'), uri=True, isolation_level=None, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status IN ('blocked','triage')")]
                for task_id in ids:
                    target = recover_task(conn, task_id, dry_run=args.dry_run)
                    if target:
                        print(json.dumps({'board': path.parent.name, 'task': task_id, 'status': target, 'dry_run': args.dry_run}), flush=True)
            finally:
                conn.close()
        except Exception as exc:
            failures += 1
            print(f'{path.parent.name}: recovery failed: {exc}', file=sys.stderr, flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
