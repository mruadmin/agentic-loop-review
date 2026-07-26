#!/usr/bin/env python3
"""Refuse to dispatch a spec whose own factual claims are false.

WHY THIS EXISTS (2026-07-26). Two full lifecycle arms -- 54 and 10 sub-agents, ~3.7M subagent
tokens, 85.8 and 46.8 minutes -- both ended STUCK on the same three-line import fix. Neither
failure was the loop's fault. Both traced to one sentence in the spec I had written:

    "This is the LAST file in harness/ with this defect ... Do not sweep other files;
     there are none."

That was false at the base commit: harness/prioritize.py had the identical defect, and its fix
existed only on an unmerged branch behind MR !7. The spec even contradicted itself -- its own
commit message said "prioritize.py, already fixed in MR !7", which means NOT fixed on this base.
So the implementer reasonably fixed both files, and the reviewer just as reasonably blocked it for
scope. An impossible position, created before either agent started, by a claim that took one grep
to falsify.

The lesson is not "write better specs." It is that a spec's factual claims are CHECKABLE, and
checking them costs seconds against a dispatch that costs hours. So:

  A spec that asserts something about the codebase must carry the command that proves it.

This runs BEFORE any agent is spawned. Exit 0 = safe to dispatch. Exit 2 = a claim failed, do not
dispatch. Exit 3 = the spec makes checkable-looking claims but binds no commands (a warning that
becomes an error under --strict), because "no claims" and "unchecked claims" must not look alike.

SPEC FORMAT -- a fenced block, anywhere in the spec:

    ```preflight
    # every line is: <expected> :: <shell command>
    # <expected> is one of: exit0, exit-nonzero, empty, nonempty
    exit0        :: test -f harness/circuit_breaker.py
    nonempty     :: grep -n "sys.path.insert" harness/circuit_breaker.py
    empty        :: grep -rln "sys.path.insert(0, str(config.REPO))" harness/ --include=*.py
    ```

Commands run with cwd = the repo root, a hard timeout, and NO shell metacharacter cleverness
required -- they go through the shell exactly as written, because that is what makes them the same
command a human would paste to check the claim by hand.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PREFLIGHT_BLOCK = re.compile(r"```preflight\s*\n(.*?)```", re.S | re.I)

# Prose that ASSERTS something about the codebase. Used only to warn when a spec makes claims of
# this shape and binds nothing -- never to guess what the check should be. Guessing a check from
# prose is how the review tiering went wrong earlier the same day; the spec author names the
# command or there is no check.
CLAIM_PROSE = re.compile(
    r"\b(only file|last file|there are none|no other|nothing else|the sole|"
    r"already fixed|does not exist|never used|unused|all (?:other )?(?:files|call sites)|"
    r"every (?:other )?(?:file|caller)|verified by|intersecting)\b",
    re.I)

EXPECTATIONS = ("exit0", "exit-nonzero", "empty", "nonempty")


class Claim:
    __slots__ = ("expected", "command", "lineno")

    def __init__(self, expected: str, command: str, lineno: int):
        self.expected = expected
        self.command = command
        self.lineno = lineno


def parse_claims(spec_text: str) -> tuple[list[Claim], list[str]]:
    """Return (claims, parse_errors). A malformed line is an ERROR, never a skip.

    Silently ignoring an unparseable claim would mean a spec could carry a check that never runs
    while looking checked -- the exact false-green this tool exists to prevent.
    """
    claims: list[Claim] = []
    errors: list[str] = []
    for block in PREFLIGHT_BLOCK.finditer(spec_text):
        base = spec_text[: block.start(1)].count("\n") + 1
        for i, raw in enumerate(block.group(1).splitlines()):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "::" not in line:
                errors.append(f"line {base + i}: no '::' separator in {line!r}")
                continue
            expected, _, command = line.partition("::")
            expected, command = expected.strip().lower(), command.strip()
            if expected not in EXPECTATIONS:
                errors.append(f"line {base + i}: unknown expectation {expected!r} "
                              f"(use one of {', '.join(EXPECTATIONS)})")
                continue
            if not command:
                errors.append(f"line {base + i}: empty command")
                continue
            claims.append(Claim(expected, command, base + i))
    return claims, errors


def check(claim: Claim, cwd: Path, timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(claim.command, shell=True, cwd=str(cwd), timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    except Exception as exc:                                  # pragma: no cover - defensive
        return False, f"could not run: {exc}"
    out = (r.stdout or "") + (r.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-8:])
    if claim.expected == "exit0":
        return r.returncode == 0, f"exit {r.returncode}\n{tail}"
    if claim.expected == "exit-nonzero":
        return r.returncode != 0, f"exit {r.returncode}\n{tail}"
    if claim.expected == "nonempty":
        return bool((r.stdout or "").strip()), f"exit {r.returncode}, stdout empty={not (r.stdout or '').strip()}\n{tail}"
    # empty
    return not (r.stdout or "").strip(), f"exit {r.returncode}\n{tail}"


def preflight(spec_path: Path, cwd: Path = REPO, strict: bool = False) -> tuple[int, list[str]]:
    """Return (exit_code, report_lines). 0 = dispatch, 2 = a claim failed, 3 = unchecked claims."""
    report: list[str] = []
    text = spec_path.read_text()
    claims, errors = parse_claims(text)

    if errors:
        report.append(f"MALFORMED preflight block in {spec_path.name}:")
        report += [f"  {e}" for e in errors]
        return 2, report

    if not claims:
        prose = sorted({m.group(0).lower() for m in CLAIM_PROSE.finditer(text)})
        if prose:
            report.append(f"{spec_path.name} asserts things about the codebase but binds NO "
                          f"preflight checks.")
            report.append(f"  unverified claim language: {', '.join(prose)}")
            report.append("  Add a ```preflight``` block. On 2026-07-26 one unchecked sentence of "
                          "exactly this kind cost ~3.7M subagent tokens across two STUCK runs.")
            return (2 if strict else 3), report
        report.append(f"{spec_path.name}: no factual claims to check.")
        return 0, report

    failed = 0
    for c in claims:
        ok, detail = check(c, cwd)
        report.append(f"  {'PASS' if ok else 'FAIL'}  [{c.expected}] {c.command}")
        if not ok:
            failed += 1
            report += [f"        {l}" for l in detail.splitlines() if l]
    head = (f"{spec_path.name}: {len(claims) - failed}/{len(claims)} preflight claims hold")
    report.insert(0, head)
    if failed:
        report.append(f"DO NOT DISPATCH — {failed} claim(s) in this spec are FALSE against the "
                      f"current tree. Fix the spec, not the loop.")
        return 2, report
    return 0, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", help="path to the spec .md")
    ap.add_argument("--repo", default=str(REPO), help="cwd for the checks (default: this repo)")
    ap.add_argument("--strict", action="store_true",
                    help="treat 'claims prose but binds no checks' as a hard failure")
    a = ap.parse_args(argv)
    p = Path(a.spec)
    if not p.exists():
        print(f"spec not found: {p}", file=sys.stderr)
        return 2
    code, report = preflight(p, Path(a.repo), a.strict)
    print("\n".join(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
