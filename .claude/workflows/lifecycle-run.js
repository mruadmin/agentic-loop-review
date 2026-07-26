export const meta = {
  name: 'lifecycle-run',
  description: 'Runs ONE spec through the full L3 lifecycle in sub-agents, returning only a small verdict',
  whenToUse: 'Called by /spec-queue (or /lifecycle) to execute a spec without loading any of it into the calling context',
  phases: [
    { title: 'Setup',       detail: 'resolve repo, state dir, branch' },
    { title: 'Tools gate',  detail: 'buy-vs-build research before any code' },
    { title: 'Plan',        detail: '3 explorers -> planner -> 4 plan reviewers' },
    { title: 'Build',       detail: 'per step: implement -> review -> refute -> fix -> verify' },
    { title: 'Security',    detail: 'branch-scoped vulnerability pass' },
    { title: 'Flows',       detail: 'browser user-flow verify + design review' },
    { title: 'PR',          detail: 'artifacts, mermaid, open PR/MR' },
    { title: 'Greptile',    detail: 'trigger, poll check-run, fix, merge on clean' },
    { title: 'Blast',       detail: 'merge-risk verdict for the scorecard' },
  ],
}

// ---------------------------------------------------------------------------
// Context isolation is the whole point of this file.
//
// Every phase below runs in a sub-agent. Results move from phase to phase as
// JS values in THIS script -- they are never returned to the agent that called
// Workflow(). That caller gets only the small object at the bottom.
//
// Sub-agents are blind to memory and CLAUDE.md, so every prompt names the
// on-disk files it must open. Do not remove those references.
// ---------------------------------------------------------------------------

// args can arrive as an object OR as a JSON-encoded string depending on how the
// caller serialized it. Normalise once, here, rather than trusting either.
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})

const ROOT = A.root || '/path/to/repo'
const SPEC = A.spec
if (!SPEC) throw new Error('lifecycle-run requires args.spec (absolute path to the spec .md); got: ' + JSON.stringify(args))

const SKILL = ROOT + '/.claude/skills/lifecycle/SKILL.md'
const MAX_ATTEMPTS = 3
const MAX_FINDINGS_PER_ROUND = 24   // capped fan-out; anything dropped is log()ged, never silent
// Cheap implement->run-the-command rounds allowed before any expensive review is spent. See the
// CHEAP CONVERGENCE block in Phase 3 for why this exists and why the probe cannot certify.
const CONVERGE_TRIES = Number(A.converge_tries) || 3

// --- Hard ceiling on sub-agents (added 2026-07-26) --------------------------------------------
// Until today NOTHING in this file had a cap of any kind. On 2026-07-26 a run reached 34
// sub-agents / 222k output tokens / 52 minutes on a THREE-LINE import fix and was still going
// when it was killed by hand; an earlier run hit 77 agents in 1h42m. Both were dispatched from a
// spec that turned out to be factually wrong, so none of that spend bought anything. Nothing in
// the loop could notice, because nothing was counting.
//
// Agent COUNT is the ceiling, not wall-clock: Date.now() is unavailable inside a workflow script
// (it would break resume), and agent count is what actually tracks spend.
//
// On breach spawn() THROWS, which aborts the workflow and surfaces the message in the tool
// result. What matters is that it cannot silently continue -- a loop that cannot stop is precisely
// what produced the 52-minute three-line fix.
//
// CORRECTION 2026-07-26, same day, from run wf_a34c2778-e5d: the sentence that used to end this
// comment -- "partial work survives in the branch and the evidence dir either way" -- was WRONG,
// and it was the reason this was left as a bare throw. That run hit the ceiling at 48 agents after
// 67.5 min and 2.53M tokens, and the worktree contained ZERO product-code changes: the finished
// plan, five implementer attempts and 26 completed review agents all died with the exception,
// because implementers write to the branch only once a step is certified and none ever were.
// So the cap still stops the run -- that is the whole point of a ceiling -- but it now EMITS what
// it learned on the way out. Cost of hitting the ceiling should be the remaining work, not all of it.
let AGENT_CAP = Number(A.agent_cap) || 48
let agentsUsed = 0
class CapExceeded extends Error {}

// The record the degraded exit reads. It exists as an explicit module-level object rather than
// having the exit path read `plan` / `stepResults` directly, because those are `let`/`const` in the
// script body: if the ceiling is hit before they initialise, touching them throws a ReferenceError
// out of the temporal dead zone and the degraded exit becomes a second, more confusing crash.
const PROGRESS = { phase: 'init', plan_steps: null, steps_done: [], findings: [] }

// Wrapping the runtime's phase() instead of sprinkling `PROGRESS.phase = ...` at ten call sites:
// one place to keep in sync, and a new phase added later records itself for free rather than
// silently reporting whichever phase happened to update the record last.
const _phase = phase
function stage(title) {
  PROGRESS.phase = title
  _phase(title)
}

function emitPartialVerdict(why) {
  // log() is the channel because workflow scripts have no filesystem access -- this lands in the
  // run transcript, which is what a human or a resume actually reads afterwards.
  log('===== PARTIAL VERDICT (ceiling reached, run stopping) =====\n' +
      JSON.stringify({
        reason: why,
        phase_when_stopped: PROGRESS.phase,
        agents_spawned: agentsUsed,
        agent_cap: AGENT_CAP,
        plan_steps: PROGRESS.plan_steps,
        steps_done: PROGRESS.steps_done,
        findings: PROGRESS.findings,
      }, null, 1) +
      `\nTo continue: resume with a higher agent_cap (resumeFromRunId replays completed agents from ` +
      `cache, so the spend above is not repeated). Before raising it, check the role split -- on ` +
      `wf_a34c2778-e5d 83% of agents were planning/reviewing and 10% were building, so a bigger ` +
      `ceiling would likely have bought more deliberation rather than a finished fix.`)
}

// --- Watchdog on silent agents (added 2026-07-26) ---------------------------------------------
// Measured in the 2026-07-26 run: three Explore agents started together, ran for 13.4 MINUTES and
// emitted ~0 output tokens between them. They were then re-spawned and did the same job in 2.5
// minutes. That is 13.4 min of a 79-min run -- 17% of the wall clock -- lost to a hang, for free,
// with no quality trade whatsoever. It is the same failure class as the mermaid-cli call that hung
// for 7.6 hours on 2026-07-25, which we guard at the Bash level but never at the agent level.
//
// Probed the sandbox before building this (run wf_469d92dc-7bc): setTimeout and Promise.race are
// available; performance and Date.now are not. A relative bell needs neither, so this is safe for
// resume -- no wall-clock value is ever read into a result.
//
// Caveat, stated plainly: a timed-out agent is ABANDONED, not killed -- nothing here can reap it,
// so it may keep spending in the background. That is still strictly better than blocking the whole
// run behind it, which is what happens today. It counts against AGENT_CAP either way, so a run
// that keeps timing out terminates rather than looping forever.
const AGENT_TIMEOUT_MS = Number(A.agent_timeout_ms) || 6 * 60 * 1000
const WATCHDOG_RETRIES = 1
const TIMED_OUT = { timedOut: true }

