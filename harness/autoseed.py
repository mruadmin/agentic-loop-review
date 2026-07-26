"""AUTO-SEED — the UserPromptSubmit hook that captures the triggering prompt and writes a
Definition-of-Done contract automatically, so the completion gate (harness/done_gate.py) bites on
real build tasks without anyone hand-dropping a contract.

The forcing function: a build/spec prompt seeds a contract whose requirements start UNVERIFIED (no
test bound). The Stop gate then refuses to let the turn end until a REAL test is bound to each and
passes. Casual turns (replies, questions, pure research) are NOT gated — they produce answers, not
testable artifacts — so the assistant stays usable. An in-flight contract is never clobbered, so
progress is preserved across a multi-turn task; completion (all-green) archives it (see done_gate).

Wire as:  UserPromptSubmit -> python3 harness/autoseed.py   (reads hook JSON on stdin).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import done_gate

CONTRACT = done_gate.CONTRACT

# imperative build verbs => a testable artifact is expected
# "make" excludes the idiom "make sure" (negative lookahead) -- "make sure he's on LinkedIn" is
# not a build command (2026-07-13: this exact phrase misfired a testless contract on a pure
# research/strategy prompt).
BUILD_VERBS = re.compile(
    r'\b(build|implement|wire|add|create|fix|refactor|write|make(?!\s+sure\b)|set up|setup|'
    r'integrate|migrate|patch|hook up|extend|rebuild|rewrite|delete|remove|rename|replace|enable|'
    r'disable|generate)\b',
    re.I)
# leading question words => an answer, not an artifact
QUESTION_LEAD = re.compile(
    r'^\s*(what|why|how|is|are|was|were|can|could|would|should|does|do|did|who|when|where|which|will)\b',
    re.I)
# an imperative build request LEADS with the build verb ("fix the bug", "wire X in").
# A question can CONTAIN a build verb without being one ("make it faster, is there?"),
# so a '?'-terminated prompt that does not lead with a build verb is an answer, not an
# artifact — this stops tag questions ("…, is there?", "…, right?") seeding a bogus gate.
STARTS_BUILD = re.compile(
    r'^\s*(please\s+|can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)?'
    r'(build|implement|wire|add|create|fix|refactor|write|make|set up|setup|integrate|migrate|'
    r'patch|hook up|extend|rebuild|rewrite|delete|remove|rename|replace|enable|disable|generate)\b',
    re.I)
# pure-research / advice verbs => an answer, not an artifact (unless a build verb also appears)
# approval leads => the user is green-lighting work already discussed in-context; the turn
# that PROPOSED the work is where a contract belongs, not the "yes". (2026-07-03: "Yes, add
# the minimal code rule" seeded a bogus binding contract on a conversational approval.)
APPROVAL_LEAD = re.compile(
    r"^\s*(yes|yeah|yep|ok(ay)?|sure|approved?|sounds good|go ahead|do it|that'?s (fine|good|approved))\b",
    re.I)
RESEARCH = re.compile(
    r'\b(research|look into|have a look|look for|find out|want to know|explain|tell me|'
    r'summari[sz]e|review|investigate|compare|recommend|advise|opinion|thoughts|should i|'
    r'what do you think)\b', re.I)
# "create/use/spawn a (sub-)agent" describes dispatching a research/investigation helper, not a
# code deliverable — strip these phrases before testing for BUILD_VERBS so asking for a subagent
# to do research doesn't itself flip a pure-research prompt into a "build task" (2026-07-13: a
# dictated business-strategy research request containing "create another sub-agent" misfired a
# testless contract despite having no buildable artifact at all).
AGENT_DISPATCH = re.compile(
    r'\b(create|make|use|spawn|launch|dispatch)\s+(?:a\s+|another\s+|the\s+)?(sub-?agents?)\b',
    re.I)
# an explicit read-only directive means any spec-like filename mentioned in the prompt is a
# document to REVIEW, not a spec to implement -> a build verb elsewhere in the prompt (e.g.
# quoted inside a pasted commit message: "fix(closure): ...") must not fold that file's own
# MUST/SHALL lines in as requirements of this task (2026-07-03: doc-drift-audit regression).
READONLY_DIRECTIVE = re.compile(
    r'\b(read.only|must not edit any file|do not edit any file)\b', re.I)
# This project's own living context docs, AT THE REPO ROOT specifically, are never a per-turn
# spec. The global CLAUDE.md tells every session to "read STATE.md FIRST" for context, so nearly
# every prompt in this repo mentions it by name -- but STATE.md is a running project journal, not a
# Definition-of-Done for the current turn. Selecting it as `spec` makes done_gate.load_contract()
# fold EVERY MUST/SHALL line in the ENTIRE file (1000+ lines of historical solver-internals rules
# unrelated to this turn) in as fresh UNVERIFIED requirements, permanently blocking the Stop gate
# (2026-07-21: a housekeeping/triage turn that opened "Read STATE.md... first" got spec=STATE.md
# seeded this way). Scoped to the REPO ROOT only (not by basename) -- a nested doc that happens to
# share one of these names, e.g. `harness/README.md`, is a genuine per-subsystem spec and must still
# be selectable (test_autoseed.py pins this). Filtered out of BOTH the is_gateable "references a
# spec" check and seed()'s spec selection -- a bare mention must not gate at all, and a real build
# prompt that also mentions one of these must still gate on its own build-verb signal without
# folding the root doc's clauses in.
NEVER_SPEC_DOCS = {"STATE.md", "WIKI.md", "README.md", "CLAUDE.md", "AGENTS.md", "MEMORY.md"}


def _is_never_spec_doc(ref: str) -> bool:
    name = Path(ref).name
    return name in NEVER_SPEC_DOCS and done_gate._resolve(ref) == done_gate.REPO / name


def _spec_candidates(prompt: str) -> list[str]:
    return [s for s in done_gate._spec_refs(prompt) if not _is_never_spec_doc(s)]
# Beyond suppressing spec-CLAUSE folding (that's READONLY_DIRECTIVE, used in seed()), an explicit
# "must/do not edit any file" is an AUDIT/REVIEW brief that yields a REPORT, never a built+tested
# artifact -> is_gateable rejects the WHOLE prompt. Deliberately narrower than READONLY_DIRECTIVE:
# it must NOT match "read-only" as a feature noun ("add a read-only flag"), only the unambiguous
# no-edit directive. (2026-07-13: the 2-hourly doc-drift auditor's own "Read-only. You must NOT edit
# any file." prompt pasted a recent commit log — subjects like "fix(reconcile): ..." trip
# BUILD_VERBS — and listed docs as review targets — tripping the spec-ref path — so it seeded a
# bogus testless `deliverable` contract that then BLOCKED the Stop gate on every unrelated turn.)
NO_EDIT_DIRECTIVE = re.compile(r'\b(?:must|do)\s+not\s+edit\s+any\s+file\b', re.I)
# an explicit "propose a plan/design before building/implementing" directive means THIS turn's
# deliverable is a proposal, not a tested artifact -- the build verbs elsewhere in the prompt's prose
# (recapping prior work, describing the eventual goal) must not gate it. Same category as
# NO_EDIT_DIRECTIVE. (2026-07-25: "Propose a concrete design before building anything... not silent
# implementation" still tripped BUILD_VERBS on "fix it" / "set this up" elsewhere in the prompt and
# seeded a bogus testless contract that blocked the Stop gate on a pure investigate-and-propose turn.)
PROPOSE_FIRST_DIRECTIVE = re.compile(
    r'\bpropose\b[^.]{0,80}\bbefore\s+(building|implementing|writing|shipping|coding)\b|'
    r'\bnot\s+silent\s+implementation\b',
    re.I)
# IDEATION / "a question written as a statement" (2026-07-26). The prompt that exposed this:
#   "I'd like to ask you, Claude Code hypothesize any way to get the life cycle run so that it
#    gives the best quality fix but in a faster time."
# Every existing guard missed it: no '?' so the endswith('?') branch never ran; QUESTION_LEAD only
# matches an interrogative FIRST word and this leads with "I'd like to ask you"; RESEARCH carries
# only retrieval verbs (research/look into/investigate), not ideation ones. So `has_build` decided
# it -- firing on the NOUN "fix" in "the best quality fix" -- and the Stop hook then blocked the
# turn on a deliverable that cannot exist, because "hypothesize a way" has no artifact to test.
#
# Unlike RESEARCH, this must beat has_build rather than defer to it: the whole failure mode is a
# build verb appearing as a noun inside a request for analysis.
ANALYSIS_REQUEST = re.compile(
    r'\b(hypothesi[sz]e|brainstorm|speculate|theori[sz]e)\b|'
    r"\b(i'?d like to ask|i want to ask|let me ask|can you think of)\b|"
    r'\bwhat (do |are )?you think\b',
    re.I)
# ...but "think about it THEN do it" is how most real work here is actually requested, so an
# explicit build follow-through re-arms the gate. Without this, ideation + a committed build
# would silently escape -- a missed gate, which is the overclaiming this mechanism exists to stop.
BUILD_FOLLOW_THROUGH = re.compile(
    r'\b(then|and then|after that|and)\s+'
    r'(implement|build|fix|write|add|wire|create|refactor|patch|do)\b',
    re.I)
# BACK-REFERENCE to a question already asked (2026-07-26). The prompt that exposed this:
#   "I was asking because Quen323235B is available on Cerebus AI ... one of our things was trying to
#    speed up the development, you know, make it more efficient. ... So you could try things like
#    having Claude as the planner and Quen as the coder maybe or other things like that."
# `has_build` decided it, firing on the bare word "make" in "make it more efficient" -- a build verb
# used DESCRIPTIVELY about a goal, not imperatively about a change. Every other escape hatch missed
# for a legitimate reason: ANALYSIS_REQUEST covers "i'd like to ask" but not the PAST tense "i was
# asking"; QUESTION_LEAD needs a leading interrogative and this leads with "I"; the '?' branch needs
# a trailing '?' and this ends in a full stop; RESEARCH has no retrieval verb to match.
#
# Explaining the MOTIVE for an earlier question cannot produce an artifact, so this is one of the few
# genuinely unambiguous shapes in prompt classification -- which is why it gets to beat has_build.
QUESTION_BACKREF = re.compile(
    r"\b(i\s+was\s+asking|i\s+asked|i\s+only\s+asked|i\s+just\s+asked|i\s+was\s+wondering"
    r"|the\s+reason\s+i\s+asked|that'?s\s+why\s+i\s+asked|my\s+question\s+was)\b",
    re.I)
# ...re-armed by an imperative that starts a SENTENCE rather than the prompt ("I asked about that
# earlier. Implement the fast path."). STARTS_BUILD anchors at the start of the whole prompt, so it
# cannot see an order that arrives in the second sentence, and letting that escape would be a MISSED
# gate -- the overclaiming failure this mechanism exists to stop, and the worse of the two errors.
#
# "make" is deliberately absent from this verb list. It is the exact word that misfired above, and
# it is far more often descriptive ("make it more efficient") than imperative in Michael's phrasing.
# It stays in BUILD_VERBS, where a leading "make the report land in S3" is still caught.
SENTENCE_BUILD = re.compile(
    r'(?:^|[.!?\n—-]\s*)'
    r'(?:please\s+|now\s+|go\s+ahead\s+and\s+|go\s+and\s+|can\s+you\s+|could\s+you\s+)*'
    r'(build|implement|wire|add|create|fix|refactor|rewrite|rebuild|patch|integrate|migrate|'
    r'hook\s+up|extend|delete|remove|rename|replace|enable|disable|generate|set\s+up|setup)\b',
    re.I)


def is_gateable(prompt: str) -> tuple[bool, str]:
    """Decide whether this prompt starts a buildable task that the completion gate should enforce."""
    p = (prompt or "").strip()
    low = p.lower()
    # System-injected events (agent-completion task-notifications, system reminders) flow through
    # the same UserPromptSubmit path but are NOT user build requests. Their text often contains
    # build verbs ("implement…"), which previously seeded a bogus testless contract every time an
    # agent finished — clobbering the real test-bound loop contract. Never gate on a system event.
    # `<agent-message` / `<teammate-message` ADDED 2026-07-26: peer-agent messages arrive through
    # this same path and were missed when the guard above was written. A subagent's Phase-0 status
    # report ("this spec is already done ... let me know how to proceed") seeded a contract whose
    # only requirement had text=None and test=None -- unsatisfiable by construction -- and the Stop
    # hook then refused to end the turn on a "deliverable" that was the first 100 characters of
    # somebody else's report. Exactly the failure the comment above describes, one envelope later.
    #
    # Matched at the START only, deliberately. A USER message that quotes an agent report ("the
    # agent said <agent-message>it is done</agent-message>, I don't believe it -- fix it properly")
    # is a real build request, and a substring test would let pasting an agent's output silently
    # disable the gate, which is the exact thing this mechanism exists to prevent.
    if low.startswith(("<task-notification", "<system-reminder", "<system", "[system",
                       "<agent-message", "<teammate-message")):
        # Keep the literal substring "system event" in this reason: test_autoseed_no_system_event
        # asserts on it, and that assertion is the thing that caught this string being renamed.
        return False, "system event or peer-agent message, not a user build request"
    if low.startswith(("/contract", "gate this", "gate:")):
        return True, "explicit opt-in"
    if low.startswith(("/nogate", "no gate", "skip gate", "don't gate", "dont gate")):
        return False, "explicit opt-out"
    if low.startswith("/"):
        return False, "skill/command invocation (the skill governs its own gating)"
    if len(p) < 12:
        return False, "too short / casual"
    if APPROVAL_LEAD.match(low):
        return False, "approval of in-context work (contract belongs to the proposing turn)"
    if NO_EDIT_DIRECTIVE.search(low):
        return False, "read-only audit/review brief (produces a report, not a tested artifact)"
    if PROPOSE_FIRST_DIRECTIVE.search(low):
        return False, "explicit propose-before-building directive (produces a proposal, not a tested artifact)"
    build_scan = AGENT_DISPATCH.sub(' ', low)
    has_build = bool(BUILD_VERBS.search(build_scan))
    # Ideation / question-as-a-statement. Checked BEFORE the has_build fallback and allowed to
    # beat it, because the failure this fixes is a build verb appearing as a NOUN inside a request
    # for analysis ("the best quality fix"). Re-armed by an explicit build follow-through.
    if ANALYSIS_REQUEST.search(low) and not BUILD_FOLLOW_THROUGH.search(low) \
            and not STARTS_BUILD.match(build_scan):
        return False, "ideation/analysis request (produces an answer, not a tested artifact)"
    # Explaining why an EARLIER question was asked. Same precedence as ideation and for the same
    # reason: the build verb is incidental to the sentence, not the point of it.
    if QUESTION_BACKREF.search(low) and not BUILD_FOLLOW_THROUGH.search(low) \
            and not SENTENCE_BUILD.search(build_scan):
        return False, "explains why an earlier question was asked (produces an answer, not an artifact)"
    # a '?'-terminated prompt that does not LEAD with a build verb is a question, even if
    # it contains one ("There's no way to make it faster, is there?") -> answer, not artifact.
    if p.endswith("?") and not STARTS_BUILD.match(build_scan):
        return False, "question (produces an answer)"
    # Same LEADS-with-the-verb test as the '?' branch above, not a bare "contains a build
    # verb anywhere" check: a question-word lead ("how long would it take to fix X") can
    # mention a build verb without commanding one (2026-07-08: this exact prompt — no "?",
    # so it skipped the branch above — misfired a contract on an effort-estimate question).
    if QUESTION_LEAD.match(low) and not STARTS_BUILD.match(build_scan):
        return False, "question (produces an answer)"
    if RESEARCH.search(low) and not has_build:
        return False, "research/advice (produces an answer)"
    if _spec_candidates(p):
        return True, "references a spec"
    if has_build:
        return True, "build task"
    return False, "no build signal"


def seed(prompt: str, path: Path = CONTRACT) -> dict:
    """Write a contract for a gateable prompt. Never clobbers an in-flight contract. Returns
    {seeded, reason, ...}."""
    path = Path(path)
    if path.exists():
        return {"seeded": False, "reason": "contract already in flight (progress preserved)"}
    ok, why = is_gateable(prompt)
    if not ok:
        return {"seeded": False, "reason": why}
    specs = _spec_candidates(prompt)
    spec = specs[0] if specs and not READONLY_DIRECTIVE.search(prompt) else None
    summary = " ".join(prompt.split())[:120]
    requirements = [{
        "id": "deliverable",
        "desc": f"the task is BUILT and PROVEN by an executed test: {summary}",
        "test": None,   # UNVERIFIED on purpose -> forces a real test to be bound before stop
    }]
    done_gate.open_contract(task=summary, origin_prompt=prompt.strip(),
                            requirements=requirements, spec=spec, path=path)
    return {"seeded": True, "reason": why, "spec": spec, "requirements": len(requirements)}


def handle(hook_input: dict) -> dict:
    """UserPromptSubmit entry: pull the prompt out of the hook payload and seed if gateable.
    Uses the SAME session-scoped path done_gate.gate() reads, so concurrent terminals against this
    repo each get their own contract instead of clobbering one shared file (2026-07-16)."""
    prompt = hook_input.get("prompt") or hook_input.get("user_prompt") or ""
    path = done_gate.contract_path_for(hook_input)
    return seed(prompt, path=path)


if __name__ == "__main__":
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
    out = handle(payload)
    # UserPromptSubmit: a one-line note on stdout is injected as context so the turn knows it's armed.
    if out.get("seeded"):
        extra = f" (spec: {out['spec']})" if out.get("spec") else ""
        print(f"[completion-gate] Contract seeded for this task{extra}. A passing test must be bound to "
              f"each requirement in harness/loop/done_contract.json before this turn can end.")
    sys.exit(0)
