"""The test suite must be COLLECTABLE. One script-shaped file silently disables all the others.

Origin (2026-07-26). Three files in harness/tests/ were one-shot verification scripts, not tests:
module-level assertions ending in `sys.exit(1)`, and no test functions at all. pytest imports every
file during collection, so any one of them firing `sys.exit` aborted collection for the WHOLE
directory with INTERNALERROR:

    INTERNALERROR>   File ".../harness/tests/test_design_plan_ready.py", line 16, in fail
    INTERNALERROR>     sys.exit(1)
    INTERNALERROR> SystemExit: 1
    no tests ran

Which means: for an unknown period, running `pytest harness/tests/` ran NOTHING, while looking like
an infrastructure problem rather than a testing gap. With those three excluded the suite is 1128
passed / 105 failed / 92 skipped. That is the exact false-green this repo exists to prevent, sitting
inside the mechanism meant to prevent it.

The three files were fixed. This guard is the part that matters, because it stops the class from
coming back: the next person who drops a `sys.exit`-shaped script into harness/tests/ gets a red test
naming their file, instead of silently muting 1300 others.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "harness" / "tests"


def _test_files():
    return sorted(p for p in TESTS.glob("test_*.py"))


# --- the real invariant: pytest can collect the directory -----------------------------------------

def test_pytest_can_collect_the_whole_directory():
    """The end-to-end check. Everything else in this file is a faster diagnosis of the same thing."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(TESTS)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    out = r.stdout + r.stderr
    assert "INTERNALERROR" not in out, (
        "collection ABORTED — one file killed the whole suite. Offending traceback:\n"
        + "\n".join(l for l in out.splitlines() if "INTERNALERROR" in l)[:2000]
    )
    # Exit 0 = collected fine; 5 = "no tests" (also wrong here, we have hundreds); 2 = usage/internal
    assert r.returncode == 0, f"pytest --collect-only exited {r.returncode}\n{out[-2500:]}"


# --- static guards, so a failure names the file instead of dumping a traceback ---------------------

def _exits_directly(node) -> bool:
    """Does this subtree call sys.exit / exit / os._exit, or raise SystemExit?"""
    for n in ast.walk(node):
        if isinstance(n, ast.Raise):
            exc = n.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name == "SystemExit":
                return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in ("exit", "_exit"):
                return True
            if isinstance(f, ast.Name) and f.id == "exit":
                return True
    return False


def _exiting_helpers(tree) -> set:
    """Module-level functions that themselves exit.

    This exists because the FIRST version of this guard missed all three files that caused the
    outage: each defined `def fail(msg): print(...); sys.exit(1)` and then called `fail(...)` at
    module scope. Looking only for a literal `sys.exit` in module scope found nothing, so the guard
    passed the exact code it was written to catch. One level of indirection is what real scripts
    actually do, so it has to be followed.
    """
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _exits_directly(n)}


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_no_module_level_sys_exit(path):
    """Nothing reachable at IMPORT time may exit the interpreter.

    AST rather than substring search, so a `sys.exit` inside a function body -- fine, it only runs
    when called -- does not trip this. What matters is module scope and the bodies of module-level
    `if` statements, since both execute on import. Calls to local helpers that exit count too.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    helpers = _exiting_helpers(tree)

    def offends(node):
        if _exits_directly(node):
            return True
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers:
                return True
        return False

    offenders = []
    for node in tree.body:
        # def/class bodies are not executed on import.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # `if __name__ == "__main__":` runs only on direct execution, not import.
        if isinstance(node, ast.If) and _is_main_guard(node):
            continue
        if offends(node):
            offenders.append(getattr(node, "lineno", "?"))

    assert not offenders, (
        f"{path.name} can exit the interpreter at IMPORT time (line(s) {offenders}"
        + (f", via helper(s) {sorted(helpers)}" if helpers else "") + "). pytest imports every test "
        "file during collection, so this aborts collection for the ENTIRE directory and every other "
        "test silently stops running. Put it behind a test function, or behind "
        'if __name__ == "__main__".'
    )


def _is_main_guard(node: ast.If) -> bool:
    t = node.test
    if not isinstance(t, ast.Compare) or not isinstance(t.left, ast.Name):
        return False
    if t.left.id != "__name__":
        return False
    return any(isinstance(c, ast.Constant) and c.value == "__main__" for c in t.comparators)


# RATCHET: known script-shaped files that define no test function, frozen as of 2026-07-26.
#
# These four do NOT break collection — they have no exit path — so they were out of scope for the
# fix. They are still dead weight: pytest imports and executes them during collection, they assert
# nothing (0 `assert` statements between them, they only print), and any future edit that adds an
# exit or a raise turns one of them into the outage this file exists to prevent.
#
# They are NOT relocated because 10-15 other files reference them by path (docs, done-contracts), so
# moving them silently breaks those references. Converting them means deciding what they should
# assert, which is real work and easy to get wrong — they currently pass by not raising.
#
# The list is a ratchet, not an amnesty: any NEW test_*.py with no test function fails immediately.
# Shrinking this list is the follow-up; growing it is not allowed.
KNOWN_NO_TEST_FILES = {
    "test_da_pattern_linetype.py",
    "test_deliverable_redacted_display.py",
    "test_invoice_number_glued.py",
    "test_skill_the-project_rowtype.py",
}


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_every_test_file_defines_at_least_one_test(path):
    """A file named test_*.py with no tests is a script in the wrong folder.

    It contributes nothing on a green run and can only ever break collection. Naming it test_* is
    what gets it imported in the first place.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        for n in ast.walk(tree)
    )
    if not found and path.name in KNOWN_NO_TEST_FILES:
        pytest.skip(f"{path.name}: known script-shaped file, frozen in KNOWN_NO_TEST_FILES "
                    "(harmless today — no exit path — but asserts nothing and is imported anyway)")
    assert found, (
        f"{path.name} defines no test function. If it is a one-off verification script, move it out "
        "of harness/tests/ (scripts/ or harness/checks/) so pytest stops importing it. Do NOT add it "
        "to KNOWN_NO_TEST_FILES — that list is frozen and only shrinks."
    )


def test_the_ratchet_only_shrinks():
    """The frozen list must not grow, and must not keep entries that no longer need freezing."""
    present = {p.name for p in _test_files()}
    stale = KNOWN_NO_TEST_FILES - present
    assert not stale, (
        f"KNOWN_NO_TEST_FILES lists files that no longer exist: {sorted(stale)}. Remove them — a "
        "ratchet that keeps dead entries stops being evidence of anything."
    )
    assert len(KNOWN_NO_TEST_FILES) <= 4, (
        f"the frozen list has grown to {len(KNOWN_NO_TEST_FILES)}. It was 4 on 2026-07-26 and is "
        "allowed to shrink only. A new script-shaped file belongs outside harness/tests/."
    )
