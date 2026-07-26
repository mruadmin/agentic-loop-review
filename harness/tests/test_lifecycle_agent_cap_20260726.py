"""The lifecycle loops must have a sub-agent ceiling, and it must actually fire.

Origin (2026-07-26). Neither `.claude/workflows/lifecycle-run.js` nor `lifecycle-fix.js` had a cap
of ANY kind. Measured consequences on a single three-line `sys.path` import fix:

  arm A3  lifecycle-run   34 sub-agents   222k output tokens   52 min   never finished, killed by hand
  arm B3  lifecycle-fix   10 sub-agents    83k output tokens   47 min   STUCK
  arm C   plain prompt     1 sub-agent      8k output tokens   20 min   working fix

An earlier lifecycle-run reached 77 sub-agents in 1h42m. Both long runs were dispatched from a spec
that turned out to be factually wrong, so none of that spend bought anything, and nothing in either
loop could notice — because nothing was counting.

These tests do NOT re-implement the cap and check the copy. They pull the real `spawn()` out of the
shipped file and execute it, so the thing under test is the thing that runs.

Why agent count and not wall-clock: `Date.now()` is unavailable inside a Workflow script (it would
break resume), and agent count is what tracks spend anyway.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / ".claude" / "workflows" / "lifecycle-run.js"
FIX = ROOT / ".claude" / "workflows" / "lifecycle-fix.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(params=[RUN, FIX], ids=["lifecycle-run", "lifecycle-fix"])
def loop(request):
    assert request.param.exists(), f"missing workflow script: {request.param}"
    return request.param


def _src(p):
    return p.read_text()


# --- the cap exists and nothing bypasses it ---------------------------------------------------

def test_no_call_site_bypasses_the_cap(loop):
    """Only withWatchdog() may touch the runtime `agent()`; everything else goes via spawn().

    This is the assertion that would catch someone adding a new phase later and calling the
    runtime's `agent()` directly out of habit — which would bypass BOTH the cap and the watchdog.

    Updated 2026-07-26 when the watchdog landed: the single legitimate call to the primitive moved
    from spawn() into withWatchdog(), so the exemption is now "inside withWatchdog" rather than one
    hard-coded line. Narrowing the exemption to that one function is what keeps this test from
    degrading into "any line mentioning agent( is fine."
    """
    src = _src(loop)
    watchdog = re.search(r"async function withWatchdog\([\s\S]*?\n\}", src)
    assert watchdog, "no withWatchdog() — the exemption below would hide every bypass"
    outside = src.replace(watchdog.group(0), "")
    body = "\n".join(l for l in outside.splitlines() if not l.lstrip().startswith("//"))
    bare = re.findall(r"\bagent\(", body)
    assert not bare, (
        f"{len(bare)} call site(s) outside withWatchdog() call the runtime agent() directly, "
        "bypassing the cap and the watchdog. Route them through spawn()."
    )
    assert len(re.findall(r"\bagent\(", watchdog.group(0))) == 1, (
        "withWatchdog calls the runtime primitive more than once — each call must be counted"
    )


def test_spawn_is_defined_and_counts(loop):
    src = _src(loop)
    assert "async function spawn(" in src, "no spawn() wrapper — the cap has nothing to hang on"
    assert "agentsUsed++" in src, "spawn() does not increment the counter"
    assert "AGENT_CAP" in src, "no AGENT_CAP"


def test_cap_is_overridable_per_dispatch(loop):
    """The queue must be able to hand a tighter budget to a small spec."""
    assert re.search(r"Number\(A\.agent_cap\)", _src(loop)), "args.agent_cap is not honoured"


# --- the cap FIRES (executing the real function) -----------------------------------------------

HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')

// Pull the real declarations out of the shipped file rather than restating them here.
// withWatchdog and its constants are extracted too, since spawn() now delegates to it -- the thing
// under test has to be the whole real path from spawn() down to the runtime primitive.
const capDecl   = src.match(/^let AGENT_CAP = .*$/m)
const usedDecl  = src.match(/^let agentsUsed = .*$/m)
const errDecl   = src.match(/^class CapExceeded extends Error \{\}$/m)
const toDecl    = src.match(/^const AGENT_TIMEOUT_MS = .*$/m)
const retDecl   = src.match(/^const WATCHDOG_RETRIES = .*$/m)
const symDecl   = src.match(/^const TIMED_OUT = .*$/m)
const wdDecl    = src.match(/^async function withWatchdog\(prompt, opts\) \{[\s\S]*?^\}$/m)
const spawnDecl = src.match(/^async function spawn\(prompt, opts\) \{[\s\S]*?^\}$/m)
// Added 2026-07-26 with the degraded-exit change: spawn() calls emitPartialVerdict() before it
// throws, and that reads PROGRESS. Without both here the cap path raises ReferenceError and this
// test would report a cap failure that does not exist.
const progDecl2 = src.match(/^const PROGRESS = \{.*\}$/m)
const emitDecl2 = src.match(/^function emitPartialVerdict\(why\) \{[\s\S]*?^\}$/m)
if (!capDecl || !usedDecl || !errDecl || !spawnDecl || !wdDecl || !toDecl || !retDecl || !symDecl) {
  console.log(JSON.stringify({ error: 'could not extract spawn()/withWatchdog()/AGENT_CAP from source' }))
  process.exit(0)
}

let spawned = 0
const agent = async () => { spawned++; return {} }        // the runtime primitive, stubbed
const budget = { total: null, spent: () => 0, remaining: () => Infinity }
const log = () => {}
const A = {}          // args: empty, so the cap falls through to the shipped default

const mod = [capDecl[0], usedDecl[0], errDecl[0], toDecl[0], retDecl[0], symDecl[0],
             progDecl2 && progDecl2[0], emitDecl2 && emitDecl2[0],
             wdDecl[0], spawnDecl[0]].filter(Boolean).join('\n')

;(async () => {
  const run = new Function('agent', 'budget', 'log', 'A', `
    ${mod}
    return (async () => {
      const seen = { cap: AGENT_CAP, calls: 0, threw: null }
      for (let i = 0; i < AGENT_CAP + 25; i++) {
        try { await spawn('p', {}); seen.calls++ }
        catch (e) { seen.threw = e.constructor.name + ': ' + e.message; break }
      }
      return seen
    })()
  `)
  const seen = await run(agent, budget, log, A)
  console.log(JSON.stringify({ ...seen, actually_spawned: spawned }))
})()
"""


