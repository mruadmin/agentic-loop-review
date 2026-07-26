"""The JS copy of verify-scoping must agree with the Python one, or the guard is a guess.

Origin (2026-07-26). Verify-command scoping has to exist in two places and there is no way around it:

  * `harness/verify_scope.py` is the real checker -- full edge cases, a CLI, and importable.
  * `.claude/workflows/lifecycle-run.js` needs it INSIDE the workflow script, applied to the plan the
    moment the planner returns and before any implementer runs. Workflow scripts have no filesystem
    or subprocess access, so they cannot call the Python. The logic is duplicated by necessity.

Duplicated logic drifts, and a scope guard that has drifted is worse than none: it reports that the
plan was corrected while leaving a command pointing at the wrong checkout. So the two are pinned
against each other here, on the shapes that actually occurred plus the ones that nearly did.

If this test fails, the two implementations disagree. Fix whichever is wrong -- do not delete the
fixture.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from harness.verify_scope import rewrite  # noqa: E402

WF = REPO / ".claude" / "workflows" / "lifecycle-run.js"
WT = "/home/michael/Documents/Agent/_wt-arm-a3"
MAIN = "/path/to/repo"

# Every shape that mattered. The first is verbatim from the plan that caused the incident.
CASES = [
    f"cd {MAIN} && PYTHONPATH=. python3 -m pytest harness/tests/test_circuit_breaker_cli_import.py -q",
    f"cd {MAIN} && python3 scripts/core_sentinel.py",
    "PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q",
    "python3 scripts/core_sentinel.py",
    f"cd {WT} && PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q",
    f"cd {WT}/harness && python3 -m pytest tests/test_x.py -q",
    "cd harness && python3 -m pytest tests/test_x.py -q",
    f"cd {WT} && pytest -q 2>&1 | tee /tmp/cb_red.out",
    f"PYTHONPATH=. python3 -m pytest {MAIN}/harness/tests/test_x.py -q",
    f"pushd {MAIN} && pytest -q",
    f"git -C {MAIN} diff --quiet",
    "git -C /tmp/some-clone diff --quiet",
    "cd /tmp/scratch && pytest -q",
    "cd /home/michael/Documents/Agent/_wt-arm-b3 && pytest -q",
    f"cd {WT} && pytest -q; cd {MAIN} && pytest -q",
    f"cd {WT} && pytest -q; cd /tmp/scratch && pytest -q",
    f"cd {WT}-old && pytest -q",
    "git diff --stat HEAD~1",
    f"cd {MAIN} && pytest -q && python3 -c 'import harness.circuit_breaker'",
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_scope_verify() -> str:
    """Pull the real `scopeVerify` out of the shipped workflow script.

    Reading the function out of the file that actually runs -- rather than keeping a copy here -- is
    the only version of this test worth having. A copy would pass forever while the shipped script
    drifted, which is the exact failure this file exists to prevent.

    The span is taken from the declaration to the first `}` at column zero. That relies on the
    function being top-level and conventionally indented, which it is; the assertions below fail
    loudly rather than handing malformed JS to node if that ever stops being true.
    """
    src = WF.read_text()
    start = src.index("function scopeVerify(")
    end = src.index("\n}\n", start) + len("\n}")
    fn = src[start:end]
    assert fn.count("{") == fn.count("}"), (
        "extracted span has unbalanced braces — scopeVerify is no longer a top-level function with "
        "its closing brace at column zero, and this extractor needs updating"
    )
    assert "return out" in fn, "extracted the wrong span from the workflow script"
    return fn


def test_the_function_is_still_present_in_the_shipped_script():
    """If someone removes it, this suite must fail loudly rather than silently pass on nothing."""
    assert "function scopeVerify(" in WF.read_text(), (
        "scopeVerify is gone from lifecycle-run.js — the workflow no longer scopes verify commands"
    )


def _run_js(cases: list[str], repo: str) -> list[str]:
    harness = _extract_scope_verify() + f"""
const CASES = {json.dumps(cases)};
console.log(JSON.stringify(CASES.map(c => scopeVerify(c, {json.dumps(repo)}))));
"""
    r = subprocess.run([shutil.which("node"), "-e", harness],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def js_results() -> list[str]:
    return _run_js(CASES, WT)


@pytest.mark.parametrize("idx", range(len(CASES)))
def test_js_and_python_agree(idx: int, js_results: list[str]):
    cmd = CASES[idx]
    assert js_results[idx] == rewrite(cmd, WT), (
        f"the workflow's scopeVerify and harness/verify_scope.py disagree.\n"
        f"  input:  {cmd}\n"
        f"  js:     {js_results[idx]}\n"
        f"  python: {rewrite(cmd, WT)}"
    )


def test_neither_implementation_leaves_the_main_repo_in_the_incident_command(js_results):
    """The specific outcome that had to change: the shipped command must end up in the worktree."""
    i = 0
    assert MAIN not in js_results[i] and js_results[i].startswith(f"cd {WT} &&")
    assert MAIN not in rewrite(CASES[i], WT)


def test_clean_commands_are_untouched_by_both(js_results):
    """A guard that rewrites correct commands would be its own source of false failures."""
    for i, cmd in enumerate(CASES):
        if MAIN in cmd or "/tmp/scratch" in cmd or "/tmp/some-clone" in cmd or f"{WT}-old" in cmd \
                or "_wt-arm-b3" in cmd:
            continue
        assert js_results[i] == cmd, f"js rewrote a clean command: {cmd!r} -> {js_results[i]!r}"
        assert rewrite(cmd, WT) == cmd, f"python rewrote a clean command: {cmd!r}"


def test_both_are_idempotent(js_results):
    """Re-scoping an already-scoped plan must be a no-op, or repeated planning drifts."""
    again = _run_js(js_results, WT)
    assert again == js_results, "js scopeVerify is not idempotent"
    assert [rewrite(c, WT) for c in js_results] == js_results, "python rewrite is not idempotent"


def test_the_planner_prompt_states_where_verify_runs():
    """The enforcement exists because the prompt was silent; the prompt must not go silent again."""
    src = WF.read_text()
    planner = src[src.index("Turn the spec into ORDERED atomic steps"):]
    planner = planner[:planner.index("Write the checklist to")]
    assert "${S.repo_path}" in planner, (
        "the planner is still not told which directory verify runs in — the original defect"
    )
    assert re.search(r"do not put an absolute `cd`", planner, re.I), (
        "the planner is not told to avoid an absolute cd in verify commands"
    )
