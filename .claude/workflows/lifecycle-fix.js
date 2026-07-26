export const meta = {
  name: 'lifecycle-fix',
  description: 'The SMALL L3 loop: diagnose one bug to root cause, fix it test-first, verify, one review round, PR',
  whenToUse: 'For bug-fix / red-test / refactor specs. NOT for new features — those go to lifecycle-run.',
  phases: [
    { title: 'Setup',    detail: 'resolve repo, state dir, branch' },
    { title: 'Diagnose', detail: 'reproduce RED, trace to root cause' },
    { title: 'Fix',      detail: 'test-first, smallest diff' },
    { title: 'Verify',   detail: 'independent verifier runs the checks' },
    { title: 'Review',   detail: 'one adversarial round, refute, fix' },
    { title: 'PR',       detail: 'open PR/MR' },
    { title: 'Greptile', detail: 'trigger, poll check-run, fix, merge on clean' },
  ],
}

// ---------------------------------------------------------------------------
// WHY THIS FILE EXISTS (2026-07-26)
//
// The Loopy AI course designs TWO L3 loops, not one. The big one is for
// implementing features; of the other it says: "we can also make another loop
// for like debugging as well. But that would generally be much smaller and less
// complicated because you would just be going back and forth between some
// explore agents, builders, and verifiers."
//
// We had only the feature loop. Running a bug-fix spec through it cost
// 1.7M tokens and 36 minutes to add SIX LINES to harness/szloop/lock.py --
// because the fix inherited the tools-first web-research gate, 3 explorers, 4
// opus plan reviewers, a four-persona pass per step, a security pass, an HTML
// artifact + mermaid diagram, and a blast-radius assessment.
//
// So this loop deliberately DROPS: the tools gate (you do not research
// buy-vs-build to fix a bug you already understand), the 4-lens plan review
// (there is no plan to review -- there is a root cause), the prototype gate,
// the persona fan-out by default, and the artifact ceremony.
//
// It KEEPS the things that stop overclaiming, because those are not overhead:
// an independent verifier that RUNS the command, refute-before-fix, and the
// Greptile merge gate.
// ---------------------------------------------------------------------------

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})

const ROOT = A.root || '/path/to/repo'
const SPEC = A.spec
if (!SPEC) throw new Error('lifecycle-fix requires args.spec (absolute path to the spec .md); got: ' + JSON.stringify(args))

const SKILL = ROOT + '/.claude/skills/lifecycle/SKILL.md'
const MAX_ATTEMPTS = 3

// --- Hard ceiling on sub-agents (added 2026-07-26) --------------------------------------------
// Same ceiling as lifecycle-run.js, and it is the SAME CODE ON PURPOSE. The lesson from the two
// PreToolUse guards on 2026-07-26 was that a safety mechanism copied into two files silently
// diverges and the un-narrowed copy is the one that fires. Change both or neither.
//
// This loop is the CHEAP arm -- it used 10 agents where the full loop used 34 on the same fix --
// so the cap here is lower. It still needs one: 10 was the happy path, not a bound.
//
// Agent COUNT, not wall-clock: Date.now() is unavailable inside a workflow script (it would break
// resume), and agent count is what tracks spend. On breach spawn() THROWS and the run aborts, with
// the message surfacing in the tool result. Partial work survives in the branch.
let AGENT_CAP = Number(A.agent_cap) || 20
let agentsUsed = 0
class CapExceeded extends Error {}
// The record the degraded exit reads -- kept identical in shape to lifecycle-run.js on purpose
// (same reasoning, same field names, so one reader handles both). It is an explicit module-level
// object rather than reading the phase-local bindings, because those are `let`/`const` in the
// script body: if the ceiling is hit before they initialise, touching them throws a ReferenceError
// out of the temporal dead zone and the degraded exit becomes a second, more confusing crash.
//
// Why this exists at all (2026-07-26, run wf_a34c2778-e5d on the sibling script): the cap fired
// after 67.5 min / 2.53M tokens and the exception discarded EVERYTHING -- plan, five implementer
// attempts, 26 completed review agents -- leaving zero product-code changes in the worktree. A
// ceiling should cost the remaining work, not the work already done.
const PROGRESS = { phase: 'init', plan_steps: null, steps_done: [], findings: [] }

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
      `\nTo continue: resume with a higher agent_cap (resumeFromRunId replays completed agents ` +
      `from cache, so the spend above is not repeated).`)
}


