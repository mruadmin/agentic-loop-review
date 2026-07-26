"""A verify command that `cd`s out of the worktree tests the wrong code and reports STUCK.

Origin (2026-07-26). Three loop arms committed a working fix to a branch and all three reported
failure. The mechanism was not the loop's reasoning; it was one line of shell per step.

`lifecycle-run.js` threads the worktree through as `S.repo_path` and tells the verify runner to
"Run EXACTLY this command in ${S.repo_path}". But the planner prompt never mentions `repo_path` at
all -- the only absolute path in its context is the MAIN repo (from `${ROOT}/STATE.md`) -- so every
verify command it wrote began:

    cd /path/to/repo && PYTHONPATH=. pytest ...

A `cd` inside the command overrides the cwd the runner supplies. So verify ran against main, where
the bug is still present and the new test file does not exist. It failed three times, the step was
marked unverifiable, and the loop reported STUCK while the branch held a passing fix.

The prompt is being fixed too, but a prompt instruction is a request. This module is the guard, and
the point of it is that it is not a matter of the planner remembering.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from harness.verify_scope import (  # noqa: E402
    ScopeViolation,
    check,
    enforce,
    rewrite,
)

WT = "/home/michael/Documents/Agent/_wt-arm-a3"
MAIN = "/path/to/repo"


# --- the exact command that caused it -------------------------------------------------------------

THE_REAL_ONE = (
    "cd /path/to/repo && "
    "PYTHONPATH=. python3 -m pytest harness/tests/test_circuit_breaker_cli_import.py -q"
)


def test_the_command_that_actually_shipped_is_rejected():
    """Verbatim from .claude/orchestrator/circuit-breaker-cli-import/plan.md, step S1."""
    v = check(THE_REAL_ONE, WT)
    assert v.ok is False
    assert MAIN in v.reason
    assert "outside" in v.reason.lower() or "escapes" in v.reason.lower()


def test_the_command_that_actually_shipped_is_rewritten_to_the_worktree():
    fixed = rewrite(THE_REAL_ONE, WT)
    assert fixed.startswith(f"cd {WT} &&")
    assert MAIN not in fixed
    # the rest of the command must be untouched -- a rewrite that "fixes" the test selection too
    # would silently change what is being verified
    assert "PYTHONPATH=. python3 -m pytest harness/tests/test_circuit_breaker_cli_import.py -q" in fixed
    assert check(fixed, WT).ok is True


def test_a_rewritten_command_is_stable_under_a_second_pass():
    once = rewrite(THE_REAL_ONE, WT)
    assert rewrite(once, WT) == once, "rewrite must be idempotent or repeated planning drifts"


# --- what must still be allowed -------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q",
    "python3 scripts/core_sentinel.py",
    "pytest -q && python3 -c 'import harness.circuit_breaker'",
    "git diff --stat HEAD~1",
    # a cd INTO the worktree is the correct form, not a violation
    f"cd {WT} && PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q",
    # a cd to a subdirectory of the worktree is fine
    f"cd {WT}/harness && python3 -m pytest tests/test_x.py -q",
    # relative cd stays inside by construction
    "cd harness && python3 -m pytest tests/test_x.py -q",
])
def test_legitimate_commands_pass(cmd):
    v = check(cmd, WT)
    assert v.ok is True, f"false positive on {cmd!r}: {v.reason}"


def test_a_passing_command_is_returned_unchanged():
    cmd = "PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q"
    assert rewrite(cmd, WT) == cmd, "rewriting a clean command risks changing its meaning"


def test_writes_to_tmp_are_not_treated_as_escaping_the_worktree():
    """The real plans tee'd evidence to /tmp; that is not a scope violation."""
    cmd = f"cd {WT} && pytest -q 2>&1 | tee /tmp/cb_red.out"
    assert check(cmd, WT).ok is True


# --- the other ways a command leaves the worktree -------------------------------------------------

def test_an_absolute_test_path_into_the_main_repo_is_caught():
    """No `cd` at all, but pytest is pointed at main -- same wrong tree, same false STUCK."""
    v = check(f"PYTHONPATH=. python3 -m pytest {MAIN}/harness/tests/test_x.py -q", WT)
    assert v.ok is False and MAIN in v.reason


def test_a_pushd_is_caught_as_well_as_a_cd():
    v = check(f"pushd {MAIN} && pytest -q", WT)
    assert v.ok is False


def test_git_dash_C_into_another_tree_is_caught():
    v = check(f"git -C {MAIN} diff --quiet", WT)
    assert v.ok is False and MAIN in v.reason


def test_git_dash_C_into_tmp_is_caught_even_though_tmp_is_write_allowed():
    """/tmp is exempt as a DATA path (`| tee /tmp/out`), never as a working directory."""
    v = check("git -C /tmp/some-clone diff --quiet", WT)
    assert v.ok is False, "running verification in /tmp verifies a tree that is not this branch"
    assert "/tmp/some-clone" in v.reason


def test_cd_into_tmp_is_caught_even_though_tmp_is_write_allowed():
    v = check("cd /tmp/scratch && pytest -q", WT)
    assert v.ok is False and "/tmp/scratch" in v.reason