async function withWatchdog(prompt, opts) {
  const label = (opts && opts.label) || 'agent'
  for (let attempt = 0; attempt <= WATCHDOG_RETRIES; attempt++) {
    // Second chance gets a longer leash: a genuinely slow agent should not be killed twice.
    const ms = AGENT_TIMEOUT_MS * (attempt + 1)
    let bell
    const res = await Promise.race([
      agent(prompt, opts),
      new Promise(r => { bell = setTimeout(() => r(TIMED_OUT), ms) }),
    ])
    if (typeof clearTimeout === 'function') clearTimeout(bell)
    if (res !== TIMED_OUT) return res
    log(`WATCHDOG: ${label} produced nothing in ${Math.round(ms / 60000)} min — ` +
        (attempt < WATCHDOG_RETRIES ? 'abandoning it and respawning' : 'giving up on it'))
    if (attempt < WATCHDOG_RETRIES) {
      if (agentsUsed >= AGENT_CAP) {
        const why = `sub-agent cap ${AGENT_CAP} reached during watchdog retry`
        emitPartialVerdict(why)
        throw new CapExceeded(why)
      }
      agentsUsed++
    }
  }
  return null   // callers already .filter(Boolean) on agent results
}

async function spawn(prompt, opts) {
  if (agentsUsed >= AGENT_CAP) {
    const why = `sub-agent cap ${AGENT_CAP} reached (${agentsUsed} spawned)`
    emitPartialVerdict(why)
    throw new CapExceeded(why)
  }
  if (budget.total && budget.remaining() <= 0) {
    const why = `token budget exhausted (${Math.round(budget.spent() / 1000)}k spent)`
    emitPartialVerdict(why)
    throw new CapExceeded(why)
  }
  agentsUsed++
  return withWatchdog(prompt, opts)
}

// The cheapest reliable signal that work is SMALL is the plan's own step count, known right after
// Phase 2 and long before the expensive phases. A one-step plan has no business spending 34
// agents. Called once, after the plan is final.
function tightenCapForSmallPlan(stepCount) {
  if (stepCount > 2) return
  const tightened = Math.min(AGENT_CAP, 16)
  if (tightened < AGENT_CAP) {
    log(`plan is ${stepCount} step(s) — tightening sub-agent cap ${AGENT_CAP} → ${tightened}`)
    AGENT_CAP = tightened
  }
}

// Pasted into every prompt. The lifecycle's standing rules live in SKILL.md;
// this is only what an agent needs to orient before it opens that file.
const CTX = `
You are one stage of the the project L3 lifecycle. You are blind to memory and to CLAUDE.md.

Read these before acting:
  - ${SKILL}            <- the lifecycle definition. Follow YOUR phase exactly.
  - ${SPEC}             <- the spec being built
  - ${ROOT}/STATE.md    <- what the system currently IS. Never assert from priors.

Standing rules (from SKILL.md "Standing rules", repeated because they are load-bearing):
  - Minimal code, maximal clarity. Smallest diff that fully and correctly satisfies the requirement.
    Never skip error handling or boundary validation to save lines.
  - Investigate before you touch it. Learn why existing code is written that way (git history,
    tests, comments) before changing it.
  - Unrelated bugs are REPORTED, never fixed in this diff.
  - Evidence, not assertions: real commands and their real output, INCLUDING failures.
  - "Done" is an exit code, not a claim.
  - Image is truth: never conclude a statement/PDF is unsolvable from extracted text. Run
    scripts/verify_statement.py and Read the PNG first.
`.trim()

const VERDICT = {
  type: 'object',
  properties: {
    ok:     { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['ok', 'reason'],
}

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          what:     { type: 'string' },
          where:    { type: 'string' },
          severity: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
        },
        required: ['what', 'where', 'severity'],
      },
    },
  },
  required: ['findings'],
}

// --- helpers ---------------------------------------------------------------

// Every finding is attacked before it is fixed. A reviewer that agrees with
// itself is an echo chamber; the refuter is prompted to DISAGREE.
//
// Collapse the same defect reported by several reviewers into one refuter.
// Six lenses on one small diff will all notice the same thing; paying for a
// high-effort refuter per REPORT rather than per DEFECT is most of the cost of a
// review round. Clusters by file plus significant-word overlap, and keeps the
// most severe phrasing of each cluster.
const SEV_RANK = { HIGH: 3, MEDIUM: 2, LOW: 1 }
const STOP = new Set(['this','that','when','with','from','into','then','than','they','them','have',
  'been','will','would','could','should','only','also','which','where','there','because','does',
  'code','line','lines','file','function','method','case','value','values'])

// Significant-word set for a claim. Order-insensitive on purpose: five reviewers
// describe one defect in five word orders, and keying on a word SEQUENCE (or on
// the first N words) treats those as five defects -- which is exactly what it did
// on 2026-07-26, when three reports of one check-then-act race in lock.py each
// got their own high-effort refuter.
function gistSet(what) {
  return new Set(String(what).toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(/\s+/)
    .filter(w => w.length > 3 && !STOP.has(w)))
}

function overlap(a, b) {
  if (!a.size || !b.size) return 0
  let shared = 0
  for (const w of a) if (b.has(w)) shared++
  return shared / (a.size + b.size - shared)   // Jaccard
}

const SAME_DEFECT = 0.5   // two claims about one file describing >=50% the same terms

function dedupeFindings(findings) {
  const clusters = []
  for (const f of findings) {
    if (!f || !f.what) continue
    const where = String(f.where || '').split(/[:\s]/)[0].split('/').pop() || '?'
    const gist = gistSet(f.what)
    const hit = clusters.find(c => c.where === where && overlap(c.gist, gist) >= SAME_DEFECT)
    if (!hit) { clusters.push({ where, gist, best: f }); continue }
    // Keep the most severe phrasing, and widen the cluster's vocabulary.
    if ((SEV_RANK[String(f.severity).toUpperCase()] || 2) > (SEV_RANK[String(hit.best.severity).toUpperCase()] || 2)) hit.best = f
    for (const w of gist) hit.gist.add(w)
  }
  return clusters.map(c => c.best)
}