// --- Watchdog on silent agents --------------------------------------------------------------
// SAME CODE as lifecycle-run.js, deliberately (see the AGENT_CAP note above). Origin: three
// Explore agents in the 2026-07-26 run burned 13.4 minutes emitting ~0 output tokens, then did the
// job in 2.5 on respawn -- 17% of that run's wall clock, recoverable at zero quality cost.
// Sandbox probe wf_469d92dc-7bc confirmed setTimeout + Promise.race exist (Date.now/performance do
// not, and a relative bell needs neither, so resume stays deterministic).
// A timed-out agent is ABANDONED, not killed -- it may keep spending -- but it counts against the
// cap, so a run that keeps timing out terminates instead of looping.
const AGENT_TIMEOUT_MS = Number(A.agent_timeout_ms) || 6 * 60 * 1000
const WATCHDOG_RETRIES = 1
const TIMED_OUT = { timedOut: true }

async function withWatchdog(prompt, opts) {
  const label = (opts && opts.label) || 'agent'
  for (let attempt = 0; attempt <= WATCHDOG_RETRIES; attempt++) {
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
  return null
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

const CTX = `
You are one stage of the the project BUG-FIX loop (the small L3 loop). You are blind to memory and
to CLAUDE.md.

Read these before acting:
  - ${SPEC}             <- the bug being fixed. Its "Done bar" is the contract.
  - ${SKILL}            <- lifecycle conventions (evidence rules, DURABLE/DISPOSABLE, Phase 7)
  - ${ROOT}/STATE.md    <- what the system currently IS. Never assert from priors.

Standing rules:
  - Smallest diff that fully and correctly fixes the ROOT CAUSE. No speculative abstraction, no
    "while I'm here" refactors. Never skip error handling or boundary validation to save lines.
  - **Fix the cause, not the symptom.** A fix that makes the test pass without explaining WHY it
    was failing is not a fix. If you cannot say what the mechanical cause was, you are not done.
  - Investigate before you touch it: git history, tests, comments. Learn why the code is like this.
  - Unrelated bugs are REPORTED, never fixed in this diff.
  - Evidence, not assertions: real commands and their real output, INCLUDING failures.
  - "Done" is an exit code, not a claim.
`.trim()

const VERDICT = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, reason: { type: 'string' } },
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
          what: { type: 'string' }, where: { type: 'string' },
          severity: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
        },
        required: ['what', 'where', 'severity'],
      },
    },
  },
  required: ['findings'],
}

// Findings are attacked before they are fixed. A reviewer that agrees with itself
// is an echo chamber -- the refuter is prompted to DISAGREE.
//
// Two things keep this from becoming the cost centre it was in the feature loop:
// duplicate REPORTS of one defect collapse to a single refuter, and on an
// ordinary diff the bar is MEDIUM+ (the course: severity threshold OR round cap,
// whichever comes first). `opts.highStakes` drops the bar to everything, because
// on a money/auth/concurrency diff a LOW-looking finding is how incidents start.
// Nothing is dropped silently -- every exclusion is log()ged with its reason.
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

async function refute(findings, phaseName, opts) {
  const highStakes = !!(opts && opts.highStakes)

  const unique = dedupeFindings(findings)
  if (findings.length > unique.length) {
    log(`refute: ${findings.length} report(s) -> ${unique.length} distinct defect(s)`)
  }

  const dropped = highStakes ? [] : unique.filter(f => String(f.severity).toUpperCase() === 'LOW')
  const kept = highStakes ? unique : unique.filter(f => String(f.severity).toUpperCase() !== 'LOW')
  if (dropped.length) {
    log(`refute: ${dropped.length} LOW finding(s) NOT verified (bar is MEDIUM+ on a non-high-stakes diff): ` +
        dropped.map(f => `${f.where} ${String(f.what).slice(0, 60)}`).join(' | '))
  }

  const votes = await parallel(kept.slice(0, 12).map((f, i) => () =>
    spawn(`${CTX}

Adversarially REFUTE this finding. Default to it being WRONG until the code proves otherwise.

  finding:  ${f.what}
  location: ${f.where}

Open the actual code. Can the described failure occur on a reachable path with realistic inputs?
Return ok=true only if you could NOT refute it. Cite file:line.`,
      { label: `refute:${i}`, phase: phaseName, schema: VERDICT,
        effort: (highStakes || String(f.severity).toUpperCase() === 'HIGH') ? 'high' : undefined })
      .then(v => ({ f, real: v && v.ok }))
  ))
  if (kept.length > 12) log(`NOTE: ${kept.length - 12} finding(s) beyond the 12 cap were NOT verified`)
  // Survivors are recorded so a ceiling breach does not discard review work already paid for.
  const survivors = votes.filter(Boolean).filter(v => v.real).map(v => v.f)
  for (const f of survivors) PROGRESS.findings.push(`[${phaseName}] ${f.what || JSON.stringify(f).slice(0,160)}`)
  return survivors
}

