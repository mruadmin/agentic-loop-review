"""A STUCK verdict must say how to check the branch, because we believed one that was wrong.

Origin (2026-07-26). Three loop arms reported STUCK or died on their agent ceiling. All three had
committed, or left in the working tree, a one-line fix and a passing test. The verdicts were taken at
face value, the branches were never opened, and the arms were written up as having produced no code —
in a public repo, and in a message already sent to a third party.

The verdict was not lying. STUCK means "this loop could not CERTIFY the work", which is a statement
about the loop's confidence, not about the branch. Nothing in the returned object said so, and the
obvious command for checking — `git diff` — compares the working tree to HEAD and therefore goes
EMPTY the moment an agent commits. The natural check confirms the wrong conclusion.

So every STUCK return now carries the distinction and the command that settles it. These tests read
the shipped workflow scripts, so they fail if a future edit drops the note from a return site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "lifecycle-run.js": REPO / ".claude" / "workflows" / "lifecycle-run.js",
    "lifecycle-fix.js": REPO / ".claude" / "workflows" / "lifecycle-fix.js",
}


def _src(name: str) -> str:
    p = SCRIPTS[name]
    if not p.exists():
        pytest.skip(f"{p} not present")
    return p.read_text()


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_note_is_defined_once_and_names_the_inspection_command(name):
    s = _src(name)
    assert s.count("const BRANCH_NOTE = {") == 1, "BRANCH_NOTE must be defined exactly once"
    note = s[s.index("const BRANCH_NOTE = {"):]
    note = note[:note.index("\n}\n") + 2]
    assert "harness.arm_report" in note, (
        "the note does not name the tool that answers 'did this branch produce anything' — a note "
        "that only says 'go and check' repeats the original failure, because the obvious check lies"
    )
    assert "${S.repo_path}" in note, "the command must be filled in with the actual worktree"


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_note_warns_against_git_diff_specifically(name):
    """Naming the wrong command is the load-bearing part. It is what we actually reached for."""
    s = _src(name)
    note = s[s.index("const BRANCH_NOTE = {"):]
    note = note[:note.index("\n}\n")]
    assert "git diff" in note and re.search(r"working tree", note), (
        "the note must say WHY git diff is the wrong check (working tree vs HEAD), not merely that "
        "the branch should be checked"
    )


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_every_stuck_return_carries_the_note(name):
    """The whole point is that no STUCK verdict escapes without it."""
    s = _src(name)
    missing = []
    for m in re.finditer(r"outcome: 'STUCK'", s):
        # look at the object literal this appears in: from the preceding `{` to the following `}`
        start = s.rindex("{", 0, m.start())
        window = s[start:m.end() + 400]
        if "BRANCH_NOTE" not in window and "no branch to inspect" not in window:
            line = s[:m.start()].count("\n") + 1
            missing.append(line)
    assert not missing, (
        f"{name}: STUCK returned without the branch note at line(s) {missing}. Every STUCK verdict "
        f"must either spread BRANCH_NOTE or explain that setup never produced a branch."
    )


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_presetup_stuck_says_there_is_no_branch_rather_than_naming_a_missing_one(name):
    """Before setup runs there is no worktree, so the note must not promise one that does not exist."""
    s = _src(name)
    idx = s.index("outcome: 'STUCK'")  # the first one is the pre-setup guard in both scripts
    window = s[max(0, idx - 200):idx + 300]
    assert "no branch to inspect" in window, (
        "the earliest STUCK return happens before the worktree exists; it must say so instead of "
        "printing an arm_report command for a path that was never created"
    )
    assert "BRANCH_NOTE" not in window, (
        "the pre-setup guard cannot spread BRANCH_NOTE — S does not exist yet, and referencing it "
        "there would throw a ReferenceError in place of the real reason"
    )


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_note_is_defined_after_S_is_assigned(name):
    """BRANCH_NOTE interpolates S.repo_path at definition time, so S must already hold the setup."""
    s = _src(name)
    assign = re.search(r"^const S = ", s, re.M)
    assert assign, f"{name}: could not find where S is assigned"
    assert s.index("const BRANCH_NOTE = {") > assign.start(), (
        "BRANCH_NOTE is defined before S is assigned; interpolating S.repo_path there throws a "
        "ReferenceError out of the temporal dead zone and replaces the real failure reason"
    )
