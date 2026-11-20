"""Automatic exact-head review for plain review handoffs, and PR-state cleanup.

Three behaviors, all owned by the dispatcher tick or the worker's own handoff:

* ``on_handoff``: a writer called ``kanban_request_review`` naming a PR. Create
  ONE independent review card for that PR's exact head (idempotent per
  PR+head). The card is a normal ready-lane task, so it dispatches even when
  ``kanban.review_dispatch`` is off, and it has no dependency link, so the
  writer staying in ``review`` never parks it in ``todo``.
* ``reconcile_verdicts``: the review card finished. PASS with the PR open,
  non-Draft, unchanged head and green checks -> writer ``done`` + a
  ``delivery_accepted`` (acceptance ``ready``) event, which the notifier renders
  as the Ready ping in the writer card's subscribed thread. BLOCK -> the writer
  is reopened to its implementer with the findings.
* ``cleanup_closed_prs``: a writer card in ``review`` whose PR is MERGED is
  completed with a note; CLOSED (unmerged) is archived with a note. Pending
  (not running) auto/delivery review cards for such a PR are archived. Cleanup
  events are deliberately silent (not notifier kinds): adopting this must not
  ping every thread that had a stale card.

The structured ``delivery_review`` contract stays owned by
``kanban_review_reconcile``; cards carrying a ``review_requirement`` are skipped.
Nothing here approves or merges a PR. ``HERMES_KANBAN_AUTO_REVIEW=0`` disables
all three.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Callable, Optional

from hermes_cli import kanban_db as kb

log = logging.getLogger(__name__)

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
CREATOR = "auto-review"
DEFAULT_REVIEWER = "pr-reviewer"
_HEAD_KEYS = ("head", "head_sha", "exact_head", "pr_head", "commit")
# Bounded GitHub use from the dispatcher tick.
PR_CACHE_TTL = 600
MAX_LOOKUPS_PER_TICK = 3
# A PASS whose checks are still running waits this long for them before the
# owner is asked to look instead.
CI_PENDING_GRACE = 6 * 3600

PrLookup = Callable[[str], dict]
_PR_CACHE: dict[str, tuple[float, dict]] = {}


def enabled() -> bool:
    return os.environ.get("HERMES_KANBAN_AUTO_REVIEW", "1").strip() not in {"0", "false", "no", "off"}


def gh_pr_view(url: str) -> dict:
    """``gh pr view`` reduced to state/draft/head/ci. ``{"error": ...}`` on any failure."""
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    try:
        proc = subprocess.run(
            [gh, "pr", "view", url, "--json", "state,isDraft,headRefOid,statusCheckRollup"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"error": str(exc)[:160]}
    if not isinstance(data, dict):
        return {"error": ((proc.stderr or "gh failed").strip().splitlines() or ["gh failed"])[-1][:160]}
    data["ci"] = ci_state(data.pop("statusCheckRollup", None))
    return data


def ci_state(rollup) -> str:
    """``success`` / ``failure`` / ``pending`` / ``none`` from a statusCheckRollup list."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    states = set()
    for check in rollup:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").upper()
        result = str(check.get("conclusion") or check.get("state") or "").upper()
        if status and status != "COMPLETED" and not result:
            states.add("pending")
        elif result in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            states.add("success")
        elif result in {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", ""}:
            states.add("pending")
        else:
            states.add("failure")
    for state in ("failure", "pending", "success"):
        if state in states:
            return state
    return "none"


def _lookup(url: str, pr_lookup: Optional[PrLookup], budget: Optional[list] = None,
            now: Optional[float] = None, *, fresh: bool = False) -> Optional[dict]:
    """PR state, cached for ``PR_CACHE_TTL`` unless ``fresh``. ``budget`` is a
    one-item counter of uncached lookups left this tick (None = unbounded)."""
    now = time.time() if now is None else now
    hit = _PR_CACHE.get(url)
    if not fresh and hit and now - hit[0] < PR_CACHE_TTL:
        return hit[1]
    if budget is not None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
    data = (pr_lookup or gh_pr_view)(url) or {}
    _PR_CACHE[url] = (now, data)
    return data


def _obj(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _event(conn, task_id: str, kind: str):
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()


def _head_from(metadata: dict, text: str) -> Optional[str]:
    review = _obj(metadata.get("delivery_review"))
    for source in (review, metadata):
        for key in _HEAD_KEYS:
            value = source.get(key)
            if isinstance(value, str) and SHA_RE.fullmatch(value.strip().lower()):
                return value.strip().lower()
    found = SHA_RE.findall(text or "")
    return found[-1] if found else None


def _handoff_run(conn, task_id: str):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND outcome = 'review_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()


def find_pr_url(conn, task_id: str, run=None) -> Optional[str]:
    """The PR the handoff names: the handoff run first, then the newest comment."""
    if run is not None:
        hits = PR_URL_RE.findall(f"{run['summary'] or ''}\n{run['metadata'] or ''}")
        if hits:
            return hits[-1]
    for row in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (task_id,),
    ):
        hits = PR_URL_RE.findall(row["body"] or "")
        if hits:
            return hits[-1]
    # The card body is not consulted: it routinely cites OTHER PRs ("stack on
    # PR #2"), and closing a card on a referenced PR's merge would be wrong.
    row = conn.execute("SELECT result FROM tasks WHERE id = ?", (task_id,)).fetchone()
    hits = PR_URL_RE.findall(row["result"] or "") if row else []
    return hits[-1] if hits else None


def _review_body(task, url: str, head: str) -> str:
    verdict = json.dumps({"delivery_review": {"head": head, "verdict": "PASS or BLOCK",
                                              "findings": "concrete findings (required for BLOCK)"}})
    return (
        f"Independent exact-head review of card {task.id} ({task.title}).\n\n"
        f"PR: {url}\nExact head: {head}\n\n"
        "Review ONLY this commit. You did not write this change; review it as an outsider "
        "(correctness, tests, scope, security). Do not push to the branch, approve, or merge: "
        "Brett is the only approver.\n\n"
        f"Finish with kanban_complete and metadata {verdict}. PASS posts the Ready ping to the "
        "writer's thread once the PR is non-Draft with green checks; BLOCK reopens the writer "
        "card with your findings."
    )


def on_handoff(conn, task_id: str, *, pr_lookup: Optional[PrLookup] = None,
               reviewer: str = DEFAULT_REVIEWER) -> Optional[str]:
    """Create the one independent review card for a plain review handoff.

    Returns the review card id, or None when the handoff names no PR, the PR is
    already merged/closed, no exact head can be established, the writer named
    its own reviewer, or the card is on the structured delivery-review path.
    """
    if not enabled():
        return None
    task = kb.get_task(conn, task_id)
    if task is None or task.status != "review" or getattr(task, "review_requirement", None):
        return None
    handoff = _obj((_event(conn, task_id, "review_requested") or {"payload": None})["payload"])
    if handoff.get("reviewer"):
        return None  # an explicitly named same-card reviewer owns this review
    run = _handoff_run(conn, task_id)
    url = find_pr_url(conn, task_id, run)
    if not url:
        return None
    pr = _lookup(url, pr_lookup, fresh=True) or {}
    if pr.get("state") in {"MERGED", "CLOSED"}:
        return None
    head = pr.get("headRefOid") if isinstance(pr.get("headRefOid"), str) else None
    if not (head and SHA_RE.fullmatch(head)):
        head = _head_from(_obj(run["metadata"]) if run else {}, run["summary"] if run else "")
    implementer = handoff.get("implementer") or (run["profile"] if run else None) or task.assignee
    if not head and "error" in pr and not _event(conn, task_id, "auto_review_pending"):
        # GitHub unreachable and no SHA in the handoff: the tick retries (bounded).
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "auto_review_pending", {"pr": url, "error": pr["error"]})
    if not head or reviewer == implementer:
        return None
    key = f"{CREATOR}:{url}@{head}"
    existing = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()
    if existing is not None:
        verdict = _event(conn, existing["id"], "auto_review_verdict")
        if verdict is not None and _obj(verdict["payload"]).get("verdict") != "PASS":
            # Same head handed off again after a BLOCK: nothing changed to review.
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "delivery_review_hold", {
                    "pr": url, "head": head, "review_task": existing["id"],
                    "reason": f"Head {head[:10]} was already reviewed and blocked. Push a fix to {url} "
                              "and hand off again.",
                })
        return existing["id"]
    child = kb.create_task(
        conn, title=f"Independent review: {task.title}"[:200], body=_review_body(task, url, head),
        assignee=reviewer, created_by=CREATOR, tenant=task.tenant, idempotency_key=key,
        independent_review=True, priority=task.priority,
    )
    with kb.write_txn(conn):
        target = {"writer": task_id, "pr": url, "head": head, "implementer": implementer}
        kb._append_event(conn, child, "auto_review_target", target)
        kb._append_event(conn, task_id, "auto_review_requested", {**target, "review_task": child})
    return child