// --- Setup ------------------------------------------------------------------

stage('Setup')
const S = await spawn(`${CTX}

1. Read the spec. ${A.repo_override
  ? `The target checkout is ALREADY PREPARED at ${A.repo_override} -- use exactly that as repo_path.
   It is a clean git worktree. Do NOT touch the main checkout.`
  : `Resolve its front-matter 'repo' to a local checkout (~/Documents/Agent/<repo> or ~/Documents/<repo>).`}
   Derive the platform from the git remote: GitHub -> gh, GitLab -> glab.
2. Create the state dir ${ROOT}/.claude/orchestrator/<slug>/ (slugify the spec filename).
3. In the TARGET repo: git fetch, then a fresh branch off the default branch.
4. Read the spec's Constraints section and report any gate command it names (e.g. a moat/sentinel
   script that must stay green) as 'extra_gate' -- verbatim, or "" if none.

Report the real command output for the branch creation.`,
  {
    label: 'setup',
    schema: {
      type: 'object',
      properties: {
        slug: { type: 'string' }, repo_path: { type: 'string' },
        platform: { type: 'string', enum: ['gh', 'glab'] },
        state_dir: { type: 'string' }, branch: { type: 'string' },
        extra_gate: { type: 'string' },
      },
      required: ['slug', 'repo_path', 'platform', 'state_dir', 'branch', 'extra_gate'],
    },
  })

if (!S) return { outcome: 'STUCK', reason: 'setup failed — could not resolve the repo or branch' }
log(`${S.slug}: ${S.repo_path} (${S.platform}) on ${S.branch}${S.extra_gate ? ' [gate: ' + S.extra_gate + ']' : ''}`)

const parked = (gate, question) => ({
  outcome: 'PARKED', slug: S.slug, branch: S.branch, evidence_path: S.state_dir, gate, question,
})

// --- SPEC PREFLIGHT ---------------------------------------------------------
// SAME GATE as lifecycle-run.js. On 2026-07-26 both loops ran the same spec and both ended STUCK,
// and neither failure was the loop's: one false sentence in that spec ("this is the last file with
// this defect ... there are none") put the implementer in an impossible position before it started.
// This loop was arm B; it burned 10 agents and 46.8 minutes on it. Run the spec's claims as
// commands first -- seconds, not minutes.
const preflight = await spawn(`${CTX}

Before ANY work begins, check this spec's factual claims against the live tree. Run EXACTLY:

  cd ${ROOT} && PYTHONPATH=. python3 harness/spec_preflight.py "${SPEC}" --repo ${S.repo_path}

Report its verbatim output in 'reason' and set:
  ok=true   if it exits 0, ok=false if it exits 2 (a claim is FALSE or a check is malformed),
  ok=true   if it exits 3 (asserts things but binds no checks) -- prefix 'reason' with
            UNCHECKED-CLAIMS and name the flagged language.

Do not fix the spec. Report exactly what the tool said and its exit code.`,
  { label: 'preflight', phase: 'Setup', effort: 'low', schema: VERDICT })

if (preflight && preflight.ok === false) {
  log(`PREFLIGHT FAILED — not dispatching. ${preflight.reason}`)
  return {
    outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: S.state_dir,
    reason: 'spec preflight failed: a factual claim in the spec is false against the current ' +
            'tree. Fix the spec, not the loop.\n' + (preflight.reason || ''),
  }
}
if (preflight && /UNCHECKED-CLAIMS/i.test(String(preflight.reason || ''))) {
  log(`preflight: spec asserts things about the codebase but binds no checks — ${preflight.reason}`)
}

