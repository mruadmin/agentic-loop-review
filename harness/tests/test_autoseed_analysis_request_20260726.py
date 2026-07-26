"""An "-ize me an idea" prompt asks for an ANSWER, so it must not seed a build contract.

Origin (2026-07-26). The real prompt:

    "I'd like to ask you, Claude Code hypothesize any way to get the life cycle run so that
     it gives the best quality fix but in a faster time."

`is_gateable` returned True ("build task"), seeded a contract whose only requirement was the
first ~110 characters of that sentence, and the Stop hook then refused to end the turn on a
deliverable that cannot exist -- there is no artifact to bind a test to when the request is
"hypothesize a way." The reply was analysis, which was what was asked for.

Why the existing guards all missed it, in order:
  - It does NOT end with '?', so the `p.endswith("?")` branch never ran. It is a question
    written as a statement -- the single most common way Michael phrases one.
  - QUESTION_LEAD only matches an interrogative FIRST word; this leads with "I'd like to ask you".
  - RESEARCH has no ideation verbs (hypothesize/brainstorm/speculate), only retrieval-shaped
    ones (research/look into/investigate).
  - So `has_build` decided it, firing on the NOUN "fix" in "the best quality fix".

The fix must not become a blanket off-switch. The boundary tests below are the point of this
file: a genuine build request still gates, and an analysis prompt that explicitly asks for the
build to follow ("hypothesize, then implement it") still gates -- because a missed gate is the
overclaiming this whole mechanism exists to stop.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _autoseed():
    spec = importlib.util.spec_from_file_location("autoseed", ROOT / "harness" / "autoseed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


autoseed = _autoseed()

REAL_PROMPT = (
    "I'd like to ask you, Claude Code hypothesize any way to get the life cycle run so that "
    "it gives the best quality fix but in a faster time."
)


def test_the_exact_prompt_that_caused_this():
    gateable, reason = autoseed.is_gateable(REAL_PROMPT)
    assert gateable is False, (
        f"seeded a contract on a hypothesis question (reason={reason!r}). There is no artifact "
        "to bind a test to; the Stop hook then blocks the turn forever."
    )


@pytest.mark.parametrize("prompt", [
    # ideation verbs, no question mark
    "Hypothesize a faster way to run the lifecycle loop and give the best quality fix.",
    "Brainstorm some ways we could cut the token cost of the build phase.",
    "Speculate on why the explore agents hung for 13 minutes and produced nothing.",
    # question phrased as a statement, with a build verb in it as a noun
    "I'd like to ask you whether a watchdog would fix the hung-agent problem.",
    "I want to ask if there's a cheaper way to write the review phase.",
    "Tell me what you think we should change about how we build the plan.",
    # "would it help" framing -- the shape of the Composer-setup question earlier the same day
    "Would it help if we could get a copy of their setup to fix our loop?",
])
def test_analysis_requests_are_not_gateable(prompt):
    gateable, reason = autoseed.is_gateable(prompt)
    assert gateable is False, f"{prompt!r} seeded a contract (reason={reason!r})"


# --- boundaries: the gate must still fire on real work ----------------------------------------

@pytest.mark.parametrize("prompt", [
    "Fix the import bug in harness/circuit_breaker.py and add a regression test.",
    "Add a watchdog to the lifecycle loop that kills agents producing no output for 3 minutes.",
    "Please implement the deterministic verify gate in lifecycle-run.js.",
    "Refactor the review phase so the lenses run in parallel.",
])
def test_real_build_requests_still_gate(prompt):
    gateable, reason = autoseed.is_gateable(prompt)
    assert gateable is True, (
        f"{prompt!r} stopped being gated (reason={reason!r}) — the fix over-corrected into a "
        "blanket off-switch, which is worse than the false gate it replaced."
    )


@pytest.mark.parametrize("prompt", [
    "Hypothesize a faster lifecycle loop, then implement it.",
    "Brainstorm the options and then build the best one.",
    "Speculate about the cause and then fix it.",
])
def test_analysis_followed_by_an_explicit_build_still_gates(prompt):
    """Ideation with a committed build follow-through IS a build turn.

    Without this, "think about X then do X" would silently escape the gate — and "then do it"
    is how most of this project's real work is actually requested.
    """
    gateable, reason = autoseed.is_gateable(prompt)
    assert gateable is True, (
        f"{prompt!r} escaped the gate (reason={reason!r}) despite an explicit build follow-through"
    )


def test_the_earlier_fixes_still_hold():
    """Guard against this change regressing today's other two autoseed fixes."""
    assert autoseed.is_gateable("<agent-message from=\"x\">\nimplement the fix please")[0] is False
    assert autoseed.is_gateable(
        "Fix harness/circuit_breaker.py and add a test that fails if it regresses."
    )[0] is True