def test_tee_to_tmp_is_still_allowed_after_that_distinction():
    """The exemption must survive: the real plans tee evidence to /tmp and that is correct."""
    assert check(f"cd {WT} && pytest -q 2>&1 | tee /tmp/cb_red.out", WT).ok is True


def test_a_sibling_worktree_is_caught_not_just_the_main_repo():
    """Arms cross-contaminating each other is the same defect between two branches."""
    v = check("cd /home/michael/Documents/Agent/_wt-arm-b3 && pytest -q", WT)
    assert v.ok is False
    assert "_wt-arm-b3" in v.reason


def test_a_cd_in_a_later_segment_is_caught_not_only_the_first():
    """Two cds: the first correct, the second escaping. Inspecting only the first would pass this."""
    v = check(f"cd {WT} && pytest -q; cd {MAIN} && pytest -q", WT)
    assert v.ok is False, "only checking the leading cd misses a mid-command escape"
    assert MAIN in v.reason


def test_a_later_cd_into_tmp_is_caught_where_no_other_pattern_can_see_it():
    """The one case only the cd/pushd scan can catch, so it pins that scan specifically.

    A bare absolute path under /tmp is deliberately exempt (evidence files). A *later* `cd` into /tmp
    is therefore invisible to every check except the working-directory scan — and it is exactly the
    shape that would silently run verification in a scratch dir.
    """
    v = check(f"cd {WT} && pytest -q; cd /tmp/scratch && pytest -q", WT)
    assert v.ok is False, "a second cd must be inspected, not just the first"
    assert "/tmp/scratch" in v.reason


def test_the_repo_path_itself_being_a_prefix_string_is_not_enough():
    """`/home/.../_wt-arm-a3-old` must not pass merely because it starts with the worktree path."""
    v = check(f"cd {WT}-old && pytest -q", WT)
    assert v.ok is False, "prefix matching instead of path containment lets a sibling dir through"


# --- enforce(): the plan-level gate ---------------------------------------------------------------

def _steps():
    return [
        {"id": "S1", "verify": THE_REAL_ONE},
        {"id": "S2", "verify": "PYTHONPATH=. python3 -m pytest harness/tests/test_y.py -q"},
        {"id": "S3", "verify": f"cd {MAIN} && python3 scripts/core_sentinel.py"},
    ]


def test_enforce_rewrites_every_offending_step_and_reports_which():
    steps, fixes = enforce(_steps(), WT)
    assert [f["id"] for f in fixes] == ["S1", "S3"], "must name the steps it changed, not just fix them"
    assert all(MAIN not in s["verify"] for s in steps)
    assert steps[1]["verify"] == "PYTHONPATH=. python3 -m pytest harness/tests/test_y.py -q"
    for f in fixes:
        assert f["before"] != f["after"] and WT in f["after"]


def test_enforce_does_not_mutate_the_caller_s_steps():
    original = _steps()
    snapshot = [dict(s) for s in original]
    enforce(original, WT)
    assert original == snapshot, "enforce must not mutate in place; the log needs the before-state"


def test_enforce_on_clean_steps_reports_no_fixes():
    steps, fixes = enforce([{"id": "S1", "verify": "pytest -q"}], WT)
    assert fixes == [] and steps[0]["verify"] == "pytest -q"


def test_enforce_raises_when_a_violation_cannot_be_rewritten():
    """Not every escape has a safe mechanical rewrite; those must stop the run, not be guessed at."""
    steps = [{"id": "S1", "verify": "ssh build-box 'pytest -q'"}]
    with pytest.raises(ScopeViolation) as e:
        enforce(steps, WT, strict=True)
    assert "S1" in str(e.value)


@pytest.mark.parametrize("cmd", [
    "ssh build-box 'pytest -q'",
    "docker exec ci-container pytest -q",
    "kubectl exec pod/runner -- pytest -q",
])
def test_verify_on_another_host_is_a_violation_with_no_mechanical_fix(cmd):
    """No path escapes, so the path patterns are blind — but it can exit 0 from an unrelated tree."""
    v = check(cmd, WT)
    assert v.ok is False
    assert "false PASS" in v.reason
    assert rewrite(cmd, WT) == cmd, "there is no safe rewrite; guessing would manufacture a pass"


def test_a_step_with_an_empty_verify_is_a_violation_not_a_pass():
    """A step with no verify command trivially 'passes' every check, which is the worst failure."""
    v = check("", WT)
    assert v.ok is False and "empty" in v.reason.lower()


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_exits_nonzero_on_a_violation_and_prints_the_fix():
    r = subprocess.run(
        [sys.executable, "-m", "harness.verify_scope", "--repo", WT, THE_REAL_ONE],
        cwd=REPO, capture_output=True, text=True)
    assert r.returncode != 0
    assert WT in r.stdout, "the CLI must show the corrected command, not just complain"


def test_cli_exits_zero_on_a_clean_command():
    r = subprocess.run(
        [sys.executable, "-m", "harness.verify_scope", "--repo", WT, "pytest -q"],
        cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