// --- Diagnose ---------------------------------------------------------------
// Two angles, not three, and both are pointed at THIS bug rather than at
// mapping the codebase. The course's debug loop is explore -> build -> verify.

stage('Diagnose')
const diagnosis = await parallel([
  () => spawn(`${CTX}

REPRODUCE the failure in ${S.repo_path} on branch ${S.branch}, and paste the REAL output. RED
evidence first — before anyone proposes a fix.

Then trace it to its MECHANICAL root cause: the exact file:line and the exact reason. Not "an import
problem" — which import, resolved how, failing under which invocation.`,
    { label: 'reproduce', phase: 'Diagnose', effort: 'high' }),

  () => spawn(`${CTX}

Find the PRIOR ART for this fix in ${S.repo_path}. Somewhere in this repo the same problem has
almost certainly been solved already — an existing idiom, bootstrap, helper or pattern.

Report the idiom with file:line so the fixer adopts it instead of inventing a competing one. Also
report any SIBLING code with the same latent defect (the spec may name some; find the rest).`,
    { label: 'prior-art', phase: 'Diagnose', agentType: 'Explore' }),
])

const [repro, priorArt] = diagnosis
if (!repro) return { outcome: 'STUCK', slug: S.slug, evidence_path: S.state_dir, reason: 'could not reproduce the bug — cannot fix what will not fail' }

// --- Fix + Verify -----------------------------------------------------------
// One unit of work, retried up to 3x against an independent verifier. No plan
// document, because the diagnosis IS the plan.

stage('Fix')
let done = false
let attempts = 0
let feedback = ''
let lastReason = ''

while (!done && attempts < MAX_ATTEMPTS) {
  attempts++

  await spawn(`${CTX}

Fix this bug in ${S.repo_path} on branch ${S.branch}. Test-first: bind a test that FAILS before your
fix and PASSES after, and show both.

ROOT CAUSE (from the diagnosis):
${repro}

PRIOR ART — adopt this idiom rather than inventing one:
${priorArt || '(none found; say so and justify your approach)'}

${S.extra_gate ? `The spec names a gate that must stay green: ${S.extra_gate}\n` : ''}
${feedback ? `A previous attempt FAILED. Do not repeat it:\n${feedback}` : ''}

Fix the sibling occurrences the diagnosis identified, since they are the same defect — but nothing
beyond that. Report anything else you notice.`,
    { label: `fix:${attempts}`, phase: 'Fix', agentType: 'implementer' })

  stage('Verify')

  // CHEAP PROBE BEFORE THE EXPENSIVE CERTIFY (2026-07-26). The certifier below is a high-effort
  // reviewer -- the most expensive agent in this loop -- and it ran on every attempt, including
  // attempts whose fix does not even make the reproduction stop reproducing. One low-effort agent
  // running one command answers "is this worth certifying yet" for a fraction of the cost.
  // Boris Cherny's most-cited tip is exactly this shape: cheap verification before expensive
  // review. It is also this repo's own rule -- prefer deterministic over probabilistic -- which we
  // had applied to the solver and never to the loop driving it.
  //
  // It CANNOT certify. It reports an exit code; the reviewer below remains the sole authority on
  // done and still owns hollow-test detection and the cause-vs-symptom judgement.
  const probe = await spawn(`${CTX}

The diagnosis below contains a command that REPRODUCED this bug. Extract that command, run it in
${S.repo_path} on branch ${S.branch}, and report what happened. Nothing else.

DIAGNOSIS:
${repro}

Report passes=true ONLY if the bug no longer reproduces (the command now exits 0, or fails for a
reason clearly unrelated to this bug -- say which). Put the last ~40 lines of real output in
'output', failures verbatim.

Do NOT fix anything. Do NOT review the code. Do NOT judge whether the fix is GOOD -- that belongs to
the certifier. You are a thermometer, not a doctor.`,
    { label: `probe:${attempts}`, phase: 'Verify', effort: 'low',
      schema: { type: 'object',
        properties: { passes: { type: 'boolean' }, output: { type: 'string' } },
        required: ['passes', 'output'] } })

  if (probe && probe.passes === false) {
    feedback = `The bug still reproduces after your fix:\n${probe.output || '(no output)'}`
    lastReason = 'reproduction still reproduces (cheap probe); no certifier spent'
    log(`attempt ${attempts}: bug still reproduces — skipping the high-effort certify this round`)
    continue
  }

  const gate = await spawn(`${CTX}

Certify this fix in ${S.repo_path} on branch ${S.branch}. You did not write it. RUN things; exit
codes are ground truth.

1. Run the spec's Done-bar checks. Paste real output.
2. Confirm RED->GREEN is real: would the new test FAIL if the fix were reverted? Check, don't assume.
   A test that passes either way is a hollow test — that is ok=false.
3. Confirm the fix addresses the CAUSE named in the diagnosis, not just the symptom.
4. Run the surrounding suite and list any PRE-EXISTING failures separately from new ones.
${S.extra_gate ? `5. Run the gate the spec names and require it green: ${S.extra_gate}` : ''}

Write ${S.state_dir}/steps/fix.json with the real command output either way.`,
    { label: `verify:${attempts}`, phase: 'Verify', agentType: 'reviewer', schema: VERDICT, effort: 'high' })

  done = !!(gate && gate.ok)
  lastReason = (gate && gate.reason) || 'verify did not pass'
  PROGRESS.steps_done.push(`attempt ${attempts}: ${done ? "DONE" : "not done -- " + lastReason}`)
  if (!done) { feedback = lastReason; log(`attempt ${attempts} NOT done: ${lastReason}`) }
}

