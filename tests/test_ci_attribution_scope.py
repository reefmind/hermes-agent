"""Exercise the workflow's real git range against divergent PR bases."""
import os
from pathlib import Path
import subprocess

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/contributor-check.yml"


def test_attribution_uses_event_base_not_default_branch(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "historical@example.invalid")
    git("commit", "--allow-empty", "-m", "default base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-b", "review-base")
    git("commit", "--allow-empty", "-m", "installed history")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "repair")
    git("config", "user.email", "repair@example.invalid")
    git("commit", "--allow-empty", "-m", "repair")
    head = git("rev-parse", "HEAD")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["check-attribution"]["steps"]
                if s.get("id") == "check-emails")
    # Execute the actual range selection, before contributor-map processing.
    script = step["run"].split('if [ -z "$NEW_EMAILS" ]')[0]
    result = subprocess.check_output(
        ["bash", "-eu", "-c", script + '\nprintf "%s" "$NEW_EMAILS"'],
        cwd=tmp_path,
        env={**os.environ, "ATTRIBUTION_BASE": base, "ATTRIBUTION_HEAD": head},
        text=True,
    )
    assert result == "repair@example.invalid"
    # Missing event history must fail, not silently check an unrelated range.
    failed = subprocess.run(
        ["bash", "-eu", "-c", script], cwd=tmp_path,
        env={**os.environ, "ATTRIBUTION_BASE": "missing-ref", "ATTRIBUTION_HEAD": head},
        capture_output=True,
    )
    assert failed.returncode != 0
