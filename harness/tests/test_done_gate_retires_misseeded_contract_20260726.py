"""A contract whose own origin prompt is no longer gateable must be RETIRED, not enforced forever.

Origin (2026-07-26). `is_gateable` mis-seeded a contract from an analysis question ("hypothesize
any way to get the lifecycle run faster"). The classifier was then fixed so that prompt is
correctly rejected — but the already-written contract file kept blocking the Stop hook, because
`gate()` only ever asks "are the requirements green?", never "should this contract exist?".

That leaves a session permanently stuck: the contract's single requirement is
`{"test": null}` on a deliverable that cannot exist, so no amount of work can turn it green. The
only escapes were deleting the file or RR_DONE_GATE_ONESHOT — i.e. bypassing the gate, which is
precisely the behaviour this project forbids and which destroys the anti-overclaiming property
the gate exists to provide.

The general defect, which matters more than this one stuck turn: EVERY future improvement to the
classifier orphans the contracts its old version mis-seeded. A gate that cannot recognise its own
false positives trains people to bypass it, and a bypassed gate protects nothing.

So retirement is deliberately narrow — it requires ALL of:
  - the contract's `origin_prompt` is one the CURRENT classifier rejects, and
  - no requirement carries an executable test (nothing real was ever bound), and
  - the contract names no spec.
A contract with a bound test, or one naming a spec, is REAL work and stays enforced no matter what
the classifier now thinks of the wording that started it.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "harness" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


done_gate = _mod("done_gate")

# The real contract that blocked this session, verbatim from
# harness/loop/done_contract.55a51c6a-1082-4a74-b6da-d8a1f03b26f4.json
MISSEEDED = {
    "task": "I'd like to ask you, Claude Code hypothesize any way to get the life cycle run so "
            "that it gives the best quality fix but",
    "origin_prompt": "I'd like to ask you, Claude Code hypothesize any way to get the life cycle "
                     "run so that it gives the best quality fix but in a faster time.",
    "requirements": [{
        "id": "deliverable",
        "desc": "the task is BUILT and PROVEN by an executed test: I'd like to ask you, Claude "
                "Code hypothesize any way to get the life cycle run so that it gives the best "
                "quality fix but",
        "test": None,
    }],
    "spec": None,
}


def _write(tmp_path, contract):
    p = tmp_path / "done_contract.json"
    p.write_text(json.dumps(contract))
    return p


def test_the_contract_that_blocked_this_session_is_retired(tmp_path):
    p = _write(tmp_path, MISSEEDED)
    assert done_gate.is_misseeded(p) is True, (
        "the gate cannot recognise a contract seeded from a prompt its own classifier now "
        "rejects, so the session stays blocked forever with no non-bypass escape"
    )


def test_retiring_lets_the_gate_allow(tmp_path):
    """End to end: a mis-seeded contract must not block the Stop hook."""
    p = _write(tmp_path, MISSEEDED)
    verdict = done_gate.gate({"contract": str(p)})
    assert verdict.get("decision") == "allow", (
        f"gate still blocks on a mis-seeded contract: {verdict!r}"
    )


def test_retirement_is_recorded_not_silent(tmp_path):
    """A gate that silently drops contracts is indistinguishable from a broken gate."""
    p = _write(tmp_path, MISSEEDED)
    done_gate.gate({"contract": str(p)})
    assert not p.exists(), "the retired contract file is still in place and will block again"
    archive = p.parent / "done_contract.archive.jsonl"
    assert archive.exists(), "retirement left no archive record"
    rec = [json.loads(l) for l in archive.read_text().splitlines() if l.strip()][-1]
    blob = json.dumps(rec).lower()
    assert "retired" in blob or "misseeded" in blob or "mis-seeded" in blob, (
        f"archive record does not say WHY it was retired: {rec!r}"
    )


# --- the narrowness is the point --------------------------------------------------------------

def test_a_contract_with_a_bound_test_is_never_retired(tmp_path):
    """Real work stays enforced even if the wording that started it now reads as a question."""
    c = dict(MISSEEDED)
    c["requirements"] = [{"id": "deliverable", "desc": "the thing", "test": "python3 -c 'pass'"}]
    p = _write(tmp_path, c)
    assert done_gate.is_misseeded(p) is False, (
        "a contract with an executable test was retired — that is a gate bypass, not a fix"
    )


def test_a_contract_naming_a_spec_is_never_retired(tmp_path):
    c = dict(MISSEEDED)
    c["spec"] = "specs/pending/2026-07-26b-circuit-breaker-cli-import.md"
    p = _write(tmp_path, c)
    assert done_gate.is_misseeded(p) is False, "a spec-backed contract was retired"


def test_a_real_build_prompt_is_never_retired(tmp_path):
    """The decisive check: same testless shape, but the prompt IS a build request."""
    c = dict(MISSEEDED)
    c["origin_prompt"] = ("Fix the import bug in harness/circuit_breaker.py and add a regression "
                          "test that fails if it comes back.")
    c["requirements"] = [{"id": "deliverable", "desc": "fix the import bug", "test": None}]
    p = _write(tmp_path, c)
    assert done_gate.is_misseeded(p) is False, (
        "a genuine build request was retired for lacking a bound test — that inverts the gate: "
        "'no test yet' is the state it exists to block, not a reason to stand down"
    )


@pytest.mark.parametrize("bad", [{}, {"origin_prompt": ""}, {"origin_prompt": None}])
def test_missing_or_empty_origin_is_not_retired(tmp_path, bad):
    """Absent provenance means we cannot prove it was mis-seeded — so enforce, don't retire."""
    p = _write(tmp_path, bad)
    assert done_gate.is_misseeded(p) is False, f"retired a contract with no provenance: {bad!r}"


def test_retirement_agrees_with_the_live_classifier(tmp_path):
    """is_misseeded must DELEGATE to autoseed.is_gateable rather than re-implement it.

    Two copies of a classification rule diverge — the lesson from the two PreToolUse guards
    earlier the same day, where the un-narrowed copy was the one that fired.
    """
    autoseed = _mod("autoseed")
    prompt = MISSEEDED["origin_prompt"]
    assert autoseed.is_gateable(prompt)[0] is False, "precondition: classifier rejects this prompt"
    src = (ROOT / "harness" / "done_gate.py").read_text()
    assert "is_gateable" in src, "done_gate does not consult the real classifier"
