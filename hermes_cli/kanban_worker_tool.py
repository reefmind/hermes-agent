"""Run one ``kanban_*`` tool handler from the shell (direct Claude CLI workers).

Usage: ``<python> kanban_worker_tool.py kanban_<name> '<json args>'`` or
``... kanban_<name> -`` with the JSON on stdin. Prints the handler's result.

A ``claude -p`` worker has no Hermes tool loop, and ``hermes kanban <verb>``
skips the block-contract / one-card gates that only the tool handlers enforce.
Dispatching through the registry gives both lanes identical enforcement.
Ownership comes from the worker env the dispatcher pinned
(``HERMES_KANBAN_TASK`` / ``_RUN_ID`` / ``_CLAIM_LOCK`` / ``_DB``).
Local addition 2026-09-22, see ``kanban_db_claude_cli``.
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].startswith("kanban_"):
        print(json.dumps({"error": "usage: kanban_worker_tool.py kanban_<name> ['<json>' | -]"}))
        return 2
    name = argv[1]
    raw = argv[2] if len(argv) > 2 else "-"
    if raw == "-":
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"arguments are not valid JSON: {exc}"}))
        return 2
    if not isinstance(args, dict):
        print(json.dumps({"error": "arguments must be a JSON object"}))
        return 2

    import tools.kanban_tools  # noqa: F401 -- registers the kanban_* handlers
    from tools.registry import registry

    if registry.get_entry(name) is None:
        known = sorted(registry.get_tool_names_for_toolset("kanban"))
        print(json.dumps({"error": f"unknown tool {name!r}", "known": known}))
        return 2
    result = registry.dispatch(name, args)
    print(result if isinstance(result, str) else json.dumps(result))
    return 0


if __name__ == "__main__":
    # Run by path, sys.path[0] is hermes_cli/ itself, whose module names would
    # shadow top-level ones. Anchor on the repo root instead.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(_here))
    sys.exit(main(sys.argv))