/**
 * Adversarially verify findings, then return only those that survived.
 *
 * `opts.highStakes` decides where the bar sits. The course sets it by "severity
 * threshold OR round cap, whichever comes first" -- on an ordinary diff a LOW
 * finding is not worth a high-effort agent to disprove, and the round cap alone
 * (24) is no bar at all. On a money/auth/concurrency diff the bar drops to
 * everything, because a LOW-looking finding there is how incidents start.
 *
 * Nothing is dropped silently: every exclusion is log()ged with a reason, so a
 * quiet round cannot be mistaken for a clean one.
 */
async function refute(findings, phaseName, opts) {
  // Survivors are recorded so a ceiling breach does not discard the review work already paid for.
  const _record = (kept) => { for (const f of kept) PROGRESS.findings.push(`[${phaseName}] ${f.what || f.title || JSON.stringify(f).slice(0,160)}`); return kept }
  const highStakes = !!(opts && opts.highStakes)

  const unique = dedupeFindings(findings)
  if (findings.length > unique.length) {
    log(`refute: ${findings.length} report(s) -> ${unique.length} distinct defect(s) (${findings.length - unique.length} duplicate report(s) collapsed)`)
  }

  const bar = highStakes ? [] : unique.filter(f => String(f.severity).toUpperCase() === 'LOW')
  const kept = highStakes ? unique : unique.filter(f => String(f.severity).toUpperCase() !== 'LOW')
  if (bar.length) {
    log(`refute: ${bar.length} LOW-severity finding(s) NOT verified (bar is MEDIUM+ on a non-high-stakes diff): ` +
        bar.map(f => `${f.where} ${String(f.what).slice(0, 60)}`).join(' | '))
  }

  const list = kept.slice(0, MAX_FINDINGS_PER_ROUND)
  if (kept.length > list.length) {
    log(`NOTE: ${kept.length - list.length} finding(s) beyond the ${MAX_FINDINGS_PER_ROUND} cap were NOT verified this round`)
  }
  const votes = await parallel(list.map((f, i) => () =>
    spawn(`${CTX}

Adversarially REFUTE this review finding. Your default is that it is WRONG until the code proves otherwise.

  finding:  ${f.what}
  location: ${f.where}
  claimed severity: ${f.severity}

Open the actual code. Check whether the described failure can really occur on a reachable path with
realistic inputs. Reviewers routinely flag things that are already guarded elsewhere, unreachable,
or true only under inputs the type system prevents.

Return refuted=true (ok=true means "I could not refute it") if the finding does not survive contact
with the code. Say WHY in one sentence, citing file:line.`,
      // Reasoning effort tracks what is at stake in getting the verdict wrong:
      // high for a HIGH-severity claim or any high-stakes diff, default otherwise.
      { label: `refute:${i}`, phase: phaseName, schema: VERDICT,
        effort: (highStakes || String(f.severity).toUpperCase() === 'HIGH') ? 'high' : undefined })
      .then(v => ({ finding: f, real: v && v.ok }))
  ))
  return _record(votes.filter(Boolean).filter(v => v.real).map(v => v.finding))
}

async function runFixer(findings, phaseName, extra) {
  if (!findings.length) return null
  return spawn(`${CTX}

You are the fixer. These findings ALREADY survived an adversarial refute pass -- they are real.
Fix exactly these and nothing else. Report, do not fix, anything else you notice.

${JSON.stringify(findings, null, 2)}

${extra || ''}`,
    { label: 'fix', phase: phaseName, agentType: 'fixer' })
}

// --- Phase 0: setup --------------------------------------------------------

stage('Setup')
const setup = await spawn(`${CTX}

Phase 0 of ${SKILL}.

1. Read the spec. ${A.repo_override
  ? `The target checkout is ALREADY PREPARED for you at ${A.repo_override} -- use exactly that
   path as repo_path. It is a clean git worktree; do NOT resolve the front-matter 'repo' to
   anywhere else, and do not touch the main checkout.`
  : `Resolve its front-matter 'repo' to a local checkout (~/Documents/Agent/<repo> or
   ~/Documents/<repo>).`} Derive the platform from its git remote: GitHub -> gh, GitLab -> glab.
2. Create the state dir ${ROOT}/.claude/orchestrator/<slug>/ (slugify the spec filename).
3. In the TARGET repo: git fetch, then create a fresh branch off the default branch.
4. Judge two things from the spec and report them honestly:
   - is_ui:      does this spec add or change rendered UI?
   - is_trivial: is this small enough that the Phase 1 tools gate would be theatre?
                 (a bug fix, a refactor, a config change -- NOT a new capability)

Report the real command output for the branch creation.`,
  {
    label: 'setup',
    schema: {
      type: 'object',
      properties: {
        slug: { type: 'string' }, repo_path: { type: 'string' },
        platform: { type: 'string', enum: ['gh', 'glab'] },
        state_dir: { type: 'string' }, branch: { type: 'string' },
        is_ui: { type: 'boolean' }, is_trivial: { type: 'boolean' },
      },
      required: ['slug', 'repo_path', 'platform', 'state_dir', 'branch', 'is_ui', 'is_trivial'],
    },
  })

if (!setup) return { outcome: 'STUCK', reason: 'Phase 0 setup agent failed -- could not resolve the target repo or create the branch' }
const S = setup
const EVIDENCE = S.state_dir
log(`${S.slug}: ${S.repo_path} (${S.platform}) on ${S.branch}${S.is_ui ? ' [UI]' : ''}`)

const parked = (gate, question) => ({
  outcome: 'PARKED', slug: S.slug, branch: S.branch, evidence_path: EVIDENCE,
  gate, question,
})

