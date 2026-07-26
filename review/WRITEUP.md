# Three attempts at a loop that codes without us

> Prepared for **Ray** (Loopy AI / Agentic Coding School) · 2026-07-26
> Authors: Michael + Claude Opus 5 · Every number below comes from run transcripts, not recollection.

We built your L3/L4 design, measured it against a control, and **it lost badly to a single agent.**
This is what we tried, what the instruments said, and where we have knowingly walked away from the
course — including one divergence we are not confident about.

---

## What we got wrong, and what is still open

Ray sent us his own `task-lifecycle` skill after reading a first draft of this. It answered our three
biggest questions immediately, so they are recorded here as **diagnosis rather than questions** — and
the diagnosis is not flattering to us.

### The mistake: we inverted the core loop

His design builds **the whole change in one builder**, then reviews **once, on the complete PR diff**,
capped at two rounds. Ours decomposes into atomic steps and reviews **every step's diff, on every
attempt**. That single choice produces everything measured below: step count × attempt count ×
reviewers is where 26 of our 48 agents went.

Counting his worst case for a whole feature — explorers, one builder, two review rounds of three,
two fixers, verify — comes to roughly **16 agents**. We spent **48 on a three-line import fix**.

We did not adapt his design; we mis-transcribed it, and then measured the result instead of the
intent.

### The three things we thought were gaps, and were not

- **Sizing the process to the change.** We could not find the mechanism. It is there and it is
  explicit: scale the explore fan-out to the ask (or skip exploring entirely when the files are
  already named), choose review depth from a *measured* diff rather than from the words in the spec,
  stop the moment a review comes back clean, and cap the rounds. We had independently arrived at the
  diff-measured review tier (commit `5747214`) — we simply never applied the same thinking to the
  pipeline itself, only to the tier inside it.
- **Sharing context across sub-agents.** Our measurement said 99.4% of token spend is cached-prefix
  re-supply. The answer is not a caching feature: sub-agents are supposed to return **maps — paths,
  symbols, patterns — not file contents**, and the orchestrator holds a small synthesised brief rather
  than the material behind it. Nothing large is ever in its hands to re-supply. Our 24–43k prefixes
  are a consequence of handing every agent a fat context block.
- **Running unattended without permission prompts.** Answered by his environment rather than by a
  flag: the loop runs in a hosted headless sandbox, cloned per run. Permissions are not bypassed — the
  *blast radius* is. That is strictly better than our answer, which is `bypassPermissions` on a machine
  holding live credentials.

### What is still genuinely open

1. **If nothing merges without a human, how does a queue worker make progress overnight?** His rule is
   absolute — merging is the user's call. We added a blast-radius gate that auto-merges small changes.
   Given our track record that looks like the wrong bet, but it is the difference between a loop that
   drains a backlog unattended and one that needs a human every cycle.
2. **Does it matter that our second opinion arrives after the PR rather than beside the build?** We
   substituted a PR review bot for a second-model review. We take the point that two *different*
   mandates finding the same defect is stronger evidence than one mandate run twice — the asymmetry is
   the value — but ours is also later in the loop.
3. **What happens when the task needs local credentials or a private network,** and the sandbox answer
   to the permissions question is unavailable?
4. **Is the L4 queue meant to have durable state at all,** or is git-plus-the-thread deliberately the
   whole database? Ours has `specs/pending|active|waiting` folders and a per-spec state directory,
   which may be a database we did not need.
5. **Was a deterministic turn-end gate considered and rejected?** The verify discipline in his skill is
   strong prose — real DB confirmation, and an honest "couldn't verify" beats a claimed pass. Ours is a
   `Stop` hook that refuses to end the turn while any requirement lacks a passing test. Prose is more
   flexible; a hook cannot be talked out of it.

### One thing we owe him back

His skill is emphatic that `background: false` is mandatory on every dispatch, because backgrounded
agents notify a parent that a one-shot turn will never be around to hear — and that *parallel* and
*backgrounded* are not the same thing.

