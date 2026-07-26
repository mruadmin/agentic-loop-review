#!/usr/bin/env python3
"""What did this worktree arm actually produce? Answer it the same way every time.

Origin (2026-07-26). We ran four loop configurations on one three-line import fix, each in its own
git worktree, and reported that three of them produced no code. That was wrong, and it was published
to a third party before being caught.

The mechanism of the error was a single wrong command. `git diff` compares the WORKING TREE to HEAD,
so it is empty by construction the moment an agent commits — which is exactly what a working agent
does. Every committed fix was invisible. `git diff main...HEAD` is the question we meant to ask.

This is not a hard thing to know, which is why a tool exists now rather than a resolution to be more
careful. Anything asking "did that arm do anything?" runs this. It reports FOUR states, because the
interesting distinction the manual check kept collapsing is between *no work* and *uncommitted work*:

    committed      diff vs the merge-base is non-empty  -> work exists on the branch
    uncommitted    working tree dirty / untracked files -> work exists but would be lost
    both           committed AND still-dirty
    empty          neither

Usage:
    python3 -m harness.arm_report <worktree> [<worktree> ...] [--base main] [--json]
    python3 -m harness.arm_report _wt-*                       # shell-globbed, one row each

Exit codes: 0 = every arm produced something; 1 = at least one arm was empty; 2 = bad usage.
The nonzero-on-empty is deliberate, so a sweep over arms can be asserted in CI or a test.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return stdout, or '' if the command fails.

    Failures are swallowed to a sentinel rather than raised because the common ones are expected:
    the base ref may not exist in a worktree that was cut before the branch was named, and a
    worktree may have been removed since the run. A missing answer must not abort a sweep over
    twelve arms -- it is reported as its own state instead.
    """
    try:
        r = subprocess.run(("git", "-C", str(repo)) + args,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


@dataclass
class Arm:
    path: Path
    base: str
    exists: bool = True
    head: str = ""
    head_subject: str = ""
    merge_base: str = ""
    commits: list[str] = field(default_factory=list)
    committed_files: list[str] = field(default_factory=list)
    dirty_files: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        c, d = bool(self.committed_files), bool(self.dirty_files)
        if c and d:
            return "both"
        if c:
            return "committed"
        if d:
            return "uncommitted"
        return "empty"

    @property
    def produced(self) -> bool:
        return self.state != "empty"


def inspect(path: Path, base: str = "main") -> Arm:
    arm = Arm(path=path, base=base)
    if not (path / ".git").exists() and not _git(path, "rev-parse", "--git-dir"):
        arm.exists = False
        return arm

    arm.head = _git(arm.path, "rev-parse", "--short", "HEAD").strip()
    arm.head_subject = _git(arm.path, "log", "-1", "--format=%s").strip()

    # `main...HEAD` (three dots) is the merge-base comparison: what THIS branch added, ignoring
    # whatever main gained meanwhile. Resolve the base explicitly so a missing ref is reported as a
    # missing base rather than silently degrading into a two-dot diff against nothing.
    arm.merge_base = _git(arm.path, "merge-base", base, "HEAD").strip()
    if arm.merge_base:
        arm.commits = [ln for ln in _git(
            arm.path, "log", "--format=%h %s", f"{arm.merge_base}..HEAD").splitlines() if ln.strip()]
        arm.committed_files = [ln for ln in _git(
            arm.path, "diff", "--name-only", f"{arm.merge_base}..HEAD").splitlines() if ln.strip()]

    # Untracked files count. An arm that wrote a brand-new test file and never `git add`ed it has
    # done the work; reporting that as "empty" is the same mistake in a different costume.
    arm.dirty_files = [ln[3:].strip() for ln in _git(
        arm.path, "status", "--porcelain").splitlines() if ln.strip()]
    return arm


_LABEL = {
    "committed": "committed",
    "uncommitted": "UNCOMMITTED (would be lost)",
    "both": "committed + uncommitted",
    "empty": "EMPTY",
}


def render(arm: Arm) -> str:
    if not arm.exists:
        return f"{arm.path.name}: not a git worktree (removed?)"
    head = f"{arm.head} {arm.head_subject}" if arm.head else "(no HEAD)"
    lines = [f"{arm.path.name}: {_LABEL[arm.state]}", f"  HEAD      {head}"]
    if not arm.merge_base:
        lines.append(f"  base      {arm.base!r} not resolvable here — committed work NOT assessed")
    else:
        lines.append(f"  vs {arm.base:<7} {len(arm.commits)} commit(s), "
                     f"{len(arm.committed_files)} file(s)")
        for c in arm.commits:
            lines.append(f"    · {c}")
        for f in arm.committed_files:
            lines.append(f"    + {f}")
    for f in arm.dirty_files:
        lines.append(f"    ~ {f}   (uncommitted)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="harness.arm_report",
        description="Report what each worktree arm actually produced, committed or not.")
    ap.add_argument("worktrees", nargs="+", type=Path)
    ap.add_argument("--base", default="main", help="base ref to compare against (default: main)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)

    arms = [inspect(p, a.base) for p in a.worktrees]

    if a.as_json:
        print(json.dumps([{
            "path": str(x.path), "exists": x.exists, "state": x.state, "produced": x.produced,
            "head": x.head, "head_subject": x.head_subject, "base": x.base,
            "commits": x.commits, "committed_files": x.committed_files,
            "dirty_files": x.dirty_files,
        } for x in arms], indent=1))
    else:
        print("\n\n".join(render(x) for x in arms))
        empty = [x.path.name for x in arms if x.exists and not x.produced]
        if empty:
            print(f"\nEMPTY: {', '.join(empty)}")

    return 0 if all(x.produced for x in arms if x.exists) else 1


if __name__ == "__main__":
    sys.exit(main())
