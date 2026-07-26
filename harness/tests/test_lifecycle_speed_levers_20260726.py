"""The four speed/cost levers must stay wired into both lifecycle loops.

Measured baseline that motivated all four (2026-07-26, one three-line sys.path import fix):

    lifecycle-run   54 agents  342k output  79.3 min  STUCK
    lifecycle-fix   10 agents   83k output  46.8 min  STUCK
    plain prompt     1 agent      8k output 20   min  working fix

Anatomy of the 79 minutes, from the per-agent trace:
    13.4 min  three Explore agents emitting ~0 output tokens, then re-run and done in 2.5 -> lever 1
    14   min  planner + 4 plan-review lenses + a full re-plan, on a ONE-step plan       -> lever 3
    48   min  four build cycles, each paying the full review fan-out BEFORE any check
              that the code even runs                                                  -> lever 2
    and both runs were dispatched from a spec whose scope claim was false               -> lever 4

These are STRUCTURAL assertions on the shipped scripts. They cannot prove the runtime saving -- only
a real run does that -- but they do prove the mechanisms are present, wired to something, and
ordered correctly. Every one of them was written after watching the corresponding cost in a trace.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / ".claude" / "workflows" / "lifecycle-run.js"
FIX = ROOT / ".claude" / "workflows" / "lifecycle-fix.js"


def src(p):
    return p.read_text()


@pytest.fixture(params=[RUN, FIX], ids=["lifecycle-run", "lifecycle-fix"])
def loop(request):
    return request.param


# --- lever 1: watchdog on silent agents -------------------------------------------------------

def test_watchdog_present_in_both_loops(loop):
    s = src(loop)
    assert "async function withWatchdog(" in s, "no watchdog — a hung agent blocks the whole run"
    assert "Promise.race" in s, "watchdog does not race the agent against anything"
    assert "setTimeout" in s, "watchdog has no timer"


def test_every_spawn_goes_through_the_watchdog(loop):
    """spawn() must delegate to withWatchdog, not call the runtime primitive directly."""
    s = src(loop)
    assert re.search(r"async function spawn\(prompt, opts\) \{[\s\S]*?return withWatchdog\(prompt, opts\)",
                     s), "spawn() bypasses the watchdog"


def test_watchdog_retry_counts_against_the_cap(loop):
    """Otherwise a run that keeps timing out respawns forever, which is the failure it replaces."""
    fn = re.search(r"async function withWatchdog\([\s\S]*?\n\}", src(loop))
    assert fn, "could not extract withWatchdog"
    body = fn.group(0)
    assert "agentsUsed++" in body, "a watchdog respawn is not counted against AGENT_CAP"
    assert "CapExceeded" in body, "watchdog respawn does not honour the cap"


def test_watchdog_uses_no_forbidden_clock(loop):
    """Date.now()/performance would break resume — the sandbox probe confirmed both are absent."""
    fn = re.search(r"async function withWatchdog\([\s\S]*?\n\}", src(loop)).group(0)
    for banned in ("Date.now", "performance.now", "new Date"):
        assert banned not in fn, f"watchdog reads {banned}, which breaks workflow resume"


# --- lever 2: cheap deterministic convergence before expensive review -------------------------

def test_run_loop_probes_before_it_reviews():
    """The ordering IS the lever: a cheap exit-code check must precede the review fan-out."""
    s = src(RUN)
    probe = s.find("label: `probe:${step.id}")
    lenses = s.find("const lenses = [")
    assert probe != -1, "lifecycle-run has no cheap verify probe"
    assert lenses != -1, "could not locate the review fan-out"
    assert probe < lenses, (
        "the expensive review fan-out runs BEFORE the cheap probe — that is the inversion that "
        "made a failing step pay ~50k tokens of review per attempt for code that did not run"
    )


def test_run_loop_probe_is_cheap():
    s = src(RUN)
    block = s[s.find("label: `probe:${step.id}") - 1200: s.find("label: `probe:${step.id}") + 300]
    assert "effort: 'low'" in block, "the probe is not low-effort, so it is not cheap"


def test_fix_loop_probes_before_the_high_effort_certifier():
    s = src(FIX)
    probe = s.find("label: `probe:${attempts}")
    certify = s.find("label: `verify:${attempts}")
    assert probe != -1, "lifecycle-fix has no cheap probe"
    assert certify != -1
    assert probe < certify, "the high-effort certifier runs before the cheap probe"
    assert "continue" in s[probe:certify], (
        "a failing probe does not short-circuit the attempt, so the expensive certifier still runs"
    )


@pytest.mark.parametrize("loop_path,marker", [(RUN, "probe:${step.id}"), (FIX, "probe:${attempts}")])
def test_the_probe_may_never_certify(loop_path, marker):
    """The cheap probe decides 'worth reviewing yet', never 'done'.

    Collapsing those roles would hand certification to the cheapest agent in the loop — the exact
    inversion of this project's rule that the implementer never certifies its own work and that
    'done' is an independent exit code.
    """
    s = src(loop_path)
    i = s.find("label: `" + marker)
    block = s[max(0, i - 2000):i]
    assert "thermometer, not a doctor" in block, (
        "the probe prompt does not forbid it from judging/certifying"
    )
    assert re.search(r"Do NOT (fix|review)", block), "the probe is not told to stay read-only"


def test_authoritative_verifier_survives_in_both_loops(loop):
    """The independent high-effort certifier must NOT have been replaced by the cheap probe."""
    s = src(loop)
    assert "agentType: 'reviewer'" in s, "the independent reviewer-verifier is gone"
    assert "effort: 'high'" in s, "the certifier is no longer high-effort"


def test_converge_tries_is_bounded():
    s = src(RUN)
    assert "CONVERGE_TRIES" in s
    m = re.search(r"const CONVERGE_TRIES = Number\(A\.converge_tries\) \|\| (\d+)", s)
    assert m, "CONVERGE_TRIES is not a bounded, overridable constant"
    assert 1 <= int(m.group(1)) <= 5, f"CONVERGE_TRIES default {m.group(1)} is not a sane bound"


# --- lever 3: skip the plan review on tiny plans ----------------------------------------------

def test_plan_review_is_skipped_on_tiny_plans():
    s = src(RUN)
    assert "PLAN_REVIEW_MIN_STEPS" in s, "the 4-lens plan review has no small-plan exclusion"
    assert re.search(r"const planLenses = plan\.steps\.length < PLAN_REVIEW_MIN_STEPS \? \[\] :", s), (
        "planLenses is not gated on the step count, so the review still runs on a 1-step plan"
    )


def test_plan_review_threshold_agrees_with_the_cap_tightening():
    """Two different notions of 'small' in one file would drift."""
    s = src(RUN)
    thresh = int(re.search(r"const PLAN_REVIEW_MIN_STEPS = (\d+)", s).group(1))
    tighten = int(re.search(r"function tightenCapForSmallPlan\(stepCount\) \{\s*\n\s*if \(stepCount > (\d+)\)", s).group(1))
    assert thresh == tighten + 1, (
        f"plan review skips below {thresh} steps but the cap tightens at <= {tighten} — "
        "these must describe the same boundary"
    )


# --- lever 4: spec preflight gates dispatch ---------------------------------------------------

def test_preflight_gates_dispatch_in_both_loops(loop):
    s = src(loop)
    assert "spec_preflight.py" in s, "the loop never runs the spec preflight"
    assert "label: 'preflight'" in s, "preflight is not a real phase step"
    assert re.search(r"preflight\.ok === false", s), "a failed preflight does not stop the run"
    assert "outcome: 'STUCK'" in s


def test_preflight_runs_before_any_expensive_phase(loop):
    """It exists to make failure cheap; running it late defeats the entire point."""
    s = src(loop)
    pf = s.find("label: 'preflight'")
    assert pf != -1
    for later, name in [("label: 'planner'", "planner"), ("label: 'reproduce'", "diagnose"),
                        ("label: `impl:", "implementer"), ("label: `fix:", "fixer")]:
        j = s.find(later)
        if j != -1:
            assert pf < j, f"preflight runs AFTER the {name} phase — failure is no longer cheap"


def test_preflight_tool_exists_and_is_executable_as_a_cli():
    tool = ROOT / "harness" / "spec_preflight.py"
    assert tool.exists(), "the loops reference spec_preflight.py but it does not exist"
    body = tool.read_text()
    assert "DO NOT DISPATCH" in body
    assert 'if __name__ == "__main__"' in body, "not runnable as the CLI the loops invoke"