def handoff_pending(conn) -> list:
    """Review cards whose handoff could not create a review (e.g. gh was down)."""
    return [r["id"] for r in conn.execute(
        "SELECT t.id FROM tasks t WHERE t.status = 'review' AND t.review_requirement IS NULL "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = 'auto_review_pending' "
        "  AND e.id > COALESCE((SELECT MAX(id) FROM task_events x WHERE x.task_id = t.id "
        "  AND x.kind IN ('auto_review_requested', 'delivery_review_hold')), 0))"
    )]


def _findings_text(review: dict) -> str:
    findings = review.get("findings")
    if isinstance(findings, list):
        findings = "\n".join(f"- {f}" if not isinstance(f, dict) else f"- {json.dumps(f)}" for f in findings)
    return kb.redact_review_value(str(findings or "").strip())[:4000]


def _reopen_writer(conn, writer: str, *, target: dict, review_task: str, reason: str, findings: str) -> bool:
    body = f"Independent review ({review_task}) of {target['pr']} at {target['head'][:10]}: {reason}"
    if findings:
        body += f"\n\nFindings:\n{findings}"
    if not kb.reopen_review_task(conn, writer, reason_comment=(CREATOR, body)):
        return False
    with kb.write_txn(conn):
        kb._append_event(conn, writer, "delivery_changes_requested", {
            "pr": target["pr"], "head": target["head"], "review_task": review_task,
            "status": "ready", "auto_review": True, "reason": reason,
            "findings": findings or reason,
        })
    return True


