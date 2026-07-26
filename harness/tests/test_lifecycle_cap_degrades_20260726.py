"""When the sub-agent cap fires, the run must EMIT what it learned — not vanish.

Origin (2026-07-26, run `wf_a34c2778-e5d`). The cap built earlier the same day worked exactly as
designed and was still a net loss, because of how it ended:

    48 agents · 67.5 min · 2,527,123 tokens · 9 empty results
    Error: sub-agent cap 48 reached (48 spawned)  at spawn (workflow.js:91:11)
    -> workflow FAILED. No return value. Nothing written to the worktree.

`CapExceeded` propagates out of `spawn()` with no top-level catch, so the entire accumulated state
died with it: the finished plan, the five implementer attempts, and 26 completed review agents' worth
of findings. 67 minutes of work produced literally nothing a human or a resume could use.

Where the 48 went, by role (inferred from each agent's own first prompt):

    review     26   54%
    plan       14   29%
    implement   5   10%     <- the only ones that change code
    verify      2    4%
    preflight   1    2%

So 83% of the fleet was deliberating and 10% was building, on a three-line `sys.path` import fix.
That ratio is a separate finding and NOT what this file fixes (see the note at the bottom).

What this fixes is the cheap half: a ceiling should DEGRADE, not detonate. The cap must still stop
the run — that is the entire point of it — but on the way out it must emit the partial verdict, so
hitting the ceiling costs the remaining work and not the work already done.

Assertions execute the REAL `spawn()` pulled out of the shipped script, so the thing under test is
the thing that runs — same approach as test_lifecycle_agent_cap_20260726.py.
"""

import json
import re
import shutil
import subprocess
import tempfile
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


HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')

const grab = (re_) => { const m = src.match(re_); return m ? m[0] : null }

const capDecl   = grab(/^let AGENT_CAP = .*$/m)
const usedDecl  = grab(/^let agentsUsed = .*$/m)
const errDecl   = grab(/^class CapExceeded extends Error \{\}$/m)
const toDecl    = grab(/^const AGENT_TIMEOUT_MS = .*$/m)
const retDecl   = grab(/^const WATCHDOG_RETRIES = .*$/m)
const symDecl   = grab(/^const TIMED_OUT = .*$/m)
const wdDecl    = grab(/^async function withWatchdog\(prompt, opts\) \{[\s\S]*?^\}$/m)
const spawnDecl = grab(/^async function spawn\(prompt, opts\) \{[\s\S]*?^\}$/m)
// PROGRESS is a single-line object literal, so anchor on the same line -- a [\s\S]*?^\}$ pattern
// would silently swallow every function between it and the next top-level closing brace.
const progDecl  = grab(/^const PROGRESS = \{.*\}$/m)
const phaseAlias = grab(/^const _phase = phase$/m)
const stageDecl = grab(/^function stage\(title\) \{[\s\S]*?^\}$/m)
const emitDecl  = grab(/^function emitPartialVerdict\(why\) \{[\s\S]*?^\}$/m)

if (!capDecl || !usedDecl || !errDecl || !spawnDecl || !wdDecl) {
  console.log(JSON.stringify({ error: 'could not extract spawn()/withWatchdog()/AGENT_CAP' }))
  process.exit(0)
}
if (!progDecl) {
  console.log(JSON.stringify({ error: 'no module-level PROGRESS object', logs: [] }))
  process.exit(0)
}
if (!emitDecl) {
  console.log(JSON.stringify({ error: 'no emitPartialVerdict() -- the ceiling has no way to report', logs: [] }))
  process.exit(0)
}

const logs = []
const agent = async () => ({})                      // runtime primitive, stubbed
const budget = { total: null, spent: () => 0, remaining: () => Infinity }
const log = (m) => logs.push(String(m))
const phase = () => {}                              // runtime phase hook, stubbed
const A = { agent_cap: 3 }                          // tiny cap so this is fast

const mod = [capDecl, usedDecl, errDecl, toDecl, retDecl, symDecl, progDecl, phaseAlias,
             stageDecl, emitDecl, wdDecl, spawnDecl].filter(Boolean).join('\n')

