
from hermes_cli import kanban_db_connect, kanban_db_dispatch, kanban_db_notify
"""Controller-owned review continuation, with real isolated DB/tool/dispatch paths."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as tools

def test_maintenance_reapply_preserves_canonical_review(tmp_path, monkeypatch):
    """Exercise the installed maintenance patcher only against copied sources."""
    import os
    import runpy
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = root.parent / "scripts" / "kanban_one_card_patch.py"
    if not script.exists():
        pytest.skip("local installed maintenance script only")
    fixture = tmp_path / "agent"
    fixture.mkdir()
    for name in ("hermes_cli", "tools", "gateway", "agent", "plugins"):
        shutil.copytree(root / name, fixture / name, ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))
    shutil.copy2(root / "cli.py", fixture / "cli.py")
    patchers = [runpy.run_path(str(script)), runpy.run_path(str(script.with_name("kanban_block_contract_patch.py")))]
    for namespace in patchers:
        namespace["main"].__globals__.update(AGENT=fixture, PY=Path(sys.executable), CHECK=False)
        for edit in namespace["EDITS"]:
            edit["path"] = fixture / edit["path"].relative_to(root)
    monkeypatch.setenv("PYTHONPATH", str(fixture) + os.pathsep + str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    # The patcher itself also runs its retained safety behavior checks.
    for namespace in patchers:
        assert namespace["main"]() == 0
    probe = '''
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools import kanban_tools
from plugins.kanban.dashboard import plugin_api
assert "A self-review is not independent evidence" in kanban_tools._ONE_CARD_DOCTRINE
with connect(Path("fixture.db")) as conn:
    parent = kb.create_task(conn, title="Implementation", assignee="backend")
    child = kb.create_task(conn, title="Review", assignee="integrator", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, child, claimer="fixture") is None
    run = kb.claim_task(conn, parent, claimer="fixture")
    assert run
    assert kb.request_review(conn, parent, summary="candidate", reviewer="integrator", expected_run_id=run.current_run_id)
    assert plugin_api._set_status_direct(conn, parent, "ready")
assert "kanban_request_review" in [entry[0] for entry in kanban_tools._TOOLS]
import os, json
from tools.registry import registry
os.environ["HERMES_KANBAN_DB"] = str(Path("goal.db").resolve())
with connect(Path("goal.db")) as conn:
    parent = kb.create_task(conn, title="Deliver reviewed artifact", assignee="backend", goal_mode=True)
    child = kb.create_task(conn, title="Review", assignee="integrator", parents=[parent])
    run = kb.claim_task(conn, parent, claimer="fixture")
    os.environ.update(HERMES_KANBAN_TASK=parent, HERMES_KANBAN_RUN_ID=str(run.current_run_id))
    kanban_tools._goal_judge_available = lambda: True
    kanban_tools.judge_goal = lambda **kw: ("blocked", "review missing", False, None, False)
    result = json.loads(registry.dispatch("kanban_complete", {"summary": "Candidate phase", "metadata": {
        "review_requirement": {"required": True, "owner": "orchestrator", "review_task_id": child},
        "delivery_review": {"head": "a" * 40, "evidence": {"artifact": "fixture",
            "checks": [{"command": "focused", "result": "passed"}]}}}}))
    assert result.get("ok"), result
    assert kb.get_task(conn, parent).status == "review"
'''
    result = subprocess.run([sys.executable, "-c", probe], cwd=fixture, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for namespace in patchers:
        assert namespace["main"]() == 0
    result = subprocess.run([sys.executable, "-c", probe], cwd=fixture, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


A = "a" * 40
B = "b" * 40


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    monkeypatch.setattr(kanban_db_dispatch, "_memory_pressure_level", lambda: "normal")
    with kanban_db_connect.connect(db) as c:
        assert Path(c.execute("PRAGMA database_list").fetchone()[2]) == db
        yield c


def pair(conn):
    parent = kb.create_task(conn, title="Implementation", assignee="integrator")
    child = kb.create_task(conn, title="Independent review", assignee="pr-reviewer", parents=[parent])
    return parent, child


def candidate(child, head):
    return {"review_requirement": {"required": True, "owner": "orchestrator", "review_task_id": child},
            "delivery_review": {"head": head,
            "evidence": {"artifact": "https://github.com/example/collector/pull/3340",
                         "checks": [{"command": "focused test", "result": "passed"}],
                         "ci_head": head, "ci": "success", "draft": False,
                         "proof_head": head, "proof": "passed"}}}


def submit(conn, parent, child, head):
    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert run
    assert kb.complete_task(conn, parent, summary="Implementation phase; awaiting independent review",
                             metadata=candidate(child, head), expected_run_id=run.current_run_id)


def tick(conn, **kw):
    return kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *args: None, reconcile_orphans=False, **kw)


def verdict(conn, child, head, value):
    run = kb.get_task(conn, child)
    assert run.status == "running"
    assert kb.complete_task(conn, child, expected_run_id=run.current_run_id,
        summary=f"{value} exact candidate", metadata={"delivery_review": {
            "head": head, "verdict": value, "findings": "Fix ordering" if value == "BLOCK" else ""}})


@pytest.mark.parametrize("review_head", [A, B])
def test_controller_review_verdict_retains_exact_artifact(conn, review_head):
    """Documented verdict metadata retains its controller-owned artifact identity."""
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    verdict(conn, child, review_head, "PASS")
    evidence = kb._completed_artifact_evidence(conn, child)
    assert evidence == ({"head": A, "artifact": candidate(child, A)["delivery_review"]["evidence"]["artifact"],
                         "run_id": kb.latest_run(conn, child).id} if review_head == A else None)


@pytest.mark.parametrize("stale_review", [False, True])
@pytest.mark.parametrize("entrypoint", ["kanban_complete", "kanban_request_review", "cli_complete", "cli_request_review"])
def test_goal_judge_cannot_deadlock_phase_handoff(conn, monkeypatch, entrypoint, stale_review):
    """Real registered tools + owned runs; the whole goal judge rejects pending review."""
    from tools.registry import registry
    from hermes_cli import goals

    parent = kb.create_task(conn, title="Deliver reviewed change", assignee="integrator",
                            body="Final Ready requires independent PASS and green CI.", goal_mode=True)
    child = kb.create_task(conn, title="Independent review", assignee="pr-reviewer", parents=[parent], goal_mode=True)
    judge_calls = []

    def rejecting_judge(*args, **kwargs):
        judge_calls.append((args, kwargs))
        return "blocked", "Independent PASS is missing", False, None, False

    monkeypatch.setattr(tools, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(tools, "judge_goal", rejecting_judge)
    monkeypatch.setattr(goals, "judge_goal", rejecting_judge)
    import agent.auxiliary_client as auxiliary
    monkeypatch.setattr(auxiliary, "get_text_auxiliary_client", lambda _: (object(), "judge"))

    def invoke(tid, name, metadata):
        task = kb.get_task(conn, tid)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
        monkeypatch.setenv("HERMES_PROFILE", task.assignee)
        if name.startswith("cli_"):
            import shlex
            from hermes_cli import kanban
            command = name[4:].replace("_", "-")
            output = kanban.run_slash(f"{command} {tid} "
                                     f"--summary candidate --metadata {shlex.quote(json.dumps(metadata))}")
            return {"ok": "Completed" in output or "Requested review" in output, "output": output}
        return json.loads(registry.dispatch(name, {
            "summary": "Exact candidate and focused checks supplied; review pending",
            "metadata": metadata}))

    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert run
    assert kb.get_task(conn, child).status == "todo"
    out = invoke(parent, entrypoint, candidate(child, A))
    assert out.get("ok"), out
    assert not judge_calls  # phase submission is checked by the deterministic controller
    loop = goals.run_kanban_goal_loop(
        task_id=parent, goal_text="Deliver reviewed change", first_response="Review pending",
        task_status_fn=lambda: kb.goal_run_status(conn, parent, run.current_run_id),
        run_turn=lambda _: pytest.fail("ended phase must not be rejudged"),
        block_fn=lambda _: pytest.fail("review is not a human decision"))
    assert loop["outcome"] == "review_requested_by_worker"
    assert [x[0] for x in tick(conn).spawned] == [child]
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    # Findings return the same implementer. A new candidate never inherits old PASS.
    assert invoke(child, "kanban_complete", {"delivery_review": {
        "head": A, "verdict": "BLOCK", "findings": "Fix ordering"}}).get("ok")
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, parent).status == "ready"
    assert kb.claim_task(conn, parent, claimer="remote:test")
    red_ci = candidate(child, B)
    red_ci["delivery_review"]["evidence"]["ci"] = "failure"
    assert invoke(parent, entrypoint, red_ci).get("ok")
    assert [x[0] for x in tick(conn).spawned] == [child]
    assert invoke(child, "kanban_complete", {"delivery_review": {
        "head": A if stale_review else B, "verdict": "PASS"}}).get("ok")
    tick(conn, max_spawn=0)
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    if not stale_review:
        assert kb.get_task(conn, parent).status == "ready"
        assert kb.claim_task(conn, parent, claimer="remote:test")
        assert invoke(parent, entrypoint, candidate(child, B)).get("ok")
        assert not tick(conn, max_spawn=0).spawned
        accepted = [e for e in kb.list_events(conn, parent) if e.kind == "delivery_accepted"]
        assert len(accepted) == 1 and accepted[0].payload["head"] == B
    assert not judge_calls
    assert not any(e.kind == "blocked" for e in kb.list_events(conn, parent))
    assert len(kb.list_tasks(conn)) == 2


@pytest.mark.parametrize("entrypoint", ["kanban_complete", "kanban_request_review"])
@pytest.mark.parametrize("guard", [None, "checks", "claim", "crash", "operator_hold", "new_failure", "wrong_run"])
def test_successful_phase_supersedes_timeout_only(conn, monkeypatch, entrypoint, guard):
    """An authorized successful continuation resolves old timeout, not live failure."""
    from tools.registry import registry
    parent, child = pair(conn)
    failed = kb.claim_task(conn, parent, claimer="remote:test")
    error = "Iteration budget exhausted (120/120)"
    kanban_db_dispatch._record_task_failure(
        conn, parent, error, outcome="crashed" if guard == "crash" else "timed_out",
        release_claim=True, end_run=True, failure_limit=5)
    # Explicit continuation authority, without changing failure accounting.
    kb.add_comment(conn, parent, author="operator", body="Authorized same-card continuation")
    with kanban_db_connect.write_txn(conn):
        kb._append_event(conn, parent, "promoted_manual", {"actor": "operator"})
    resumed = kb.claim_task(conn, parent, claimer="remote:test")
    assert resumed and kb.get_task(conn, parent).consecutive_failures == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(resumed.current_run_id + (1 if guard == "wrong_run" else 0)))
    monkeypatch.setenv("HERMES_PROFILE", resumed.assignee)
    data = candidate(child, A)
    if guard == "checks":
        data["delivery_review"]["evidence"]["checks"] = []
    out = json.loads(registry.dispatch(entrypoint, {"summary": "Candidate ready", "metadata": data}))
    assert bool(out.get("ok")) is (guard != "wrong_run"), out
    if guard == "new_failure":
        kanban_db_dispatch._record_task_failure(conn, parent, "new failure", outcome="spawn_failed")
    if guard in {"claim", "operator_hold"}:
        with kanban_db_connect.write_txn(conn):
            if guard == "claim":
                conn.execute("UPDATE tasks SET claim_lock='live-owner' WHERE id=?", (parent,))
            else:
                conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (parent,))
                kb._append_event(conn, parent, "blocked", {"operator_held": True})
    spawned = []
    for _ in range(3):
        spawned.extend(x[0] for x in tick(conn).spawned)
    assert spawned == ([child] if guard is None else [])
    task = kb.get_task(conn, parent)
    assert task.consecutive_failures == (0 if guard is None else 2 if guard == "new_failure" else 1)
    assert task.last_failure_error == (None if guard is None else "new failure" if guard == "new_failure" else error)
    historical = next(r for r in kb.list_runs(conn, parent) if r.id == failed.current_run_id)
    assert historical.error == error and historical.outcome == ("crashed" if guard == "crash" else "timed_out")
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    if guard is None:
        resets = [e for e in kb.list_events(conn, parent) if e.kind == "delivery_phase_failure_resolved"]
        assert len(resets) == 1 and resets[0].run_id == resumed.current_run_id
        verdict(conn, child, A, "BLOCK")
        tick(conn, max_spawn=0)
        assert kb.get_task(conn, parent).status == "ready"
        submit(conn, parent, child, B)
        assert [x[0] for x in tick(conn).spawned] == [child]
        verdict(conn, child, B, "PASS")
        tick(conn, max_spawn=0)
        assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_accepted"]) == 1


@pytest.mark.parametrize("mismatch", [None, "identity", "head"])
def test_snapshot_change_preserves_canonical_identity(conn, monkeypatch, tmp_path, mismatch):
    """Legacy snapshot-as-identity can continue explicitly, never by path guessing."""
    from tools.registry import registry
    parent = kb.create_task(conn, title="Local repair", assignee="integrator", goal_mode=True)
    child = kb.create_task(conn, title="Independent review", assignee="pr-reviewer", parents=[parent])
    identity = str(tmp_path / "run8" / "candidate")
    monkeypatch.setattr(tools, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(tools, "judge_goal", lambda **kw:
                        ("blocked", "Independent review pending", False, None, False))

    def invoke(tid, metadata):
        task = kb.get_task(conn, tid)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
        monkeypatch.setenv("HERMES_PROFILE", task.assignee)
        result = json.loads(registry.dispatch("kanban_complete", {
            "summary": "Candidate phase", "metadata": metadata}))
        assert result.get("ok"), result

    assert kb.claim_task(conn, parent, claimer="remote:test")
    first = candidate(child, A)
    first["delivery_review"]["evidence"].update(artifact=identity, ci="failure")
    invoke(parent, first)
    assert [x[0] for x in tick(conn).spawned] == [child]
    invoke(child, {"delivery_review": {"head": A, "verdict": "PASS"}})
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, parent).status == "ready"
    assert kb.get_task(conn, child).status == "done"
    assert kb.claim_task(conn, parent, claimer="remote:test")
    second = candidate(child, B)
    second["delivery_review"]["evidence"].update(
        artifact=str(tmp_path / "run10" / "candidate"),
        artifact_identity="different-repository" if mismatch == "identity" else identity)
    invoke(parent, second)
    result = tick(conn)
    if mismatch == "identity":
        assert not result.spawned
        assert any("artifact identity changed" in reason for _, reason in result.delivery_holds)
        return
    assert [x[0] for x in result.spawned] == [child]
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    invoke(child, {"delivery_review": {"head": A if mismatch == "head" else B, "verdict": "PASS"}})
    tick(conn, max_spawn=0)
    accepted = [e for e in kb.list_events(conn, parent) if e.kind == "delivery_accepted"]
    if mismatch == "head":
        assert not accepted
    else:
        assert [e.payload["head"] for e in accepted] == [B]
        assert kb._completed_artifact_evidence(conn, child)["artifact"] == identity
    assert len(kb.list_tasks(conn)) == 2
    assert len(kb.list_runs(conn, child)) == 2


@pytest.mark.parametrize("invalid", [None, "run", "dependency"])
def test_owned_review_result_can_close_after_parent_resumes(conn, monkeypatch, invalid):
    """A dependency change cannot strand a claimed reviewer recording its result."""
    from tools.registry import registry
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    reviewer = kb.get_task(conn, child)
    with kanban_db_connect.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (parent,))
    assert kb.claim_task(conn, parent, claimer="remote:test")
    if invalid == "dependency":
        other = kb.create_task(conn, title="Unfinished dependency", assignee="qa")
        kb.link_tasks(conn, other, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(reviewer.current_run_id + (1 if invalid == "run" else 0)))
    monkeypatch.setenv("HERMES_PROFILE", reviewer.assignee)
    out = json.loads(registry.dispatch("kanban_complete", {"summary": "Review result only", "metadata": {
        "delivery_review": {"head": A, "verdict": "BLOCK", "findings": "Repair continuation"}}}))
    assert bool(out.get("ok")) is (invalid is None), out
    assert kb.get_task(conn, parent).status == "running"
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))


def test_goal_loop_requires_worker_evidence_before_human_block(conn, monkeypatch):
    from types import SimpleNamespace
    import cli
    from hermes_cli import goals
    parent, child = pair(conn)
    run = kb.claim_task(conn, parent, claimer="remote:test")
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k:
                        ("blocked", "Review and CI are pending", False, None, False))
    def phase_turn(**kwargs):
        assert kb.complete_task(conn, parent, summary="Candidate ready", metadata=candidate(child, A),
                                expected_run_id=run.current_run_id)
        return {"final_response": "Implementation phase handed off"}
    worker = SimpleNamespace(agent=SimpleNamespace(run_conversation=phase_turn), conversation_history=[])
    cli._run_kanban_goal_loop_q(worker, "Review pending")
    assert kb.get_task(conn, parent).status == "review"
    assert not any(e.kind == "blocked" for e in kb.list_events(conn, parent))


def test_done_review_corrected_head_without_cross_card_authority(conn, monkeypatch):
    parent, child = pair(conn)
    origin = dict(task_id=parent, platform="discord", chat_id="fixture", thread_id="origin")
    kanban_db_notify.add_notify_sub(conn, **origin)
    submit(conn, parent, child, A)
    # Handoff is consumed even with no capacity; no user completion/requeue call.
    assert not tick(conn, max_spawn=0).spawned
    assert kb.get_task(conn, parent).status == "done"  # phase, not acceptance
    assert kb.get_task(conn, child).status == "ready"
    assert [x[0] for x in tick(conn).spawned] == [child]
    verdict(conn, child, A, "BLOCK")
    assert not tick(conn, max_spawn=0).spawned
    assert kb.get_task(conn, parent).status == "ready"
    # Same implementation, same reviewer. Worker still cannot reopen another card.
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setattr(tools, "_is_dispatcher_owned_worker", lambda: True)
    denied = json.loads(tools._handle_complete({"task_id": child, "summary": "forbidden"}))
    assert "refusing to mutate" in str(denied)
    assert kb.get_task(conn, child).status == "done"
    submit(conn, parent, child, B)
    assert [x[0] for x in tick(conn).spawned] == [child]
    assert not tick(conn).spawned
    verdict(conn, child, B, "PASS")
    for _ in range(3):
        assert not tick(conn).spawned
    events = kb.list_events(conn, parent)
    assert len([e for e in events if e.kind == "delivery_accepted"]) == 1
    assert len(kb.list_tasks(conn)) == 2
    result_event = [e for e in kb.list_events(conn, child) if e.kind == "completed"][-1]
    assert result_event.payload["completion_kind"] == "delivery_review_result"
    assert result_event.payload["ready"] is False
    assert len(kanban_db_notify.list_notify_subs(conn, parent)) == 1
    kinds = ("delivery_phase_completed", "delivery_changes_requested", "delivery_accepted")
    notices = kanban_db_notify.claim_unseen_events_for_sub(conn, **origin, kinds=kinds)[2]
    assert len(notices) == 4
    assert not kanban_db_notify.claim_unseen_events_for_sub(conn, **origin, kinds=kinds)[2]


@pytest.mark.parametrize("hold", ["blocked", "triage", "crashed", "exhausted"])
def test_controller_never_revives_parked_reviewer(conn, hold):
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    with kanban_db_connect.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?, consecutive_failures=?, last_failure_error=? WHERE id=?",
                     ("blocked" if hold in {"crashed", "exhausted"} else hold,
                      3 if hold == "exhausted" else 0, hold, child))
        kb._append_event(conn, child, "gave_up", {"reason": hold})
    for _ in range(3):
        assert not tick(conn).spawned
    assert kb.get_task(conn, child).status in {"blocked", "triage"}
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))


@pytest.mark.parametrize("invalid", ["ci", "draft", "proof", "stale_review", "scientific_block"])
def test_no_acceptance_without_exact_gates(conn, invalid):
    parent, child = pair(conn)
    data = candidate(child, A)
    evidence = data["delivery_review"]["evidence"]
    if invalid == "ci":
        evidence["ci"] = "failure"
    if invalid == "draft":
        evidence["draft"] = True
    if invalid == "proof":
        evidence["proof_head"] = B
    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert kb.complete_task(conn, parent, metadata=data, expected_run_id=run.current_run_id)
    tick(conn)
    verdict(conn, child, B if invalid == "stale_review" else A,
            "BLOCK" if invalid == "scientific_block" else "PASS")
    for _ in range(3):
        tick(conn, max_spawn=0)
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))


@pytest.mark.parametrize("guard", [None, "reclaimed", "missing_authority", "worker_authority", "foreign_comment", "unmerged", "wrong_prior", "wrong_head", "reused_head", "wrong_pair", "held"])
def test_authorized_merged_successor_uses_same_pair(conn, monkeypatch, guard):
    """Owner's verified merge receipt permits a new artifact, never a stale PASS."""
    from tools.registry import registry
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    if guard == "reclaimed":
        kb.invalidate_descendants_for_parent_reopen(conn, parent, author="operator")
    else:
        verdict(conn, child, A, "PASS")
        tick(conn, max_spawn=0)
    original = candidate(child, A)["delivery_review"]["evidence"]["artifact"]
    successor = "https://github.com/example/collector/pull/3497"
    data = candidate(child, B)
    evidence = data["delivery_review"]["evidence"]
    evidence.update(artifact=successor, ci="failure")
    receipt = {"implementation_task": parent, "review_task": child,
               "previous_artifact": original, "previous_head": A,
               "merged": True, "merge_commit": "c" * 40,
               "merge_verified_at": "2026-09-22T12:00:00Z",
               "merge_source": original, "artifact": successor, "head": B}
    if guard == "unmerged":
        receipt["merged"] = False
    if guard == "wrong_prior":
        receipt["previous_artifact"] = "another artifact"
    if guard == "wrong_head":
        receipt["head"] = A
    if guard == "wrong_pair":
        receipt["review_task"] = "another reviewer"
    if guard == "reused_head":
        receipt["head"] = data["delivery_review"]["head"] = A
    comment_id = kb.add_comment(conn, child if guard == "foreign_comment" else parent,
        author="integrator" if guard == "worker_authority" else "orchestrator",
        body=json.dumps({"delivery_artifact_successor": receipt}))
    if guard != "missing_authority":
        evidence["successor_authorization_comment"] = comment_id
    # An authorized operator resumes the same implementation, not a new card.
    with kanban_db_connect.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (parent,))
        if guard == "held":
            conn.execute("UPDATE tasks SET status='blocked', last_failure_error='operator hold' WHERE id=?", (child,))
    if guard == "reclaimed":
        assert kb.promote_task(conn, child, actor="operator", force=True)[0]
        assert kb.claim_task(conn, child, claimer="remote:test") is None
    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert run
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", run.assignee)
    out = json.loads(registry.dispatch("kanban_complete", {"summary": "Authorized successor phase", "metadata": data}))
    assert out.get("ok"), out
    dispatched = tick(conn)
    assert [x[0] for x in dispatched.spawned] == ([child] if guard in {None, "reclaimed"} else [])
    assert not tick(conn, max_spawn=0).spawned
    assert not any(e.kind == "delivery_accepted" and e.payload["head"] == B for e in kb.list_events(conn, parent))
    if guard not in {None, "reclaimed"}:
        assert dispatched.delivery_holds
        return
    verdict(conn, child, B, "PASS")
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, parent).status == "ready"  # red CI is actionable continuation
    assert kanban_db_dispatch.check_respawn_guard(conn, parent) is None
    # The latest accepted artifact, not the historical first PR, is now stable.
    run = kb.claim_task(conn, parent, claimer="remote:test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    evidence.pop("successor_authorization_comment")
    evidence["ci"] = "success"
    assert json.loads(registry.dispatch("kanban_complete", {"summary": "CI repaired", "metadata": data})).get("ok")
    assert not tick(conn, max_spawn=0).spawned
    assert len(kb.list_runs(conn, child)) == 2
    assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_accepted" and e.payload["head"] == B]) == 1
    assert len(kb.list_tasks(conn)) == 2


@pytest.mark.parametrize("guard", [None, "acknowledged_crash", "unblock_only", "acknowledged_new_hold", "no_promotion", "not_forced", "crash", "new_hold"])
def test_explicit_reclaimed_reviewer_recovery(conn, monkeypatch, guard):
    """Supported reclaim then forced promote is not an automatic crash retry."""
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    if guard in {"crash", "acknowledged_crash", "unblock_only", "acknowledged_new_hold"}:
        with kanban_db_connect.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=999999999, started_at=0 WHERE id=?", (child,))
        monkeypatch.setattr(kb, "_pid_alive", lambda _: False)
        assert kanban_db_dispatch._reclaim_dead_workers(conn).crashed == [child]
    else:
        assert kb.reclaim_task(conn, child, reason="Operator interrupts superseded review")
    with kanban_db_connect.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (parent,))
    if guard in {"acknowledged_crash", "unblock_only", "acknowledged_new_hold"}:
        assert kb.unblock_task(conn, child)  # supported, explicit operator acknowledgment
    if guard not in {"no_promotion", "unblock_only"}:
        # Parent is still unfinished; force is an explicit supported operator action.
        ok, reason = kb.promote_task(conn, child, actor="operator", force=guard != "not_forced",
                                    reason="Authorized review of corrected candidate")
        assert ok is (guard != "not_forced"), reason
    if guard in {"new_hold", "acknowledged_new_hold"}:
        kb.block_task(conn, child, reason="Operator stop", kind="needs_input")
    submit(conn, parent, child, B)
    result = tick(conn)
    assert [x[0] for x in result.spawned] == ([child] if guard in {None, "acknowledged_crash"} else []), (result.delivery_holds, [(e.kind, e.run_id, e.payload) for e in kb.list_events(conn, child)], kb.list_runs(conn, child))
    assert not tick(conn, max_spawn=0).spawned
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    if guard == "acknowledged_crash":
        assert kb.list_runs(conn, child)[0].outcome == "crashed"  # immutable history