// --- Phase 0b: SPEC PREFLIGHT (added 2026-07-26) ----------------------------------------------
// Refuse to dispatch a spec whose own factual claims are false. On 2026-07-26 two arms of this
// loop -- 54 and 10 sub-agents, ~3.7M subagent tokens, 85.8 and 46.8 minutes -- both ended STUCK on
// one three-line fix, and NEITHER failure was the loop's. Both traced to a single sentence in the
// spec ("this is the last file with this defect ... there are none") that was false at the base
// commit and contradicted the spec's own commit message. The implementer reasonably fixed both
// affected files; the reviewer reasonably blocked it for scope. An impossible position, created
// before either agent started, by a claim one grep could falsify.
//
// So the claims get run as commands, first, for seconds. Cheapest possible failure: a spec that
// cannot pass its own preflight never spends an agent.
const preflight = await spawn(`${CTX}

Before ANY work begins, check this spec's factual claims against the live tree. Run EXACTLY:

  cd ${ROOT} && PYTHONPATH=. python3 harness/spec_preflight.py "${SPEC}" --repo ${S.repo_path}

Report its verbatim output in 'reason' and set:
  ok=true   if it exits 0 (every bound claim holds, or the spec makes no checkable claims)
  ok=false  if it exits 2 (a claim is FALSE, or a check is malformed)
  ok=true   if it exits 3 (the spec asserts things but binds no checks) -- but say so loudly in
            'reason', prefixed with UNCHECKED-CLAIMS, and name the flagged language.

Do not fix the spec. Do not judge whether the claims SHOULD be true. Report exactly what the tool
said and its exit code.`,
  { label: 'preflight', phase: 'Setup', effort: 'low', schema: VERDICT })

if (preflight && preflight.ok === false) {
  log(`PREFLIGHT FAILED — not dispatching. ${preflight.reason}`)
  return {
    outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: EVIDENCE,
    reason: 'spec preflight failed: a factual claim in the spec is false against the current ' +
            'tree. Fix the spec, not the loop.\n' + (preflight.reason || ''),
    steps: [],
  }
}
if (preflight && /UNCHECKED-CLAIMS/i.test(String(preflight.reason || ''))) {
  log(`preflight: spec asserts things about the codebase but binds no checks — ${preflight.reason}`)
}

// --- Phase 1: tools-first gate --------------------------------------------

if (!S.is_trivial) {
  stage('Tools gate')
  const tools = await spawn(`${CTX}

Phase 1 of ${SKILL} -- the TOOLS-FIRST GATE. This is Michael's #1 rule and is never skipped silently.

Using LIVE web research (never training data), answer:
  1. Is there a tool/library/PAID product that solves most of this outright?
  2. What is the single hardest piece, and is there a tool for it?
  3. What accelerates the ordinary parts?

Pick by EVIDENCE, not existence -- benchmarks, recency, head-to-head. Write an options table
(coverage %, cost including paid tiers, integration effort, trade-offs, recommendation) to
${S.state_dir}/tools-decision.md.

Set needs_human=true if this is a real choice with a cost or architectural consequence that
Michael should make. Set needs_human=false ONLY if the answer is unambiguous (e.g. the obvious
library is already a dependency of this repo).`,
    {
      label: 'tools-gate', schema: {
        type: 'object',
        properties: { needs_human: { type: 'boolean' }, question: { type: 'string' }, decision: { type: 'string' } },
        required: ['needs_human', 'question', 'decision'],
      },
    })
  if (tools && tools.needs_human) return parked('tools-decision', tools.question)
}

// --- Phase 2: plan + plan review ------------------------------------------

stage('Plan')

// Explorers are genuinely independent -> parallel is correct here.
const explorerAngles = [
  'entry points and routes: how does a request/interaction reach the area this spec touches?',
  'the data model: what tables/types/state does this spec read or write, and who else uses them?',
  'existing similar features: what in this repo already does something like this, and how?',
]
const explored = await parallel(explorerAngles.map((angle, i) => () =>
  spawn(`${CTX}

Explore ${S.repo_path} from ONE angle and report what a planner needs to know.

  Your angle: ${angle}

Report concrete file paths, symbol names and line numbers. Do not propose a design; you are
mapping the terrain, not choosing the route.`,
    { label: `explore:${i + 1}`, phase: 'Plan', agentType: 'Explore' })
))

const findings0 = explored.filter(Boolean).join('\n\n---\n\n')

const STEPS_SCHEMA = {
  type: 'object',
  properties: {
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id:              { type: 'string' },
          desc:            { type: 'string' },
          done_definition: { type: 'string' },
          verify:          { type: 'string' },
          durability:      { type: 'string', enum: ['DURABLE', 'DISPOSABLE'] },
          touches_ui:      { type: 'boolean' },
        },
        required: ['id', 'desc', 'done_definition', 'verify', 'durability', 'touches_ui'],
      },
    },
  },
  required: ['steps'],
}

let plan = await spawn(`${CTX}

Phase 2 of ${SKILL}. Turn the spec into ORDERED atomic steps.

Explorer findings:
${findings0}

Each step needs an id, desc, done_definition, and a concrete 'verify' shell command that exits 0
ONLY if the step is truly done. Right-size them -- do NOT over-decompose; split only where
verification needs it.

Mark each verify DURABLE or DISPOSABLE (see Phase 3c in the skill): DURABLE = user-facing behaviour
or a hard constraint. DISPOSABLE = an implementation choice a builder may legitimately improve on.
Default to DISPOSABLE for anything naming a specific library, file path, or helper.

Write the checklist to ${S.state_dir}/plan.md as well as returning it.`,
  { label: 'planner', phase: 'Plan', agentType: 'planner', schema: STEPS_SCHEMA, effort: 'high' })

if (!plan || !plan.steps || !plan.steps.length) {
  return { outcome: 'STUCK', slug: S.slug, evidence_path: EVIDENCE, reason: 'planner produced no steps' }
}

// PLAN REVIEW GATE. One bad line of plan becomes hundreds of bad lines of code.
// Barrier is correct here: the revision needs all four verdicts together.
//
// SKIPPED ON TINY PLANS (2026-07-26). This is the course's own exclusion, not our shortcut: the
// "Automatic Plan Reviewing with Subagents" video (Advanced Techniques / Multi-Agent
// Orchestration) is scoped "not for tiny changes where hook overhead dominates."
//
// Measured cost of ignoring that: on a ONE-step plan for a three-line sys.path fix, four
// high-effort lenses plus the full re-plan they triggered cost ~80k tokens and ~14 minutes -- more
// than the entire fix. "One bad line of plan becomes hundreds of bad lines of code" is true of a
// feature; a one-step plan has no architecture to get wrong, and the per-step reuse and cross-model
// lenses in Phase 3 already read the real diff.
//
// Threshold matches tightenCapForSmallPlan so the two cannot disagree about what "small" means.
const PLAN_REVIEW_MIN_STEPS = 3
if (plan.steps.length < PLAN_REVIEW_MIN_STEPS) {
  log(`plan is ${plan.steps.length} step(s) — skipping the 4-lens plan review ` +
      `(the course scopes it away from tiny changes; Phase 3 still reviews the real diff)`)
}
const planLenses = plan.steps.length < PLAN_REVIEW_MIN_STEPS ? [] : [
  ['architecture', 'Does this plan reuse the repo\'s existing patterns, or invent a competing one? Name the existing pattern it should have adopted.'],
  ['security',     'What does this plan get wrong about auth, secrets, input trust, or data exposure?'],
  ['blast-radius', 'What ALREADY-SHIPPED behaviour does this plan change? Trace outward to call sites the plan never mentions.'],
  ['simpler-path', 'Does a library, an existing helper, or a 10-line version remove most of this plan? Be aggressive.'],
]
const planReviews = await parallel(planLenses.map(([key, question]) => () =>
  spawn(`${CTX}

Review this PLAN before any code is written. Read-only.

  Your lens: ${question}

PLAN:
${JSON.stringify(plan.steps, null, 2)}

Return ok=false if you found something that must change. Be specific and cite file:line from the
real repo at ${S.repo_path}.`,
    { label: `plan-review:${key}`, phase: 'Plan', schema: VERDICT, effort: 'high' })
    .then(v => ({ key, v }))
))

