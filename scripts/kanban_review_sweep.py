#!/usr/bin/env python3
"""One-shot, READ-ONLY sweep: list kanban cards stuck in ``review`` with the state of their PR.

Usage:
    python scripts/kanban_review_sweep.py --db ~/.hermes/kanban/boards/<board>/kanban.db
    python scripts/kanban_review_sweep.py --db <path> --json
    python scripts/kanban_review_sweep.py --db <path> --no-gh      # board data only, no network

It never writes: the database is opened read-only and the only external call is
``gh pr view`` (a read). It changes no card, starts no worker and leaves no
process behind. Acting on what it prints is the operator's decision.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def open_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection. A WAL database whose ``-shm`` cannot be mapped read-only is
    retried as immutable, which may miss the newest uncheckpointed writes but cannot write."""
    uri = f"file:{Path(path).expanduser().resolve()}"
    last: Optional[Exception] = None
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri + suffix, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
            return conn
        except sqlite3.Error as exc:
            last = exc
    raise SystemExit(f"cannot open {path} read-only: {last}")


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _texts(conn: sqlite3.Connection, task_id: str) -> list:
    """Every text a worker could have named the PR in, newest first."""
    out: list = []
    for table, cols in (("task_events", ("payload",)), ("task_comments", ("body",)),
                        ("task_runs", ("summary", "metadata", "error"))):
        have = [c for c in cols if c in _columns(conn, table)]
        if not have:
            continue
        for row in conn.execute(
            f"SELECT {', '.join(have)} FROM {table} WHERE task_id = ? ORDER BY id DESC", (task_id,),
        ):
            out.extend(str(v) for v in row if v)
    task_cols = [c for c in ("body", "result") if c in _columns(conn, "tasks")]
    if task_cols:
        row = conn.execute(f"SELECT {', '.join(task_cols)} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        out.extend(str(v) for v in (row or ()) if v)
    return out


def find_pr_url(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The most recently mentioned PR URL on the card, or None."""
    for text in _texts(conn, task_id):
        hits = PR_URL_RE.findall(text)
        if hits:
            return hits[-1]
    return None


def gh_pr_lookup(url: str) -> dict:
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,isDraft,headRefOid,mergedAt"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    if proc.returncode != 0:
        return {"error": (proc.stderr or "gh failed").strip().splitlines()[-1][:160]}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "unreadable gh output"}


def sweep(conn: sqlite3.Connection, pr_lookup: Optional[Callable[[str], dict]] = gh_pr_lookup,
          now: Optional[int] = None) -> list:
    now = int(now if now is not None else time.time())
    cache: dict = {}
    rows = []
    for task in conn.execute(
        "SELECT id, title, assignee FROM tasks WHERE status = 'review' ORDER BY id"
    ).fetchall():
        since = conn.execute(
            "SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (task["id"],),
        ).fetchone()[0]
        url = find_pr_url(conn, task["id"])
        pr: dict = {}
        if url and pr_lookup is not None:
            if url not in cache:
                cache[url] = pr_lookup(url) or {}
            pr = cache[url]
        rows.append({
            "task_id": task["id"],
            "assignee": task["assignee"],
            "title": task["title"],
            "idle_hours": round((now - since) / 3600, 1) if since else None,
            "pr": url,
            "pr_state": ("DRAFT" if pr.get("isDraft") and pr.get("state") == "OPEN" else pr.get("state"))
                        if pr and "error" not in pr else None,
            "pr_head": (pr.get("headRefOid") or "")[:10] or None,
            "pr_error": pr.get("error"),
        })
    return rows


def render(rows: list) -> str:
    lines = [f"{'TASK':<12} {'IDLE_H':>7} {'PR_STATE':<9} {'HEAD':<10} {'ASSIGNEE':<12} PR / TITLE"]
    for r in rows:
        state = r["pr_state"] or ("ERROR" if r["pr_error"] else ("?" if r["pr"] else "NO_PR"))
        idle = "" if r["idle_hours"] is None else f"{r['idle_hours']:.1f}"
        lines.append(f"{r['task_id']:<12} {idle:>7} {state:<9} {r['pr_head'] or '':<10} "
                     f"{(r['assignee'] or '')[:12]:<12} {r['pr'] or ''}  {(r['title'] or '')[:60]}")
    lines.append(f"{len(rows)} card(s) in review")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path, help="path to the board's kanban.db")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-gh", action="store_true", help="skip the GitHub PR state lookup")
    args = parser.parse_args(argv)
    conn = open_readonly(args.db)
    try:
        rows = sweep(conn, pr_lookup=None if args.no_gh else gh_pr_lookup)
    finally:
        conn.close()
    print(json.dumps(rows, indent=2) if args.json else render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