def _exercise(loop_path, tmp_path):
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    r = subprocess.run(["node", str(h), str(loop_path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed: {r.stderr[:400]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cap_stops_the_loop_at_the_limit(loop, tmp_path):
    """The real spawn(), driven past its cap, must throw instead of continuing forever."""
    got = _exercise(loop, tmp_path)
    assert "error" not in got, got.get("error")
    assert got["threw"], (
        f"spawn() never threw: it allowed {got['calls']} spawns against a cap of {got['cap']}. "
        "An uncapped loop is what burned 34 agents on a three-line fix."
    )
    assert got["threw"].startswith("CapExceeded"), got["threw"]
    assert got["calls"] == got["cap"], (
        f"cap off by {got['calls'] - got['cap']}: allowed {got['calls']}, cap is {got['cap']}"
    )
    assert got["actually_spawned"] == got["cap"], (
        "the counter and the real spawn count disagree — the cap is not guarding the primitive"
    )


def test_cap_is_low_enough_to_have_caught_the_real_runs(loop, tmp_path):
    """A cap above the observed blowouts would be decoration.

    34 was the measured lifecycle-run blowout and 77 the earlier one, so anything >= 34 would have
    let A3 through untouched. 48 is the deliberate default for a multi-step feature; the runtime
    tightening for small plans is what handles the A3 case, and is asserted separately.
    """
    got = _exercise(loop, tmp_path)
    assert got["cap"] <= 48, f"cap {got['cap']} is too high to bound anything observed"


# --- the small-plan tightening (lifecycle-run only) --------------------------------------------

def test_small_plan_tightens_the_cap():
    """A3's plan was small and still spent 34 agents. Step count is the earliest cheap signal."""
    src = _src(RUN)
    assert "function tightenCapForSmallPlan" in src, "no small-plan tightening"
    assert "tightenCapForSmallPlan(plan.steps.length)" in src, (
        "tightenCapForSmallPlan is defined but never called — dead safety code"
    )
    # It must be called AFTER the plan is final (post plan-review), or it tightens against a
    # step count the reviewers may still change.
    assert src.index("tightenCapForSmallPlan(plan.steps.length)") > src.index("plan-revise"), (
        "cap is tightened before the plan review can revise the step count"
    )


def test_tightening_actually_lowers_the_cap(tmp_path):
    src = _src(RUN)
    fn = re.search(r"^function tightenCapForSmallPlan\(stepCount\) \{[\s\S]*?^\}$", src, re.M)
    assert fn, "could not extract tightenCapForSmallPlan"
    cap = re.search(r"^let AGENT_CAP = .*$", src, re.M).group(0)
    h = tmp_path / "t.js"
    h.write_text(
        "const A = {}; const log = () => {};\n" + cap + "\n" + fn.group(0) + "\n"
        "const before = AGENT_CAP;\n"
        "tightenCapForSmallPlan(1); const oneStep = AGENT_CAP;\n"
        "AGENT_CAP = before;\n"
        "tightenCapForSmallPlan(9); const nineStep = AGENT_CAP;\n"
        "console.log(JSON.stringify({before, oneStep, nineStep}));\n"
    )
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:400]
    got = json.loads(r.stdout.strip())
    assert got["oneStep"] < got["before"], (
        f"a 1-step plan did not tighten the cap ({got['before']} -> {got['oneStep']})"
    )
    assert got["oneStep"] <= 16, f"1-step cap {got['oneStep']} still above the 34 that was measured"
    assert got["nineStep"] == got["before"], (
        "a 9-step plan was tightened too — real multi-step features would be strangled"
    )


# --- the two copies must not drift ------------------------------------------------------------

def test_both_loops_carry_the_same_mechanism():
    """The 2026-07-26 PreToolUse-guard lesson: a safety mechanism living in two files diverges,
    and the copy nobody narrowed is the one that fires. Pin the shape in both."""
    for probe in ("async function spawn(", "class CapExceeded extends Error {}",
                  "budget.remaining() <= 0"):
        assert probe in _src(RUN), f"lifecycle-run.js lost: {probe}"
        assert probe in _src(FIX), f"lifecycle-fix.js lost: {probe}"
