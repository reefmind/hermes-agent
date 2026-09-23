"""Kanban notifier: claim terminal task events per subscription and deliver them.

``GatewayKanbanWatchersMixin._kanban_notifier_watcher`` owns the loop and
the GC cadence; the per-tick claim (``_notifier_collect``) and the
per-subscription delivery (``_KanbanNotification``) live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import json
from functools import partial
from pathlib import Path
import weakref
from typing import Any, Callable, Optional

from agent.i18n import t

from gateway.kanban_watchers_common import _list_boards, _to_thread_process_service, logger
from gateway.wake import session_owned_by_profile


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbn():
    from hermes_cli import kanban_db_notify
    return kanban_db_notify

# "status" covers dashboard drag-drop and `_set_status_direct()`.
# ``review_requested`` wakes the origin like a block but is not one;
# the task is not archived so later review cycles keep notifying.
TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status", "archived", "unblocked", "block_loop_detected", "review_requested", "changes_requested") + ("dependency_wait", "review_handoff_required", "delivery_phase_completed", "delivery_changes_requested", "delivery_accepted", "delivery_review_hold")
# Kinds that hand a decision back to the origin, which must take a turn.
# status/archived/unblocked are bookkeeping.
_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked", "review_requested", "changes_requested", "block_loop_detected")


def diagnostic_event(ev) -> bool:
    """Infrastructure attention is distinct from an explicit owner decision."""
    if ev.kind in {"crashed", "timed_out", "gave_up"}:
        return True
    if ev.kind in {"blocked", "block_loop_detected"}:
        return (ev.payload or {}).get("kind") != "needs_input"
    return ev.kind == "status" and (ev.payload or {}).get("status") in {"blocked", "triage"}
# Consecutive send failures (adapter raised OR reported SendResult(success=False))
# before a sub is dropped as a dead chat. 12 ≈ 60s at the 5s cadence: a transient
# API outage must not permanently unsubscribe a live review-gate channel.
# Subscriptions are removed only when the task reaches the irreversible archived status. ``done`` is
# reversible in review/controller flows, so removing its subscription would silence a later reopen. We used
# to also unsub on any terminal event kind (gave_up / crashed / timed_out / blocked), but that silently
# dropped the user out of the loop whenever the dispatcher respawned the task: a worker that crashes, gets
# reclaimed, runs again, and crashes a second time would only notify on the first crash because the
# subscription was deleted after the first event. Same shape as the reblock-after-unblock cycle that PR
# #22941 fixed for `blocked`. Keeping the subscription alive until the task is archived lets the cursor
# (advanced atomically by claim_unseen_events_for_sub) handle dedup, and any retry-loop event reaches the
# user. Per-subscription send-failure counter. Adapter.send raising means the chat is dead (deleted, bot
# kicked, etc.) — after N consecutive send failures the sub is dropped so we don't spin against a dead chat
# every 5 seconds forever. A genuinely dead chat still drops, just ~60s later — a fine trade for an
# unattended gate where a false drop means silent work pileup.
MAX_SEND_FAILURES = 12

_DEFAULT_WAKE_KINDS = ("completed", "crashed", "review_requested", "review_handoff_required", "changes_requested")
_ALL_WAKE_KINDS = _WAKE_KINDS + ("review_handoff_required",)

def _configured_wake_kinds() -> tuple:
    """Event kinds allowed to wake the subscribed agent (``kanban.wake_events``).

    Reads the read-only config on every call (cheap dict lookup; the loader
    caches) so an operator edit takes effect at the next notifier tick without
    a restart. Unknown kinds are ignored; an unset or invalid key falls back to
    ``_DEFAULT_WAKE_KINDS``.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("wake_events")
    except Exception:
        raw = None
    if isinstance(raw, (list, tuple)):
        kinds = tuple(str(k).strip() for k in raw if str(k).strip() in _ALL_WAKE_KINDS)
        if kinds:
            return kinds
    return _DEFAULT_WAKE_KINDS


_LOCAL_PATH_RE = re.compile(r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|" r"[A-Za-z]:\\[^\s,;]+)")


def _safe_review_reason(value: Any, limit: int = 160) -> str:
    """Return a mobile-friendly review reason safe for external delivery."""
    from agent.redact import redact_sensitive_text

    reason = redact_sensitive_text("" if value is None else str(value), force=True, redact_url_credentials=True)
    reason = " ".join(_LOCAL_PATH_RE.sub("[local path]", reason).split())
    if len(reason) > limit:
        reason = reason[: limit - 1].rstrip() + "…"
    return reason


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``scope_id`` on multi-tenant platforms,
    so the wake must carry the same scope as inbound messages. Persisted
    ``delivery_metadata`` wins (it records the creating scope); the adapter's
    live chat → scope map only covers rows without metadata. ``None`` means
    unscoped, matching an unscoped platform's key.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "guild_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if not callable(resolver):
        return None
    try:
        resolved = resolver(str(sub.get("chat_id") or ""))
    except Exception as exc:
        # An adapter-side lookup failure yields no scope, never an error.
        logger.debug("kanban notifier: scope lookup failed for chat %s: %s", sub.get("chat_id"), exc, exc_info=True)
        return None
    return str(resolved) if resolved else None


_ANCHORLESS_WARNED: set[tuple] = set()


def _warn_anchorless_thread_sub_once(sub: dict, platform: str) -> None:
    """A thread-shaped subscription without ``parent_chat_id`` cannot match a channel-level
    ``profile_routes`` entry, so the fail-closed route gate skips it on every tick. Say so ONCE per
    row at WARNING — a subscription that can never deliver was invisible below DEBUG (#110919)."""
    metadata = sub.get("delivery_metadata") or {}
    thread_like = bool(sub.get("thread_id")) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic"}
    if not thread_like or metadata.get("parent_chat_id"):
        return
    key = (sub.get("task_id"), platform, sub.get("chat_id"), sub.get("thread_id") or "")
    if key in _ANCHORLESS_WARNED:
        return
    _ANCHORLESS_WARNED.add(key)
    logger.warning(
        "kanban notifier: subscription for %s on %s thread %s has no parent_chat_id anchor and matched no "
        "profile route; it will not be delivered. Re-subscribe with `hermes kanban notify-subscribe ... "
        "--parent-chat-id <channel id> [--guild-id <guild id>]`.",
        sub.get("task_id"), platform, sub.get("chat_id"),
    )


_UNROUTABLE_WARNED: set[tuple] = set()


def _warn_unroutable_sub_once(sub: dict, platform: Any, message: str, *extra_args: Any) -> None:
    """A routed subscription the credential gate fail-closes is a permanent dead-end: delivery
    rewinds every tick with only a DEBUG line. Say so ONCE per row at WARNING, mirroring
    ``_warn_anchorless_thread_sub_once`` (#115460)."""
    key = (sub.get("task_id"), platform, sub.get("chat_id"), sub.get("thread_id") or "")
    if key in _UNROUTABLE_WARNED:
        return
    _UNROUTABLE_WARNED.add(key)
    logger.warning(message, sub.get("task_id"), getattr(platform, "value", platform),
                   sub.get("chat_id"), *extra_args)


def _platform_names(mapping: Any) -> set[str]:
    """Lower-cased platform names of an adapters mapping (Platform enums or strings)."""
    return {getattr(platform, "value", str(platform)).lower() for platform in mapping}


def _adapter_for_subscription(runner: Any, platform: Any, sub: dict, owner_profile: Optional[str]) -> Any:
    """Resolve a durable route without turning a missing secondary bot into primary authority."""
    adapter = runner._authorization_adapter(platform, owner_profile)
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return adapter
    primary = runner.adapters.get(platform)
    if adapter is not None and adapter is not primary:
        return adapter
    profile = owner_profile or getattr(runner, "_kanban_notifier_profile", None)
    primary_profile = getattr(runner, "_primary_profile_name", None) or runner._active_profile_name()
    profile = profile or primary_profile
    # A profile holding its OWN adapter for this platform is an independent credential boundary —
    # ``_authorization_adapter`` already answered for it, so the primary never stands in. Adapters
    # on OTHER platforms do not gate this one: the primary bot is the only credential serving the
    # pinned chat, for inbound turns and for these notifications alike (#115460).
    own_adapters = (getattr(runner, "_profile_adapters", {}) or {}).get(profile) or {}
    if getattr(platform, "value", str(platform)).lower() in _platform_names(own_adapters):
        return None
    metadata = sub.get("delivery_metadata") or {}
    guild = metadata.get("scope_id") or metadata.get("guild_id")
    parent = metadata.get("parent_chat_id")
    chat, thread = sub.get("chat_id"), sub.get("thread_id") or None
    user_id = sub.get("user_id") or None
    thread_like = bool(thread) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic",
    }
    # Preserve canonical route order, including equal-specificity ties. An older
    # row missing an anchor must not skip a potentially winning route. Reuse the
    # route's matcher (including platform-specific identity aliases), not a second
    # hand-maintained equality implementation.
    for route in getattr(config, "profile_routes", None) or []:
        if route.matches(platform.value, guild_id=guild, chat_id=chat,
                         thread_id=thread, parent_chat_id=parent, user_id=user_id):
            if route.profile != profile:
                _warn_unroutable_sub_once(
                    sub, platform,
                    "kanban notifier: subscription for %s on %s chat %s is stamped with profile %s but a "
                    "profile_routes entry pins that chat to profile %s; it will not be delivered. "
                    "Re-subscribe with `hermes kanban notify-subscribe ... --notifier-profile %s`.",
                    profile, route.profile, route.profile)
                return None
            from gateway.run import _multiplex_profile_homes
            served = {name for name, _home in _multiplex_profile_homes(config)}
            return primary if profile in served else None
        if route.matches(platform.value, guild_id=guild or route.guild_id, chat_id=chat,
                         thread_id=thread, parent_chat_id=parent or (route.chat_id if thread_like else None),
                         user_id=user_id or route.user_id):
            return None
    # A stateless (api_server) subscription carries a RAW session id, not a routable chat, so no
    # profile_routes entry can anchor it — and a platform-wide api_server route would deny the
    # default profile's own api_server destinations. The shared listener mirrors /p/<profile>/ for
    # every served profile, so the owner's own session store is the proof: authorize exactly the
    # session that lives in the served profile's state.db, never the platform. The default profile
    # keeps the historical fallthrough below (no store read).
    if profile != primary_profile and getattr(platform, "value", platform) == "api_server" \
            and session_owned_by_profile(config, profile, chat):
        return primary
    return primary if profile == primary_profile else None