const planProblems = planReviews.filter(Boolean).filter(r => r.v && !r.v.ok)
if (planProblems.length) {
  log(`plan review: ${planProblems.length}/4 lenses flagged problems -- revising`)
  const revised = await spawn(`${CTX}

Revise the plan to address every one of these review findings. Keep what was right; change what
was flagged. Rewrite ${S.state_dir}/plan.md and record what changed and why in
${S.state_dir}/tools-decision.md.

CURRENT PLAN:
${JSON.stringify(plan.steps, null, 2)}

REVIEW FINDINGS:
${planProblems.map(p => `[${p.key}] ${p.v.reason}`).join('\n')}`,
    { label: 'plan-revise', phase: 'Plan', agentType: 'planner', schema: STEPS_SCHEMA, effort: 'high' })
  if (revised && revised.steps && revised.steps.length) plan = revised
}

log(`plan: ${plan.steps.length} steps`)
tightenCapForSmallPlan(plan.steps.length)
PROGRESS.plan_steps = plan.steps.map(st => `${st.id}: ${st.desc}`)

// --- Phase 3: build ---------------------------------------------------------
// SEQUENTIAL on purpose. pipeline() is for independent items; plan steps build
// on each other and touch the same files, so they must not overlap.

stage('Build')
const stepResults = []