We measured **9 of 48 agents returning empty results** and built a six-minute watchdog to abandon
silent agents. If any part of that is the same dispatch mismatch rather than genuine hangs, then our
watchdog is treating a dispatch bug as a performance problem — and that is worth checking before
tuning it further. It is on our list.

---

## Attempt 1 — our own orchestrator
*before 2026-07-25 · retired*

A single `/orchestrator` skill. Ad-hoc intake from arguments, one planner sub-agent producing steps
that each carried an exit-0 `verify` command, one adversarial reviewer, and a deterministic `Stop`
hook that refused to let a turn end while any contract requirement lacked a passing test.

**Why we moved off it**

- **No queue.** Every task needed a human to start it. No way to leave work running overnight.
- **One reviewer.** A single adversarial pass missed whole classes of defect — most notably reuse:
  five reviewers hunting bugs are all blind to *"this hand-rolls something the repo already
  provides."*
- **Nothing after the PR.** No review-bot loop, no merge gate, no post-merge monitoring. The loop
  stopped exactly where the risk starts.
- **No user-flow verification.** A screenshot rule, not a loop.

Every item on that list is something your L3/L4 design already had, which is why we adopted it
rather than patching ours.

---

## Attempt 2 — your L3/L4 skeleton, with our floors grafted in
*2026-07-25 · `/lifecycle` + `/spec-queue`*

We wrote an explicit adopt-versus-keep decision for every aspect **before** building, so the
divergences are deliberate rather than drift. Abbreviated:

| Aspect | Ours (attempt 1) | Course L3/L4 | Decision |
|---|---|---|---|
| Intake | ad-hoc arguments | interview → spec file → queue folder | **Course** |
| Review | one adversarial reviewer | multi-angle + per-finding verify agents | **Course** |
| Second model | none | Codex alongside | *Adapted* — a PR review bot is our second voice |
| User-flow verify | screenshot rule | dedicated L2 loop, GIF per flow | **Course** |
| Merge gate | human always | blast-radius agent, small→auto | *Course + hardened* — protected paths always human |
| Post-merge | none | monitor logs 1h → hotfix loop | **Course** |
| Queueing | none | L4 worker drains queue | **Course** |
| Tools-first research gate | mandatory phase | absent | **Kept ours** |
| Done discipline | exit-0 per step + Stop-hook backstop | agent judgement | **Kept ours** |
| Model matching per step | haiku/sonnet/opus | not addressed | **Kept ours** |
| GUI / design gates | design tokens + live screenshot | not addressed | **Kept ours, strengthened** |

The one we would defend hardest is **done discipline**. This harness exists because work kept getting
reported finished when it wasn't, so "done" is an exit code here, never a claim, and a `Stop` hook
enforces it deterministically at turn-end. Agent judgement was the thing that failed us.

```mermaid
flowchart LR
  Q[specs/pending] --> L[L4 worker]
  L --> P[L3 lifecycle: one spec]
  P --> PL[plan] --> B[build per step] --> R[review] --> V[verify] --> PR[open PR]
  PR --> G[review-bot loop] --> BR{blast radius}
  BR -->|small| M[auto-merge] --> MON[monitor 1h]
  BR -->|large| H[human]
  V -->|fails 3x| S[stuck/]
```

---

## The day we finally measured it
*2026-07-26 — the control beat the loop*

Same trivial bug to four configurations: a Python CLI that crashed with `ImportError` when run as a
script. The fix is three lines. Each ran in its own git worktree, on Opus, from the same spec.

| Arm | Agents | Output | Wall clock | Fix produced? | What the loop *reported* |
|---|---|---|---|---|---|
| L3 `lifecycle-run` | 54 | 342k | 79.3 min | **yes — committed, test passes** | STUCK |
| L3 `lifecycle-fix` | 10 | 83k | 46.8 min | yes, but **never committed**, and it edited a file the spec explicitly forbade | STUCK |
| L3 + caps/watchdog/probe/preflight | 48 | 237k | 67.5 min | **yes — committed, test passes** | ceiling hit, no verdict |
| **One agent, chose its own process** | **1** | **8k** | **20 min** | **yes — committed, test passes** | **success, accurately** |

