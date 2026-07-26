"""`git diff` cannot see committed work, and that is how we published a false result.

Origin (2026-07-26). Four loop configurations were run on one three-line import fix, each in its own
git worktree. We inspected each with `git status` / `git diff` and reported that the loop arms
produced no product code. Three of them had in fact committed a working fix and a passing test.

`git diff` compares the WORKING TREE to HEAD. An agent that commits its work therefore leaves an
empty `git diff` — success and failure look identical. `harness/arm_report.py` exists so the question
"did that arm produce anything?" is answered by one tool with one definition, and these tests pin the
distinction the manual check collapsed.

The load-bearing test here is `test_a_committed_only_arm_is_not_reported_empty`: that is the exact
regression, and it fails if anyone reimplements this on two-dot `git diff`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from harness.arm_report import inspect, main, render  # noqa: E402


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """A repo with a `main` holding one file, ready to branch from."""
    r = tmp_path / "base"
    r.mkdir()
    _run(r, "git", "init", "-q", "-b", "main")
    _run(r, "git", "config", "user.email", "t@example.com")
    _run(r, "git", "config", "user.name", "T")
    (r / "mod.py").write_text("def f():\n    return 1\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-qm", "base")
    return r


def _arm(base_repo: Path, name: str) -> Path:
    """A worktree branched off main, the shape every measurement arm had."""
    wt = base_repo.parent / name
    _run(base_repo, "git", "worktree", "add", "-q", "-b", name, str(wt), "main")
    return wt


# --- the regression itself -----------------------------------------------------------------------

def test_a_committed_only_arm_is_not_reported_empty(base_repo: Path):
    """The exact 2026-07-26 error: work committed, working tree clean, reported as nothing."""
    wt = _arm(base_repo, "committed-arm")
    (wt / "mod.py").write_text("import sys\n\n\ndef f():\n    return 1\n")
    (wt / "test_new.py").write_text("def test_f():\n    assert True\n")
    _run(wt, "git", "add", "-A")
    _run(wt, "git", "commit", "-qm", "fix: the import")

    # This is what we ran, and why it lied. Kept in the test so the failure mode is legible.
    plain_diff = subprocess.run(["git", "diff"], cwd=wt, capture_output=True, text=True).stdout
    assert plain_diff == "", "premise of this test broken: a clean tree should have an empty git diff"

    arm = inspect(wt, base="main")
    assert arm.state == "committed"
    assert arm.produced is True, "a committed fix was reported as no work — the published error"
    assert sorted(arm.committed_files) == ["mod.py", "test_new.py"]
    assert len(arm.commits) == 1 and "fix: the import" in arm.commits[0]


def test_uncommitted_work_is_reported_as_at_risk_not_as_done(base_repo: Path):
    """The other arm's real state: fixed in the working tree, never committed, would be lost."""
    wt = _arm(base_repo, "dirty-arm")
    (wt / "mod.py").write_text("import sys\n\n\ndef f():\n    return 1\n")
    (wt / "test_new.py").write_text("def test_f():\n    assert True\n")  # untracked on purpose

    arm = inspect(wt, base="main")
    assert arm.state == "uncommitted"
    assert arm.produced is True
    assert arm.committed_files == []
    assert sorted(arm.dirty_files) == ["mod.py", "test_new.py"], (
        "an untracked new test file is work; omitting it repeats the same mistake"
    )
    assert "would be lost" in render(arm), "the at-risk state must be visible in the human output"


def test_untracked_only_still_counts_as_work(base_repo: Path):
    """An arm whose entire output is a new file it never `git add`ed."""
    wt = _arm(base_repo, "untracked-arm")
    (wt / "brand_new.py").write_text("x = 1\n")
    arm = inspect(wt, base="main")
    assert arm.produced is True and arm.state == "uncommitted"
    assert arm.dirty_files == ["brand_new.py"]


def test_both_states_are_distinguished(base_repo: Path):
    wt = _arm(base_repo, "both-arm")
    (wt / "mod.py").write_text("# committed change\n")
    _run(wt, "git", "add", "-A")
    _run(wt, "git", "commit", "-qm", "part one")
    (wt / "later.py").write_text("# still dirty\n")

    arm = inspect(wt, base="main")
    assert arm.state == "both"
    assert arm.committed_files == ["mod.py"] and arm.dirty_files == ["later.py"]