for (const step of plan.steps) {
  let done = false
  let attempts = 0
  let feedback = ''

  while (!done && attempts < MAX_ATTEMPTS) {
    attempts++

    const implPrompt = fb => `${CTX}

Implement EXACTLY this ONE step in ${S.repo_path} on branch ${S.branch}. Test-first. Then stop.

  id:              ${step.id}
  desc:            ${step.desc}
  done_definition: ${step.done_definition}
  verify:          ${step.verify}

${step.touches_ui ? `This step touches rendered UI. BEFORE choosing any look, extract this app's OWN
design tokens (colors, fonts, spacing, shared components) from the live repo and design to match.
Brand consistency is the bar; "distinctive" is not a licence to invent a new palette. Your evidence
must name which existing tokens/components you reused.` : ''}

${fb ? `A previous attempt FAILED. Do not repeat it:\n${fb}` : ''}`

    // --- CHEAP CONVERGENCE FIRST (2026-07-26) --------------------------------------------------
    // Until now the expensive review fan-out ran BEFORE anything checked whether the code even
    // executes. Measured consequence: a step that failed its verify three times paid three full
    // review rounds -- ~50k tokens each -- to review code that did not run. That is the whole cost
    // of the 79-minute run.
    //
    // This is the creator of Claude Code's single most-cited tip, applied to our own loop: "build
    // cheap, rapid verification checks to test outputs BEFORE expensive reviews... iterate quickly
    // to catch errors early, reducing token costs and latency." It is also this project's own
    // standing rule -- prefer deterministic over probabilistic -- which we had applied to the
    // solver and never to the harness driving it.
    //
    // So: let the implementer iterate against the step's OWN verify command, judged only by its
    // exit code, for a few rounds. One low-effort agent per round that runs a command and reports
    // an integer. Nothing expensive is spent until the code at least passes its own bar.
    //
    // THIS PROBE CANNOT CERTIFY THE STEP. It decides one thing only: "is this diff worth reviewing
    // yet." The independent high-effort verifier below remains the sole authority on done, still
    // owns hollow-test detection and the DURABLE/DISPOSABLE call, and is unchanged. Collapsing
    // those two roles would hand certification to the cheapest agent in the loop, which is the
    // opposite of what this project exists to enforce.
    let converged = false
    for (let c = 1; c <= CONVERGE_TRIES && !converged; c++) {
      await spawn(implPrompt(feedback), { label: `impl:${step.id}`, phase: 'Build', agentType: 'implementer' })

      const probe = await spawn(`${CTX}

Run EXACTLY this command in ${S.repo_path} and report what happened. Nothing else.

  ${step.verify}

Report passes=true ONLY if it exited 0. Put the last ~40 lines of real output in 'output',
including failures verbatim.

Do NOT fix anything. Do NOT review the code. Do NOT amend the verify command even if it looks
wrong -- that judgement belongs to the certifying verifier later in this phase, not to you. You are
a thermometer, not a doctor.`,
        { label: `probe:${step.id}#${c}`, phase: 'Build', effort: 'low',
          schema: { type: 'object',
            properties: { passes: { type: 'boolean' }, output: { type: 'string' } },
            required: ['passes', 'output'] } })

      converged = !!(probe && probe.passes)
      if (!converged) {
        feedback = `Its own verify command still fails:\n${(probe && probe.output) || 'probe agent returned nothing'}`
        log(`${step.id} converge ${c}/${CONVERGE_TRIES}: verify still failing (no review spent yet)`)
      } else if (c > 1) {
        log(`${step.id} converged on its own verify after ${c} cheap rounds — now reviewing`)
      }
    }
    if (!converged) {
      // Deliberately still review once: a diff that cannot pass its own command usually has a
      // diagnosable reason, and the reuse/cross-model lenses are what catch "you used the wrong
      // pattern" -- the exact failure that made this step fail 3x on 2026-07-26 (the plan
      // adjudicated re-derive; the implementer used config.REPO). One round, not one per attempt.
      log(`${step.id} did NOT converge in ${CONVERGE_TRIES} cheap rounds — reviewing once for diagnosis`)
    }

    // Review fan-out. The personas are attention steering, not redundancy --
    // each is prompted into a different failure mode.
    const reviewPrompt = `${CTX}

Review the diff on branch ${S.branch} in ${S.repo_path} for step ${step.id} (${step.desc}).
Run: git diff $(git merge-base HEAD origin/HEAD)...HEAD`

    const lenses = [
      () => spawn(`${reviewPrompt}

Perform a thorough multi-angle code review. If the Skill tool is available to you, invoke
/code-review at high effort over this diff and report its findings; otherwise do the equivalent
review yourself: correctness, error handling, boundary conditions, and regressions in call sites
the diff did not open.`, { label: `review:${step.id}`, phase: 'Build', schema: FINDINGS, effort: 'high' }),

      // THE OUTSIDE VOICE. Everything else in this loop is Claude reviewing Claude
      // refuting Claude -- the echo-chamber failure mode. The course runs its
      // adversarial review on a different model ("I like to have Codex do review
      // instead"); Codex is not installed here, so this routes through OpenRouter.
      // Auto-tiers gpt-5.6-luna -> gpt-5.6-sol on high-stakes FILE PATHS.
      () => spawn(`${CTX}

Get a CROSS-MODEL review of step ${step.id} on branch ${S.branch}. Run:

  cd ${S.repo_path} && git diff $(git merge-base HEAD origin/HEAD)...HEAD \\
    | python3 ${ROOT}/code-tools/review/openrouter_review.py \\
        --repo ${S.repo_path} --context "${step.desc.replace(/"/g, "'").slice(0, 200)}"

Return its findings verbatim. If it exits non-zero or prints {"error":...}, put that in 'reason' and
return an EMPTY findings list — never invent findings, and never report the diff as clean on the
strength of a reviewer that failed to run.`,
        { label: `cross-model:${step.id}`, phase: 'Build', schema: FINDINGS }),

  // THE REUSE LENS. Every lens above hunts for BUGS; none of them asks whether the
  // diff hand-rolled something the repo already has. Measured 2026-07-26: two of
  // three approaches to one bug re-derived a repo-root path that already existed as
  // `config.REPO`, and five bug reviewers passed it because it was CORRECT. A
  // blind run of this lens found it and named the file:line.
  //
  // Emits MEDIUM, not LOW, on HIGH confidence -- a LOW here would be filtered out
  // by the severity bar on exactly the ordinary diffs where this defect lives.
  () => spawn(`${CTX}

Review the diff on branch ${S.branch} in ${S.repo_path} for REUSE only:

  cd ${S.repo_path} && git diff $(git merge-base HEAD origin/HEAD)...HEAD

Follow your agent instructions exactly. Assume the diff is correct -- other reviewers own that.
Report ONLY things it hand-rolled that this repository already provides, and only when you can name
the file:line of what it should have used instead.

Map your output into the findings schema: 'what' = what was hand-rolled AND what to use instead
(quote the file:line), 'where' = the location in the diff, 'severity' = MEDIUM for HIGH confidence,
LOW for anything less. Return an empty findings list if it reuses correctly -- that is a real answer.`,
    { label: `reuse:${step.id}`, phase: 'Build', agentType: 'code-simplifier', schema: FINDINGS }),
    ]

    // NO PERSONA FAN-OUT HERE. This is deliberate and it is the course's design.
    //
    // The four-persona pass belongs on the PLAN, once, before any code exists --
    // Advanced Techniques / Multi-Agent Orchestration, "Automatic Plan Reviewing
    // with Subagents." It runs specialised reviewers (his four: accessibility
    // tester, architect reviewer, penetration tester, performance engineer) over
    // the plan, folds their feedback into a second draft, and then:
    //
    //   "rather than having multiple subagents implement a plan, you would have
    //    multiple subagents critiquing a plan from different perspectives, all that
    //    feedback being incorporated into a second draft, and then just having ONE
    //    session execute on that plan"
    //
    // and, explicitly: "Personally, I don't like having subagents have different
    // roles in terms of actually implementing things."
    //
    // We had it on every step's DIFF, on every attempt -- 4 personas x steps x up to
    // 3 attempts. On 2026-07-26 that cost 78 agents and 126 minutes to change six
    // lines of lock.py. The rationale for personas is CONTEXT FOCUS (one session
    // giving security AND react AND perf advice gets worse from context-switching),
    // and that rationale is spent once on the plan; repeating it per diff buys
    // almost nothing because each step's diff is small enough for one reviewer to
    // hold entirely.
    //
    // Our plan review (Phase 2) already carries four domain lenses: architecture,
    // security, blast-radius, simpler-path. That is where this lives now.
    //
    // What stays per-step is what a plan review CANNOT do, because it runs before
    // the code exists: read the actual diff for correctness, get an outside model to
    // disagree, and check the code against what the repo already provides.
    //
    // The one exception is a UI step, which needs eyes on rendered output rather
    // than on a diff -- that is the Flows phase (browser verify + design-reviewer),
    // not a persona here.
    //
    // The tier decision below is NOT a persona gate. It survives because it sets the
    // severity bar for the refute pass: on a money/auth/concurrency diff every
    // finding gets verified, on an ordinary one the bar is MEDIUM+. It is one
    // low-effort agent and it calls no model of its own.
    const tier = await spawn(`${CTX}

Decide the review tier for step ${step.id}. Run EXACTLY this and return its JSON fields verbatim:

  cd ${S.repo_path} && git diff $(git merge-base HEAD origin/HEAD)...HEAD \\
    | python3 ${ROOT}/code-tools/review/openrouter_review.py --repo ${S.repo_path} --decide-only

Do NOT substitute your own judgement, and do NOT decide from the step description — a description
that merely MENTIONS a dangerous word is not a dangerous change, and deciding from prose is what made
a six-line import fix take the most expensive review path. If it exits non-zero, report
high_stakes=true: an undecidable diff gets the full review, never the cheap one.`,
      { label: `tier:${step.id}`, phase: 'Build', effort: 'low',
        schema: { type: 'object',
          properties: { high_stakes: { type: 'boolean' }, tier_reason: { type: 'string' } },
          required: ['high_stakes', 'tier_reason'] } })

    const risky = tier ? tier.high_stakes !== false : true
    log(`${step.id} refute bar ${risky ? 'ALL findings' : 'MEDIUM+'} — ${tier ? tier.tier_reason : 'tier decision failed, failing safe'}`)

    const reviews = (await parallel(lenses)).filter(Boolean)

    const raw = reviews.flatMap(r => (r && r.findings) || [])
    const real = raw.length ? await refute(raw, 'Build', { highStakes: risky }) : []
    log(`${step.id} attempt ${attempts}: ${raw.length} findings -> ${real.length} survived refute`)

    if (real.length) {
      await runFixer(real, 'Build', `Work in ${S.repo_path} on branch ${S.branch}. Step: ${step.desc}`)
    }

    // Gate: an independent verifier RUNS the verify command. The implementer
    // never certifies its own work.
    const gate = await spawn(`${CTX}

Certify step ${step.id} in ${S.repo_path} on branch ${S.branch}.

  done_definition: ${step.done_definition}
  verify:          ${step.verify}
  this verify is:  ${step.durability}

RUN the verify command. Its exit code is ground truth. Also check the test would FAIL if the code
were wrong -- a test that passes regardless is a hollow test.

If verify FAILS, classify the failure using Phase 3c of ${SKILL}:
  - DURABLE  -> the failure is real. ok=false.
  - DISPOSABLE -> the PLAN is stale, not the code (the plan was written before any code existed).
    Amend the verify to match what was actually learned, record why in
    ${S.state_dir}/steps/${step.id}.json, and return ok=true with the amendment in 'reason'.

Write ${S.state_dir}/steps/${step.id}.json with the REAL command output either way.`,
      { label: `verify:${step.id}`, phase: 'Build', agentType: 'reviewer', schema: VERDICT, effort: 'high' })

    // Spec-clause coverage used to be gated here by persona-auditor, per step. It
    // moved to ONE spec-audit pass after the whole build loop (below): a clause can
    // legitimately be unsatisfied at step 2 and satisfied by step 5, so auditing
    // per step both fails spuriously and pays for the audit N times.
    done = !!(gate && gate.ok)
    if (!done) {
      feedback = (gate && gate.reason) || 'verify did not pass'
      log(`${step.id} attempt ${attempts} NOT done: ${feedback}`)
    }
  }

  stepResults.push({ id: step.id, done, attempts })
  PROGRESS.steps_done.push(`${step.id} ${done ? 'DONE' : 'NOT-DONE'} after ${attempts} attempt(s)`)
  if (!done) {
    return {
      outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: EVIDENCE,
      reason: `step ${step.id} (${step.desc}) failed a DURABLE verify ${MAX_ATTEMPTS} times`,
      steps: stepResults,
    }
  }
}

