"""Direct Claude Code CLI worker executor (``kanban.worker_executor: claude_cli``).

Runs ``claude -p`` instead of ``hermes chat`` so a worker bills against the
operator's Claude subscription (static ``CLAUDE_CODE_OAUTH_TOKEN``) rather than
the native ``provider=anthropic`` adapter's extra-usage credits.

Local port (2026-09-22) of scripts/patches/kanban-claude-{executor,cli-effort}.patch,
which stopped applying once ``kanban_db`` was split into siblings. One deliberate
change from those patches: the worker drives the board through the SAME
``kanban_*`` tool handlers a native worker uses (via ``kanban_worker_tool``), not
through ``hermes kanban <verb>``. The block-contract and one-card gates live only
in ``tools/kanban_tools.py``; the CLI verbs bypass them. The prompt embeds
``KANBAN_GUIDANCE`` verbatim so the two lanes cannot drift on doctrine.
MARKER: KANBAN-CLAUDE-CLI-SIBLING
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import sys
import time
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

WORKER_EXECUTOR_HERMES = "hermes"
WORKER_EXECUTOR_CLAUDE_CLI = "claude_cli"

_WORKER_EXECUTOR_ALIASES = {
    "": WORKER_EXECUTOR_HERMES,
    "hermes": WORKER_EXECUTOR_HERMES,
    "native": WORKER_EXECUTOR_HERMES,
    "claude": WORKER_EXECUTOR_CLAUDE_CLI,
    "claude_cli": WORKER_EXECUTOR_CLAUDE_CLI,
    "claude-cli": WORKER_EXECUTOR_CLAUDE_CLI,
    "claude_code": WORKER_EXECUTOR_CLAUDE_CLI,
    "claude-code": WORKER_EXECUTOR_CLAUDE_CLI,
}

# CLAUDE_CONFIG_DIR would route the child to a Hermes-managed store whose 8h
# rotating Keychain token three concurrent workers race; the static token needs
# no config dir. ANTHROPIC_* / CLAUDE_CODE_USE_* would silently move the run onto
# metered API, a proxy, or Bedrock/Vertex billing.
CLAUDE_CLI_STRIPPED_ENV_VARS = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

_CLAUDE_CLI_PERMISSION_FLAGS = (
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--allowedTools",
    "--allowed-tools",
    "--settings",
)

_CLAUDE_CLI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_CLI_EFFORT_ALIASES = {"minimal": "low", "ultra": "max"}

CLAUDE_CLI_DEFAULT_SPAWN_STAGGER_SECONDS = 2.0
_CLAUDE_CLI_SPAWN_LOCK_TIMEOUT_SECONDS = 30.0
_CLAUDE_CLI_SPAWN_LOCK_POLL_SECONDS = 0.05


def _log():
    from hermes_cli import kanban_db as _kb
    return _kb._log


def load_kanban_config() -> dict:
    """Private copy of the dispatcher's ``kanban:`` block; never raises."""
    try:
        from hermes_cli.config import load_config_readonly

        block = load_config_readonly().get("kanban")
        return dict(block) if isinstance(block, dict) else {}
    except Exception as exc:
        _log().debug("kanban worker: config.yaml unreadable (%s)", exc)
        return {}


def resolve_worker_executor(kanban_cfg: Optional[dict] = None) -> str:
    """Configured lane; anything but an explicit claude spelling is native.

    A typo must never silently change how workers bill, so an unknown value
    logs loudly and falls back to the native lane.
    """
    if kanban_cfg is None:
        kanban_cfg = load_kanban_config()
    raw = (kanban_cfg or {}).get("worker_executor")
    if raw is None:
        return WORKER_EXECUTOR_HERMES
    resolved = _WORKER_EXECUTOR_ALIASES.get(str(raw).strip().lower())
    if resolved is None:
        _log().warning(
            "kanban worker: unknown kanban.worker_executor=%r — using %r.",
            raw, WORKER_EXECUTOR_HERMES,
        )
        return WORKER_EXECUTOR_HERMES
    return resolved