if (!done) {
  return {
    outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: S.state_dir,
    reason: `fix failed verification ${MAX_ATTEMPTS} times: ${lastReason}`,
  }
}

// --- Review -----------------------------------------------------------------
// ONE round by default. The course: "usually I find it only has to go back and
// forth once because most of the bugs would have been captured quite early."
//
// BUT the persona pass escalates on HIGH-STAKES DIFFS. The minimal debug loop is
// right for an import fix and wrong for a bug fix that lands in billing or a
// lease.
//
// Escalation is decided by ONE piece of code -- `openrouter_review.py
// --decide-only` -- and NOT by a regex living here. Two reasons:
//   1. A regex over the SPEC WORDING is what made a 6-line import fix trip a
//      concurrency persona pass and cost 1.7M tokens. Paths, never prose.
//   2. Paths alone still lie. `harness/szloop/lock.py` matches every concurrency
//      keyword there is, but the change that landed in it was a sys.path
//      bootstrap. So the decider ALSO requires the diff to contain a
//      behaviour-changing line, not just imports and comments.
// Keeping the rule in one place means the cheap reviewer and the expensive
// persona pass can never disagree about whether a diff is dangerous.

stage('Review')

const decide = await spawn(`Decide the review tier for branch ${S.branch}. Run EXACTLY this and return its JSON verbatim:
  cd ${S.repo_path} && git diff $(git merge-base HEAD origin/HEAD)...HEAD \\
    | python3 ${ROOT}/code-tools/review/openrouter_review.py --repo ${S.repo_path} --decide-only

This calls no model and costs nothing. Do NOT substitute your own judgement for its output, and do
NOT decide from the spec text. If it exits non-zero, report high_stakes=true (fail safe: an
undecidable diff gets the full review, never the cheap one) and put the error in tier_reason.`,
  { label: 'tier-decision', phase: 'Review', effort: 'low',
    schema: { type: 'object',
      properties: {
        high_stakes: { type: 'boolean' },
        tier_reason: { type: 'string' },
        high_stakes_paths: { type: 'array', items: { type: 'string' } },
        files_in_diff: { type: 'number' },
      },
      required: ['high_stakes', 'tier_reason'] } })

// No decision at all (agent died) is NOT "safe to skip" -- escalate.
const escalate = decide ? decide.high_stakes !== false : true
log(escalate
  ? `persona pass ON -- ${decide ? decide.tier_reason : 'tier decision failed, failing safe to full review'}`
  : `single review round -- ${decide.tier_reason}`)

const reviewPrompt = `${CTX}

Review the diff on branch ${S.branch} in ${S.repo_path}:
  git diff $(git merge-base HEAD origin/HEAD)...HEAD

This is a BUG FIX, so weight the review accordingly:
  - did it fix the cause or paper over the symptom?
  - does it break any CALLER of the changed code? (grep call sites the diff did not open)
  - are there remaining siblings with the same defect?
  - did it drag in anything out of scope?

Report only findings you would defend. Style preferences are not findings.`