// --- Phase 4: security ------------------------------------------------------

stage('Security')
const sec = await spawn(`${CTX}

Phase 4 of ${SKILL}. Run a branch-scoped security pass over ${S.branch} in ${S.repo_path}.
If the Skill tool is available, invoke /security-review; otherwise review the diff yourself for
newly-introduced vulnerabilities (auth bypass, injection, IDOR, privilege escalation, secret
exposure, unsafe deserialization).

Report only vulnerabilities this branch INTRODUCES -- not pre-existing ones.`,
  { label: 'security', phase: 'Security', schema: FINDINGS, effort: 'high' })

const secReal = sec && sec.findings && sec.findings.length
  ? await refute(sec.findings, 'Security', { highStakes: true })  // security findings never get a severity bar
  : []
if (secReal.length) await runFixer(secReal, 'Security', `Work in ${S.repo_path} on branch ${S.branch}.`)

// --- Phase 5: user-flow verify ---------------------------------------------

if (S.is_ui) {
  stage('Flows')
  const flows = await spawn(`${CTX}

Phase 5 of ${SKILL}. Identify the new/changed USER FLOWS on branch ${S.branch}, then verify each
against the LOCAL dev stack via Claude-in-Chrome. Start the stack as the skill describes and seed
test data freely.

For each flow: drive the REAL routed component (never a hand-rolled harness), screenshot at normal
AND narrow widths, use the JavaScript tool to confirm computed state (visible, no console errors,
no overlap or clipping), and GIF-record it into ${S.state_dir}/flows/.

Return ok=false with specifics if any flow does not work. List every screenshot path you wrote.`,
    {
      label: 'user-flows', phase: 'Flows', effort: 'high',
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' }, reason: { type: 'string' },
          screenshots: { type: 'array', items: { type: 'string' } },
        },
        required: ['ok', 'reason', 'screenshots'],
      },
    })

  if (flows && flows.screenshots && flows.screenshots.length) {
    const design = await spawn(`${CTX}

Judge whether this UI looks GOOD and on-brand. A separate agent already confirmed it FUNCTIONS.

Screenshots:
${flows.screenshots.join('\n')}

Repo: ${S.repo_path}, branch ${S.branch}.

Apply the \`frontend-design\` skill's bar (installed as the official plugin
\`frontend-design@claude-plugins-official\` -- invoke it, or read its SKILL.md under
~/.claude-sz/plugins/cache/claude-plugins-official/frontend-design/).
ORDER MATTERS: that skill optimises for DISTINCTIVE interfaces that avoid generic AI aesthetics.
This is an EXISTING app, so the app's own design tokens and shared components outrank distinctiveness
-- extract them first and judge craft WITHIN them. A beautiful screen that looks unlike every other
screen in the app is a FAIL, not a win.`,
      { label: 'design-review', phase: 'Flows', agentType: 'design-reviewer' })

    const uiFindings = []
      .concat(flows.ok ? [] : [{ what: flows.reason, where: 'user flow', severity: 'HIGH' }])
      .concat((design && design.findings) || [])
      .filter(f => f.severity === 'HIGH' || f.severity === 'MEDIUM')

    if (uiFindings.length) {
      await runFixer(uiFindings, 'Flows', `Work in ${S.repo_path} on branch ${S.branch}. These are UI findings from a real browser session.`)
    }
  }
}

// --- Phase 6: artifacts + PR ------------------------------------------------

// stop_after_review exists for MEASUREMENT: it ends the run at "verified,
// reviewed, on a branch" so this loop can be compared against lifecycle-fix or a
// plain prompt at the SAME milestone, without one arm also paying for artifacts,
// a PR and a Greptile poll. It is NOT a shortcut for real work.
if (A.stop_after_review) {
  return {
    outcome: 'PARKED', slug: S.slug, branch: S.branch, evidence_path: S.state_dir,
    gate: 'stop_after_review',
    question: `Verified and reviewed on ${S.branch} in ${S.repo_path}. stop_after_review was set, so no artifacts or PR.`,
  }
}