def test_red_ci_returns_canonical_implementation_and_reuses_exact_review(conn, monkeypatch):
    parent, child = pair(conn)
    data = candidate(child, A)
    data["delivery_review"]["evidence"]["ci"] = "failure"
    run = kb.claim_task(conn, parent, claimer="remote:test")
    kb.complete_task(conn, parent, metadata=data, expected_run_id=run.current_run_id)
    tick(conn)
    verdict(conn, child, A, "PASS")
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, parent).status == "ready"
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))
    assert kanban_db_dispatch.check_respawn_guard(conn, parent) is None
    submit(conn, parent, child, A)  # CI rerun succeeded without a source change
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, child).status == "done"
    assert len(kb.list_runs(conn, child)) == 1
    assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_accepted"]) == 1
    completed_at = kb.get_task(conn, parent).completed_at
    monkeypatch.setattr(kb.time, "time", lambda: completed_at + 900)
    tick(conn, max_spawn=0)
    assert kb.get_task(conn, parent).completed_at == completed_at


def test_same_sha_cannot_replenish_review_or_rework(conn):
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    verdict(conn, child, A, "BLOCK")
    tick(conn, max_spawn=0)
    submit(conn, parent, child, A)
    for _ in range(3):
        assert not tick(conn).spawned
    assert kb.get_task(conn, child).status == "done"
    assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_changes_requested"]) == 1