def _profile_kanban_value(profile: str, key: str) -> Optional[str]:
    """``kanban.<key>`` from the worker PROFILE's config.yaml.

    The dispatcher's ``kanban_cfg`` is the root block; per-role model/effort
    (planner max, reviewer high, implementers medium) live in each profile.
    """
    name = (profile or "").strip()
    if not name:
        return None
    try:
        import yaml
        from hermes_cli.profiles import resolve_profile_env

        with open(os.path.join(resolve_profile_env(name), "config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        raw = (cfg.get("kanban") or {}).get(key)
        return str(raw).strip() if raw else None
    except Exception as exc:
        _log().debug("kanban worker: profile %r %s unreadable (%s)", name, key, exc)
        return None


def _resolve_claude_bin(kanban_cfg: dict) -> str:
    """Absolute path of the ``claude`` binary. Missing is a hard error: the
    operator opted into this lane, so never downgrade to the native provider."""
    configured = str(kanban_cfg.get("claude_cli_bin") or "").strip()
    candidate = os.path.expanduser(configured or "claude")
    if os.sep in candidate:
        if not os.path.isfile(candidate):
            raise RuntimeError(
                f"kanban.claude_cli_bin={configured!r} does not exist. Fix the path "
                f"or set kanban.worker_executor back to {WORKER_EXECUTOR_HERMES!r}."
            )
        return os.path.abspath(candidate)
    found = shutil.which(candidate)
    if not found:
        raise RuntimeError(
            f"kanban.worker_executor={WORKER_EXECUTOR_CLAUDE_CLI!r} but {candidate!r} is not "
            "on the dispatcher's PATH. Set kanban.claude_cli_bin to its absolute path or "
            f"set kanban.worker_executor back to {WORKER_EXECUTOR_HERMES!r}."
        )
    return os.path.abspath(found)


def apply_claude_cli_env(env: dict) -> list[str]:
    """Strip credential-routing vars and attach a live Claude Code session.

    Prefer the isolated worker store (``~/.hermes/claude-worker-config``) when
    it has ``.credentials.json``. Injecting ``CLAUDE_CODE_OAUTH_TOKEN`` on this
    Claude Max org is treated as third-party extra-usage and returns 403
    "organization has disabled Claude subscription access for Claude Code".
    The worker-store oauth session bills against the Max plan.

    Fallback: inject the static token from root ``.env``. Hermes does not load
    ``.env`` into ``os.environ`` and launchd does not read it either. Returns
    the names stripped from the incoming env.
    """
    stripped = [name for name in CLAUDE_CLI_STRIPPED_ENV_VARS if env.pop(name, None) is not None]
    worker_cfg = os.path.expanduser("~/.hermes/claude-worker-config")
    if os.path.isfile(os.path.join(worker_cfg, ".credentials.json")):
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env["CLAUDE_CONFIG_DIR"] = worker_cfg
        return stripped
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return stripped
    token = None
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        token = get_env_value_prefer_dotenv("CLAUDE_CODE_OAUTH_TOKEN")
    except Exception:
        token = None
    if not token:
        from hermes_cli.profiles import _get_default_hermes_home

        try:
            with open(os.path.join(str(_get_default_hermes_home()), ".env"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except OSError:
            token = None
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return stripped


def _model_arg(task: Task, kanban_cfg: dict) -> Optional[str]:
    """Card override (only if it names a Claude model) > profile > root."""
    override = (task.model_override or "").strip()
    provider = (task.provider_override or "").strip().lower()
    if override:
        if provider in ("", "anthropic", "claude") and override.lower().startswith("claude"):
            return override
        _log().warning(
            "kanban worker %s: model_override=%r (provider=%r) is not a Claude model — "
            "ignoring it on the %s executor.",
            task.id, override, task.provider_override, WORKER_EXECUTOR_CLAUDE_CLI,
        )
    return (
        _profile_kanban_value(task.assignee or "", "claude_cli_model")
        or str(kanban_cfg.get("claude_cli_model") or "").strip()
        or None
    )


def _effort_arg(task: Task, kanban_cfg: dict) -> Optional[str]:
    """Card ``reasoning_effort`` (``none`` = CLI default) > profile > root."""
    candidates = (
        ("task", task.reasoning_effort or ""),
        ("profile", _profile_kanban_value(task.assignee or "", "claude_cli_effort") or ""),
        ("root", str(kanban_cfg.get("claude_cli_effort") or "")),
    )
    for source, raw in candidates:
        level = str(raw).strip().lower()
        if not level:
            continue
        if level == "none":
            return None
        level = _CLAUDE_CLI_EFFORT_ALIASES.get(level, level)
        if level in _CLAUDE_CLI_EFFORT_LEVELS:
            return level
        _log().warning(
            "kanban worker %s: %s effort %r is not a Claude CLI level %r — ignoring it.",
            task.id, source, raw, _CLAUDE_CLI_EFFORT_LEVELS,
        )
    return None


def _extra_args(kanban_cfg: dict) -> list[str]:
    raw = kanban_cfg.get("claude_cli_extra_args")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            return shlex.split(raw)
        except ValueError as exc:
            _log().warning("kanban worker: unparseable claude_cli_extra_args=%r (%s)", raw, exc)
            return []
    if not isinstance(raw, (list, tuple)):
        _log().warning("kanban worker: claude_cli_extra_args must be a list, got %s", type(raw).__name__)
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def _strip_effort_flag(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--effort":
            skip = True
        elif not arg.startswith("--effort="):
            out.append(arg)
    return out


def tool_shim_argv() -> list[str]:
    """``<dispatcher python> <abs path of kanban_worker_tool.py>``."""
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "kanban_worker_tool.py")]


def worker_prompt(task: Task, workspace: str) -> str:
    """Self-contained prompt: a tool-invocation preface + ``KANBAN_GUIDANCE`` verbatim.

    ``claude -p`` has no ``kanban_*`` tools and no Hermes system prompt, so the
    native doctrine is embedded unchanged and the preface says how to reach the
    same tool handlers from the shell.
    """
    from agent.prompt_builder import KANBAN_GUIDANCE

    tool = shlex.join(tool_shim_argv())
    return (
        "You are a Hermes kanban worker running as a direct Claude Code CLI process.\n"
        f"Task id: {task.id}\nWorkspace: {workspace}\n\n"
        "# How to call the kanban_* tools in this lane\n"
        "You have no native kanban_* tools. Every `kanban_<name>(...)` in the protocol below is the SAME "
        "handler, reached by running this shell command with the tool's arguments as one JSON object:\n"
        f"    {tool} kanban_<name> '<json args>'\n"
        "For arguments containing quotes or newlines (block reasons, results), pass the JSON on stdin:\n"
        f"    {tool} kanban_block - <<'JSON'\n"
        '    {"reason": "WHAT I NEED: ...\\nWHY: ...\\nDO THIS:\\n1. ..."}\n'
        "    JSON\n"
        f"Examples: `{tool} kanban_show '{{}}'` · `{tool} kanban_heartbeat '{{\"note\": \"running tests\"}}'` · "
        f"`{tool} kanban_comment '{{\"body\": \"...\"}}'`. Omit task_id; it defaults to your task. "
        "The command prints the tool's JSON result, including refusals — read it and act on it exactly as a "
        "native worker would. Where the protocol below says not to shell out to `hermes kanban <verb>`, that "
        "still holds: this command is the tool surface, `hermes kanban` is not.\n"
        "Exactly one of kanban_complete or kanban_block must succeed before you exit.\n\n"
        "# Ending your turn ends this process\n"
        "`claude -p` exits the moment you end your turn. Background shells, monitors and "
        "'I'll resume when the watch completes' die with it, nothing wakes you, and the card is "
        "marked crashed. Never end your turn to wait. Wait in the foreground: run blocking commands "
        "such as `gh pr checks <n> --watch --fail-fast` or a `sleep 60` poll loop, with the Bash "
        "timeout set up to 600000 ms, repeat as needed, and call kanban_heartbeat between waits. "
        "Do not use run_in_background or a monitor for anything whose result you need. If a wait "
        "cannot finish in this run, call kanban_block with the reason; never exit without "
        "kanban_complete or kanban_block.\n\n"
        f"{KANBAN_GUIDANCE}"
    )


def build_command(task: Task, workspace: str, kanban_cfg: dict) -> list[str]:
    """Full ``claude ... -p <prompt>`` argv. Raises for goal_mode (caller routes
    those to the native lane, which owns the goal judge loop)."""
    if task.goal_mode:
        raise RuntimeError(
            f"kanban task {task.id} has goal_mode, which the {WORKER_EXECUTOR_CLAUDE_CLI!r} "
            "executor does not implement."
        )
    cmd = [_resolve_claude_bin(kanban_cfg)]
    model = _model_arg(task, kanban_cfg)
    if model:
        cmd.extend(["--model", model])
    extra = _extra_args(kanban_cfg)
    effort = _effort_arg(task, kanban_cfg)
    if effort:
        extra = _strip_effort_flag(extra)
        cmd.extend(["--effort", effort])
    if not any(arg.split("=", 1)[0] in _CLAUDE_CLI_PERMISSION_FLAGS for arg in extra):
        _log().warning(
            "kanban worker %s: no permission flag in kanban.claude_cli_extra_args — "
            "`claude -p` will be unable to edit files or run commands.", task.id,
        )
    cmd.extend(extra)
    # `-p <prompt>` last: several CLI flags are variadic and would swallow a
    # trailing positional prompt.
    cmd.extend(["-p", worker_prompt(task, workspace)])
    return cmd


def _stagger_seconds(kanban_cfg: dict) -> float:
    raw = kanban_cfg.get("claude_cli_spawn_stagger_seconds")
    if raw is None:
        return CLAUDE_CLI_DEFAULT_SPAWN_STAGGER_SECONDS
    try:
        return max(0.0, min(float(raw), 60.0))
    except (TypeError, ValueError):
        return CLAUDE_CLI_DEFAULT_SPAWN_STAGGER_SECONDS


@contextlib.contextmanager
def spawn_gate(kanban_cfg: dict):
    """Serialize ``claude`` startups across boards on this Hermes root.

    Simultaneous startups race on the per-user ``~/.claude`` state they
    rewrite at boot. A lock that cannot be taken within the timeout is
    abandoned so a stuck holder degrades to "unstaggered", never to "the board
    stops dispatching".
    """
    import fcntl
    from hermes_cli import kanban_db as _kb

    lock_path = _kb.kanban_home() / "kanban" / "claude-cli-spawn.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        _log().debug("kanban worker: spawn gate unavailable (%s)", exc)
        yield
        return
    acquired = False
    try:
        deadline = time.monotonic() + _CLAUDE_CLI_SPAWN_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_CLAUDE_CLI_SPAWN_LOCK_POLL_SECONDS)
        if acquired:
            stagger = _stagger_seconds(kanban_cfg)
            handle.seek(0)
            try:
                last = float((handle.read() or "0").strip() or 0.0)
            except ValueError:
                last = 0.0
            wait = min(stagger, stagger - (time.time() - last))
            if wait > 0:
                time.sleep(wait)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{time.time():.3f}")
            handle.flush()
        else:
            _log().warning("kanban worker: claude spawn gate busy >%.0fs — spawning unstaggered",
                           _CLAUDE_CLI_SPAWN_LOCK_TIMEOUT_SECONDS)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def log_header(cmd: list[str], stripped: list[str], env: dict, board: str, profile: str) -> bytes:
    """Worker-log header: lane, binary, flag NAMES only, token fingerprint only."""
    flags = ",".join(a.split("=", 1)[0] for a in cmd[1:] if a.startswith("-")) or "-"
    tok = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    return (
        f"[kanban] executor={WORKER_EXECUTOR_CLAUDE_CLI} bin={cmd[0]} board={board} profile={profile} "
        f"flags={flags} stripped_env={','.join(stripped) or '-'} "
        f"oauth_token={'yes:' + tok[:18] if tok else 'NO'}\n"
    ).encode()
