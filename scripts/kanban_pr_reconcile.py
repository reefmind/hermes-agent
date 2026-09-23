#!/usr/bin/env python3
"""Finish explicit merge-wait cards only after ALL referenced PRs are merged.

Open, draft, pending, failed, unknown and closed-unmerged PRs never count as
successful delivery. Task state changes use Hermes' completion transaction and
an observation guard, retaining history and releasing dependent tasks normally.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT if (ROOT / 'hermes_cli').is_dir() else ROOT / 'hermes-agent'
PYTHON = SOURCE / 'venv' / 'bin' / 'python3'
if __name__ == '__main__' and PYTHON.exists() and Path(sys.prefix) != PYTHON.parent.parent:
    os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
sys.path.insert(0, str(SOURCE))
from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

H = get_hermes_home()
BOARDS = {
    'reefmind': (H / 'kanban/boards/reefmind/kanban.db', 'millermindsolutions-com/reefmind-project'),
    'menubridge': (H / 'kanban/boards/menubridge/kanban.db', 'millermindsolutions-com/dealer-dms-menu-bridge'),
}
# Only explicit PR references; a bare #123 may be an issue, not a PR.
PR_RE = re.compile(r'\bPR\s*#(\d+)\b', re.I)
URL_RE = re.compile(r'https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)\b', re.I)
WAITING_RE = re.compile(r'review-required|awaiting (?:review|approval|merge)|waiting (?:for|on) (?:review|approval|merge)|needs? (?:a )?(?:human )?(?:review|approval|merge)|blocked only on human|cannot self-approve', re.I)
# A needs_input label alone is not evidence that merge is the only remaining work.
OTHER_BLOCK_RE = re.compile(r'credential|device access|product decision|deployment|post.deploy|production verif|acceptance test|billing|quota', re.I)


def referenced_prs(text, repo):
    urls = URL_RE.findall(text or '')
    if any(owner.lower() != repo.lower() for owner, _ in urls):
        return set()  # Do not resolve a different repository's number locally.
    return {int(n) for n in PR_RE.findall(text or '')} | {int(n) for _, n in urls}


def observation(conn, task_id):
    row = conn.execute('''SELECT t.*,
        (SELECT body FROM task_comments WHERE task_id=t.id ORDER BY id DESC LIMIT 1) AS last_comment,
        (SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=t.id) AS event_id,
        (SELECT count(*) FROM task_runs WHERE task_id=t.id AND ended_at IS NOT NULL) AS ended_runs
        FROM tasks t WHERE t.id=?''', (task_id,)).fetchone()
    return dict(row) if row else None


def eligible(row, repo):
    if not row or row['status'] not in ('blocked', 'review') or not row['ended_runs']:
        return set()
    if row['current_run_id'] is not None or row['claim_lock'] or row['worker_pid'] is not None:
        return set()
    if row['block_kind'] in ('capability', 'dependency'):
        return set()
    latest = row['last_comment'] or ''
    if not WAITING_RE.search(latest) or OTHER_BLOCK_RE.search(latest):
        return set()
    return referenced_prs(f"{row['title']}\n{latest}", repo)


def pr_states(repo, numbers):
    states = {}
    for number in sorted(numbers):
        try:
            response = subprocess.run(
                ['gh', 'pr', 'view', str(number), '--repo', repo, '--json', 'number,state,mergedAt'],
                capture_output=True, text=True, timeout=15, check=True)
            value = json.loads(response.stdout)
            if value.get('number') == number:
                states[number] = value
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # Unknown is NOT merged.
    return states


def apply_observation(conn, row, repo, states, *, dry_run=False):
    numbers = eligible(row, repo)
    if not numbers or any(n not in states or states[n].get('state') != 'MERGED' or not states[n].get('mergedAt') for n in numbers):
        return False
    # Recheck even raw legacy edits that didn't emit an event during GitHub I/O.
    if observation(conn, row['id']) != row:
        return False
    if dry_run:
        return True
    note = f"Merge confirmed for PR(s) {', '.join('#' + str(n) for n in sorted(numbers))} in {repo}."
    result = '\n'.join(filter(None, [row['result'], note]))
    return kb.complete_task(
        conn, row['id'], result=result, summary=note,
        metadata={'reconciler': 'merge-confirmed', 'repository': repo, 'pull_requests': sorted(numbers)},
        expected_status=row['status'], expected_event_id=row['event_id'],
        fire_lifecycle_hook=False,
    )


def reconcile(board, path, repo, *, dry_run=False):
    path = Path(path)
    if not path.is_file():
        return 0
    conn = sqlite3.connect(path.as_uri() + ('?mode=ro' if dry_run else '?mode=rw'), uri=True, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = [observation(conn, r[0]) for r in conn.execute("SELECT id FROM tasks WHERE status IN ('blocked','review')").fetchall()]
        wanted = set().union(*(eligible(row, repo) for row in rows)) if rows else set()
        states = pr_states(repo, wanted)
        changed = 0
        with kb.scoped_current_board(board):
            for row in rows:
                if apply_observation(conn, row, repo, states, dry_run=dry_run):
                    changed += 1
                    print(json.dumps({'board': board, 'task': row['id'], 'action': 'merge-confirmed completion', 'dry_run': dry_run}), flush=True)
        return changed
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    failures = 0
    for board, (path, repo) in BOARDS.items():
        try:
            reconcile(board, path, repo, dry_run=args.dry_run)
        except Exception as exc:
            failures += 1
            print(f'{board}: reconcile failed: {exc}', file=sys.stderr, flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
