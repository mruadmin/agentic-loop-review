#!/usr/bin/env python3
"""Keep a step's verify command inside the worktree it is supposed to be verifying.

Origin (2026-07-26). Three loop arms committed a working fix and all three reported failure. The
cause was one line of shell per step, not the loop's reasoning.

`lifecycle-run.js` creates a git worktree per run and threads it through as `S.repo_path`, and the
verify runner is told "Run EXACTLY this command in ${S.repo_path}". But the planner prompt never
mentioned `repo_path`; the only absolute path in its context came from `${ROOT}/STATE.md`, the MAIN
repo. So it wrote, for every step:

    cd /path/to/repo && PYTHONPATH=. pytest ...

A `cd` inside the command overrides whatever cwd the runner supplies -- the `cd` wins, silently.
Verify therefore ran against main, where the bug is still present and the new test file does not
exist, failed three times, and the step was declared unverifiable while the branch held the fix.

The planner prompt is being fixed as well. This module exists because a prompt instruction is a
request, and because the failure is invisible: the wrong command does not error, it just tests the
wrong code and produces a confident, wrong FAIL. That is the worst shape a defect can have here.

What counts as leaving the worktree, in the order they bit us:
  * `cd` / `pushd` to an absolute path that is not inside the worktree
  * `git -C <path>` pointed at another tree
  * an absolute file argument (a pytest target) under a different repo
Anything relative stays inside by construction and is left alone.

Usage:
    python3 -m harness.verify_scope --repo <worktree> '<command>'      # exit 1 if it escapes
    python3 -m harness.verify_scope --repo <worktree> --plan plan.json # check every step's verify
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Absolute paths that are legitimately outside the worktree. Evidence tee'd to /tmp is normal in the
# real plans and must not be flagged, or the guard gets switched off for crying wolf.
ALLOWED_PREFIXES = ("/tmp/", "/dev/", "/proc/", "/var/tmp/", "/usr/", "/bin/", "/etc/ssl/")

# `cd`/`pushd` taking an absolute argument. Anchored to a command boundary (start, ;, &&, ||, |, or
# a newline) so `# cd /elsewhere` in a comment or `--cd=/x` as a flag value is not matched.
_CD = re.compile(r'(?:(?<=^)|(?<=[;&|\n(]))\s*(cd|pushd)\s+(?P<q>["\']?)(?P<path>/[^\s"\';&|)]+)(?P=q)')

# `git -C <abs>` -- git's own cd, which the `cd` pattern above cannot see.
_GIT_C = re.compile(r'\bgit\s+(?:[^\s;&|]+\s+)*?-C\s+(?P<q>["\']?)(?P<path>/[^\s"\';&|)]+)(?P=q)')

# Any other bare absolute path used as an argument, e.g. `pytest /main/repo/harness/tests/x.py`.
_ABS_ARG = re.compile(r'(?<![\w=:/-])(?P<path>/(?!tmp/|dev/|proc/|var/tmp/|usr/|bin/)[\w./+-]{4,})')

# Verify running somewhere other than this machine. No path leaves the worktree, so the patterns
# above are blind to it, but the failure is the same and worse: `ssh box 'pytest -q'` can exit 0
# from a tree that has nothing to do with this branch, manufacturing a false PASS. There is no
# mechanical correction for these -- the command has to be rewritten by a human -- so they are
# reported as unmappable rather than guessed at.
_REMOTE = re.compile(r'(?:(?<=^)|(?<=[;&|\n(]))\s*(?:ssh|scp|rsync|kubectl\s+exec|'
                     r'docker\s+(?:exec|run)|podman\s+(?:exec|run))\b')


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    offenders: tuple[str, ...] = ()


class ScopeViolation(Exception):
    """A verify command leaves the worktree and cannot be mechanically corrected."""


def _inside(path: str, repo: str) -> bool:
    """True if `path` is the worktree or below it.

    Uses path-component containment, not `startswith`: `/x/_wt-arm-a3-old` starts with
    `/x/_wt-arm-a3` but is a different directory, and treating it as inside is how a sibling arm's
    tree gets verified instead of this one's.
    """
    p, r = PurePosixPath(path), PurePosixPath(repo)
    return p == r or r in p.parents


def _allowed(path: str) -> bool:
    return path.startswith(ALLOWED_PREFIXES)


def _escapes(cmd: str, repo: str) -> list[tuple[str, str]]:
    """Every (kind, path) in `cmd` that points outside `repo`. Order is source order."""
    found: list[tuple[int, str, str]] = []
    for m in _CD.finditer(cmd):
        found.append((m.start('path'), m.group(1), m.group('path')))
    for m in _GIT_C.finditer(cmd):
        found.append((m.start('path'), 'git -C', m.group('path')))
    for m in _ABS_ARG.finditer(cmd):
        # skip anything already reported by the more specific patterns above
        if any(pos == m.start('path') for pos, _, _ in found):
            continue
        found.append((m.start('path'), 'path argument', m.group('path')))

    out = []
    for _, kind, path in sorted(found):
        if _inside(path, repo):
            continue
        # ALLOWED_PREFIXES exempts a path used as DATA -- `| tee /tmp/cb_red.out` is normal in the
        # real plans. It must not exempt a path used as a WORKING DIRECTORY: `cd /tmp/scratch` or
        # `git -C /tmp/clone` runs the verification somewhere that is not this branch, which is the
        # whole defect. Writing to /tmp is fine; running in it is not.
        if kind == 'path argument' and _allowed(path):
            continue
        out.append((kind, path))
    return out


def check(cmd: str, repo: str) -> Verdict:
    """Does this verify command stay inside `repo`?"""
    if not cmd or not cmd.strip():
        return Verdict(False, "verify command is empty — an empty command exits 0 and certifies "
                              "nothing, which passes every gate while proving nothing")
    repo = str(Path(repo))
    if _REMOTE.search(cmd):
        m = _REMOTE.search(cmd)
        return Verdict(
            False,
            f"verify runs on another host or container ({m.group(0).strip()!r}), so it cannot be "
            f"verifying the worktree at {repo}. It can exit 0 from an unrelated tree — a false PASS.",
            (m.group(0).strip(),),
        )
    bad = _escapes(cmd, repo)
    if not bad:
        return Verdict(True)
    detail = "; ".join(f"{kind} → {path}" for kind, path in bad)
    return Verdict(
        False,
        f"verify escapes the worktree ({repo}): {detail}. A path outside the worktree tests code "
        f"the step did not change — it fails on work that is actually correct.",
        tuple(p for _, p in bad),
    )


def rewrite(cmd: str, repo: str) -> str:
    """Repoint out-of-worktree absolute paths at `repo`. Returns `cmd` unchanged if already clean.

    Only paths are substituted; the rest of the command -- test selection, flags, pipes -- is left
    exactly as written. A rewrite that "improved" the pytest arguments would change what is being
    verified, which defeats the purpose of having a verify command at all.
    """
    repo = str(Path(repo))
    if check(cmd, repo).ok:
        return cmd

    def _sub_path(path: str, *, is_workdir: bool) -> str | None:
        """Map an outside path to its equivalent inside the worktree, or None if unmappable.

        `is_workdir` carries the same distinction `_escapes` makes, and it has to: a /tmp path used
        as DATA is legitimate and must be left alone, while a /tmp path used as a WORKING DIRECTORY
        is a violation and must be repointed. Without the flag, check() would report `cd /tmp/x` as a
        violation and rewrite() would decline to fix it -- the two halves of this module disagreeing,
        which is worse than either behaviour on its own.
        """
        if _inside(path, repo):
            return None
        if not is_workdir and _allowed(path):
            return None
        p = PurePosixPath(path)
        # A path under some OTHER repo root: keep the tail after that root. We do not know the other
        # root's length, so anchor on the deepest ancestor that is itself a repo-looking directory --
        # in practice the arms all sit as siblings, so the parent of the worktree is the anchor.
        anchor = PurePosixPath(repo).parent
        try:
            rel = p.relative_to(anchor)
        except ValueError:
            return repo if p.name and '.' not in p.name else None
        parts = rel.parts[1:]  # drop the other repo's own directory name
        return str(PurePosixPath(repo, *parts)) if parts else repo

    def _repl(is_workdir: bool):
        def inner(m: re.Match) -> str:
            new = _sub_path(m.group('path'), is_workdir=is_workdir)
            if new is None:
                return m.group(0)
            return m.group(0).replace(m.group('path'), new)
        return inner

    out = _CD.sub(_repl(True), cmd)          # cd / pushd -- a working directory
    out = _GIT_C.sub(_repl(True), out)       # git -C     -- also a working directory
    out = _ABS_ARG.sub(_repl(False), out)    # everything else -- a data path
    return out


def enforce(steps: list[dict], repo: str, *, strict: bool = False) -> tuple[list[dict], list[dict]]:
    """Scope-correct every step's verify command.

    Returns (corrected_steps, fixes) where `fixes` is one record per changed step with before/after.
    The caller is expected to LOG the fixes: a silently corrected plan looks like a plan that was
    written correctly, and then nobody fixes the planner.

    Input is never mutated -- the before-state is the evidence.

    strict=True raises ScopeViolation for a violation that cannot be mechanically corrected, rather
    than passing the still-broken command through. Guessing at a remote or otherwise unmappable
    command is how a false PASS gets manufactured.
    """
    repo = str(Path(repo))
    out: list[dict] = []
    fixes: list[dict] = []
    for step in steps:
        s = dict(step)
        cmd = s.get('verify', '') or ''
        v = check(cmd, repo)
        if v.ok:
            out.append(s)
            continue
        fixed = rewrite(cmd, repo)
        if not check(fixed, repo).ok:
            msg = (f"step {s.get('id', '?')}: verify cannot be scoped to {repo} automatically — "
                   f"{v.reason}")
            if strict:
                raise ScopeViolation(msg)
            out.append(s)
            fixes.append({"id": s.get('id'), "before": cmd, "after": cmd,
                          "reason": v.reason, "corrected": False})
            continue
        s['verify'] = fixed
        out.append(s)
        fixes.append({"id": s.get('id'), "before": cmd, "after": fixed,
                      "reason": v.reason, "corrected": True})
    return out, fixes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="harness.verify_scope",
        description="Check that verify commands stay inside the worktree they verify.")
    ap.add_argument("command", nargs="?", help="a single verify command to check")
    ap.add_argument("--repo", required=True, help="the worktree the command must stay inside")
    ap.add_argument("--plan", help="a JSON file with a 'steps' list, each having a 'verify'")
    ap.add_argument("--fix", action="store_true", help="with --plan, write corrections back")
    a = ap.parse_args(argv)

    if a.plan:
        p = Path(a.plan)
        doc = json.loads(p.read_text())
        steps = doc.get("steps", doc if isinstance(doc, list) else [])
        corrected, fixes = enforce(steps, a.repo)
        for f in fixes:
            state = "FIXED" if f["corrected"] else "UNFIXABLE"
            print(f"[{state}] {f['id']}\n  before: {f['before']}\n  after:  {f['after']}")
        if a.fix and fixes:
            if isinstance(doc, list):
                p.write_text(json.dumps(corrected, indent=1))
            else:
                doc["steps"] = corrected
                p.write_text(json.dumps(doc, indent=1))
            print(f"\nwrote {len(fixes)} correction(s) to {p}")
        if not fixes:
            print("all verify commands are correctly scoped")
        return 1 if any(not f["corrected"] for f in fixes) else 0

    if not a.command:
        ap.error("give a command, or --plan")
    v = check(a.command, a.repo)
    if v.ok:
        print("OK — verify stays inside the worktree")
        return 0
    print(f"SCOPE VIOLATION: {v.reason}\n")
    fixed = rewrite(a.command, a.repo)
    if check(fixed, a.repo).ok:
        print(f"corrected:\n  {fixed}")
    else:
        print("no mechanical correction available — rewrite this command by hand")
    return 1


if __name__ == "__main__":
    sys.exit(main())