def test_missing_reviewer_metadata_never_emits_review_approved(conn):
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn)
    run = kb.get_task(conn, child)
    assert kb.complete_task(conn, child, summary="No structured verdict", expected_run_id=run.current_run_id)
    result = tick(conn)
    assert result.delivery_holds
    event = [e for e in kb.list_events(conn, child) if e.kind == "completed"][-1]
    assert event.payload["completion_kind"] == "delivery_review_result"
    assert event.payload["ready"] is False
    assert not any(e.kind == "delivery_accepted" for e in kb.list_events(conn, parent))


def test_ordinary_same_card_review_unchanged(conn):
    tid = kb.create_task(conn, title="Ordinary", assignee="integrator")
    run = kb.claim_task(conn, tid, claimer="remote:test")
    assert kb.request_review(conn, tid, reviewer="pr-reviewer", expected_run_id=run.current_run_id)
    assert [x[0] for x in tick(conn).spawned] == [tid]


@pytest.mark.parametrize("tamper", ["head", "checks", "binding"])
def test_malformed_candidate_fails_closed_with_deduped_visible_hold(conn, tamper):
    parent, child = pair(conn)
    data = candidate(child, A)
    if tamper == "head":
        data["delivery_review"]["head"] = "short"
    elif tamper == "checks":
        data["delivery_review"]["evidence"]["checks"] = []
    else:
        # A second child makes the automatic pair selection ambiguous.
        kb.create_task(conn, title="Unrelated child", assignee="qa", parents=[parent])
    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert kb.complete_task(conn, parent, metadata=data, expected_run_id=run.current_run_id)
    for _ in range(3):
        result = tick(conn)
        assert not result.spawned
        assert result.delivery_holds
    assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_review_hold"]) == 1


