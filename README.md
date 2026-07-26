# Three attempts at an autonomous coding loop — review package

Prepared for **Ray** (Loopy AI / Agentic Coding School), 2026-07-26.

We built the course's L3/L4 design, measured it against a control, and **it lost badly to a single
agent**. This repo is the actual scaffolding — skills, sub-agent definitions, workflow scripts, all
four hooks, and the tests that hold them in place — so the critique can be about real code rather
than a description of it.

**Start here: [`review/WRITEUP.md`](review/WRITEUP.md)** — the numbers, what we got wrong, and
what is still open. Everything below is the supporting material.

> **Updated after Ray's reply.** He sent his own `task-lifecycle` skill, which answered our three
> biggest questions outright. The short version: his loop builds the whole change in **one**
> builder and reviews **once** on the complete PR diff, capped at two rounds. Ours decomposes
> into atomic steps and reviews every step's diff on every attempt — which is exactly where 26
> of our 48 agents went. We did not adapt his design so much as mis-transcribe it. The write-up
> now records that as diagnosis rather than as an open question.

## The result that prompted this

One trivial bug: a Python CLI that raised `ImportError` when run as a script. Three-line fix. Four
configurations, same spec, same model (Opus), each in its own git worktree.

| Arm | Agents | Output | Wall clock | Fix produced? | What the loop *reported* |
|---|---|---|---|---|---|
| L3 `lifecycle-run` | 54 | 342k | 79.3 min | **yes — committed, test passes** | STUCK |
| L3 `lifecycle-fix` | 10 | 83k | 46.8 min | yes, but **never committed** (and it edited a file the spec forbade) | STUCK |
| L3 + caps/watchdog/probe/preflight | 48 | 237k | 67.5 min | **yes — committed, test passes** | ceiling hit, no verdict |
| **One agent, chose its own process** | **1** | **8k** | **20 min** | **yes — committed, test passes** | **success, accurately** |

**All three loop arms solved the bug. None of them could tell us they had.** That is the finding, and
it is not the one we first reported — an earlier draft of this page said the levered run changed zero
product code, which was wrong: it was read off `git diff` (working tree vs HEAD), which by
construction cannot show committed work. Corrected here, and in the write-up.

So the failure is in **recognising and reporting** completion, not in producing it. Efficiency is
still the story — 48 agents and 68 minutes versus 1 agent and 20 minutes for the same one-line
change — but "the loop cannot fix a three-line bug" was false.

**The root cause turned out to be one line of shell per step.** The planner was never told which
directory verify runs in, so it wrote `cd <main repo> && pytest ...` — and a `cd` overrides the
working directory the runner supplies. Verify tested a checkout that did not contain the change, on
all 5 of 5 steps, and produced a confident wrong FAIL on correct work. The loop's own verifiers
diagnosed this repeatedly and had nowhere to put the finding. Full account in the write-up under
*"The root cause, found after publishing"*; guards in `harness/verify_scope.py` and
`harness/arm_report.py`.

Role split in the 48-agent run: **26 review, 14 plan, 5 implement, 2 verify, 1 preflight** — 83%
deliberating, 10% building.

Two further measurements, taken from run transcripts:

- **Wall clock** is 87.5% model generation, 12.1% tool execution.
- **Token cost** is 99.4% cached-prefix re-supply, 0.6% generation — `agents × turns-per-agent ×
  prefix-size`, measured at `40 × 21.3 × 46,880`. Cache read accrues per API call, so every turn of
  an agent's tool loop re-reads its whole prefix.

Those two point in opposite directions, which we had not noticed: "the loop is model-bound" is true
of time and false of cost.

## What is in here

### The loops
| Path | What it is |
|---|---|
| `.claude/skills/lifecycle/SKILL.md` | **L3** — one spec end-to-end: plan → build → review → security → user-flow → PR → review-bot loop → blast-radius-gated merge → post-merge monitor |
| `.claude/skills/spec-queue/SKILL.md` | **L4** — drains a spec queue through L3; park-and-continue on human gates |
| `.claude/skills/solve/SKILL.md` | **attempt 3** — one lead agent that sizes its own process; verifies against sealed intent |
| `.claude/skills/atomic/SKILL.md` | the one-unit-at-a-time primitive L3 uses |
| `.claude/skills/spec-plan/SKILL.md` | spec → enforced done-contract, one requirement per MUST clause |
| `.claude/workflows/lifecycle-run.js` | L3 as a deterministic workflow script — the file the measurements above came from |
| `.claude/workflows/lifecycle-fix.js` | the small-bug variant |

