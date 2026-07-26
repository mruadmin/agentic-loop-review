"""Preflight must catch the exact false spec claim that cost two STUCK runs.

Origin (2026-07-26). Two lifecycle arms -- 54 and 10 sub-agents, ~3.7M subagent tokens, 85.8 and
46.8 minutes -- both ended STUCK on one three-line import fix. Neither failure was the loop's.
Both traced to a sentence in the spec:

    "This is the LAST file in harness/ with this defect ... Do not sweep other files;
     there are none."

False at the base commit: harness/prioritize.py had the identical defect, fixed only on an unmerged
branch behind MR !7. The spec contradicted its own commit message. The implementer fixed both files
(reasonable) and the reviewer blocked it for scope (also reasonable) -- an impossible position
created before either agent started, by a claim one grep could falsify.

The decisive test in this file is test_it_catches_the_claim_that_actually_cost_us: it builds a spec
asserting "no file in harness/ contains sys.path.insert" -- the same SHAPE of claim, against the
same tree -- and requires preflight to reject the dispatch.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "harness" / "spec_preflight.py"


def _mod():
    spec = importlib.util.spec_from_file_location("spec_preflight", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pf = _mod()


def _spec(tmp_path, body, name="s.md"):
    p = tmp_path / name
    p.write_text(body)
    return p


# --- the decisive case ------------------------------------------------------------------------

def test_it_catches_the_claim_that_actually_cost_us(tmp_path):
    """A spec claiming 'there are none' when there demonstrably ARE must block dispatch."""
    s = _spec(tmp_path, """
# SPEC — fix the import

## Scope
harness/circuit_breaker.py ONLY. This is the last file in harness/ with this defect.
Do not sweep other files; there are none.

```preflight
empty :: grep -rln "sys.path.insert" harness/ --include=*.py
```
""")
    code, report = pf.preflight(s, ROOT)
    blob = "\n".join(report)
    assert code == 2, f"preflight allowed a dispatch on a false claim:\n{blob}"
    assert "DO NOT DISPATCH" in blob, blob
    assert "FAIL" in blob, blob


def test_a_true_claim_passes(tmp_path):
    """The mirror of the above: the same shape of check, but the claim is true."""
    s = _spec(tmp_path, """
```preflight
nonempty :: grep -rln "sys.path.insert" harness/ --include=*.py
exit0    :: test -f harness/circuit_breaker.py
```
""")
    code, report = pf.preflight(s, ROOT)
    assert code == 0, "\n".join(report)
    assert "2/2" in "\n".join(report)


# --- unchecked claims must not look like no claims ---------------------------------------------

def test_claim_prose_without_checks_is_flagged(tmp_path):
    s = _spec(tmp_path, "## Scope\nharness/x.py only. There are none elsewhere. Verified by "
                        "intersecting the two greps.\n")
    code, report = pf.preflight(s, ROOT)
    assert code == 3, f"unchecked claim prose was treated as clean: {report}"
    blob = "\n".join(report)
    assert "binds NO preflight checks" in blob, blob
    assert "there are none" in blob.lower(), "the report does not name the offending language"


def test_strict_makes_unchecked_claims_fatal(tmp_path):
    s = _spec(tmp_path, "## Scope\nThis is the only file that does this.\n")
    assert pf.preflight(s, ROOT, strict=True)[0] == 2
    assert pf.preflight(s, ROOT, strict=False)[0] == 3


def test_a_spec_with_no_claims_is_clean(tmp_path):
    """Not every spec asserts something checkable; those must not be penalised."""
    s = _spec(tmp_path, "# SPEC\nAdd a --json flag to the CLI. It should print JSON.\n")
    code, report = pf.preflight(s, ROOT)
    assert code == 0, "\n".join(report)
    assert "no factual claims" in "\n".join(report)


# --- a malformed check is an error, never a silent skip ----------------------------------------

@pytest.mark.parametrize("bad,why", [
    ("exit0 test -f harness/x.py", "no :: separator"),
    ("maybe :: test -f harness/x.py", "unknown expectation"),
    ("exit0 ::", "empty command"),
])
def test_malformed_checks_block_rather_than_skip(tmp_path, bad, why):
    """A check that silently fails to run is worse than no check: it looks verified."""
    s = _spec(tmp_path, f"```preflight\n{bad}\n```\n")
    code, report = pf.preflight(s, ROOT)
    assert code == 2, f"{why!r} was silently skipped, leaving the spec looking checked: {report}"
    assert "MALFORMED" in "\n".join(report)


def test_comments_and_blanks_are_not_malformed(tmp_path):
    s = _spec(tmp_path, "```preflight\n# a comment\n\nexit0 :: true\n```\n")
    code, _ = pf.preflight(s, ROOT)
    assert code == 0


# --- expectations behave --------------------------------------------------------------------

@pytest.mark.parametrize("expected,cmd,should_pass", [
    ("exit0",        "true",            True),
    ("exit0",        "false",           False),
    ("exit-nonzero", "false",           True),
    ("exit-nonzero", "true",            False),
    ("nonempty",     "echo hi",         True),
    ("nonempty",     "true",            False),
    ("empty",        "true",            True),
    ("empty",        "echo hi",         False),
])
def test_each_expectation(tmp_path, expected, cmd, should_pass):
    s = _spec(tmp_path, f"```preflight\n{expected} :: {cmd}\n```\n")
    code, report = pf.preflight(s, ROOT)
    assert (code == 0) is should_pass, f"{expected} :: {cmd} -> {report}"


def test_a_hanging_check_times_out_rather_than_blocking_forever(tmp_path):
    """Preflight exists to save time; it must not become the thing that costs it."""
    c = pf.Claim("exit0", "sleep 30", 1)
    ok, detail = pf.check(c, ROOT, timeout=2)
    assert ok is False
    assert "TIMEOUT" in detail


# --- the CLI contract the dispatcher depends on -----------------------------------------------

def test_cli_exit_codes(tmp_path):
    good = _spec(tmp_path, "```preflight\nexit0 :: true\n```\n", "good.md")
    bad = _spec(tmp_path, "```preflight\nexit0 :: false\n```\n", "bad.md")
    r0 = subprocess.run([sys.executable, str(TOOL), str(good)], capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, str(TOOL), str(bad)], capture_output=True, text=True)
    assert r0.returncode == 0, r0.stdout + r0.stderr
    assert r2.returncode == 2, r2.stdout + r2.stderr
    assert "DO NOT DISPATCH" in r2.stdout


def test_missing_spec_is_an_error_not_a_pass():
    r = subprocess.run([sys.executable, str(TOOL), "/nope/absent.md"],
                       capture_output=True, text=True)
    assert r.returncode == 2, "a missing spec exited 0 — that would wave through every dispatch"
