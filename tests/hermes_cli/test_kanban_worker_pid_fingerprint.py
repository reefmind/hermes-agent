"""A recycled worker PID is never mistaken for our worker.

``tasks.worker_pid`` survives a reboot; the number can then belong to an unrelated process. Every
liveness decision (extend/defer the claim) and every kill (SIGTERM/SIGKILL on timeout or reclaim)
must require the spawn-time start fingerprint to match, never bare PID existence.
"""

import os
import signal
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _claimed_running(conn, *, pid: int, started_at, max_runtime=None) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
                     (started_at, old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))
    return tid


def test_recycled_pid_is_reclaimed_without_being_signalled(board):
    """Our own live PID with a foreign fingerprint models a post-reboot recycle: the claim is released
    (dead worker), no signal is sent, and max-runtime enforcement does not SIGTERM the stranger either."""
    conn = board
    killed = []
    stranger_fingerprint = 1  # no live process started at tick 1
    tid = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint, max_runtime=1)

    assert kbd._worker_alive(os.getpid(), stranger_fingerprint) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    tid2 = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint)
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 1
    assert killed == []
    assert kb.get_task(conn, tid2).status == "ready"


def test_matching_fingerprint_keeps_the_live_worker(board):
    """The same PID with ITS OWN fingerprint (recorded at spawn) is our worker: the expired claim is
    extended rather than reclaimed, and the timeout path signals it."""
    from gateway.status import get_process_start_time

    conn = board
    killed = []
    tid = _claimed_running(conn, pid=os.getpid(), started_at=get_process_start_time(os.getpid()))
    assert kbd._worker_alive(os.getpid(), get_process_start_time(os.getpid())) is True
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "claim_extended" in kinds

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
    kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed and killed[0] == (os.getpid(), signal.SIGTERM)


def test_same_pid_and_start_tick_on_another_boot_is_foreign(board, monkeypatch):
    """A row that survived a reboot: the PID AND the boot-relative start tick both match a process on
    this boot (the Linux start time is clock ticks since boot, so that recurs), but the persisted
    instantiation epoch does not. The worker is foreign: claim released, zero signals."""
    from gateway import drain_control

    conn = board
    killed = []
    live_fingerprint = kbd._process_fingerprint(os.getpid())
    assert live_fingerprint is not None and live_fingerprint.split("|", 1)[1] == str(
        __import__("gateway.status", fromlist=["x"]).get_process_start_time(os.getpid()))
    tid = _claimed_running(conn, pid=os.getpid(), started_at=live_fingerprint, max_runtime=1)
    assert kbd._worker_alive(os.getpid(), live_fingerprint) is True

    # Same PID, same start tick, different boot identity.
    other_boot = "deadbeef-boot:1|" + live_fingerprint.split("|", 1)[1]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", (other_boot, tid))
    assert kbd._worker_alive(os.getpid(), other_boot) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    # The same value re-derived on THIS boot still identifies our worker (the witness is stable
    # within a boot, unlike the recorded epoch of a previous one).
    drain_control.current_instantiation_epoch.cache_clear()
    assert kbd._process_fingerprint(os.getpid()) == live_fingerprint


def test_unverified_fingerprint_capture_never_authorizes_a_signal(board, monkeypatch):
    """Fingerprint capture fails for a new spawn: the row is NOT a legacy NULL row. A live PID under
    it is never SIGTERM/SIGKILLed by any reclaim/timeout path, and the claim is held (not released
    beside the live process); once the PID is gone the claim is reclaimed normally."""
    import gateway.status as status

    conn = board
    killed = []
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: None)
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=1)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["worker_started_at"] == kbd.UNVERIFIED_WORKER_FINGERPRINT
    monkeypatch.undo()
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ?, claim_expires = ? WHERE id = ?", (old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))

    sig = lambda pid, s: killed.append((pid, s))  # noqa: E731
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert killed == []
    assert kb.get_task(conn, tid).status == "running"
    # An explicit operator reclaim releases the claim (human override) but still sends nothing.
    assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=sig) is True
    assert killed == []

    # The process is gone (a dead PID): the row is reclaimed like any dead worker, still no signal.
    tid2 = kb.create_task(conn, title="job2", assignee="worker")
    kb.claim_task(conn, tid2)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ?, claim_expires = ? WHERE id = ?",
                     (os.getpid(), kbd.UNVERIFIED_WORKER_FINGERPRINT, old, tid2))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert kb.release_stale_claims(conn, signal_fn=sig) == 1
    assert killed == [] and kb.get_task(conn, tid2).status == "ready"


def test_start_time_drift_within_tolerance_is_still_our_worker(board):
    """macOS shifts psutil create_time ~1 s between spawn and a later read; a live worker whose
    composite fingerprint drifted by 1 s must stay alive, not read as recycled ("pid N not alive")."""
    epoch, _, start = kbd._process_fingerprint(os.getpid()).partition("|")
    drifted = f"{epoch}|{int(start) - 100}"
    assert kbd._worker_alive(os.getpid(), drifted) is True


def test_launch_grace_runs_from_the_current_run_not_the_first_start(board, monkeypatch):
    """A retried card's ``tasks.started_at`` is its first-ever start; the grace must come from the
    current run, so a just-respawned worker is not reclaimed before it is visible."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "30")
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=1)  # reads as dead
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (int(time.time()), tid))
    kbd._reclaim_dead_workers(conn)
    assert kb.get_task(conn, tid).status == "running"


def test_one_negative_liveness_read_does_not_book_a_crash(board, monkeypatch):
    """A single transient "dead" read followed by a live re-read leaves the card running."""
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=1)
    reads = iter([False, True])
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, fp: next(reads))
    kbd._reclaim_dead_workers(conn)
    assert kb.get_task(conn, tid).status == "running"


def test_live_pid_with_unreadable_start_time_is_not_booked_dead(board, monkeypatch):
    """The PID exists but its start time cannot be read right now: that proves nothing about a
    recycle, so the worker stays alive. The kill path still refuses to signal it."""
    recorded = kbd._process_fingerprint(os.getpid())
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: None)
    assert kbd._worker_alive(os.getpid(), recorded) is True
    assert kbd._pid_recycled(os.getpid(), recorded) is True


def _dead(kind, output, **flags):
    payload = {"pid": 4242, "claimer": "host:1"}
    if output:
        payload["worker_output"] = output
    return kbd._DeadWorker(kind, 1, "pid 4242 not alive", "crashed", payload, **flags)


def test_quota_wall_in_the_dead_workers_final_output_requeues_instead_of_crashing(board):
    """A worker that died right after the provider refused it for quota is a rate-limited requeue."""
    dead = kbd._reclassify_quota_clean_exit(
        _dead("unknown", "This request would exceed your account's rate limit. Please try again later."), "t_x")
    assert dead.rate_limited is True and dead.run_outcome == "rate_limited"


def test_dead_worker_without_quota_output_stays_a_crash(board):
    dead = kbd._reclassify_quota_clean_exit(_dead("unknown", "Traceback: KeyError 'x'"), "t_x")
    assert dead.rate_limited is False and dead.run_outcome == "crashed"


def test_signal_killed_worker_is_never_reclassified_as_quota(board):
    dead = kbd._reclassify_quota_clean_exit(_dead("signaled", "rate limit"), "t_x")
    assert dead.run_outcome == "crashed"