const lenses = [
  () => spawn(reviewPrompt, { label: 'review', phase: 'Review', schema: FINDINGS, effort: 'high' }),
  // The outside voice. Claude reviewing Claude is an echo chamber; the course runs
  // its review on a different model. Tier auto-selects gpt-5.6-luna, escalating to
  // sol on high-stakes paths. If the key is missing the script FAILS LOUDLY rather
  // than returning an empty list -- silence must not read as clean.
  () => spawn(`${CTX}

Get a CROSS-MODEL review of this diff. Run, from ${ROOT}:

  cd ${S.repo_path} && git diff $(git merge-base HEAD origin/HEAD)...HEAD \\
    | python3 ${ROOT}/code-tools/review/openrouter_review.py \\
        --repo ${S.repo_path} --context "bug fix: ${S.slug}"

Return its findings verbatim. If it exits non-zero or prints {"error":...}, report that as your
'reason' and return an EMPTY findings list — do NOT invent findings, and do NOT report the diff as
clean, because a failed reviewer is not a passing review.`,
    { label: 'review:cross-model', phase: 'Review', schema: FINDINGS }),

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
    { label: 'review:reuse', phase: 'Review', agentType: 'code-simplifier', schema: FINDINGS }),
]

if (escalate) {
  for (const p of ['persona-hacker', 'persona-race-hunter', 'persona-auditor']) {
    lenses.push(() => spawn(reviewPrompt, { label: `${p}`, phase: 'Review', agentType: p, schema: FINDINGS }))
  }
}

const reviews = (await parallel(lenses)).filter(Boolean)
const raw = reviews.flatMap(r => (r && r.findings) || [])
const blocking = raw.filter(f => f.severity !== 'LOW')
log(`review: ${raw.length} findings, ${blocking.length} above LOW`)

if (blocking.length) {
  const real = await refute(blocking, 'Review', { highStakes: escalate })
  log(`${real.length}/${blocking.length} survived refute`)
  if (real.length) {
    await spawn(`${CTX}

These findings survived an adversarial refute pass — they are real. Fix exactly these, nothing else.

${JSON.stringify(real, null, 2)}

Work in ${S.repo_path} on branch ${S.branch}. Re-run the spec's Done-bar checks after.
${S.extra_gate ? `The gate must still be green: ${S.extra_gate}` : ''}`,
      { label: 'fix-findings', phase: 'Review', agentType: 'fixer' })
  }
}

// --- PR ---------------------------------------------------------------------
// No HTML artifact, no mermaid diagram. A bug fix's deliverable is the diff and
// the RED->GREEN evidence; ceremony here is what made the feature loop expensive.

// stop_after_review exists for MEASUREMENT: it ends the run at "verified fix,
// reviewed, on a branch" so this loop can be compared against another approach at
// the same milestone, without one of them also paying for a PR and a Greptile
// poll. It is NOT a shortcut for real work -- an unmerged branch is not done.
if (A.stop_after_review) {
  return {
    outcome: 'PARKED', slug: S.slug, branch: S.branch, evidence_path: S.state_dir,
    gate: 'stop_after_review',
    question: `Fix verified and reviewed on ${S.branch} in ${S.repo_path}. stop_after_review was set, so no PR was opened.`,
  }
}

stage('PR')
const pr = await spawn(`${CTX}

Open the PR/MR for ${S.branch} in ${S.repo_path} using ${S.platform}.

Body: what was broken, the MECHANICAL root cause, what changed, and the RED->GREEN evidence as real
pasted output. Plain language — Michael reads this, not reviewers. No HTML artifact needed.
Link ${S.state_dir}/steps/fix.json.`,
  {
    label: 'pr-open', phase: 'PR',
    schema: {
      type: 'object',
      properties: { pr_number: { type: 'string' }, pr_url: { type: 'string' }, ok: { type: 'boolean' } },
      required: ['pr_number', 'pr_url', 'ok'],
    },
  })

if (!pr || !pr.ok) return { outcome: 'STUCK', slug: S.slug, branch: S.branch, evidence_path: S.state_dir, reason: 'could not open the PR/MR' }

// --- Greptile ---------------------------------------------------------------
// Kept verbatim in spirit from lifecycle-run Phase 7: this is the merge gate,
// it costs almost nothing in tokens, and getting it wrong stalled the queue for
// ten hours on 2026-07-26.

stage('Greptile')
let merged = false
let note = ''