# --- Collection (runs in a worker thread) ---


class _Collector:
    """One tick's claim state: which profiles/platforms this gateway serves and the GC gate."""

    def __init__(self, runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> None:
        self.runner = runner
        self.kb = kb
        self.notifier_profile = notifier_profile
        self.gc_due = gc_due
        self.gc_retention_days = gc_retention_days
        self.deliveries: list[dict] = []
        self.include_unowned = runner._owns_kanban_dispatcher_lock()
        self.profile_adapters = getattr(runner, "_profile_adapters", {})
        self.notifier_profiles = {notifier_profile}
        self.notifier_profiles.update(str(p).strip() for p in self.profile_adapters if str(p).strip())
        config = getattr(runner, "config", None)
        if getattr(config, "multiplex_profiles", False):
            self.notifier_profiles.update(
                route.profile for route in config.profile_routes
                if route.enabled and route.platform in _platform_names(runner.adapters)
            )
        # Include every platform any secondary profile has live. This is only a
        # coarse pre-filter; exact destination authorization runs before claim
        # and again at delivery, rewinding if the route or adapter changed.
        self.active_platforms = _platform_names(runner.adapters).union(
            *(_platform_names(m) for m in self.profile_adapters.values()))

    def collect(self) -> list[dict]:
        if not self.active_platforms:
            logger.debug("kanban notifier: no connected adapters; skipping tick")
            return self.deliveries
        # Poll each resolved DB path once: several slugs can map to one DB when
        # HERMES_KANBAN_DB pins the board path.
        kb = self.kb
        seen_db_paths: set[str] = set()
        for board_meta in _list_boards(kb):
            slug = board_meta.get("slug") or kb.DEFAULT_BOARD
            db_path = board_meta.get("db_path")
            try:
                resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(kb.kanban_db_path(slug).resolve())
            except Exception:
                resolved_db_path = f"slug:{slug}"
            if resolved_db_path in seen_db_paths:
                logger.debug("kanban notifier: skipping duplicate board slug %s for DB %s", slug, resolved_db_path)
                continue
            seen_db_paths.add(resolved_db_path)
            self.collect_board(slug)
        return self.deliveries

    def _board_has_subs(self, slug: str) -> bool:
        """Cheap read-only probe before the writable connect() (schema init, WAL
        sidecars, checkpoints); a probe failure falls back to the writable open."""
        try:
            count = _kbn().count_notify_subs(
                board=slug, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
        except Exception as exc:
            logger.debug("kanban notifier: read-only subscription probe failed "
                         "for board %s (%s); falling back to writable open", slug, exc)
            return True
        if count == 0:
            logger.debug("kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                         slug, sorted(self.notifier_profiles))
        return count != 0

    def _ensure_default_subs(self, conn: Any, slug: str) -> None:
        """ONE-CARD: default board subscription for any card nobody is listening to.

        Best-effort: a failure here must never stop delivery for the cards that
        DO have subscribers, so it is swallowed and retried next tick.
        """
        # ONE-CARD: triage ticket first. A card that came from a mission-control
        # ticket reports next to that ticket; only a card with no ticket falls back
        # to the board's #kanban channel. A card with neither is skipped, not
        # silently dropped -- it simply has no destination yet.
        try:
            from hermes_cli.config import load_config_readonly
            targets = (load_config_readonly() or {}).get("kanban", {}).get("default_notify_target") or {}
            fallback = str(targets.get(slug) or "").strip()
        except Exception:
            fallback = ""
        try:
            rows = conn.execute(_UNHEARD_SQL).fetchall()
        except Exception as exc:
            logger.debug("kanban notifier: unheard-card scan failed on %s: %s", slug, exc)
            return
        if not rows:
            return
        to_triage = to_channel = 0
        for row in rows:
            thread = _triage_thread_for(row["id"], row["title"], row["body"])
            chat = thread or fallback
            if not chat:
                continue
            try:
                conn.execute(
                    "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, chat_type, "
                    " thread_id, user_id, notifier_profile, delivery_metadata, created_at, "
                    " last_event_id, delivery_mode) "
                    "VALUES (?, 'discord', ?, ?, ?, NULL, ?, NULL, strftime('%s','now'), ?, 'notify')",
                    (row["id"], chat, "thread" if thread else "group",
                     thread or "", self.notifier_profile or "", row["cursor"]))
            except Exception as exc:
                logger.debug("kanban notifier: could not subscribe %s: %s", row["id"], exc)
                continue
            if thread:
                to_triage += 1
            else:
                to_channel += 1
        if to_triage or to_channel:
            conn.commit()
            logger.info("kanban notifier: board %s — subscribed %d unheard card(s): "
                        "%d to their triage ticket, %d to %s",
                        slug, to_triage + to_channel, to_triage, to_channel, fallback or "(no fallback)")

    def _emit_long_run_notices(self, conn: Any, slug: str) -> None:
        """One `still working` event per run once it passes the threshold.

        Best-effort and idempotent: the NOT EXISTS keeps it to one per run, and a
        failure here never blocks delivery for anything else.
        0 or unset disables it.
        """
        try:
            from hermes_cli.config import load_config_readonly
            minutes = int((load_config_readonly() or {}).get("kanban", {})
                          .get("long_run_notice_minutes", 0) or 0)
        except Exception:
            minutes = 0
        if minutes <= 0:
            return
        try:
            cur = conn.execute(_LONG_RUN_SQL, (minutes * 60,))
            if cur.rowcount:
                conn.commit()
                logger.info("kanban notifier: board %s — %d run(s) passed %dm, notice queued",
                            slug, cur.rowcount, minutes)
        except Exception as exc:
            logger.debug("kanban notifier: long-run notice failed on %s: %s", slug, exc)

    def _gc_stale_subs(self, conn: Any, slug: str) -> None:
        """Best-effort stale-sub sweep: a failed sweep never blocks delivery; the next hourly gate retries."""
        try:
            _purged = _kbn().purge_stale_done_notify_subs(conn, max_age_days=self.gc_retention_days)
            if _purged:
                logger.info("kanban notifier: purged %d stale done/blocked-task subscription(s) on board %s (retention %dd)",
                            _purged, slug, self.gc_retention_days)
        except Exception as _gc_exc:
            logger.debug("kanban notifier: stale-sub GC failed for board %s: %s", slug, _gc_exc)

    def _claim_for_sub(self, conn: Any, slug: str, sub: dict) -> Optional[dict]:
        """Claim one subscription's unseen events; None when skipped or nothing new."""
        owner_profile = sub.get("notifier_profile") or None
        platform = (sub.get("platform") or "").lower()
        if platform not in self.active_platforms:
            seen = getattr(self.runner, "_kanban_undeliverable_subs", set())
            key = (sub["task_id"], platform, sub["chat_id"], sub.get("thread_id"))
            if key not in seen:
                logger.warning("kanban notifier: undeliverable subscription for %s on %s", sub["task_id"], platform)
                seen.add(key)
                self.runner._kanban_undeliverable_subs = seen
            return None
        metadata = sub.get("delivery_metadata") or {}
        # Progress delivery is opt-in and passive: comments must never create
        # agent wake/comment loops or override an explicit wake-only preference.
        comments_enabled = (
            isinstance(metadata, dict) and metadata.get("notify_comments") is True
            and sub.get("delivery_mode") != "wake"
        )
        kinds = TERMINAL_KINDS + (("commented",) if comments_enabled else ())
        _kbn().stage_unseen_notify_deliveries_for_sub(
            conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "", kinds=kinds,
        )
        rows = _kbn().list_notify_deliveries(conn, task_id=sub["task_id"], platform=sub["platform"],
            chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "",
            states=("pending", "sending", "ambiguous", "reconciling", "parked"))
        if not rows:
            return None
        task = self.kb.get_task(conn, sub["task_id"])
        comments = {}
        for row in rows:
            event = row["event"]
            comment_id = _payload(event, "comment_id") if event.kind == "commented" else None
            if isinstance(comment_id, int):
                comment = self.kb.get_comment(conn, sub["task_id"], comment_id)
                if comment:
                    comments[event.id] = comment.body
        # ONE-CARD: carry any PR urls this card's worker posted, so a completion
        # that names a bare PR number can still render the url. Indexed by task_id.
        pr_urls = []
        try:
            for row in conn.execute(
                    "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE '%/pull/%' "
                    "ORDER BY created_at DESC LIMIT 40", (sub["task_id"],)):
                pr_urls.extend(_ONE_CARD_PR_RE.findall(row["body"] or ""))
        except Exception as exc:
            logger.debug("kanban notifier: PR url scan failed for %s: %s", sub["task_id"], exc)
        return {"sub": sub, "old_cursor": sub.get("last_event_id", 0), "cursor": sub.get("last_event_id", 0),
                "pr_urls": pr_urls,
                "events": [r["event"] for r in rows], "delivery_rows": rows,
                "pending_parents": self.kb.pending_parents(conn, sub["task_id"]),
                "task": task, "board": slug, "comments": comments}

    def collect_board(self, slug: str) -> None:
        """Claim events on one board, appending delivery dicts to ``deliveries``."""
        kb = self.kb
        try:
            conn = _kbc().connect(board=slug)
        except Exception as exc:
            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
            return
        try:
            # ONE-CARD: backfill precedes the gate. _board_has_subs skips a board with
            # no subscriptions, which is precisely the board whose cards nobody hears.
            # Backfilling first costs one indexed NOT EXISTS per tick and makes the
            # gate below correct instead of self-defeating.
            self._ensure_default_subs(conn, slug)
            self._emit_long_run_notices(conn, slug)
            if not self._board_has_subs(slug):
                return
            if self.gc_due:
                self._gc_stale_subs(conn, slug)
            # No explicit init_db(): connect() already runs the migration once per
            # process, and init_db() would re-run it on a second connection racing
            # the first.
            subs = _kbn().list_notify_subs(conn, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
            if not subs:
                logger.debug("kanban notifier: board %s has no subscriptions", slug)
            for sub in subs:
                try:
                    claimed = self._claim_for_sub(conn, slug, sub)
                    if claimed is not None:
                        self.deliveries.append(claimed)
                except Exception as sub_exc:
                    # One bad subscription must not block the rest of the tick.
                    logger.warning("kanban notifier: subscription for %s on board %s failed: %s",
                                   sub.get("task_id"), slug, sub_exc)
        finally:
            conn.close()


def _notifier_collect(runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> list[dict]:
    """Claim unseen terminal events for every owned subscription on every board.

    Each gateway polls only subscriptions owned by profiles whose adapters it
    hosts; legacy rows without a profile stamp are visible only to the process
    holding the singleton dispatcher lock.
    """
    return _Collector(
        runner, kb, notifier_profile=notifier_profile, gc_due=gc_due, gc_retention_days=gc_retention_days,
    ).collect()


_NOTICE_REASON_LIMIT = 400
_SELF_ADVANCING_STATUSES = ("ready", "running", "todo", "review", "in_review")

def _ready_line(task: Any) -> str:
    """Answer "will the board move this on its own?" — the Ready yes/no."""
    status = str(getattr(task, "status", "") or "unknown")
    if status in _SELF_ADVANCING_STATUSES:
        return f"yes ({status} — the board will pick this up on its own)"
    return f"no ({status} — nothing moves until a human acts)"


def _stop_notice(
    *,
    icon: str,
    board_tag: str,
    who_tag: str,
    task_id: str,
    headline: str,
    title: str,
    reason: str,
    ready: str,
    next_action: str,
) -> str:
    """Render one durable, actionable stop notice.

    Every stop carries the same four answers, because an operator reading a
    ping in a busy thread needs all four to act without opening the board:
    which card, what exactly stopped it, whether the board will recover on its
    own, and who does what next.
    """
    lines = [f"{icon} {board_tag}{who_tag}Kanban {task_id} {headline} — {title}"]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append(f"Ready: {ready}")
    lines.append(f"Next: {next_action}")
    return "\n".join(lines)


# --- Per-event message formatting: kind -> (msg, wake_handoff, wake_review_detail) ---
# ``None`` for handoff / review_detail leaves the accumulated wake value untouched.


def _payload(ev: Any, key: str) -> Any:
    """Shared "payload present and truthy" read."""
    return ev.payload.get(key) if ev.payload and ev.payload.get(key) else None


def _clip(ev: Any, key: str, msg_key: str, limit: int) -> str:
    """Catalog message ``msg_key`` (``{value}`` placeholder) rendered with the truncated payload
    value, or ``""`` when absent."""
    value = _payload(ev, key)
    return t(msg_key, value=str(value)[:limit]) if value else ""


_NL = "\n{}"


def _first_line(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    return lines[0][:limit] if lines else text[:limit]


def _fmt_completed(ev, n) -> tuple:
    # Prefer the run summary from the event payload; fall back to task.result for legacy rows.
    wake_handoff = None
    payload_summary = _payload(ev, "summary")
    if payload_summary:
        wake_handoff = _first_line(str(payload_summary), 200)
    elif n.task and n.task.result:
        wake_handoff = _first_line(n.task.result, 160)
    handoff = f"\n{wake_handoff}" if wake_handoff is not None else ""
    return t("gateway.kanban.ping.completed", head=n.head, title=n.title, handoff=handoff), wake_handoff, None


def _fmt_review_requested(ev, n) -> tuple:
    # Implementation done; task moved to the review lane. Carry the handoff
    # into the wake turn like ``completed`` so the reviewer needn't re-read the board.
    handoff = ""
    wake_handoff = None
    summary = _payload(ev, "summary")
    if summary:
        summary = str(summary)
        handoff = f"\n{summary[:200]}"
        wake_handoff = _first_line(summary, 200)
    return t("gateway.kanban.ping.review_requested", head=n.head, title=n.title, handoff=handoff), wake_handoff, None


def _fmt_changes_requested(ev, n) -> tuple:
    payload = ev.payload or {}
    reason = _safe_review_reason(payload.get("reason"))
    reviewer = _safe_review_reason(payload.get("reviewer"), 48)
    implementer = _safe_review_reason(payload.get("implementer"), 48)
    reason_text = reason or t("gateway.kanban.ping.changes_default_reason")
    provenance = t("gateway.kanban.ping.reviewer_suffix", reviewer=reviewer) if reviewer else ""
    if implementer:
        provenance += t("gateway.kanban.ping.implementer_suffix", implementer=implementer)
    msg = t("gateway.kanban.ping.changes_requested",
            board_tag=n.board_tag, task_id=n.task_id, reason=reason_text, provenance=provenance)
    return msg, None, reason_text


def _fmt_block_loop_detected(ev, n) -> tuple:
    """Re-blocked for the same cause past the limit and routed to `triage`.

    It emits no blocked/status event, so ping loudly here. A repeated-block
    circuit breaker establishes that orchestration attention is needed; it
    does NOT establish that a human decision or owner input exists. Use
    neutral orchestration wording unless the block was typed as a genuine
    owner-input request (`needs_input`, the only kind that carries a concrete
    question for the owner).
    """
    kind = _payload(ev, "kind")
    decision = kind == "needs_input"
    msg = t(
        "gateway.kanban.ping.triage", head=n.head,
        why=t("gateway.kanban.ping.triage_decision" if decision else "gateway.kanban.ping.triage_attention"),
        recurrences=_clip(ev, "recurrences", "gateway.kanban.ping.triage_recurrences", 200),
        reason=_clip(ev, "reason", "gateway.kanban.ping.reason_suffix", 160),
    )
    return msg, None, None


def _fmt_gave_up(ev, n) -> tuple:
    # The dispatcher auto-blocked the task after ``failures`` consecutive non-success attempts
    # (spawn failure, crash, or timeout alike): it is now Blocked and waiting for a human.
    failures = _payload(ev, "failures")
    count = (t("gateway.kanban.ping.failed_n_times", count=int(failures)) if failures
             else t("gateway.kanban.ping.kept_failing"))
    last = _clip(ev, "error", "gateway.kanban.ping.last_error", 160)
    return t("gateway.kanban.ping.gave_up", head=n.head, count=count, last=last, task_id=n.task_id), None, None


def _fmt_timed_out(ev, n) -> tuple:
    limit = int(_payload(ev, "limit_seconds") or 0)
    minutes = max(1, round(limit / 60)) if limit else 0
    span = t("gateway.kanban.ping.limit_minutes", minutes=minutes) if minutes else t("gateway.kanban.ping.limit_generic")
    return t("gateway.kanban.ping.timed_out", head=n.head, span=span), None, None


# archived / unblocked are claimed (so the cursor advances past them) but
# intentionally silent (no formatter), and excluded from _WAKE_KINDS so they
# never wake the creator.
def _notice_completed(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    handoff = ''
    payload_summary = None
    if ev.payload and ev.payload.get('summary'):
        payload_summary = str(ev.payload['summary'])
    if payload_summary:
        lines = payload_summary.strip().splitlines()
        h = lines[0][:200] if lines else payload_summary[:200]
        handoff = f'\n{h}'
        wake_handoff = h
    elif task and task.result:
        lines = task.result.strip().splitlines()
        r = lines[0][:160] if lines else task.result[:160]
        handoff = f'\n{r}'
        wake_handoff = r
    completion_kind = str((ev.payload or {}).get('completion_kind') or 'final')
    review_handoff = (ev.payload or {}).get('review_handoff')
    if completion_kind == 'delivery_review_result':
        implementation_task = _safe_review_reason(review_handoff.get('implementation_task_id'), 48) if isinstance(review_handoff, dict) else ''
        review_owner = _safe_review_reason(review_handoff.get('owner'), 48) if isinstance(review_handoff, dict) else ''
        msg = _stop_notice(icon='🧾', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='REVIEW RESULT RECORDED', title=title, reason=_safe_review_reason(payload_summary or 'independent review run completed', _NOTICE_REASON_LIMIT), ready='no (the review result is intermediate; the delivery controller has not accepted it)', next_action=f"@{review_owner or 'review owner'} — evaluate this result through the canonical delivery controller" + (f' for implementation task {implementation_task}.' if implementation_task else '.'))
    elif completion_kind == 'implementation_ready_for_review' and isinstance(review_handoff, dict):
        review_task_id = _safe_review_reason(review_handoff.get('review_task_id'), 48)
        review_owner = _safe_review_reason(review_handoff.get('owner'), 48)
        msg = _stop_notice(icon='👀', board_tag=board_tag, who_tag='', task_id=sub['task_id'], headline='REVIEW HANDOFF', title=title, reason=_safe_review_reason(payload_summary or 'implementation ready', _NOTICE_REASON_LIMIT), ready='no (implementation ready for independent review; acceptance is pending)', next_action=f"@{review_owner or 'review owner'} — ensure {review_task_id or 'the canonical review task'} is independently reviewed. This implementation handoff is not final acceptance.")
    else:
        msg = f"✔ {board_tag}{tag}Kanban {sub['task_id']} done — {title}{handoff}"
    return msg, wake_handoff, wake_review_detail


def _notice_delivery_phase_completed(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    payload = ev.payload or {}
    implementation_task = _safe_review_reason(payload.get('implementation_task'), 48) or sub['task_id']
    review_task = _safe_review_reason(payload.get('review_task'), 48)
    head = _safe_review_reason(payload.get('head'), 40)
    pair = f'head {head}'
    if review_task:
        pair += f'; review task {review_task}'
    review_requirement = getattr(task, 'review_requirement', None) if task else None
    if isinstance(review_requirement, str):
        try:
            review_requirement = json.loads(review_requirement)
        except (TypeError, ValueError):
            review_requirement = {}
    if not isinstance(review_requirement, dict):
        review_requirement = {}
    contract_owner = _safe_review_reason(review_requirement.get('owner'), 48)
    if kind == 'delivery_phase_completed':
        evidence = payload.get('evidence')
        evidence = evidence if isinstance(evidence, dict) else {}
        artifact = _safe_review_reason(evidence.get('artifact'), 120)
        checks = evidence.get('checks')
        check_count = len(checks) if isinstance(checks, list) else 0
        evidence_text = f'; evidence {artifact}, {check_count} focused check(s)' if artifact else f'; {check_count} focused check(s) recorded'
        msg = _stop_notice(icon='👀', board_tag=board_tag, who_tag='', task_id=implementation_task, headline='PHASE COMPLETE', title=title, reason=f'{pair}{evidence_text}; acceptance pending', ready='no (independent review and acceptance are pending)', next_action=f"review task {review_task or 'named in the contract'} — independently review exact head {head}.")
    elif kind == 'delivery_changes_requested':
        findings = _safe_review_reason(payload.get('findings'), _NOTICE_REASON_LIMIT)
        msg = _stop_notice(icon='🛑', board_tag=board_tag, who_tag='', task_id=implementation_task, headline='CHANGES REQUESTED', title=title, reason=findings or 'reviewer requested changes', ready='no (this generation is not accepted)', next_action=f'{owner} — address the findings and submit a new exact-head generation; prior {pair}.')
        wake_review_detail = findings
    elif kind == 'delivery_accepted':
        accepted = payload.get('acceptance') == 'ready'
        msg = _stop_notice(icon='✅', board_tag=board_tag, who_tag='', task_id=implementation_task, headline='DELIVERY ACCEPTED', title=title, reason=f'canonical independent review accepted {pair}' if accepted else f'acceptance metadata is incomplete for {pair}', ready='yes (canonical acceptance is ready)' if accepted else 'no (canonical acceptance is not ready)', next_action='origin owner — delivery is accepted; proceed with the separately authorized release or activation step.' if accepted else 'origin owner — inspect the malformed acceptance event.')
    else:
        reason = _safe_review_reason(payload.get('reason'), _NOTICE_REASON_LIMIT)
        msg = _stop_notice(icon='⏸', board_tag=board_tag, who_tag='', task_id=implementation_task, headline='DELIVERY HOLD', title=title, reason=reason or 'delivery review is held', ready='no (delivery acceptance is held)', next_action=f"@{contract_owner or 'review owner'} — resolve the hold for {pair}, then submit the required evidence through the same canonical pair.")
    return msg, wake_handoff, wake_review_detail


def _notice_blocked(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    payload = ev.payload or {}
    raw_reason = payload.get('reason')
    block_kind = str(payload.get('kind') or '').strip()
    ready = _ready_line(task)
    if kind == 'dependency_wait':
        pending = d.get('pending_parents') or []
        if pending:
            waiting_on = ', '.join((f"{parent['id']} “{str(parent.get('title') or '')[:60]}” ({parent.get('status')})" for parent in pending[:5]))
            next_action = f'nothing for you yet — this card resumes when {waiting_on} finishes.'
        else:
            next_action = f'{owner} — this card declared a dependency but has no unsatisfied dependency on the board, so it will be re-dispatched and repeat the same work. Link the card it is really waiting on, or re-block it as a real blocker (`--kind needs_input`).'
        msg = _stop_notice(icon='⏳', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='is waiting on a dependency', title=title, reason=_safe_review_reason(raw_reason, _NOTICE_REASON_LIMIT), ready=ready, next_action=next_action)
    else:
        if payload.get('routed_from') == 'dependency':
            next_action = f"{owner} — this arrived as a dependency wait with nothing to wait for, so it was held here instead of being re-dispatched. Resolve it, then `hermes kanban unblock {sub['task_id']}`."
        elif block_kind == 'capability':
            next_action = f"{owner} — grant the missing tool/permission above, then `hermes kanban unblock {sub['task_id']}`."
        elif block_kind == 'needs_input':
            next_action = f"{owner} — answer the blocker above, then `hermes kanban unblock {sub['task_id']}`."
        else:
            next_action = f"{owner} — resolve the blocker above, then `hermes kanban unblock {sub['task_id']}`."
        msg = _stop_notice(icon='⏸', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='blocked', title=title, reason=_safe_review_reason(raw_reason, _NOTICE_REASON_LIMIT), ready=ready, next_action=next_action)
    return msg, wake_handoff, wake_review_detail


def _notice_gave_up(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    msg = _stop_notice(icon='✖', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='gave up after repeated spawn failures', title=title, reason=_safe_review_reason((ev.payload or {}).get('error'), _NOTICE_REASON_LIMIT), ready=_ready_line(task), next_action=f'{owner} — fix the spawn failure above (profile venv, PATH, credentials, quota), then requeue the card. The dispatcher has stopped retrying it.')
    return msg, wake_handoff, wake_review_detail


def _notice_crashed(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    msg = _stop_notice(icon='✖', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='worker crashed (pid gone)', title=title, reason=_safe_review_reason((ev.payload or {}).get('error') or 'the worker exited without a terminal lifecycle call', _NOTICE_REASON_LIMIT), ready=_ready_line(task), next_action=f'{owner} — operator decision required: requeue this card or block it with the real reason. Confirm the outcome yourself rather than assuming a retry.')
    return msg, wake_handoff, wake_review_detail


def _notice_timed_out(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    limit = 0
    if ev.payload and ev.payload.get('limit_seconds'):
        limit = int(ev.payload['limit_seconds'])
    msg = _stop_notice(icon='⏱', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline='timed out', title=title, reason=f'exceeded max_runtime={limit}s', ready=_ready_line(task), next_action=f'{owner} — decide whether to raise max_runtime, split the card, or block it. Do not assume the retry will finish any faster.')
    return msg, wake_handoff, wake_review_detail


def _notice_status(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    new_status = ''
    if ev.payload and ev.payload.get('status'):
        new_status = str(ev.payload['status'])
    msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
    return msg, wake_handoff, wake_review_detail


def _notice_review_requested(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    payload = ev.payload or {}
    summary = str(payload.get('summary') or '').strip()
    if summary:
        lines = summary.strip().splitlines()
        wake_handoff = lines[0][:200] if lines else summary[:200]
    reviewer = _safe_review_reason(payload.get('reviewer'), 48)
    implementer = _safe_review_reason(payload.get('implementer'), 48)
    contract_owner = _safe_review_reason(payload.get('owner'), 48)
    if kind == 'review_handoff_required':
        review_task_id = _safe_review_reason(payload.get('review_task_id'), 48)
        if review_task_id:
            ready = 'no (canonical review generation awaits handoff)'
            next_action = f"@{contract_owner or 'review owner'} — hand exact generation evidence to review task {review_task_id}; acceptance remains pending."
        else:
            ready = 'no (canonical review task is missing)'
            next_action = f"@{contract_owner or 'review owner'} — create or assign exactly one independent review task, link it as a dependency child, attach its id to review_requirement, then complete this implementation handoff once to release it."
    elif reviewer:
        ready = 'yes (canonical reviewer is assigned)'
        next_action = f'@{reviewer} — independently review this same card; approve with complete or return it with request-changes. An OPEN/non-Draft PR is not review or acceptance proof.'
    else:
        ready = 'no (canonical reviewer is unassigned)'
        origin = f'@{implementer}' if implementer else 'origin owner'
        next_action = f'origin owner ({origin}) — assign exactly one independent reviewer to this same card. Do not create or reuse an unrelated child.'
    msg = _stop_notice(icon='👀', board_tag=board_tag, who_tag='', task_id=sub['task_id'], headline='REVIEW HANDOFF', title=title, reason=_safe_review_reason(summary or 'implementation entered structured review', _NOTICE_REASON_LIMIT), ready=ready, next_action=next_action)
    return msg, wake_handoff, wake_review_detail


def _notice_changes_requested(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    payload = ev.payload or {}
    reason = _safe_review_reason(payload.get('reason'))
    reviewer = _safe_review_reason(payload.get('reviewer'), 48)
    implementer = _safe_review_reason(payload.get('implementer'), 48)
    reason_text = reason or 'reviewer feedback requires changes'
    provenance = ''
    if reviewer:
        provenance += f' — reviewer @{reviewer}'
    if implementer:
        provenance += f' → implementer @{implementer}'
    msg = f"🛑 {board_tag}Kanban {sub['task_id']} review requested changes/BLOCK: {reason_text}{provenance}"
    wake_review_detail = reason_text
    return msg, wake_handoff, wake_review_detail


def _notice_block_loop_detected(ev, n):
    task, sub, d = n.task, n.sub, n.d
    title, board_tag = n.title, n.board_tag
    tag = f"@{task.assignee} " if task and task.assignee else ""
    owner = f"@{task.assignee}" if task and task.assignee else "origin owner"
    kind = ev.kind
    wake_handoff = wake_review_detail = None
    payload = ev.payload or {}
    recurrences = payload.get('recurrences')
    rc = f' (blocked {recurrences}x for the same cause)' if recurrences else ''
    msg = _stop_notice(icon='🛑', board_tag=board_tag, who_tag=tag, task_id=sub['task_id'], headline=f'routed to TRIAGE{rc}', title=title, reason=_safe_review_reason(payload.get('reason'), _NOTICE_REASON_LIMIT), ready=_ready_line(task), next_action=f'{owner} — the unblock loop was broken deliberately; a human has to decide what changes before this card runs again. Unblocking it as-is will loop.')
    return msg, wake_handoff, wake_review_detail

_EVENT_FORMATTERS = {
    'completed': _notice_completed,
    'delivery_phase_completed': _notice_delivery_phase_completed,
    'delivery_changes_requested': _notice_delivery_phase_completed,
    'delivery_accepted': _notice_delivery_phase_completed,
    'delivery_review_hold': _notice_delivery_phase_completed,
    'blocked': _notice_blocked,
    'dependency_wait': _notice_blocked,
    'gave_up': _notice_gave_up,
    'crashed': _notice_crashed,
    'timed_out': _notice_timed_out,
    'status': _notice_status,
    'review_requested': _notice_review_requested,
    'review_handoff_required': _notice_review_requested,
    'changes_requested': _notice_changes_requested,
    'block_loop_detected': _notice_block_loop_detected,
}




# ============================================================================
# ONE-CARD NOTIFIER (2026-09-08) — Discord gets exactly five lines.
#
#   1. Task added: <title>
#   2. <agent> started: <title>
#   3. <agent> finished: <title>
#   4. PR ready for review: <url>
#   5. Needs your decision: <title> — <one-line reason>
#
# Everything else — progress narration, retry notices, guard notices, stall
# warnings, review handoffs, delivery-phase notices — goes to logs only. The
# formatters above are left in place but unreachable: _EVENT_FORMATTERS is
# rebound here, and TERMINAL_KINDS is narrowed so nothing else is even claimed.
# ============================================================================

_ONE_CARD_PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


_DEFAULT_SUB_SQL = """
            INSERT INTO kanban_notify_subs (
                task_id, platform, chat_id, chat_type, thread_id, user_id,
                notifier_profile, delivery_metadata, created_at, last_event_id, delivery_mode)
            SELECT t.id, 'discord', ?, 'group', '', NULL, ?, NULL, strftime('%s','now'),
                   CASE WHEN c.created_at > strftime('%s','now') - 600
                        THEN MAX(COALESCE(c.id, 0) - 1, 0)
                        ELSE (SELECT COALESCE(MAX(id), 0) FROM task_events) END,
                   'notify'
              FROM tasks t
              LEFT JOIN (SELECT task_id, MIN(id) AS id, MIN(created_at) AS created_at
                           FROM task_events WHERE kind = 'created' GROUP BY task_id) c
                     ON c.task_id = t.id
             WHERE t.status NOT IN ('done', 'archived')
               AND NOT EXISTS (SELECT 1 FROM kanban_notify_subs s WHERE s.task_id = t.id)
"""


_UNHEARD_SQL = """
            SELECT t.id AS id, t.title AS title, t.body AS body,
                   CASE WHEN c.created_at > strftime('%s','now') - 600
                        THEN MAX(COALESCE(c.id, 0) - 1, 0)
                        ELSE (SELECT COALESCE(MAX(id), 0) FROM task_events) END AS cursor
              FROM tasks t
              LEFT JOIN (SELECT task_id, MIN(id) AS id, MIN(created_at) AS created_at
                           FROM task_events WHERE kind = 'created' GROUP BY task_id) c
                     ON c.task_id = t.id
             WHERE t.status NOT IN ('done', 'archived')
               AND NOT EXISTS (SELECT 1 FROM kanban_notify_subs s WHERE s.task_id = t.id)
"""

_LONG_RUN_SQL = """
            INSERT INTO task_events (task_id, run_id, kind, payload, created_at)
            SELECT t.id, r.id, 'still_working',
                   json_object('minutes', (strftime('%s','now') - r.started_at) / 60),
                   strftime('%s','now')
              FROM tasks t
              JOIN task_runs r ON r.task_id = t.id AND r.ended_at IS NULL
             WHERE t.status = 'running'
               AND strftime('%s','now') - r.started_at > ?
               AND NOT EXISTS (SELECT 1 FROM task_events e
                                WHERE e.task_id = t.id AND e.run_id = r.id
                                  AND e.kind = 'still_working')
"""


# Assignees that name a mechanism rather than a role. `default` is the root
# profile every unrouted card lands on; printing it tells the reader nothing.
# --------------------------- 16. status updates belong on the triage ticket
# Brett, 2026-09-08: "the original idea was to have those status updates be
# posted in the mission control triage threads." I had deleted the thing that
# did that -- kanban_block_alert_target.py resolved a card to its
# mission-control-triage post -- as "dead under one card", then replaced it with
# a single fallback channel, then moved that channel to #kanban. Two steps away
# from the design.
#
# Resolution order, most reliable first:
#   1. the intake's own kanban_task_id -> triage_message_id mapping
#   2. a ticket id in the card title -> that ticket's triage_message_id
#   3. the board's #kanban channel (kanban.default_notify_target)
#
# A card that came from a ticket reports next to its ticket; a card that came
# from nowhere reports to #kanban. Nothing is ever silent.
_TRIAGE_STATE_FILES = (
    "~/.hermes/state/discord-github-intake.json",
    "~/.hermes/state/mms-dms-discord-github-intake.json",
)
_TRIAGE_TICKET_RE = re.compile(r"\b((?:DMS-)?(?:BUG|INT|REQ|COST)-\d+)\b", re.I)
_triage_cache = {"stamp": None, "by_task": {}, "by_ticket": {}}

# Where a card says it came from, when it never went through the intake.
# Cards opened by an agent record the origin in prose, not as a link:
#   "Brett approved this separate feature in Discord thread1541782237887078501"
#   "Brett authorizes continuation ... in Discord channel1537572084946046976"
# and intake-authored bodies carry a real url. Both forms are read here, in
# preference order: an explicit thread beats a channel beats a url, because a
# thread is the most specific place the request actually lives.
_TRIAGE_ORIGIN_RES = (
    re.compile(r"\bthread\s*[:#]?\s*(\d{17,20})\b", re.I),
    re.compile(r"\bchannel\s*[:#]?\s*(\d{17,20})\b", re.I),
    re.compile(r"discord\.com/channels/\d{17,20}/(\d{17,20})"),
)


def _origin_from_body(body: str):
    """The Discord thread or channel a card says it came from, or None."""
    if not body:
        return None
    head = body[:4000]   # the origin is stated up front; do not scan a whole spec
    for pattern in _TRIAGE_ORIGIN_RES:
        match = pattern.search(head)
        if match:
            return match.group(1)
    return None


def _triage_index():
    """{task_id: thread} and {ticket: thread} from the intake state, cached on mtime.

    The ReefMind state file is ~3MB; re-parsing it every tick per board would be
    the most expensive thing the notifier does. Keyed on the files' mtimes, so an
    intake write is picked up on the next tick without a restart.
    """
    paths = [Path(p).expanduser() for p in _TRIAGE_STATE_FILES]
    try:
        stamp = tuple(p.stat().st_mtime_ns if p.is_file() else 0 for p in paths)
    except OSError:
        stamp = None
    if stamp is not None and stamp == _triage_cache["stamp"]:
        return _triage_cache["by_task"], _triage_cache["by_ticket"]

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("public_id"), str):
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    by_task, by_ticket = {}, {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.debug("kanban notifier: cannot read intake state %s: %s", path, exc)
            continue
        for rec in walk(data):
            thread = str(rec.get("triage_message_id") or "").strip()
            if not thread.isdigit():
                continue
            task = rec.get("kanban_task_id")
            if task:
                by_task[str(task)] = thread
            ticket = str(rec.get("public_id") or "").strip().upper()
            if ticket:
                by_ticket.setdefault(ticket, thread)
    _triage_cache.update(stamp=stamp, by_task=by_task, by_ticket=by_ticket)
    return by_task, by_ticket


def _triage_thread_for(task_id: str, title: str, body: str = ""):
    """Where this card reports, most reliable source first.

    1. the intake's own kanban_task_id -> triage_message_id mapping
    2. a ticket id in the title -> that ticket's triage thread
    3. ONE-CARD: body origin is the third path -- the Discord thread or channel
       the card itself says it came from. Cards opened by an agent rather than
       the intake have no ticket and no session, but they do say where the
       request was made, and that is where the answer belongs.
    None means the caller should fall back to the board channel.
    """
    by_task, by_ticket = _triage_index()
    thread = by_task.get(str(task_id))
    if thread:
        return thread
    match = _TRIAGE_TICKET_RE.search(title or "")
    if match:
        thread = by_ticket.get(match.group(1).upper())
        if thread:
            return thread
    return _origin_from_body(body)
_ONE_CARD_GENERIC_AGENTS = {"", "default", "hermes", "worker", "none"}


def _one_card_agent(n) -> str:
    """The role that ran the card, or "" when naming it would be noise."""
    who = (getattr(n.task, "assignee", "") or "").strip() if n.task else ""
    return "" if who.lower() in _ONE_CARD_GENERIC_AGENTS else who


def _one_card_label(n, verb: str) -> str:
    """`**backend started:**` when a role owns the card, `**Started:**` when none does."""
    who = _one_card_agent(n)
    return f"**{who} {verb}:**" if who else f"**{verb[0].upper()}{verb[1:]}:**"


_ONE_CARD_PR_NUM_RE = re.compile(r"\bPR\s*#?\s*(\d{2,6})\b", re.I)


def _one_card_pr_url(ev, n):
    """The PR this run opened, or None.

    ONE-CARD: resolve a bare PR number. A full url in the handoff wins. Failing
    that, a bare "PR #3368" is resolved against a url the worker posted in a
    comment on this same card -- matched BY NUMBER, so a stale url from an
    earlier PR can never be attached to this completion. A handoff that
    mentions no PR yields no line.
    """
    blobs = (str((ev.payload or {}).get("summary") or ""),
             str((ev.payload or {}).get("result") or ""),
             str(getattr(n.task, "result", "") or ""))
    for blob in blobs:
        found = _ONE_CARD_PR_RE.search(blob)
        if found:
            return found.group(0)
    numbers = {m.group(1) for blob in blobs for m in _ONE_CARD_PR_NUM_RE.finditer(blob)}
    if not numbers:
        return None
    for url in (n.d.get("pr_urls") or ()):
        if url.rsplit("/", 1)[-1] in numbers:
            return url
    return None


def _one_card_created(ev, n):
    return f"**Task added:** {n.title}", None, None


def _one_card_started(ev, n):
    # BLOCK-CONTRACT-FIRST-SPAWN: 3.8 'started' notices per card were requeues
    # and synthetic review runs, not news. Only the first run is announced.
    try:
        from hermes_cli import kanban_db_connect as _kbc
        with _kbc.connect_closing(board=n.board_slug) as _c:
            _runs = _c.execute('SELECT COUNT(*) FROM task_runs WHERE task_id = ?', (n.task_id,)).fetchone()[0]
        if int(_runs or 0) > 1:
            return None, None, None
    except Exception:
        pass
    return f'{_one_card_label(n, "started")} {n.title}', None, None


def _one_card_finished(ev, n):
    """Completion, plus the PR line when this run opened one."""
    if (ev.payload or {}).get("completion_kind") in {
        "implementation_ready_for_review", "delivery_review_result",
    }:
        return _notice_completed(ev, n)
    msg = f'{_one_card_label(n, "finished")} {n.title}'
    url = _one_card_pr_url(ev, n)
    if url:
        msg += f"\n**PR:** {url}"
    return msg, None, None


def _one_card_still_working(ev, n):
    """Fires once per run, past kanban.long_run_notice_minutes.

    Not progress narration: it says the run is alive and how long it has been
    going, nothing about what it is doing. A card that is working and a card
    that has died look identical otherwise.
    """
    minutes = (ev.payload or {}).get("minutes")
    span = f" ({minutes}m)" if minutes else ""
    return f'{_one_card_label(n, "still working")} {n.title}{span}', None, None


def _one_card_needs_decision(ev, n):
    """The ONLY blocker message. One sentence; no Ready/Next scaffolding.

    ONE-CARD: every park is heard. Serves three event kinds, because all three
    leave the card stopped with nothing that will move it on its own:
      blocked              a worker asked for a decision
      gave_up              the dispatcher has stopped retrying
      block_loop_detected  escalated to `triage`, and nothing promotes triage
                           now that kanban_recovery.py is gone
    `gave_up` carries its cause in `error` rather than `reason`.
    """
    payload = ev.payload or {}
    raw = payload.get("reason") or payload.get("error") or "no reason given"
    return _block_contract_notice(ev, n, str(raw)), None, None  # BLOCK-CONTRACT-NOTICE


_BLOCK_NOTICE_LIMIT = 1600


def _block_contract_notice(ev, n, raw: str) -> str:
    """Render the block contract in full: headline, the worker's WHAT/WHY/DO-THIS
    lines (per-line redaction, newlines preserved), the PR or branch, and the
    exact reply that resumes the card. 180 characters of prose was the reason
    Brett could not tell why half the board was blocked."""
    from agent.redact import redact_sensitive_text
    tid = (n.sub or {}).get("task_id") or getattr(n.task, "id", "?")
    text = redact_sensitive_text(raw, force=True, redact_url_credentials=True)
    text = _LOCAL_PATH_RE.sub("[local path]", text)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    head = (lines[0].strip().upper() if lines else "")
    structured = head.startswith(("WHAT I NEED", "CANNOT DO", "DECISION NEEDED"))
    if head.startswith("CANNOT DO"):
        icon, headline = "⛔", "CANNOT DO"
    elif head.startswith("DECISION NEEDED"):
        icon, headline = "❓", "DECISION NEEDED"
    else:
        icon, headline = "🛑", "BLOCKED"
    if not structured:
        cause = {"gave_up": "the dispatcher stopped retrying this card",
                 "block_loop_detected": "this card keeps re-blocking for the same cause"}.get(
                     ev.kind, "a worker parked this card without the block contract")
        lines = [f"WHAT I NEED: a look at why {cause}",
                 "WHY: " + " ".join(lines)[:600],
                 "DO THIS:",
                 f"  1. `hermes kanban show {tid}` and read the last run.",
                 f"  2. Reply here with `unblock {tid}` and what to change, or `archive {tid}` to drop it."]
    body, used = [], 0
    for ln in lines:
        if used + len(ln) > _BLOCK_NOTICE_LIMIT:
            body.append("…")
            break
        body.append(ln)
        used += len(ln) + 1
    out = [f"{icon} **{headline}** · `{tid}` · {n.title}"] + body
    url = _one_card_pr_url(ev, n)
    branch = getattr(n.task, "branch_name", None) if n.task else None
    if url:
        out.append(f"WORK SO FAR: {url}")
    elif branch:
        out.append(f"WORK SO FAR: branch `{branch}`, nothing lost")
    else:
        out.append("WORK SO FAR: on the card, nothing lost")
    out.append(f"THEN: reply in this thread with `unblock {tid}` and your answer, "
               f"or `archive {tid}` to close it.")
    return "\n".join(out)


_EVENT_FORMATTERS = {
    "created": _one_card_created,
    "spawned": _one_card_started,
    "completed": _one_card_finished,
    "still_working": _one_card_still_working,
    "blocked": _one_card_needs_decision,
    "gave_up": _one_card_needs_decision,
    "block_loop_detected": _one_card_needs_decision,
    "review_requested": _notice_review_requested,
    "review_handoff_required": _notice_review_requested,
    "delivery_phase_completed": _notice_delivery_phase_completed,
    "delivery_changes_requested": _notice_delivery_phase_completed,
    "delivery_accepted": _notice_delivery_phase_completed,
    "delivery_review_hold": _notice_delivery_phase_completed,
}

# Claim only what we render, so no other kind can ever reach a chat. 'archived'
# stays claimed-but-silent because it is what retires a subscription.
# crashed/timed_out are NOT here on purpose: they requeue to `ready`, so they
# are not parks; they only become one via gave_up, which is here.
TERMINAL_KINDS = ("created", "spawned", "still_working",
                  "blocked", "gave_up", "block_loop_detected",
                  "completed", "archived", "review_requested", "review_handoff_required",
                  "delivery_phase_completed", "delivery_changes_requested",
                  "delivery_accepted", "delivery_review_hold")

# ONE-CARD: no wakes. A wake makes the origin agent take a turn and narrate into
# the channel, which is the progress narration this redesign deletes.
_WAKE_KINDS = ()
_DEFAULT_WAKE_KINDS = ()
_ALL_WAKE_KINDS = ()


def _configured_wake_kinds() -> tuple:
    return ()


# --- Delivery of one claimed batch (one subscription, N events) ---


class _KanbanNotification:
    """Deliver one subscription's claimed events, then settle the cursor.

    Both legs of notify+wake must succeed before settling. Sent pings have a
    separate durable checkpoint so wake retries do not resend them. Admission
    is at-least-once queueing, not an execution or final-response receipt.
    """

    def __init__(self, runner: Any, d: dict, *, platform_cls: Any, sub_fail_counts: dict) -> None:
        self.runner = runner
        self.d = d
        self.platform_cls = platform_cls
        self.sub_fail_counts = sub_fail_counts
        self.sub = sub = d["sub"]
        self.task = task = d["task"]
        self.board_slug = d.get("board")
        self.platform_str = (sub["platform"] or "").lower()
        self.task_id = sub["task_id"]
        self.sub_profile = sub.get("notifier_profile") or ""
        self.title = (task.title if task else sub["task_id"])[:120]
        self.board_tag = t("gateway.kanban.ping.board_tag", board=self.board_slug) if self.board_slug else ""
        # Attribute the ping to the worker that did the work.
        tag = t("gateway.kanban.ping.assignee_tag", assignee=task.assignee) if task and task.assignee else ""
        self.head = t("gateway.kanban.ping.head", board_tag=self.board_tag, assignee_tag=tag, task_id=self.task_id)
        # The wake self-post path needs the key even when every event was skipped.
        self.sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")
        mode = sub.get("delivery_mode") or "notify"
        self.wake_agent = mode in ("notify+wake", "wake")
        self.send_passive = mode != "wake"
        # Worker handoff carried into the synthetic wake turn so the woken
        # creator doesn't re-decompose work already on the board.
        self.wake_handoff = self.wake_review_detail = self.session_key = self.synth = ""
        self.plat: Any = None
        self.adapter: Any = None
        self.is_push_adapter = True
        self.wake_kinds: set = set()

    # -- cursor / subscription ops (blocking, run in a fresh-context thread) --

    async def rewind(self) -> None:
        """Ledger rows retain unsettled work without rewinding delivered events."""
        return None
    async def advance(self) -> None:
        """Per-event ledger acknowledgement owns cursor progress."""
        return None
    async def unsub(self) -> None:
        await _to_thread_process_service(self.runner._kanban_unsub, self.sub, self.board_slug)

    def clear_failures(self) -> None:
        self.sub_fail_counts.pop(self.sub_key, None)

    async def delivery_failed(self, fmt: str, prefix: tuple, drop_fmt: str, exc: Exception, exc_info: bool) -> None:
        """Bump the failure counter; drop the sub past the limit, else rewind the claim so the next tick retries."""
        fails = self.sub_fail_counts.get(self.sub_key, 0) + 1
        self.sub_fail_counts[self.sub_key] = fails
        logger.warning(fmt, *prefix, fails, MAX_SEND_FAILURES, exc, exc_info=exc_info)
        # A durable pending/ambiguous row survives transport outages and restart.
        if fails >= MAX_SEND_FAILURES:
            logger.error("kanban notification held for %s: %s", self.task_id, exc)

    async def _wake_failed(self, fmt: str, exc: Exception) -> None:
        drop_fmt = "kanban notifier: dropping subscription %s on %s after %d consecutive wake failures"
        await self.delivery_failed(fmt, (self.task_id,), drop_fmt, exc, True)

    # -- formatting --

    def format_event(self, ev: Any) -> Optional[str]:
        """Render one event; accumulates wake handoff/review detail. None → silent kind."""
        if ev.kind == "commented":
            # Legacy events have no stable comment id: never guess by timestamp
            # and accidentally attribute another comment (or task) to this event.
            body = self.d.get("comments", {}).get(ev.id)
            return f"ℹ {self.head} update — {self.title}\n{_safe_review_reason(body, 700)}" if body else None
        formatter = _EVENT_FORMATTERS.get(ev.kind)
        if formatter is None:
            return None
        msg, handoff, review_detail = formatter(ev, self)
        if handoff is not None:
            self.wake_handoff = handoff
        if review_detail is not None:
            self.wake_review_detail = review_detail
        return msg

    def build_wake_text(self) -> None:
        """Set ``wake_kinds`` / ``session_key`` / ``synth`` for the wake paths."""
        task, sub = self.task, self.sub
        self.wake_kinds = {ev.kind for ev in self.wake_events if ev.kind in _configured_wake_kinds()} if self.wake_agent else set()
        if not self.wake_kinds:
            return
        if self.is_push_adapter:
            self.session_key = getattr(task, "session_id", None) or ""
        else:
            # Non-push wakes target sub["chat_id"] (the raw session id the
            # subscriber registered). task.session_id may be a WORKER session
            # for child tasks; use it only for legacy rows.
            self.session_key = sub["chat_id"] or getattr(task, "session_id", None) or ""
        # i18n keys: gateway.kanban.wake.<kind> for each _WAKE_KINDS entry.
        _parts = [t(f"gateway.kanban.wake.{k}") for k in _WAKE_KINDS if k in self.wake_kinds]
        _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
        synth = t(
            "gateway.kanban.wake.message",
            task_id=sub["task_id"], status=_status, title=self.title,
            assignee=task.assignee if task else "", board=self.board_slug,
        )
        # Label as an automatic notification and carry the handoff so the
        # creator inspects the board instead of re-decomposing.
        if self.wake_handoff:
            synth += "\n" + t("gateway.kanban.wake.handoff", summary=self.wake_handoff)
        if self.wake_review_detail:
            synth += "\n" + t("gateway.kanban.wake.review_detail", reason=self.wake_review_detail)
        self.synth = synth + "\n\n" + t("gateway.kanban.wake.guidance")

    def _log_woke(self) -> None:
        logger.info("kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                    self.task_id, self.platform_str, self.sub["chat_id"], self.sub_profile or "default", self.wake_kinds)

    def _served_wake_profile(self) -> Optional[str]:
        """The subscription's profile when THIS gateway is a multiplexer serving it, else ``None``.

        ``None`` keeps the historical path: a standalone ``hermes -p <name>`` gateway owns its own
        listener and key, so its api_server wakes keep using the HTTP self-post.
        """
        if not self.sub_profile:
            return None
        if not getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            return None
        return self.sub_profile

    def _owner_scope(self):
        """Runtime scope of the subscription's profile under multiplex, else a no-op context."""
        runner = self.runner
        served_profile = self._served_wake_profile()
        if not served_profile:
            return contextlib.nullcontext()
        from gateway.run import _async_profile_runtime_scope
        from gateway.session import SessionSource
        source = SessionSource(platform=self.plat, chat_id=self.sub["chat_id"], profile=served_profile)
        return _async_profile_runtime_scope(runner._resolve_profile_home_for_source(source))

    async def wake(self) -> None:
        """Wake the creator session (raises on failure): push adapters get a full SessionSource, non-push a raw self-post."""
        from gateway.wake import deliver_wake
        sub = self.sub
        if not self.is_push_adapter:
            # A served profile's raw-session wake runs in THAT profile's scope, in-process: the
            # shared listener's /p/<profile>/ self-post would need the profile's own
            # API_SERVER_KEY, which a route-only profile legitimately does not have, and an
            # unprefixed self-post would resume the session in the DEFAULT profile's store.
            async with self._owner_scope():
                await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key,
                                   profile=self._served_wake_profile(),
                                   notification_category="diagnostic" if self.wake_diagnostic else "result")
            self._log_woke()
            return
        from gateway.session import SessionSource
        # Rebuild the creator's real session scope from the persisted chat_type:
        # build_session_key() keys DMs differently from group/thread, so a
        # hardcoded "group" mis-routed DM/thread creators into a fresh session.
        # Legacy rows may carry chat_type in delivery_metadata; last resort is
        # "group". A mismatch only degrades to a fresh session.
        # Legacy rows written before the column existed may still carry chat_type in delivery_metadata
        # (#60600 rows) — fall back to that, then to "group" (the historical default that suits the
        # dashboard/group flows). handle_message() get_or_create_session's the target, so a mismatch only
        # ever degrades to a fresh session, never an exception.
        _chat_type = str(sub.get("chat_type") or "").strip()
        if not _chat_type:
            _delivery_meta = sub.get("delivery_metadata")
            if isinstance(_delivery_meta, dict):
                _chat_type = str(_delivery_meta.get("chat_type") or "").strip()
        _chat_id = sub["chat_id"]
        if self.platform_str == "discord" and sub.get("thread_id"):
            # Discord inbound sources use the thread for BOTH ids. Older
            # subscriptions stored the parent channel, creating a parallel
            # conversation on wake. Keep the persisted key for cursor updates.
            _chat_id = sub["thread_id"]
            _chat_type = "thread"
        _source = SessionSource(
            platform=self.plat, chat_id=_chat_id, chat_type=_chat_type or "group",
            thread_id=sub.get("thread_id") or None, user_id=sub.get("user_id"), user_id_alt=sub.get("user_id_alt"),
            profile=self.sub_profile or None, scope_id=_wake_scope_id(self.adapter, sub),
            parent_chat_id=_delivery_meta.get("parent_chat_id"),
        )
        _source._transport_adapter_ref = weakref.ref(self.adapter)
        from gateway.run import _async_profile_runtime_scope
        if self.sub_profile and getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            from hermes_cli.profiles import profile_exists
            if not profile_exists(self.sub_profile):
                raise RuntimeError(f"Kanban wake profile {self.sub_profile!r} no longer exists")
        async with _async_profile_runtime_scope(self.runner._resolve_profile_home_for_source(_source)):
            await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key, source=_source,
                               notification_category="diagnostic" if self.wake_diagnostic else "result")
        self._log_woke()

    async def ledger(self, operation: str, ev: Any, **kwargs):
        bridges = {"acknowledge_notify_delivery": "_kanban_ack_delivery",
                   "begin_notify_delivery": "_kanban_begin_delivery",
                   "retry_notify_delivery": "_kanban_retry_delivery"}
        if operation in bridges:
            arguments = {
                "acknowledge_notify_delivery": (kwargs.get("claim_token"), kwargs.get("message_id"), self.board_slug),
                "begin_notify_delivery": (self.board_slug,),
                "retry_notify_delivery": (kwargs.get("claim_token"), kwargs.get("error"), self.board_slug),
            }
            return await _to_thread_process_service(
                getattr(self.runner, bridges[operation]), self.sub, ev.id, *arguments[operation])
        def call():
            with _kbc().connect_closing(board=self.board_slug) as conn:
                return getattr(_kbn(), operation)(conn, task_id=self.task_id,
                    platform=self.sub["platform"], chat_id=self.sub["chat_id"],
                    thread_id=self.sub.get("thread_id") or "", event_id=ev.id, **kwargs)
        return await _to_thread_process_service(call)

    async def reconcile_row(self, row: dict) -> bool:
        ev = row["event"]
        claim = await self.ledger("claim_notify_reconciliation", ev)
        if not claim:
            return False
        reconcile = getattr(self.adapter, "reconcile_delivery", None)
        if not callable(reconcile) or row.get("delivery_identity") != "content_marker_v1":
            logger.error("kanban notifier: operator decision required for %s ambiguous delivery", self.task_id)
            await self.ledger("park_notify_delivery", ev, claim_token=claim,
                              error="ambiguous delivery has no durable reconciliation identity")
            return False
        metadata = dict(self.sub.get("delivery_metadata") or {})
        metadata.update(thread_id=self.sub.get("thread_id") or None,
                        delivery_identity="content_marker_v1", delivery_created_at=row["created_at"])
        try:
            result = await reconcile(self.sub["chat_id"], row["delivery_token"], metadata=metadata)
        except Exception as exc:
            await self.ledger("release_notify_reconciliation", ev, claim_token=claim, error=str(exc))
            return False
        if getattr(result, "success", False) is True:
            try:
                await self.ledger("acknowledge_notify_delivery", ev, claim_token=claim,
                                  message_id=getattr(result, "message_id", None))
            except Exception as exc:
                await self.ledger("release_notify_reconciliation", ev, claim_token=claim, error=str(exc))
            return False
        if not (getattr(result, "raw_response", None) or {}).get("delivery_unknown"):
            await self.ledger("retry_notify_delivery", ev, claim_token=claim,
                              error="confirmed absent in durable transport history")
            return False
        logger.error("kanban notifier: operator decision required for %s ambiguous delivery", self.task_id)
        await self.ledger("park_notify_delivery", ev, claim_token=claim,
                          error=getattr(result, "error", None) or "delivery acceptance cannot be established")
        return False

    async def _send_event(self, ev: Any, msg: str) -> None:
        """Send one text ping; raises on adapter exception or SendResult(success=False)."""
        from gateway.warning_notifications import present_notification
        sub, adapter = self.sub, self.adapter
        delivery_metadata = sub.get("delivery_metadata")
        metadata: dict[str, Any] = dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
        if sub.get("thread_id") and not metadata.get("thread_id"):
            metadata["thread_id"] = sub["thread_id"]
        claim, row = self.active_claim, self.active_row
        # ONE-CARD: no delivery marker in the message body. Five short lines means
        # five short lines; a visible [kanban-delivery:<token>] second line is
        # exactly the formatting the spec forbids. The token still rides in
        # metadata and the pending/acknowledged ledger still prevents replay.
        # Trade-off: a send that fails AFTER delivering (ambiguous) can no longer
        # be reconciled from transport history and is parked instead.
        metadata.update(delivery_token=row["delivery_token"], delivery_identity=None)
        try:
            _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)
        except Exception as exc:
            await self.ledger("mark_notify_delivery_ambiguous", ev, claim_token=claim, error=str(exc))
            raise
        # SendResult(success=False) without an exception is a FAILED delivery
        # (else the event is lost); None / non-SendResult keeps the
        # "no exception == delivered" contract.
        if getattr(_send_res, "success", True) is False:
            raw = getattr(_send_res, "raw_response", None) or {}
            operation = "mark_notify_delivery_ambiguous" if raw.get("delivery_ambiguous") else "retry_notify_delivery"
            await self.ledger(operation, ev, claim_token=claim, error=str(getattr(_send_res, "error", "send failed")))
            raise RuntimeError("adapter send() reported failure")
        try:
            await self.ledger("acknowledge_notify_delivery", ev, claim_token=claim,
                              message_id=getattr(_send_res, "message_id", None))
        except Exception as exc:
            await self.ledger("mark_notify_delivery_ambiguous", ev, claim_token=claim, error=str(exc))
            raise
        logger.debug("kanban notifier: delivered %s event for %s to %s/%s on board %s",
                     ev.kind, self.task_id, self.platform_str, sub["chat_id"], self.board_slug)
        # Upload artifact paths from the handoff payload / legacy result as
        # native files. Both handoff kinds stage files for exactly this: a
        # review-bound card's files exist precisely so the human sees them at
        # handoff time. Retry exposure matches ``completed`` (the sub cursor is
        # rewound only when a send failed).
        if ev.kind in ("completed", "review_requested"):
            try:
                await self.runner._deliver_kanban_artifacts(
                    adapter=adapter, chat_id=sub["chat_id"], metadata=metadata,
                    event_payload=getattr(ev, "payload", None), task=self.task,
                )
            except Exception as art_exc:
                logger.debug("kanban notifier: artifact delivery for %s failed: %s", self.task_id, art_exc)
        return True

    async def _send_pings(self) -> bool:
        """Each event is separately fenced; earlier successes never replay."""
        self.deferred = []
        self.wake_events = []
        successors = {(ev.task_id, ev.run_id) for ev in self.d["events"]
                      if ev.kind == "delivery_phase_completed" and ev.run_id is not None}
        # ONE-CARD: one stop, one line. A `blocked` that is actually spoken (i.e.
        # not a suppressed operator_held auto-block) already carries the reason, so
        # a `gave_up` on the same run is the same stop said twice.
        spoken_blocks = {(ev.task_id, ev.run_id) for ev in self.d["events"]
                         if ev.kind == "blocked"
                         and (ev.payload or {}).get("operator_held") is not True}
        for row in self.d.get("delivery_rows", []):
            ev = row["event"]
            if row["state"] != "pending":
                if row["state"] == "parked" or not await self.reconcile_row(row):
                    continue
            claim = await self.ledger("begin_notify_delivery", ev)
            if not claim:
                continue
            self.active_claim, self.active_row = claim, row
            precursor = ev.kind == "review_handoff_required" and (ev.task_id, ev.run_id) in successors
            precursor = precursor or (ev.kind == "blocked" and (ev.payload or {}).get("operator_held") is True)
            precursor = precursor or (ev.kind == "gave_up" and (ev.task_id, ev.run_id) in spoken_blocks)
            msg = None if precursor else self.format_event(ev)
            if msg is None:
                try:
                    await self.ledger("acknowledge_notify_delivery", ev, claim_token=claim)
                except Exception as exc:
                    await self.ledger("retry_notify_delivery", ev, claim_token=claim, error=str(exc))
                    return False
                continue
            if not self.send_passive or (not self.is_push_adapter and self.wake_agent):
                self.deferred.append((ev, claim))
                self.wake_events.append(ev)
                continue
            if ev.id <= self.sub.get("last_ping_event_id", 0):
                continue
            try:
                await self._send_event(ev, msg)
                self.wake_events.append(ev)
                self.clear_failures()
            except Exception as exc:
                await self.delivery_failed(
                    "kanban notifier: send failed for %s on %s (attempt %d/%d): %s",
                    (self.task_id, self.platform_str), "", exc, False)
                return False
        return True
    async def deliver(self) -> None:
        try:
            self.plat = self.platform_cls(self.platform_str)
        except ValueError:
            await self.advance()
            return
        # Recheck the exact route after claiming: config/adapters can change between ticks. The
        # recheck reads the served profile's session store for a stateless destination, so it runs
        # off the event loop (the claim path already collects in a worker thread).
        adapter = await asyncio.to_thread(
            _adapter_for_subscription, self.runner, self.plat, self.sub, self.sub_profile or None)
        if adapter is None:
            logger.debug("kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                         self.platform_str, self.task_id)
            await self.rewind()
            return
        self.adapter = adapter
        from gateway.wake import adapter_supports_push
        self.is_push_adapter = adapter_supports_push(adapter)

        # Pings, artifact uploads (media policy) and the wake text (display.language) all read the
        # SUBSCRIBER profile's config; the notifier thread itself runs in the launch profile's scope.
        async with self._owner_scope():
            if not await self._send_pings():
                return
            # All text pings delivered (or skipped for non-push / wake-only).
            original_events = self.d["events"]
            from gateway.warning_notifications import warning_notifications_enabled
            split = not warning_notifications_enabled(self.platform_str)
            wake_groups = ([original_events] if not split else [
                [ev for ev in original_events if diagnostic_event(ev)],
                [ev for ev in original_events if not diagnostic_event(ev)],
            ])
            wake_payloads = []
            for events in wake_groups:
                if not events:
                    continue
                self.d = {**self.d, "events": events}
                self.wake_handoff = self.wake_review_detail = ""
                for ev in events:
                    self.format_event(ev)
                self.build_wake_text()
                if self.wake_kinds:
                    wake_payloads.append((self.synth, self.wake_diagnostic, self.wake_kinds))
            self.d = {**self.d, "events": original_events}
        wake_kinds, is_push = self.wake_kinds, self.is_push_adapter
        from gateway.wake import WakeNotAccepted

        # A requested wake is required even when its passive ping already landed.
        if wake_payloads:
            try:
                for self.synth, self.wake_diagnostic, self.wake_kinds in wake_payloads:
                    await self.wake()
                self.clear_failures()
                for ev, claim in self.deferred:
                    await self.ledger("acknowledge_notify_delivery", ev, claim_token=claim)
            except Exception as _wk_err:
                for ev, claim in self.deferred:
                    await self.ledger("mark_notify_delivery_ambiguous", ev, claim_token=claim, error=str(_wk_err))
                await self._wake_failed(
                    "kanban notifier: wake-only delivery failed for %s (attempt %d/%d): %s" if is_push
                    else "kanban notifier: wake self-post failed for %s (attempt %d/%d): %s",
                    _wk_err,
                )
                return

        # Delivery complete: advance the cursor (the dedup mechanism).
        await self.advance()
        if not is_push:
            self.clear_failures()
        # Unsubscribe only on archive; ``done`` is reversible.
        if self.task and self.task.status == "archived":
            await self.unsub()