### Correction, and it changes the finding

An earlier version of this page said the loop arms produced no working code. That was wrong. We
verified each worktree with `git diff` — working tree versus HEAD — which by construction cannot show
work that was **committed**. Checked properly against the base branch, three of the four arms had a
one-line fix and a passing test sitting on a branch.

**All three loop arms solved the bug. None of them could tell us they had.** The 54-agent run
committed a working fix and reported STUCK. The 48-agent run committed a working fix, then blew its
agent ceiling during the *review* fan-out and threw the verdict away with the exception. The 10-agent
run fixed it in the working tree, never committed, and also modified a file the spec named as
off-limits.

So the defect is in **recognising and reporting completion**, not in producing it — which is a
different and more interesting problem than the one we set out to ask about. The efficiency gap is
unchanged and still stark: 48 agents and 68 minutes against 1 agent and 20 minutes, same one-line
change. And the single agent was the only one whose report matched reality.

### Where the 48 agents went

Roles inferred from each agent's own opening prompt:

| Role | Agents | Share |
|---|---|---|
| review | 26 | 54% |
| plan | 14 | 29% |
| **implement** — *the only ones that change code* | **5** | **10%** |
| verify | 2 | 4% |
| preflight | 1 | 2% |

> **83% of the fleet deliberated. 10% built.** On a three-line import fix. Several review agents
> re-read a 24–43k prefix in order to emit 300 tokens.

### Two other readings that surprised us

Time and cost point in **opposite** directions. We had been quoting the first as if it explained the
second:

| Bottleneck | Measurement | Implied lever |
|---|---|---|
| wall clock | 87.5% model generation / 12.1% tool exec | concurrency, fewer serial stages |
| token cost | 99.4% context re-supply / 0.6% generation | smaller prefix, fewer turns per agent |

Cost decomposes as `agents × turns-per-agent × prefix-size` — measured at `40 × 21.3 × 46,880`. We
had built a sub-agent cap, which attacks only the first and weakest multiplier. Cache read accrues
per API call, so every turn of an agent's tool loop re-reads its whole prefix.

Separately, **concurrency was idling at 1.67×**, and one baseline run lost **27.7% of its wall clock —
40.4 agent-minutes across 52 gaps** — to agents that produced nothing and then finished the same work
in minutes on respawn.

### The finding that embarrassed us most

While measuring, we discovered **554 tests had silently not been running.** Three files in the test
directory were one-shot scripts whose module-level code called `sys.exit(1)`; pytest imports every
file during collection, so any one of them aborted collection for the entire directory with
`INTERNALERROR — no tests ran`. It read as an infrastructure problem rather than a testing gap.

That is a false green sitting inside the very mechanism built to prevent false greens, and nothing in
the loop noticed for an unknown period. Guard: `harness/tests/test_tests_are_collectable_20260726.py`.

---

## The root cause, found after publishing: verify was testing the wrong checkout

The correction above raised a better question than the original one. If all three loop arms produced a
working fix, why did every one of them report failure? The answer is one line of shell per step, and
it is worth more than any of the cost numbers.

`lifecycle-run.js` creates a git worktree per run and threads it through as `S.repo_path`. The probe
that runs a step's verify command is told, literally, *"Run EXACTLY this command in
${S.repo_path}"*. But the **planner** prompt never mentioned `repo_path` at all — the only absolute
path in its context came from `${ROOT}/STATE.md`, the main repo. So it wrote, for every step:

```bash
cd /path/to/main-repo && PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q
```

A `cd` inside the command overrides the working directory the runner supplies. The `cd` wins,
silently. Verify therefore ran against a checkout that did not contain the change, failed, and kept
failing — three attempts, then the step was declared unverifiable and the run reported STUCK.

Measured on the real plan file: **5 of 5 verify commands pointed at the main repo instead of the
worktree they were verifying.**

Two details make it worse, and both are about the loop's own structure rather than the shell:

- The probe is instructed *"Do NOT amend the verify command even if it looks wrong — that judgement
  belongs to the certifying verifier."* That instruction is right in general (a thermometer should not
  doctor) but here it meant the component closest to the evidence was forbidden from acting on it.
