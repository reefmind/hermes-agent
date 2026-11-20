"""The stuck-review sweep reports each ``review`` card with its PR state and writes nothing."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kanban_review_sweep.py"
_spec = importlib.util.spec_from_file_location("kanban_review_sweep", _SCRIPT)
sweep_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep_mod)

PR = "https://github.com/acme/widgets/pull/7"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "kanban.db"
    conn = kbc.connect(path)
    try:
        with_pr = kb.create_task(conn, title="has pr", assignee="worker")
        kb.claim_task(conn, with_pr)
        kb.add_comment(conn, with_pr, "worker", f"opened {PR}")
        without_pr = kb.create_task(conn, title="no pr", assignee="worker")
        kb.create_task(conn, title="not in review", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id IN (?, ?)", (with_pr, without_pr))
    finally:
        conn.close()
    return path, with_pr, without_pr


def test_sweep_lists_only_review_cards_with_their_pr_state(db_path):
    path, with_pr, without_pr = db_path
    conn = sweep_mod.open_readonly(path)
    try:
        rows = sweep_mod.sweep(conn, pr_lookup=lambda url: {"state": "MERGED", "headRefOid": "a" * 40})
    finally:
        conn.close()
    by_id = {r["task_id"]: r for r in rows}
    assert set(by_id) == {with_pr, without_pr}
    assert (by_id[with_pr]["pr"], by_id[with_pr]["pr_state"]) == (PR, "MERGED")
    assert (by_id[without_pr]["pr"], by_id[without_pr]["pr_state"]) == (None, None)


def test_sweep_connection_cannot_write(db_path):
    path, with_pr, _ = db_path
    conn = sweep_mod.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (with_pr,))
    finally:
        conn.close()
