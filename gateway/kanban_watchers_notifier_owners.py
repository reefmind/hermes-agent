"""Who a kanban notice may name, and the one-time ask for a missing Discord thread.

Meghann, 2026-09-25: notices kept tagging @pr-reviewer, a profile retired under
one-card, and cards with no Discord thread went to #kanban without anyone being
asked where they belong.
"""

from __future__ import annotations

import os
from typing import Any

# The existing intake notify-users setting (~/.hermes/.env): the people who own
# human decisions on the ReefMind board. Not a new knob.
_DECISION_USERS_ENV = "REEFMIND_DISCORD_INTAKE_NOTIFY_USERS"


def unretired(name: Any) -> str:
    """``name`` resolved through ``kanban.retired_assignees``; "" when empty."""
    name = str(name or "").strip()
    if not name:
        return ""
    try:
        from hermes_cli.kanban_db import _remap_retired_assignee
        return str(_remap_retired_assignee(name)[0] or "").strip()
    except Exception:
        return ""


def decision_mentions() -> str:
    """``<@id> <@id>`` for the humans who decide holds, or "" when unconfigured."""
    ids = [u.strip() for u in os.environ.get(_DECISION_USERS_ENV, "").split(",") if u.strip().isdigit()]
    return " ".join(f"<@{u}>" for u in ids)


# One ask per card, ever: the NOT EXISTS survives ticks and restarts because the
# ask is a durable task event, delivered by the card's own fallback subscription.
THREAD_UNKNOWN_SQL = """
            INSERT INTO task_events (task_id, run_id, kind, payload, created_at)
            SELECT ?, NULL, 'thread_unknown', json_object('task', ?, 'board', ?, 'channel', ?),
                   strftime('%s','now')
             WHERE NOT EXISTS (SELECT 1 FROM task_events
                                WHERE task_id = ? AND kind = 'thread_unknown')
"""


def thread_unknown_notice(ev: Any, n: Any) -> tuple:
    """Ask for the card's Discord thread and give the exact command that fixes it."""
    payload = ev.payload or {}
    board = str(payload.get("board") or "<board>")
    tid = str(payload.get("task") or "<id>")
    who = decision_mentions()
    lines = [
        f"❓ **NO DISCORD THREAD** · `{tid}` · {n.title}",
        f"{who + ' — ' if who else ''}this card has no Discord thread, so its updates post here. "
        "Reply with the thread link, or run:",
        f"`hermes kanban --board {board} notify-subscribe {tid} --platform discord "
        "--chat-id <forum> --thread-id <thread> --chat-type thread`",
    ]
    return "\n".join(lines), None, None

