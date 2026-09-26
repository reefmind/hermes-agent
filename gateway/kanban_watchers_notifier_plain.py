"""Plain-English stop notices: at most three short lines, plus the PR url.

Meghann, 2026-09-26: kanban status posts were too wordy. A stop notice now says
which ticket, why in one sentence, and who has to do what -- nothing else. Card
ids, commit SHAs, board tags and role handles stay in the event payload and the
logs; they never reach the message.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from gateway.kanban_watchers_notifier_owners import decision_mentions

NO_ONE = "No one — it will continue on its own."

# Status words, the last thing on line one.
STUCK, NEEDS_YOU, IN_PROGRESS, READY = "stuck", "needs you", "back in progress", "ready"

# Brett's Discord id: an unrecognised stop goes to him to look at.
_BRETT_ID = "923685524860981310"

# Internal hold/block reason -> the plain sentence, and whether a person must act.
# Matched as lowercase substrings, first hit wins; order puts the specific first.
REASONS: tuple = (
    (("reviewer parked", "reviewer did not finish normally"),
     "The reviewer stopped before finishing.", True),
    (("artifact identity changed",),
     "The code changed after review, so it needs another review.", False),
    (("no exact pass or actionable block",),
     "The review didn't give a clear pass or fail.", False),
    (("sole independent review child", "legacy review handoff", "canonical review_requirement required",
      "review child has additional dependencies", "full exact commit sha required", "stable pr",
      "review head missing or stale"),
     "The handoff to review was incomplete.", False),
    (("delivery gates still incomplete", "proof gate"),
     "Review passed, but checks aren't green yet.", False),
    (("focused check evidence required",),
     "Tests weren't run for this change.", False),
    (("implementation has unresolved failure or claim",),
     "The build step failed.", False),
)
UNKNOWN_REASON = "Stopped for a reason the board doesn't recognize."


def plain_reason(raw: Any) -> tuple:
    """``(sentence, needs_a_person, known)`` for an internal reason string."""
    text = str(raw or "").lower()
    for needles, sentence, person in REASONS:
        if any(needle in text for needle in needles):
            return sentence, person, True
    return UNKNOWN_REASON, True, False


def decide(action: str) -> str:
    """Who line for a human decision: Brett and/or Meghann, then the one action."""
    who = " or ".join(decision_mentions().split()) or "Brett or Meghann"
    return f"{who} — {action}"


def brett(action: str) -> str:
    """Who line that goes to Brett alone (unknown stops, merge decisions)."""
    ids = decision_mentions().split()
    who = f"<@{_BRETT_ID}>" if f"<@{_BRETT_ID}>" in ids else (" or ".join(ids) or "Brett")
    return f"{who} — {action}"


def hold_who(raw: Any) -> tuple:
    """``(why, who, status)`` for a delivery hold or review-controller reason."""
    why, person, known = plain_reason(raw)
    if not known:
        return why, brett("take a look."), NEEDS_YOU
    if person:
        return why, decide("say retry or drop."), STUCK
    return why, NO_ONE, IN_PROGRESS


_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_NOISE_RE = re.compile(
    r"\bt_[0-9a-f]{6,}\b|\b[0-9a-f]{40}\b|^\s*\[[\w.-]+\]\s*|@[A-Za-z][\w-]*|\([^)]*\)")
_TITLE_LIMIT = 50


def ticket_and_title(title: Any) -> tuple:
    """``(TICKET-ID or "", short title of at most 50 chars)``."""
    text = str(title or "")
    found = _TICKET_RE.search(text)
    ticket = found.group(0) if found else ""
    if ticket:
        text = text.replace(ticket, "", 1)
    if " · " in text:  # "implementation · Real title": the label before the dot is noise
        text = text.rsplit(" · ", 1)[1]
    text = " ".join(_NOISE_RE.sub(" ", text).split()).strip(" -—·:")
    if len(text) > _TITLE_LIMIT:
        text = text[:_TITLE_LIMIT - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return ticket, text


def plain_notice(*, icon: str, title: Any, status: str, why: str, who: str,
                 pr_url: Optional[str] = None) -> str:
    """The notice: headline, Why, Who, and the PR url when one is known."""
    ticket, short = ticket_and_title(title)
    head = " ".join(part for part in (icon, ticket, short) if part)
    lines = [f"{head} — {status}", f"Why: {why}", f"Who: {who}"]
    if pr_url:
        lines.append(pr_url)
    return "\n".join(lines)