stage('PR')
const pr = await spawn(`${CTX}

Phase 6 of ${SKILL}. Produce the artifacts and open the PR/MR for ${S.branch} in ${S.repo_path}
using ${S.platform}.

Artifacts into ${S.state_dir}/:
  - an HTML page explaining what changed and WHY, in plain language, written for Michael (not for
    reviewers). What it does and why it matters first.
  - a mermaid diagram of the change.
  - link the flow GIFs in ${S.state_dir}/flows/ if any exist.

Open the PR with the artifacts linked. Return the PR number and URL.`,
  {
    label: 'pr-open', phase: 'PR',
    schema: {
      type: 'object',
      properties: { pr_number: { type: 'string' }, pr_url: { type: 'string' }, ok: { type: 'boolean' } },
      required: ['pr_number', 'pr_url', 'ok'],
    },
  })

if (!pr || !pr.ok) {
  return { outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: EVIDENCE, reason: 'could not open the PR/MR' }
}

// --- Phase 7: Greptile loop -------------------------------------------------

stage('Greptile')
let merged = false
let greptileNote = ''

for (let round = 1; round <= MAX_ATTEMPTS; round++) {
  const g = await spawn(`${CTX}

Phase 7 of ${SKILL} -- round ${round}. Read that phase IN FULL before acting; it documents the
exact bug that stalled this queue for ten hours and you must not repeat it.

PR ${pr.pr_number} (${pr.pr_url}) in ${S.repo_path}, platform ${S.platform}.

The essentials, but follow the skill for the detail:
  - A push does NOT trigger a re-review. Comment "@greptileai" after every push.
  - Poll the CHECK RUN on the head SHA -- that is the authoritative signal, NOT /reviews. A clean
    re-review creates no review object and no comments; it only updates the check run.
  - Findings live in the LINE-LEVEL comments (pulls/<n>/comments), not the review body.
  - Always compare a review's commit_id to .head.sha. A review of a superseded commit is not a
    review of what you are about to merge.
  - Distinguish CLEAN (green check run on this SHA, started after your push) from SILENT (no check
    run at all). Never read silence as clean.
  - Wait while status is in_progress (it took ~9.5 min on a 31-file PR). No check run 10 minutes
    after the trigger comment = INFRASTRUCTURE failure -> return state="infra".

Return:
  state="clean"  -> a check run on the current head SHA came back with zero open MEDIUM/HIGH findings
  state="findings" -> list them (triage each to HIGH/MEDIUM/LOW yourself if Greptile did not)
  state="infra"  -> the review never ran`,
    {
      label: `greptile:r${round}`, phase: 'Greptile', effort: 'high',
      schema: {
        type: 'object',
        properties: {
          state:    { type: 'string', enum: ['clean', 'findings', 'infra'] },
          note:     { type: 'string' },
          findings: FINDINGS.properties.findings,
        },
        required: ['state', 'note'],
      },
    })

  if (!g) { greptileNote = 'greptile agent failed'; break }
  greptileNote = g.note

  if (g.state === 'infra') {
    return parked('greptile-infrastructure', `Greptile never re-reviewed PR ${pr.pr_url}. ${g.note}`)
  }
  if (g.state === 'clean') { merged = true; break }

  const blocking = (g.findings || []).filter(f => f.severity !== 'LOW')
  if (!blocking.length) { merged = true; break }   // LOW/nit findings do not block the merge

  const realG = await refute(blocking, 'Greptile', { highStakes: true })  // Greptile already filtered; verify all of it
  if (!realG.length) { merged = true; break }

  await runFixer(realG, 'Greptile', `Work in ${S.repo_path} on branch ${S.branch}, then PUSH.
After pushing you MUST comment "@greptileai" on PR ${pr.pr_number} to trigger the re-review.`)
}

// --- Phase 8: blast radius --------------------------------------------------

stage('Blast')
const blast = await spawn(`${CTX}

Phase 8 of ${SKILL}. Assess the merge blast radius of PR ${pr.pr_number} in ${S.repo_path}.
Write your verdict to ${S.state_dir}/blast-verdict.json AND append it to ${ROOT}/specs/SCORECARD.md.
Record it even though a Greptile-clean PR no longer gates on it -- the scorecard calibrates you
against Michael's real decisions.`,
  { label: 'blast-radius', phase: 'Blast', agentType: 'blast-radius-assessor' })

if (!merged) {
  return parked('greptile-exhaustion',
    `PR ${pr.pr_url} still has open MEDIUM/HIGH findings after ${MAX_ATTEMPTS} rounds. ${greptileNote}`)
}

// Escape hatch for shakedown runs: stop at a reviewed, Greptile-clean PR and
// let a human press merge. Normal queue runs leave this unset and auto-merge.
if (A.stop_before_merge) {
  return {
    outcome: 'PARKED', slug: S.slug, branch: S.branch, pr_url: pr.pr_url, evidence_path: EVIDENCE,
    gate: 'manual-merge-requested',
    question: `PR ${pr.pr_url} is Greptile-clean and ready. stop_before_merge was set, so it was NOT merged.`,
    steps: stepResults, blast: blast && blast.verdict,
  }
}

// Merge on clean. The caller handles Slack and the post-merge watch -- an
// hour-long log monitor must never block the queue.
const mergeRes = await spawn(`${CTX}

PR ${pr.pr_number} (${pr.pr_url}) came back Greptile-clean on the current head SHA. Merge it using
${S.platform} in ${S.repo_path}.

Then determine what merging DEPLOYS: if merge publishes to production (e.g. Vercel on main for
platform-repo), name the deploy target and where its logs live (Vercel / CloudWatch /
Lambda / Supabase) so the caller can start the post-merge watch. Verify, do not assume.

Finally write ${S.state_dir}/RESULT.md: PR link, artifacts, and the REAL evidence (command output).`,
  {
    label: 'merge', phase: 'Blast',
    schema: {
      type: 'object',
      properties: {
        ok: { type: 'boolean' }, reason: { type: 'string' },
        deploy_target: { type: 'string' }, log_source: { type: 'string' },
      },
      required: ['ok', 'reason'],
    },
  })

if (!mergeRes || !mergeRes.ok) {
  return parked('merge-failed', `Greptile was clean but the merge did not complete: ${mergeRes ? mergeRes.reason : 'merge agent failed'}`)
}

return {
  outcome: 'DONE',
  slug: S.slug,
  branch: S.branch,
  pr_url: pr.pr_url,
  evidence_path: EVIDENCE,
  steps: stepResults,
  blast: blast && blast.verdict,
  monitor: {
    target: mergeRes.deploy_target || 'none',
    log_source: mergeRes.log_source || 'none',
    minutes: 60,
  },
}