def test_a_genuinely_empty_arm_is_reported_empty(base_repo: Path):
    """The tool must not be so eager to find work that it can never report none."""
    wt = _arm(base_repo, "empty-arm")
    arm = inspect(wt, base="main")
    assert arm.state == "empty" and arm.produced is False
    assert "EMPTY" in render(arm)


# --- the diff semantics that caused it -----------------------------------------------------------

def test_movement_on_main_is_not_credited_to_the_arm(base_repo: Path):
    """Three-dot semantics: commits main gained after the branch cut are not the arm's output."""
    wt = _arm(base_repo, "stale-arm")
    (base_repo / "unrelated.py").write_text("# landed on main later\n")
    _run(base_repo, "git", "add", "-A")
    _run(base_repo, "git", "commit", "-qm", "unrelated main work")

    arm = inspect(wt, base="main")
    assert arm.state == "empty", (
        "two-dot `git diff main..HEAD` would show main's later commit as a deletion by this arm"
    )
    assert "unrelated.py" not in arm.committed_files


def test_a_missing_base_ref_is_reported_not_silently_treated_as_empty(base_repo: Path):
    """An unresolvable base must be visible, because 'no base' and 'no work' are different facts."""
    wt = _arm(base_repo, "nobase-arm")
    (wt / "mod.py").write_text("# work\n")
    _run(wt, "git", "add", "-A")
    _run(wt, "git", "commit", "-qm", "work")

    arm = inspect(wt, base="does-not-exist")
    assert arm.merge_base == ""
    assert "NOT assessed" in render(arm), (
        "an unresolvable base silently reporting 'empty' is the original bug with extra steps"
    )


def test_a_removed_worktree_does_not_abort_the_sweep(tmp_path: Path):
    arm = inspect(tmp_path / "never-existed", base="main")
    assert arm.exists is False
    assert "not a git worktree" in render(arm)


# --- CLI contract ---------------------------------------------------------------------------------

def test_exit_code_is_zero_when_every_arm_produced_something(base_repo: Path, capsys):
    a = _arm(base_repo, "cli-a")
    (a / "mod.py").write_text("# a\n")
    _run(a, "git", "add", "-A")
    _run(a, "git", "commit", "-qm", "a")
    b = _arm(base_repo, "cli-b")
    (b / "dirty.py").write_text("# b\n")

    assert main([str(a), str(b), "--base", "main"]) == 0
    capsys.readouterr()


def test_exit_code_is_nonzero_when_any_arm_was_empty(base_repo: Path, capsys):
    a = _arm(base_repo, "cli-c")
    (a / "mod.py").write_text("# a\n")
    _run(a, "git", "add", "-A")
    _run(a, "git", "commit", "-qm", "a")
    empty = _arm(base_repo, "cli-d")

    assert main([str(a), str(empty), "--base", "main"]) == 1, (
        "a sweep over arms must be assertable by exit code, or it will not be asserted"
    )
    assert "cli-d" in capsys.readouterr().out


def test_json_output_is_machine_readable_and_carries_the_state(base_repo: Path, capsys):
    wt = _arm(base_repo, "json-arm")
    (wt / "mod.py").write_text("# x\n")
    _run(wt, "git", "add", "-A")
    _run(wt, "git", "commit", "-qm", "x")

    main([str(wt), "--base", "main", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["state"] == "committed" and rows[0]["produced"] is True
    assert rows[0]["committed_files"] == ["mod.py"]


def test_module_is_runnable_as_a_script(base_repo: Path):
    """The docstring tells a reader to run `python3 -m harness.arm_report`; prove that works."""
    wt = _arm(base_repo, "script-arm")
    (wt / "mod.py").write_text("# s\n")
    _run(wt, "git", "add", "-A")
    _run(wt, "git", "commit", "-qm", "s")

    r = subprocess.run([sys.executable, "-m", "harness.arm_report", str(wt), "--base", "main"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "committed" in r.stdout and "mod.py" in r.stdout