- The certifying verifiers **did** diagnose it, repeatedly, in their own words: *"a DISPOSABLE
  amendment to the verify's `cd` target"*, *"PASSES in the correct worktree"*, *"genuinely met ON THE
  CORRECT TARGET"*. The loop identified its own defect several times per run and had nowhere to put
  the finding. It is not a knowledge problem; it is a plumbing problem.

The shape of this defect is the reason it survived: a mis-scoped verify command does not error. It
runs, exits non-zero, and produces a confident, specific, **wrong** FAIL — on work that is correct.

### What changed

- The planner prompt now states which directory verify runs in, and that an absolute `cd` will
  silently test a different checkout.
- A prompt is a request, so there is also enforcement: `harness/verify_scope.py` plus a `scopeVerify`
  pass in the workflow that repoints working directories and absolute path arguments at the worktree
  the moment the plan comes back, and **logs every correction** — a silently corrected plan reads
  exactly like a correct plan, and then nobody fixes the prompt.
- Every `STUCK` verdict now carries the command that settles what is on the branch, and says plainly
  that STUCK means *could not certify*, not *produced nothing*. It also names `git diff` as the wrong
  check, because that is the one we reached for.
- `harness/arm_report.py` answers "what did this run actually produce" in one place, reporting
  committed, uncommitted, both, or empty as four distinct states — the distinction the hand check
  collapsed.

Tests: `test_verify_scope_20260726.py` (32), `test_verify_scope_parity_20260726.py` (24, pinning the
JS copy against the Python one because workflow scripts cannot call out to a subprocess),
`test_arm_report_20260726.py` (12), `test_stuck_points_at_the_branch_20260726.py` (10). The scope
guard was mutation-tested against nine deliberate breakages, two of which survived the first pass and
exposed real gaps — including one where `check()` and `rewrite()` disagreed with each other about
whether `cd /tmp/scratch` was a violation.

---

## What we changed, and what we concluded

We first assumed the problem was **spend**, and built four levers accordingly. They worked: agents
54 → 40, output 342k → 237k, wall clock 79 → 52 min. The run still finished by **reporting nothing** —
it hit the ceiling and the exception took the verdict with it. It had in fact committed the fix and a
passing test to its branch; we did not find that out for hours, because we looked with `git diff`
instead of `git diff main...HEAD`.

- **A hard sub-agent ceiling.** There had been none of any kind; an earlier run reached 77 agents in
  1h42m.
- **A watchdog on silent agents.** Six-minute bell, one respawn, longer leash on the retry.
- **A cheap probe before expensive review.** Run the step's own verify command first, fan out
  reviewers only if it passes. *"A thermometer, not a doctor."*
- **A spec preflight.** Specs carry executable claims; a spec whose factual claim is false refuses to
  dispatch. Built after a run spent 79 minutes failing to satisfy a spec sentence that was wrong.
- **A ceiling that degrades instead of detonating.** Our first cap *threw*, discarding the finished
  plan, five implementer attempts and 26 completed reviews. It now emits what it learned on the way
  out.

Then we read the winning arm's prompt again, and the conclusion changed. It is **3,814 characters** —
longer and more carefully written than we remembered. It is *not* "less scaffolding."

```
## How to work — this part is yours to decide

You choose your own process. You may do it all yourself, or spawn
sub-agents for whatever you judge worth delegating. **Size the process
to the actual change.** A one-line fix does not need a research phase,
a plan review, or four reviewers; a subtle one might need more than
you'd guess. Deciding that proportion well is the point.
```

Every one of its six non-negotiables is about **evidence**, never process: red before green; root
cause in one sentence with `file:line`; done is an exit code; verify independently of the fix;
smallest diff that fully fixes the cause; report out-of-scope findings rather than fixing them.

> **Constrain the goal and the evidence. Delegate the process.** Our L3 does the opposite — its phase
> list is hard-coded and it has no way to decide a small change doesn't need all of it.

