#!/bin/bash
# Kanban maintenance: archive stale done-tasks, then GC their workspaces.
#
# Why this exists: `hermes kanban gc` only reclaims workspaces for tasks in
# status 'archived'. Tasks that finish sit in 'done' forever, so their
# per-task repo clones (~1GB each) are never collected. On 2026-08-15 that
# had grown to 20GB across 120 workspaces and filled the disk to 100%.
#
# Runs daily via ~/Library/LaunchAgents/ai.hermes.kanban-maintenance.plist

set -uo pipefail

HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes"
BOARDS_ROOT="$HOME/.hermes/kanban/boards"
DONE_AGE_DAYS="${DONE_AGE_DAYS:-7}"
LOG="$HOME/.hermes/logs/kanban-maintenance.log"

exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') kanban maintenance ==="

cutoff=$(( $(date +%s) - DONE_AGE_DAYS * 86400 ))

for board_dir in "$BOARDS_ROOT"/*/; do
  slug=$(basename "$board_dir")
  db="$board_dir/kanban.db"
  [ -f "$db" ] || continue
  [ "$slug" = "_archived" ] && continue

  # Archive done tasks older than the cutoff, in batches.
  ids=$(sqlite3 "$db" \
    "SELECT id FROM tasks WHERE status='done' AND COALESCE(completed_at,0) < $cutoff LIMIT 500;" 2>/dev/null)

  if [ -n "$ids" ]; then
    n=$(echo "$ids" | wc -l | tr -d ' ')
    echo "[$slug] archiving $n done task(s) older than ${DONE_AGE_DAYS}d"
    # shellcheck disable=SC2086
    echo "$ids" | HERMES_KANBAN_BOARD="$slug" xargs "$HERMES_BIN" kanban archive >/dev/null 2>&1 \
      || echo "[$slug] archive returned non-zero"
  else
    echo "[$slug] nothing to archive"
  fi

  echo "[$slug] gc:"
  HERMES_KANBAN_BOARD="$slug" "$HERMES_BIN" kanban gc --event-retention-days 30 --log-retention-days 14 2>&1 | sed "s/^/[$slug] /"
done

# Every operation pins its board in the child environment. The interactive
# board selection is never changed by maintenance.

# ---------------------------------------------------------------------------
# Reap leaked headless browsers.
#
# Review / browser-proof tasks launch Chrome with
#   --user-data-dir=<board>/workspaces/<task_id>/chrome-review-profile
# and nothing tears it down when the task finishes. The browser is reparented
# to launchd (ppid=1) and survives indefinitely. Found 2026-08-16: 7 Chrome
# processes / 0.47 GB still running 11h21m after task t_0d5cddbc reached
# 'done', on a box already 4 GB into swap.
#
# One workspace leaking is an annoyance; this recurring per-task means
# unbounded growth, which is why it belongs in the daily sweep rather than a
# one-off cleanup. Only kills browsers whose OWNING TASK has reached a
# terminal status — a browser for a running task is doing its job.
# ---------------------------------------------------------------------------
echo "browser-reap:"
reaped=0
# NB: `ps -axo pid=` RIGHT-ALIGNS the pid, so every line begins with spaces and
# a naive ${line%% *} yields an EMPTY pid — the reaper then silently kills
# nothing while still reporting success. awk splits it cleanly instead.
while IFS=$'\t' read -r bpid rest; do
  [ -z "$bpid" ] && continue
  btask=$(printf '%s\n' "$rest" | grep -o 't_[0-9a-f]\{8\}' | head -1)
  [ -z "$btask" ] && continue
  bboard=$(printf '%s\n' "$rest" | sed -n 's|.*/boards/\([^/]*\)/workspaces/.*|\1|p' | head -1)
  bdb="$BOARDS_ROOT/$bboard/kanban.db"
  [ -f "$bdb" ] || continue
  bstatus=$(sqlite3 "$bdb" "SELECT status FROM tasks WHERE id='$btask';" 2>/dev/null)
  case "$bstatus" in
    done|archived|blocked|human|review)
      kill -TERM "$bpid" 2>/dev/null && {
        echo "  reaped pid=$bpid task=$btask status=$bstatus"
        reaped=$((reaped + 1))
      }
      ;;
    *)
      [ -n "$bstatus" ] && echo "  keeping pid=$bpid task=$btask status=$bstatus (still active)"
      ;;
  esac
done <<EOF
$(ps -axo pid=,command= | awk '/--user-data-dir=.*workspaces\/t_/ && !/awk/ {p=$1; $1=""; sub(/^[ \t]+/,""); printf "%s\t%s\n", p, $0}')
EOF
echo "  reaped $reaped leaked browser process(es)"

echo "workspaces now: $(du -sh "$BOARDS_ROOT" 2>/dev/null | cut -f1)"
echo "disk free:      $(df -h "$HOME" | tail -1 | awk '{print $4}')"