### The four hooks (`.claude/settings.json` wires all of these)
| Hook | Script | Job |
|---|---|---|
| `Stop` | `harness/done_gate.py` | **The backstop.** Refuses to let a turn end while any contract requirement lacks a passing test. "Done" is an exit code, never a claim. |
| `UserPromptSubmit` | `harness/autoseed.py` | Seeds that contract from the prompt — and, harder than it sounds, decides which prompts are *not* build tasks |
| `PreToolUse` | `scripts/pretooluse_guard.py` | Denies secrets, live model weights, destructive commands. Load-bearing code stays editable |
| `PostToolUse` | `scripts/posttooluse_smoke.py` | `py_compile` on every Python edit |

### The gates added on measurement day
| Path | Job |
|---|---|
| `harness/spec_preflight.py` | Specs carry executable claims; a spec whose factual claim is **false** refuses to dispatch. Built after a run spent 79 min failing to satisfy a spec sentence that was simply wrong |
| `harness/intent_contract.py` | Verify against a **sealed** statement of intent rather than the spec's literal clauses — with four rules that make intent *harder* to fudge than spec text |

### Sub-agent definitions — `.claude/agents/`
`planner`, `implementer`, `reviewer` (read-only verifier), `tester`, `fixer`,
`code-simplifier` (the reuse lens), `design-reviewer`, `blast-radius-assessor`.

The `code-simplifier` exists because of a specific blind spot: five reviewers all hunting bugs are
uniformly blind to "this hand-rolls something the repo already provides."

### Tests
`harness/tests/` — the tests that pin each mechanism. Several pull the real functions out of the
shipped workflow scripts and execute them, so the thing under test is the thing that runs.

`test_tests_are_collectable_20260726.py` is the most embarrassing and probably the most useful: we
found **554 tests had silently not been running**. Three files in the test directory were one-shot
scripts whose module-level code called `sys.exit(1)`, and pytest imports every file during
collection, so any one of them aborted collection for the whole directory with `INTERNALERROR — no
tests ran`. A false green, inside the mechanism built to prevent false greens.

### Context
- `review/ARM_C_WINNING_PROMPT.md` — the prompt that beat the scaffold, verbatim. It is 3,814
  characters; the lesson is **not** "less structure."
- `review/BELIEFS.md` — every claim we hold about agentic coding, each with a falsifier and a
  status. 14 open.
- `review/QUEUE_PROTOCOL.md` — the folder-move queue protocol.

## Where we diverge from the course

Recorded as explicit decisions before building, not discovered afterwards. Kept from our own
design: a tools-first buy-vs-build gate, per-step exit-0 verification plus the `Stop`-hook
backstop, per-step model matching, and GUI/design gates. Adopted from the course: queue intake,
multi-angle review, the user-flow loop, the blast-radius merge gate, post-merge monitoring, and the
L4 worker.

The divergence we were least confident about — sub-agents spawning with `bypassPermissions`, because
an unattended worker cannot stop for an ask-tier prompt — now has an answer we did not have when this
was written: run the loop in a hosted headless sandbox, so permissions are never bypassed and the
blast radius is the sandbox instead. Ours runs on a machine holding live credentials. Not yet fixed,
but no longer unexplained.

## Scope: this is the harness, not the application

The harness runs against a private production codebase, and **none of that codebase is here** — no
application source, no models, no data. What you get is the loop machinery itself: the skills that
drive it, the sub-agent definitions, both workflow scripts, all four hooks, and the tests that pin
each mechanism.

Everything is **as actually run**, with three deterministic substitutions so the harness reads as a
harness rather than as one project's config:

| Substituted | Now reads | Why |
|---|---|---|
| absolute repo paths | `/path/to/repo` | they were one machine's layout, and hardcoded paths obscure the pattern |
| the application's name | generic | irrelevant to the loop design |
| names of protected assets | `model_a`, `model_b`, `model_c.pt`, `golden_witness.json`, `invariant_check.py`, `confident_wrong` | the guard **pattern** is the point; the names carry no reusable insight |

Nothing else was altered. No logic, threshold, prompt, or guard rule was softened for publication —
including the parts that make us look bad, of which there are several.

### One piece of project context you do need

Two guardrails in here will look paranoid without it. The application is a document-reconciliation
engine whose core quality metric is *confidently wrong answers*, which must never increase — it is
allowed to abstain, never to guess. That is why:

- `invariant_check.py` must stay green and `golden_witness.json` is immutable and undeletable, and
- the `/solve` skill carries an explicit carve-out sending load-bearing changes back through the
  **full** review fan-out, even though the same fan-out is measurable waste on a small fix.

The 83%-deliberation ratio below is waste on an import fix and cheap insurance on that metric.
Knowing which is which is the judgement the third attempt rests on entirely.