def test_concurrent_controllers_consume_one_generation(conn):
    from concurrent.futures import ThreadPoolExecutor
    from hermes_cli.kanban_review_reconcile import reconcile
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    def run(_):
        with kanban_db_connect.connect(db) as other:
            return reconcile(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert len([e for e in kb.list_events(conn, parent) if e.kind == "delivery_phase_completed"]) == 1
    assert len([e for e in kb.list_events(conn, child) if e.kind == "delivery_review_enqueued"]) == 1
    assert [x[0] for x in tick(conn).spawned] == [child]
    assert not tick(conn).spawned


@pytest.mark.parametrize("guard", ["auth", "quota"])
def test_controller_keeps_failure_and_quota_gates(conn, guard):
    parent, child = pair(conn)
    submit(conn, parent, child, A)
    tick(conn, max_spawn=0)
    if guard == "auth":
        with kanban_db_connect.write_txn(conn):
            conn.execute("UPDATE tasks SET last_failure_error='401 Unauthorized' WHERE id=?", (child,))
    else:
        with kanban_db_connect.write_txn(conn):
            kb._synthesize_ended_run(conn, child, outcome="rate_limited", summary="quota held")
    result = tick(conn)
    assert not result.spawned
    assert (child, "blocker_auth" if guard == "auth" else "rate_limit_cooldown") in result.respawn_guarded


def test_collector_incident_real_cli_capacity_and_stale_review_context(conn, tmp_path):
    """PR3340 incident shape: stale PR3329 body, existing capacity, no user ping."""
    import subprocess
    import sys

    parent, child = pair(conn)
    with kanban_db_connect.write_txn(conn):
        conn.execute("UPDATE tasks SET body='OLD: review PR3329 with Claude only' WHERE id=?", (child,))
        conn.execute("UPDATE tasks SET model_override='gpt-6-astra', provider_override='openai-codex', reasoning_effort='ultra' WHERE id=?", (child,))
    busy = kb.create_task(conn, title="Independent collector worker", assignee="default")
    kb.claim_task(conn, busy, claimer="remote:busy")
    data = candidate(child, A)
    data["delivery_review"]["evidence"]["artifact"] = "https://github.com/example/collector/pull/3340"
    run = kb.claim_task(conn, parent, claimer="remote:test")
    assert kb.complete_task(conn, parent, metadata=data, expected_run_id=run.current_run_id)
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    env = {"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "home"),
           "HERMES_KANBAN_DB": str(db), "PATH": str(tmp_path / "empty"),
           "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
    root = Path(__file__).resolve().parents[2]

    def cli(*args):
        result = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args],
                                cwd=root, env=env, text=True, capture_output=True, timeout=40)
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    held = json.loads(cli("dispatch", "--max", "1", "--dry-run", "--json"))
    assert held["capacity_hold"] == {"scope": "board", "running": 1, "limit": 1}
    assert held["spawned"] == []
    assert "total concurrency" in cli("dispatch", "--max", "1", "--dry-run")
    assert kb.get_task(conn, child).status == "ready"
    assert kb.get_task(conn, busy).status == "running"
    assert kb.complete_task(conn, busy, summary="Fixture busy worker finished")
    # Real CLI resolves profiles. Default exists without a live profile tree;
    # the candidate's profile existence is provided by an isolated profile dir.
    profile = tmp_path / "home" / "profiles" / "pr-reviewer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: gpt-6-astra\n")
    resumed = json.loads(cli("dispatch", "--max", "1", "--dry-run", "--json"))
    assert [x["task_id"] for x in resumed["spawned"]] == [child]
    context = kb.build_worker_context(conn, child)
    assert "gpt-6-astra" in context and "ultra" in context
    assert "supersedes inherited body/skill lane text" in context
    assert context.index("pull/3340") < context.index("OLD: review PR3329")
    assert A in context


def test_authorized_astra_uses_native_spawn_not_inherited_claude(conn, tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    tid = kb.create_task(conn, title="Authorized Astra", assignee="integrator",
                         model_override="gpt-6-astra", provider_override="openai-codex",
                         reasoning_effort="ultra")
    task = kb.get_task(conn, tid)
    monkeypatch.setattr(kanban_db_dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kanban_db_dispatch, "_spawn_log_stamper", lambda *args: None)
    captured = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: (captured.append(cmd) or SimpleNamespace(pid=4245)))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kanban_db_dispatch._default_spawn(task, str(workspace))
    cmd = captured[0]
    assert cmd[0] == "hermes"
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    assert cmd[cmd.index("--provider") + 1] == "openai-codex"
    assert "ultra" in cmd