def _consume(conn, child: str, payload: dict) -> None:
    with kb.write_txn(conn):
        kb._append_event(conn, child, "auto_review_verdict", payload)


def reconcile_verdicts(conn, *, pr_lookup: Optional[PrLookup] = None, budget: Optional[list] = None,
                       now: Optional[int] = None) -> list:
    """Route each finished, unconsumed auto-review card's verdict to its writer."""
    now = int(time.time()) if now is None else now
    routed = []
    rows = conn.execute(
        "SELECT t.id, t.completed_at FROM tasks t WHERE t.created_by = ? AND t.status = 'done' "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = 'auto_review_verdict')",
        (CREATOR,),
    ).fetchall()
    for row in rows:
        child = row["id"]
        target = _obj((_event(conn, child, "auto_review_target") or {"payload": None})["payload"])
        writer_id, head, url = target.get("writer"), target.get("head"), target.get("pr")
        if not (writer_id and head and url):
            _consume(conn, child, {"verdict": None, "outcome": "no_target"})
            continue
        writer = kb.get_task(conn, writer_id)
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
            "ORDER BY id DESC LIMIT 1", (child,),
        ).fetchone()
        meta = _obj(run["metadata"]) if run else {}
        review = _obj(meta.get("delivery_review")) or meta
        verdict = str(review.get("verdict") or "").strip().upper()
        verdict = {"FAIL": "BLOCK", "REQUEST_CHANGES": "BLOCK", "APPROVE": "PASS"}.get(verdict, verdict)
        reviewed_head = _head_from(review, "")
        if writer is None or writer.status != "review":
            _consume(conn, child, {"verdict": verdict, "writer": writer_id, "outcome": "writer_moved_on"})
            continue
        base = {"pr": url, "head": head, "review_task": child}
        if verdict not in {"PASS", "BLOCK"} or reviewed_head != head:
            reason = ("The review finished without a PASS or BLOCK verdict for the exact head."
                      if verdict not in {"PASS", "BLOCK"} else
                      f"The review covered {str(reviewed_head)[:10]}, not the handed-off head {head[:10]}.")
            with kb.write_txn(conn):
                kb._append_event(conn, writer_id, "delivery_review_hold", {**base, "reason": reason})
            _consume(conn, child, {"verdict": verdict or None, "writer": writer_id, "outcome": "hold"})
            routed.append((writer_id, "hold"))
            continue
        if verdict == "BLOCK":
            findings = _findings_text(review) or "The reviewer returned BLOCK without findings."
            ok = _reopen_writer(conn, writer_id, target=target, review_task=child,
                                reason="the review asked for changes.", findings=findings)
            _consume(conn, child, {"verdict": "BLOCK", "writer": writer_id,
                                   "outcome": "reopened" if ok else "reopen_refused"})
            routed.append((writer_id, "reopened"))
            continue
        # Fresh read: the Ready gate must see the current head/draft/checks.
        pr = _lookup(url, pr_lookup, budget, now, fresh=True)
        if pr is None:
            continue  # out of lookups this tick
        state, ci = pr.get("state"), pr.get("ci")
        if state in {"MERGED", "CLOSED"}:
            _consume(conn, child, {"verdict": "PASS", "writer": writer_id, "outcome": f"pr_{state.lower()}"})
            continue  # cleanup_closed_prs owns the writer
        gate = None
        if "error" not in pr:
            if pr.get("headRefOid") and pr["headRefOid"] != head:
                gate = (f"the PR head moved to {pr['headRefOid'][:10]} after the review; hand off again "
                        "so the new head gets reviewed.")
            elif pr.get("isDraft"):
                gate = "review passed, but the PR is still a Draft; mark it ready for review."
            elif ci == "failure":
                gate = f"review passed, but checks are failing on {head[:10]}; fix them on the same PR."
            elif ci == "pending" and now - int(row["completed_at"] or now) < CI_PENDING_GRACE:
                continue  # checks still running: look again on a later tick
        if gate:
            ok = _reopen_writer(conn, writer_id, target=target, review_task=child, reason=gate, findings="")
            _consume(conn, child, {"verdict": "PASS", "writer": writer_id,
                                   "outcome": "reopened_gate" if ok else "reopen_refused"})
            routed.append((writer_id, "reopened"))
            continue
        ready = "error" not in pr and ci == "success"
        reason = None if ready else (
            "Review passed, but the checks could not be confirmed green." if ci in {"none", "pending"}
            else "Review passed, but the PR state could not be read.")
        with kb.write_txn(conn):
            # Only a fully green PR closes the writer; otherwise it stays in
            # review (merge cleanup closes it later) and the owner is told.
            cur = conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND status = 'review'",
                (now, writer_id)) if ready else None
            if cur is None or cur.rowcount == 1:
                kb._append_event(conn, writer_id, "delivery_accepted", {
                    **base, "acceptance": "ready" if ready else "review_passed", "auto_review": True,
                    "ci": ci, "draft": pr.get("isDraft"), **({"reason": reason} if reason else {}),
                })
        _consume(conn, child, {"verdict": "PASS", "writer": writer_id,
                               "outcome": "ready" if ready else "passed_unconfirmed"})
        routed.append((writer_id, "ready" if ready else "passed"))
    return routed


