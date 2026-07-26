"""A build request the USER takes back must not block the turn forever.

Origin (2026-07-26). Michael asked "you can write the spec in that folder" -- a genuine build
request, correctly gated -- and then, in the very next message of the same turn, said "Let me open up
a new terminal and I'll start doing the spec for it in that folder."

The deliverable moved to him. But `gate()` only knows how to ask "are the requirements green?", so
the contract blocked the turn on work that was, by the user's own instruction, no longer mine to do.
The only escapes were deleting the file or RR_DONE_GATE_ONESHOT -- both bypasses.

This is DIFFERENT from the mis-seeding case (`is_misseeded`). That prompt was never a build request.
This one WAS; it was withdrawn. Conflating the two would let a real request be dismissed as
mis-seeded, so retraction is its own narrow check with its own evidence requirement.

The evidence requirement is the whole design. Retraction must be proven by a REAL LATER USER MESSAGE
in the transcript -- never by my say-so, and never by a message that merely precedes the request.
Mid-turn messages are routine in this harness, so "I'll do it myself" appearing BEFORE the request
must not retroactively cancel a request made after it.

If a test was already bound, or a spec is named, the work is real and in flight: stays enforced.
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

ORIGIN = "Actually let me give you the spec here and you can write the spec in that folder."
RETRACTION = ("Let me open up a new terminal and I'll start doing the spec for it in that folder.")

CONTRACT = {
    "task": ORIGIN[:100],
    "origin_prompt": ORIGIN,
    "requirements": [{"id": "deliverable", "desc": "write the spec", "test": None}],
    "spec": None,
}


def _transcript(tmp_path, messages, name="t.jsonl"):
    """messages: list of (role, text) in order."""
    p = tmp_path / name
    with open(p, "w") as fh:
        for role, text in messages:
            fh.write(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n")
    return p


def _contract(tmp_path, data=None):
    p = tmp_path / "done_contract.json"
    p.write_text(json.dumps(data or CONTRACT))
    return p


# --- the real case ------------------------------------------------------------------------------

def test_the_retraction_that_blocked_this_turn(tmp_path):
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", ORIGIN), ("assistant", "ok"), ("user", RETRACTION)])
    assert done_gate.is_retracted(c, str(t)) is True, (
        "the gate cannot see that the user took the work over, so the turn stays blocked on a "
        "deliverable that is no longer mine"
    )


def test_gate_allows_after_a_retraction(tmp_path):
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", ORIGIN), ("user", RETRACTION)])
    v = done_gate.gate({"contract": str(c), "transcript_path": str(t)})
    assert v.get("decision") == "allow", f"still blocking after an explicit retraction: {v!r}"


def test_retraction_is_archived_with_the_quoted_evidence(tmp_path):
    """A retraction recorded without the words that justified it is indistinguishable from a bypass."""
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", ORIGIN), ("user", RETRACTION)])
    done_gate.gate({"contract": str(c), "transcript_path": str(t)})
    assert not c.exists(), "the contract file survived and will block again next turn"
    rec = [json.loads(l) for l in
           (c.parent / "done_contract.archive.jsonl").read_text().splitlines() if l.strip()][-1]
    assert rec.get("outcome") == "retracted", rec
    assert "new terminal" in json.dumps(rec), (
        "the archive does not quote the user message that proved the retraction, so the decision "
        "cannot be audited"
    )


@pytest.mark.parametrize("msg", [
    "Let me open up a new terminal and I'll start doing the spec for it in that folder.",
    "Actually I'll do it myself.",
    "never mind, I've got this one",
    "Leave it with me — I'll write it.",
    "don't worry about it, I'll handle the spec",
])
def test_retraction_phrasings(tmp_path, msg):
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", ORIGIN), ("user", msg)])
    assert done_gate.is_retracted(c, str(t)) is True, f"not recognised as retraction: {msg!r}"


# --- the narrowness IS the mechanism ------------------------------------------------------------

def test_ordinary_followups_are_not_retractions(tmp_path):
    """These are the messages a normal turn is full of. Any of them counting = a silent bypass."""
    for msg in [
        "Yeah that looks good, keep going.",
        "Can you also add a test for the empty case?",
        "I'll review it once you're done.",              # future user action, NOT a takeover
        "Let me know when it's finished.",
        "run it on the spec and measure it",
        "do all four",
    ]:
        c = _contract(tmp_path)
        t = _transcript(tmp_path, [("user", ORIGIN), ("user", msg)])
        assert done_gate.is_retracted(c, str(t)) is False, (
            f"{msg!r} was treated as a retraction — that is a gate bypass, not a fix"
        )


def test_a_retraction_BEFORE_the_request_does_not_count(tmp_path):
    """Mid-turn messages are routine here, so ordering has to be enforced.

    Otherwise 'I'll do it myself' said once, early, would cancel every later request in the session.
    """
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", "Actually I'll do it myself."),
                               ("assistant", "ok"),
                               ("user", ORIGIN)])
    assert done_gate.is_retracted(c, str(t)) is False, (
        "a retraction that PRECEDES the request cancelled it retroactively"
    )


def test_a_bound_test_means_the_work_is_real(tmp_path):
    c = _contract(tmp_path, {**CONTRACT,
                             "requirements": [{"id": "d", "desc": "x", "test": "python3 -c 'pass'"}]})
    t = _transcript(tmp_path, [("user", ORIGIN), ("user", RETRACTION)])
    assert done_gate.is_retracted(c, str(t)) is False, (
        "a contract with real bound work was dropped on a retraction"
    )


def test_a_named_spec_means_the_work_is_real(tmp_path):
    c = _contract(tmp_path, {**CONTRACT, "spec": "specs/pending/x.md"})
    t = _transcript(tmp_path, [("user", ORIGIN), ("user", RETRACTION)])
    assert done_gate.is_retracted(c, str(t)) is False


@pytest.mark.parametrize("bad", ["", "/nonexistent/path.jsonl", None])
def test_no_usable_transcript_means_enforce(tmp_path, bad):
    """Unprovable resolves toward the gate, never away from it."""
    c = _contract(tmp_path)
    assert done_gate.is_retracted(c, bad) is False


def test_assistant_saying_it_does_not_count(tmp_path):
    """Only the USER can retract. I must not be able to talk my way out of a contract."""
    c = _contract(tmp_path)
    t = _transcript(tmp_path, [("user", ORIGIN),
                               ("assistant", "Actually I'll do it myself, never mind, I've got this")])
    assert done_gate.is_retracted(c, str(t)) is False, (
        "an ASSISTANT message cleared a contract — that is the model excusing itself from the gate"
    )