Full prompt: [`ARM_C_WINNING_PROMPT.md`](ARM_C_WINNING_PROMPT.md).

---

## Attempt 3 — where we are now

A third loop, [`/solve`](../.claude/skills/solve/SKILL.md), sits *alongside* `/lifecycle` rather than
replacing it. One lead agent picks its own process; verification moved from the spec's literal clauses
to a **sealed statement of intent**.

```mermaid
flowchart TD
  A[spec] --> B[preflight the spec's factual claims]
  B -->|claim false| X[STOP: fix the spec, do not dispatch]
  B -->|claims hold| C[write INTENT.md and SEAL it]
  C --> D[ONE lead agent, own worktree,<br/>chooses its own process]
  D --> E[verify against the SEALED intent]
  E -->|clause fails| F[report the clause + its output] --> D
  E -->|all pass| G[DONE]
```

The reasoning for intent over spec-clauses: real implementers routinely find the spec was written
with less information than they end up having. A spec clause that is *itself the defect* currently
makes our loop fail rather than say so.

But "verify the intent" is also perfect cover for finishing something easier and declaring *that* the
goal — the exact overclaiming this harness exists to stop. So intent verification is made **harder**
to fudge than spec verification, by four rules a tool enforces rather than a prompt requests
([`harness/intent_contract.py`](../harness/intent_contract.py)):

- **Sealed before the work.** The intent is hashed up front. Editing it afterwards is exit code 4,
  not a pass. You cannot move the target after taking the shot.
- **Every clause binds a command**, or states explicitly why none can. Bound to neither, sealing is
  refused — silence never reads as a pass.
- **Judgement clauses cannot self-certify.** `certified_by` must be empty at seal time; it is the
  only field the seal tolerates being edited later, and it is filled by a reviewer that never saw
  the implementation.
- **Divergence is declared, not discovered.** A clause may depart from the spec, but must record what
  the spec said and why this is better. An undeclared departure is scope drift wearing an
  improvement's clothes.

We also kept an explicit carve-out: load-bearing changes keep the **full** review fan-out. The 83/10
deliberation ratio is waste on an import fix and cheap insurance on a metric that must never regress
(see the README's note on that metric). Knowing which is which is the judgement the whole design now
rests on.

---

## Divergences we are least confident about

In rough order of how much we would like to be corrected.

1. **Sub-agents spawn with `bypassPermissions`.** Anthropic's guidance says never to skip
   permissions. An unattended L4 worker cannot stop for an ask-tier prompt, so we did it anyway, on a
   standing pre-approval. Hard hook denials still apply and a `PreToolUse` guard blocks secrets and
   destructive commands. We suspect there is a correct pattern here we have simply not found, and we
   have been sitting on this rather than resolving it.

2. **We replaced four per-diff persona lenses with three** — correctness, reuse, and an outside
   model. The persona rationale is *context focus*, and we reasoned that is spent once on the plan
   rather than per diff, a step's diff being small enough for one reviewer to hold entirely. This may
   simply be us weakening review to save agents.

3. **Plan review is gated to plans of three or more steps.** Your own scope note says the pattern is
   not for tiny changes where hook overhead dominates, so we think this is in the spirit of the
   course — but it is the change we made with the least evidence.

4. **A PR review bot stands in for a second-model review.** We do not run Codex. The bot reviews the
   pull request, which means our independent second opinion arrives *after* the PR is open rather
   than beside the build. We do not know if that ordering costs us anything.

---

## How we are keeping ourselves honest from here

Every claim above is now a registered hypothesis with a written falsifier
([`BELIEFS.md`](BELIEFS.md)), so a belief cannot quietly become an assumption. Fourteen are open. The
measurement harness computes agent counts and token spend from run transcripts rather than accepting
typed numbers, because we got two of today's figures wrong by reading a mid-run snapshot and a
working tree instead of a passing verify.

The one thing we will not do again is tune before measuring. We spent a day building levers for a
problem — spend — that turned out not to be the one making the loop fail.

---

*Happy to share any run transcript or the four arm prompts. The application code stays private; none
of the loop scaffolding does.*