for (let round = 1; round <= MAX_ATTEMPTS; round++) {
  const g = await spawn(`${CTX}

Phase 7 of ${SKILL} — round ${round}. Read that phase IN FULL first; it documents the exact bug that
stalled this queue for ten hours and you must not repeat it.

PR ${pr.pr_number} (${pr.pr_url}) in ${S.repo_path}, platform ${S.platform}.

Essentials (the skill has the detail):
  - A push does NOT trigger a re-review. Comment "@greptileai" after every push.
  - Poll the CHECK RUN on the head SHA — authoritative, NOT /reviews. A clean re-review creates no
    review object and no comments; it only updates the check run.
  - Findings live in the LINE-LEVEL comments (pulls/<n>/comments), not the review body.
  - Compare a review's commit_id to .head.sha. A review of a superseded commit is not a review.
  - Distinguish CLEAN (green check run on this SHA, started after your push) from SILENT (no check
    run at all). Never read silence as clean.
  - in_progress -> wait (~9.5 min on a 31-file PR). No check run 10 min after the trigger -> "infra".

state="clean" | "findings" (list them, triaging severity yourself if Greptile omits it) | "infra"`,
    {
      label: `greptile:r${round}`, phase: 'Greptile', effort: 'high',
      schema: {
        type: 'object',
        properties: {
          state: { type: 'string', enum: ['clean', 'findings', 'infra'] },
          note: { type: 'string' },
          findings: FINDINGS.properties.findings,
        },
        required: ['state', 'note'],
      },
    })

  if (!g) { note = 'greptile agent failed'; break }
  note = g.note

  if (g.state === 'infra') return parked('greptile-infrastructure', `Greptile never re-reviewed ${pr.pr_url}. ${g.note}`)
  if (g.state === 'clean') { merged = true; break }

  const block = (g.findings || []).filter(f => f.severity !== 'LOW')
  if (!block.length) { merged = true; break }   // LOW/nit does not block

  const real = await refute(block, 'Greptile', { highStakes: true })  // Greptile already filtered; verify all of it
  if (!real.length) { merged = true; break }

  await spawn(`${CTX}

These Greptile findings survived refute. Fix exactly these in ${S.repo_path} on ${S.branch}, then PUSH.
After pushing you MUST comment "@greptileai" on PR ${pr.pr_number} to trigger the re-review.
${S.extra_gate ? `The gate must stay green: ${S.extra_gate}` : ''}

${JSON.stringify(real, null, 2)}`,
    { label: `fix-greptile:r${round}`, phase: 'Greptile', agentType: 'fixer' })
}

if (!merged) {
  return parked('greptile-exhaustion',
    `${pr.pr_url} still has open MEDIUM/HIGH findings after ${MAX_ATTEMPTS} rounds. ${note}`)
}

if (A.stop_before_merge) {
  return {
    outcome: 'PARKED', slug: S.slug, branch: S.branch, pr_url: pr.pr_url, evidence_path: S.state_dir,
    gate: 'manual-merge-requested',
    question: `${pr.pr_url} is Greptile-clean and ready. stop_before_merge was set, so it was NOT merged.`,
  }
}

const m = await spawn(`${CTX}

PR ${pr.pr_number} (${pr.pr_url}) is Greptile-clean on the current head SHA. Merge it using
${S.platform} in ${S.repo_path}.

Then state what merging DEPLOYS: if it publishes to production, name the deploy target and where its
logs live. Verify, do not assume.

Write ${S.state_dir}/RESULT.md: PR link and the REAL evidence (command output).`,
  {
    label: 'merge', phase: 'Greptile',
    schema: {
      type: 'object',
      properties: {
        ok: { type: 'boolean' }, reason: { type: 'string' },
        deploy_target: { type: 'string' }, log_source: { type: 'string' },
      },
      required: ['ok', 'reason'],
    },
  })

if (!m || !m.ok) return parked('merge-failed', `Greptile was clean but the merge did not complete: ${m ? m.reason : 'merge agent failed'}`)

return {
  outcome: 'DONE',
  loop: 'fix',
  slug: S.slug,
  branch: S.branch,
  pr_url: pr.pr_url,
  evidence_path: S.state_dir,
  attempts,
  monitor: { target: m.deploy_target || 'none', log_source: m.log_source || 'none', minutes: 60 },
}