;(async () => {
  const run = new Function('agent', 'budget', 'log', 'A', 'phase', `
    ${mod}
    return (async () => {
      // Simulate a run that got somewhere before the ceiling: phase set, work recorded.
      PROGRESS.phase = 'Build'
      if (Array.isArray(PROGRESS.steps_done)) PROGRESS.steps_done.push('step1_add_test')
      if (Array.isArray(PROGRESS.findings)) PROGRESS.findings.push('reviewer said X')
      let threw = null
      for (let i = 0; i < 10; i++) {
        try { await spawn('p', { label: 'l' + i }) }
        catch (e) { threw = e.constructor.name + ': ' + e.message; break }
      }
      return { threw, spawned: agentsUsed }
    })()
  `)
  const out = await run(agent, budget, log, A, phase)
  console.log(JSON.stringify({ ...out, logs }))
})()
"""


def _run(loop_path):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(HARNESS)
        harness = fh.name
    r = subprocess.run(["node", harness, str(loop_path)],
                       capture_output=True, text=True, timeout=60)
    Path(harness).unlink(missing_ok=True)
    assert r.returncode == 0, f"node failed: {r.stderr[-1500:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# --- a PROGRESS record must exist and be fed by the phases ---------------------------------------

def test_a_progress_record_exists(loop):
    """Without an explicit record, the catch path has nothing safe to read.

    It cannot read the phase-local `plan` / `stepResults` bindings: those are `let`/`const` in the
    script body, so if the cap fires before they initialise, touching them throws a ReferenceError
    from the temporal dead zone — turning a degraded exit into a second, more confusing crash.
    """
    src = loop.read_text()
    assert re.search(r"^const PROGRESS = \{", src, re.M), (
        "no module-level PROGRESS object. The cap path needs a record that is safe to read at ANY "
        "point in the run, including before the plan exists."
    )


def test_the_phases_actually_write_to_progress(loop):
    """An empty record degrades to an empty verdict, which is the bug wearing a hat."""
    src = loop.read_text()
    writes = re.findall(r"PROGRESS\.\w+\s*(?:=|\.push\()", src)
    assert len(writes) >= 3, (
        f"only {len(writes)} write(s) to PROGRESS. If the phases do not record what they finished, "
        "the partial verdict is empty and hitting the cap still loses everything."
    )
    assert re.search(r"PROGRESS\.phase\s*=", src), "nothing records WHICH phase was running"


# --- the cap still stops the run, but emits on the way out ---------------------------------------

def test_the_cap_still_fires(loop):
    """Degrading must not become 'carry on regardless' — that would restore the runaway."""
    out = _run(loop)
    assert not out.get("error"), out["error"]
    assert out["threw"], "the cap did NOT fire — a degraded exit is still an EXIT"
    assert "CapExceeded" in out["threw"], f"unexpected error type: {out['threw']}"
    assert out["spawned"] == 3, f"cap of 3 let {out['spawned']} agents through"


def test_the_partial_verdict_is_emitted_before_throwing(loop):
    """The whole point. 67 minutes of work must survive the ceiling."""
    out = _run(loop)
    assert not out.get("error"), out["error"]
    blob = "\n".join(out["logs"])
    assert "PARTIAL VERDICT" in blob, (
        "the cap fired without emitting a partial verdict. Everything accumulated — plan, step "
        f"results, review findings — is lost, which is exactly what run wf_a34c2778-e5d cost. "
        f"Logs were: {out['logs']!r}"
    )


def test_the_partial_verdict_carries_the_accumulated_state(loop):
    """It has to contain the WORK, not just an apology."""
    out = _run(loop)
    assert not out.get("error"), out["error"]
    blob = "\n".join(out["logs"])
    for needle, why in [
        ("Build", "the phase that was running"),
        ("step1_add_test", "the steps already finished"),
        ("reviewer said X", "the review findings already gathered"),
    ]:
        assert needle in blob, (
            f"the partial verdict does not carry {why} ({needle!r}). A verdict without the work is "
            f"no more useful than the crash it replaced. Logs: {blob[:800]!r}"
        )


def test_the_partial_verdict_names_the_resume_path(loop):
    """A degraded run should tell a human how to continue it, not make them reconstruct it."""
    out = _run(loop)
    blob = "\n".join(out["logs"]).lower()
    assert "resume" in blob or "agent_cap" in blob, (
        "the partial verdict does not mention how to resume or how to raise the cap. The runtime "
        "prints a resumeFromRunId on failure; the verdict should say what to change before reusing it."
    )


# NOT FIXED HERE, deliberately: the 83%-deliberation / 10%-building ratio that made the cap fire in
# the first place. A role-aware ceiling (cap review fan-out separately from total agents) is the real
# answer, and it is a design change with its own trade-offs — a too-tight review cap silently lowers
# review quality, which is the failure this repo cares most about. It gets its own experiment, with a
# measured before/after, rather than being smuggled in behind a crash fix.