def _note(conn, task_id: str, body: str, payload: dict) -> None:
    kb.add_comment(conn, task_id, CREATOR, body)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "pr_closed_cleanup", payload)


def cleanup_closed_prs(conn, *, pr_lookup: Optional[PrLookup] = None, budget: Optional[list] = None,
                       now: Optional[float] = None) -> list:
    """Close out cards whose PR is MERGED/CLOSED. Running cards are never touched."""
    acted = []
    writers = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'review' ORDER BY id")]
    reviews = conn.execute(
        "SELECT id, created_by, body FROM tasks WHERE created_by IN (?, 'delivery-review') "
        "AND status IN ('todo', 'ready', 'blocked', 'triage', 'scheduled') ORDER BY id", (CREATOR,),
    ).fetchall()
    for tid in writers:
        url = find_pr_url(conn, tid, _handoff_run(conn, tid))
        pr = _lookup(url, pr_lookup, budget, now) if url else None
        state = (pr or {}).get("state")
        if state == "MERGED":
            with kb.write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET status = 'done', completed_at = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL WHERE id = ? AND status = 'review'",
                    (int(now or time.time()), tid))
            if cur.rowcount == 1:
                _note(conn, tid, f"Closed automatically: {url} was merged, so this card no longer "
                                 "waits on review.", {"pr": url, "state": state, "action": "completed"})
                acted.append((tid, "completed"))
        elif state == "CLOSED":
            if kb.archive_task(conn, tid):
                _note(conn, tid, f"Archived automatically: {url} was closed without merging.",
                      {"pr": url, "state": state, "action": "archived"})
                acted.append((tid, "archived"))
    for row in reviews:
        target = _obj((_event(conn, row["id"], "auto_review_target") or {"payload": None})["payload"])
        url = target.get("pr") or find_pr_url(conn, row["id"])
        pr = _lookup(url, pr_lookup, budget, now) if url else None
        state = (pr or {}).get("state")
        if state in {"MERGED", "CLOSED"} and kb.archive_task(conn, row["id"]):
            _note(conn, row["id"], f"Archived automatically: {url} is {state.lower()}, so this "
                                   "review is no longer needed.",
                  {"pr": url, "state": state, "action": "archived"})
            acted.append((row["id"], "archived"))
    return acted


def tick(conn, *, pr_lookup: Optional[PrLookup] = None, max_lookups: int = MAX_LOOKUPS_PER_TICK) -> dict:
    """One bounded pass from the dispatcher: retries, verdicts, then PR cleanup."""
    if not enabled():
        return {}
    budget = [max_lookups]
    created = []
    for tid in handoff_pending(conn):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        child = on_handoff(conn, tid, pr_lookup=pr_lookup)
        if child:
            created.append(child)
    return {
        "created": created,
        "verdicts": reconcile_verdicts(conn, pr_lookup=pr_lookup, budget=budget),
        "cleanup": cleanup_closed_prs(conn, pr_lookup=pr_lookup, budget=budget),
    }
