"""A prompt that explains WHY an earlier question was asked is not a build task.

Origin (2026-07-26, the third gate false-positive of the day). Michael asked how Qwen3-235B compares
to Opus 5 for coding, I answered, and he followed up with the motive:

    "I was asking because Quen323235B is available on Cerebus AI and it runs at a fast speed and one
     of our things was trying to speed up the development, you know, make it more efficient. And it
     runs at something like 800 plus tokens per second. So you could try things like having Claude as
     the planner and Quen as the coder maybe or other things like that."

`is_gateable` returned `(True, 'build task')`. The mechanism: `BUILD_VERBS` matched the bare word
**"make"** inside *"make it more efficient"* — a build verb used DESCRIPTIVELY about a goal, not
imperatively about a change. The Stop hook then blocked the turn on a `deliverable` requirement that
cannot exist, because the turn's only honest output is an answer.

None of the existing escape hatches caught it, and each miss is legitimate:
  - `ANALYSIS_REQUEST` covers "i'd like to ask" / "let me ask" but not the PAST tense "i was asking"
  - `QUESTION_LEAD` needs a leading question word; this leads with "I"
  - the '?' branch needs a trailing '?'; this ends in a full stop
  - `RESEARCH` has no retrieval verb to match

The signal actually present is a **back-reference to a question already asked**. That is about as
unambiguous as prompt classification gets: the user is stating the motive for an earlier QUESTION, so
by construction the turn produces an answer, not an artifact.

DELIBERATELY NOT FIXED HERE: the hedged suggestion ("you could try things like… maybe… or other
things like that"). It reads speculative, but "you could try adding a null check" is a perfectly real
instruction, so suppressing that shape would create false NEGATIVES — un-gated build tasks. That is
the more dangerous direction in this repo, whose entire purpose is catching overclaimed work. The
back-reference alone decides this sample; scope stops there.
"""

import pytest

from harness.autoseed import is_gateable

# The verbatim prompt that deadlocked the turn.
ORIGIN = (
    "I was asking because Quen323235B is available on Cerebus AI and it runs at a fast speed and "
    "one of our things was trying to speed up the development, you know, make it more efficient. "
    "And it runs at something like 800 plus tokens per second.So you could try things like having "
    "Claude as the planner and Quen as the coder maybe or other things like that."
)


def test_the_exact_prompt_that_deadlocked_the_turn_is_not_gateable():
    gateable, reason = is_gateable(ORIGIN)
    assert gateable is False, (
        f"the origin prompt is still classified as buildable ({reason!r}). It explains why a "
        "QUESTION was asked; there is no artifact for the Stop hook to demand."
    )


BACKREFS = [
    "I was asking because Cerebras runs it at 800 tokens per second, so we could make it faster.",
    "i was asking about that because we need to make the loop cheaper",
    "The reason I asked is that it would let us rewrite the probe stage.",
    "I only asked because the planner keeps having to fix its own output.",
    "My question was whether a faster model would let us build this differently.",
    "That's why I asked — it would mean we could add a second reviewer.",
    "I was wondering because it might let us refactor the whole build loop.",
]


@pytest.mark.parametrize("prompt", BACKREFS, ids=lambda s: s[:38])
def test_back_reference_to_an_earlier_question_is_never_a_build_task(prompt):
    """Every one of these carries a build verb, and every one is still explaining a question.

    The build verb is the whole trap: it is what `has_build` fires on, and it is why the plain
    '?'-terminated and question-lead branches cannot help here.
    """
    gateable, reason = is_gateable(prompt)
    assert gateable is False, f"{prompt!r} classified buildable ({reason!r})"


def test_the_reason_names_the_back_reference():
    """A deadlocked turn is diagnosed from this string, so it has to say which rule fired."""
    _, reason = is_gateable(ORIGIN)
    assert "ask" in reason.lower() or "question" in reason.lower(), (
        f"reason {reason!r} does not identify the back-reference rule that suppressed the contract"
    )


# --- the other direction: this must not become an amnesty for real work -------------------------

STILL_BUILDS = [
    # An explicit build instruction that merely MENTIONS the earlier question.
    "I was asking because the probe is slow — now go and fix the probe stage.",
    "I asked about that earlier. Implement the fast-probe path in lifecycle-run.js.",
    # Ordinary build requests, unrelated. These are the regression floor.
    "fix the three files breaking the test suite",
    "add a watchdog to the fix loop",
    "wire the preflight gate into spec-queue",
]


@pytest.mark.parametrize("prompt", STILL_BUILDS, ids=lambda s: s[:38])
def test_real_build_requests_are_still_gated(prompt):
    """A false NEGATIVE here is worse than the deadlock this file fixes.

    An un-gated build task is exactly the overclaiming failure the completion gate exists to catch,
    so the back-reference rule must yield to an explicit build follow-through.
    """
    gateable, reason = is_gateable(prompt)
    assert gateable is True, f"{prompt!r} is a real build task but was NOT gated ({reason!r})"


def test_a_bare_descriptive_make_still_does_not_carry_a_turn_on_its_own():
    """Documents the underlying sharp edge, so the next person sees it.

    `BUILD_VERBS` matching "make" in "make it more efficient" is the mechanism behind this whole
    class. The word is kept in BUILD_VERBS on purpose ("make the report land in S3" is real work),
    which is why suppression has to come from the surrounding shape rather than from the verb list.
    """
    gateable, _ = is_gateable("I was asking because it would make it more efficient.")
    assert gateable is False
