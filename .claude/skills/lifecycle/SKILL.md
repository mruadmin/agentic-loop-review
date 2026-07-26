---
name: lifecycle
description: The L3 task lifecycle — processes exactly ONE spec end-to-end. plan → build → adversarial review → security pass → user-flow verify → PR/MR → Greptile fix-loop → blast-radius-gated merge → post-merge log monitoring → Slack report. Replaces the retired /orchestrator (2026-07-25). Use when Michael types /lifecycle <spec-file>, or when the /spec-queue worker picks a spec from specs/pending/. Michael reviews artifacts (mermaid diagram, HTML summary, user-flow GIFs) and Slack messages — not code.
argument-hint: <path to a spec .md file (usually specs/active/<spec>.md)>
allowed-tools: Bash, Read, Edit, Write, Grep, Glob, Task, WebSearch, WebFetch, AskUserQuestion, Skill, Workflow, Monitor, TaskCreate, TaskUpdate, TaskList
---

# /lifecycle — one spec, end-to-end, proven at every step

Built 2026-07-25 from `docs/SPEC_lifecycle_spec_queue_2026-07-25.md` (finalized; all decisions
Michael's). Successor to `/orchestrator` — keeps its anti-overclaiming machinery (worker never
certifies own work, adversarial reviewer, per-step exit-0 `verify`, evidence not assertions) and
adds the course L3 phases: security pass, browser user-flow verification with GIFs, PR artifacts,
the Greptile fix-loop, a blast-radius-gated merge, and post-merge log monitoring.

**Report to Michael in plain language** — what it does and why it matters first; jargon only where
unavoidable, explained on first use.


## EXECUTION MODEL — this skill is a SPEC for sub-agents, not a script for the main agent

**Do NOT execute Phases 0–8 in your own context.** Dispatch them:

```
Workflow({ name: "lifecycle-run", args: { spec: "<abs path to spec .md>" } })
```

`.claude/workflows/lifecycle-run.js` is the orchestrator. It runs in the background, spawns every
sub-agent, and hands each phase's output to the next phase **in-script** — the calling agent never
sees a plan, a diff, a review body, or a `gh api` dump. It returns ONE small object:
`{ outcome, slug, branch, pr_url, evidence_path, gate?, monitor? }` where `outcome` is
`DONE | PARKED | STUCK`.

The phases below are the **content** the workflow's agents read (each is told to open THIS file and
follow its phase). Keeping the detail here and the sequencing in the script means you edit one
document to change behaviour, and no phase's output ever lands in a long-lived context.

Two things the workflow deliberately does NOT do, because they need a human or a clock:
- **Human gates** (Phases 1, 2-prototype, 8): it returns `PARKED` + `gate` and stops. The CALLER
  posts to Slack and moves the spec to `waiting/`.
- **Phase 9's post-merge monitor**: it returns `monitor` and stops. The caller schedules the watch
  and starts the next spec immediately — an hour-long log-watch must never block the queue.

If you are running `/lifecycle` interactively and Michael explicitly wants to watch it step by
step, you may execute the phases inline — but say so, and expect the context cost.

**Slack notifications — send AS THE JARVIS BOT** via `code-tools/slack/notify.py` (see
/spec-queue "Slack channel & sender identity"): human gates/escalations → DM `USER_ID_REDACTED`
(+ #sz-loops `CHANNEL_ID_REDACTED` for the record); milestones → #sz-loops, `<@USER_ID_REDACTED>` mention only
when action is needed. NEVER notify through the router/plugin user-token paths — they post as
Michael himself and Slack suppresses self-message notifications (the 2026-07-25 missed-gate bug).


## Input & platform

`$ARGUMENTS` = path to ONE spec file. Front-matter names the target `repo`; resolve it to the
local checkout (`~/Documents/Agent/<repo>` or `~/Documents/<repo>`) and derive the platform from
its git remote: **GitHub → `gh`, GitLab → `glab`** (agent-gitlab knows glab auth). Same lifecycle
either way; only PR/MR verbs and comment-polling differ.

| Repo | Host | CLI | Greptile |
|---|---|---|---|
| platform-repo (Makerkit — **the only frontend**) | GitHub | `gh` | connected |
| engine/OCR repos (resolver, ocr-lambda, matching-service, …) | GitLab | `glab` | connected |




## Phase 0 — Workspace
Slugify the spec name → `.claude/orchestrator/<slug>/` state dir **in this repo** (durable state:
`plan.md`, `tools-decision.md`, `steps/<id>.json`, `flows/`, `RESULT.md` draft — not in context).
In the TARGET repo: fetch, then create a fresh branch off the default branch.


## Phase 1 — TOOLS-FIRST GATE (kept from /orchestrator — Michael's #1 rule; never skip silently)
Before ANY code is planned, answer with **live web research (WebSearch/WebFetch), never training
data**: (1) is there a tool/library/PAID product that solves most of this outright? (2) what's the
single hardest piece, and is there a tool for it? (3) what accelerates the ordinary parts?
Present Michael an options table (coverage %, cost incl. paid tiers, integration effort,
trade-offs, recommendation) and record his choice in `tools-decision.md`. **Pick by evidence, not
existence** — benchmarks, recency, head-to-head; bake-off top-2 on real data for quality-critical
components. Skip the checkpoint ONLY for trivially small specs — and say so explicitly.
This is a human gate: if Michael hasn't answered and the queue is running, post to Slack and
**park** (return `PARKED` to the caller — /spec-queue moves the spec to `waiting/`).


## Phase 2 — Plan
3 `Explore` subagents from different starting points (entry-points/routes; data model; existing
similar features) → `planner` subagent with the spec + explorer findings + tools decision →
ORDERED steps, each with `id`, `desc`, `done_definition`, and a concrete **`verify` command that
exits 0 ONLY if truly done**. Write `plan.md` as a checklist. Right-size the steps — don't
over-decompose; split only where verification needs it.

**Mark every step's `verify` as DURABLE or DISPOSABLE** (see Phase 3c). DURABLE = user-facing
behaviour or a hard constraint. DISPOSABLE = an implementation choice the builder may legitimately
improve on. Default to DISPOSABLE for anything naming a specific library, file path, or helper.

**PLAN REVIEW GATE (mandatory, before any code).** The planner's output is NOT trusted. Spawn 4
read-only reviewers IN PARALLEL over `plan.md` — **architecture** (does this reuse existing
patterns or invent a competing one?), **security**, **blast radius** (what already-shipped
behaviour changes?), and **simpler path** (does a library/tool
or a 10-line version remove most of this plan?). Each returns PASS/WARN/FAIL with reasoning. Fold
every WARN and FAIL into a revised `plan.md`, and record what changed in `tools-decision.md`.
One bad line of plan becomes hundreds of bad lines of code — this is the cheapest review in the
lifecycle. Interactive sessions get this automatically via the `ExitPlanMode` hook
(`~/.claude/hooks/review-plan.py`); inside the lifecycle it is this step.

**GUI-step gate (mandatory).** Any step touching rendered UI MUST, **in this order**:

1. **Extract the app's OWN design tokens FIRST** — colors, fonts, spacing scale, radii, shadows,
   and the shared component inventory — from the live repo (theme/tailwind config plus two or three
   neighbouring screens). Evidence must name which existing tokens/components were reused.
2. **Then** invoke the `frontend-design` skill (Anthropic's official plugin
   `frontend-design@claude-plugins-official`) for CRAFT WITHIN those tokens — typographic rhythm,
   spatial composition, motion, depth, the fine details that separate finished from adequate.

**The order is load-bearing, because that skill is written for greenfield work and will fight you
otherwise.** It instructs the model to "pick an extreme," to "NEVER converge on common choices
across generations," and that "no design should be the same — vary between light and dark themes,
different fonts, different aesthetics." That is correct for a standalone page and actively wrong
for a screen inside an existing product: followed literally across a queue of UI specs it yields N
deliberately divergent aesthetics in one app, each individually defensible and collectively
incoherent.

So: **inside `platform-repo`, the app's existing tokens WIN every conflict.** Read the
skill's "vary / be distinctive / avoid generic" directives as being about *execution quality*
(don't ship limp spacing, default fonts, timid palettes) — never as licence to introduce a new
palette, a new type family, or a new component idiom. A screen that is beautiful but looks unlike
every other screen in the app is a FAIL. Greenfield artifacts (standalone prototypes, one-off HTML
deliverables) are the case where the skill runs unconstrained.

**Prototype gate — only for visual/new-UI specs (decided).** When the spec introduces new screens
or significantly changes existing UI: produce a prototype/mockup FIRST and post it **to Slack,
illustrated with mermaid diagrams** (mermaid-diagram-generator plugin), for Michael's 👍 before
building. No reply → park. Backend/bug-fix/refactor specs skip straight to build.


## Phase 3 — Build + adversarial review (L2 loop, ×3 max)
Spawn every subagent with `mode: "bypassPermissions"` (Michael pre-approved 2026-07-04; hard hook
denials still apply). For each step, in order:
  a. **Implement** — `implementer` subagent, fresh context, EXACTLY one step. Smallest diff,
     test-first. **Model-match**: mechanical → haiku; ordinary → sonnet; hard design/debugging →
     opus/inherit.
  b. **Review** — run **`/code-review` at `max` effort for big changes, `high` for small** (the
     built-in multi-angle review replaces the old single reviewer). Then every finding goes to an
     independent verify agent **prompted to REFUTE it** (sycophancy guard) — only confirmed
     findings go to a fixer subagent. Big change → another full round (×3 max); small → stop.
     **Blast-radius check** (any layout/container/breakpoint change): which ALREADY-SHIPPED
     components now render inside a new container/prop/state, and was each re-verified at the new
     narrow/edge end — not just the files the diff touched? (grep newly-nested trees for
     viewport-only Tailwind breakpoints `sm:`–`2xl:` — they don't shrink with a container.)
     **Persona pass (big changes, and any change touching money, auth, or concurrency):** in the
     SAME message, spawn all four persona reviewers in parallel over the diff — `persona-hacker`,
     `persona-race-hunter`, `persona-nitpicker`, `persona-auditor`. They are attention steering, not
     redundancy: each is prompted into a different failure mode, so they find things a single
     "review this" prompt averages away. All four are read-only. Their findings join the same
     refute-then-fix path as `/code-review`'s. `persona-auditor`'s verdict is special — if it returns
     `ok: false`, the step is NOT done regardless of what the other reviewers say.
  c. **Gate — verify INTENT, not the plan.** Run the step's `verify`; require exit 0. Tick
     `plan.md`, write `steps/<id>.json` with real command output.
     **The plan is the most ignorant document in this run** — it was written before a line of code
     existed, before any edge case surfaced, before any library was found unsuitable. So when
     `verify` fails, first ask *which kind* of statement it encodes:
     - **DURABLE** (what the user must be able to do; hard constraints — perf budgets, security,
       regulatory, named copy): the failure is real. Feed it back and fix.
     - **DISPOSABLE** (a named library, a file layout, an internal helper, a step ordering the
       builder has since improved on): the *plan* is stale, not the code. **Amend `plan.md` and its
       `verify` to match what was actually learned, record the reason in `steps/<id>.json`, and
       proceed.** Never rip out a correct discovery to satisfy a pre-code guess.
     A step may only go STUCK on a DURABLE failure. Retry max 3 → then STUCK: stop, report with
     evidence (the queue parks it). Rewriting a stale disposable `verify` does NOT consume a retry.


## Phase 4 — Security pass
Run **`/security-review`** on the branch. It is branch-scoped and targets newly-introduced vulns
— NOT redundant with Greptile (~20% overlap; Michael asked, answered in spec §8.3). Confirmed
findings route to the fixer like any other bug.


## Phase 5 — User-flow verify (L2 loop, ×3 max) — against the LOCAL dev stack
App + DB running locally, test data seeded freely (for platform-repo: `pnpm dev` +
`pnpm supabase:web:start` in `~/Documents/Agent/platform-repo`). 3 explorers identify
new/changed user flows → verify each SEQUENTIALLY in a subagent via Claude-in-Chrome →
**GIF-record each flow** (`gif_creator`) into `<slug>/flows/` → fixer on failures.
**Live-gate rule kept**: the browser must exercise the REAL routed component — never a hand-rolled
harness; if a harness is unavoidable it must mount the actual component, never reimplement markup.

**Visual done-gate (Michael, 2026-07-25): ANY GUI-touching step needs TWO separate visual checks
in the real browser — green tests are not enough:**
  a. **Visually correct** — a subagent drives the REAL page via Claude-in-Chrome (screenshots +
     the JavaScript tool for computed state: element actually visible, no console errors, no
     overlapping/clipped layout at normal AND narrow widths) and confirms the feature works as a
     user would see it.
  b. **Visually good** — a SEPARATE design-review subagent judges the same screenshots against
     the frontend-design skill's bar AND the design source-of-truth doc (once it exists; until
     then, the app's own existing design tokens). "Works but looks wrong / off-brand / clunky"
     FAILS and routes back to the fixer like any other bug.


## Phase 6 — Artifacts + PR/MR
Produce: an **HTML artifact** explaining what changed and why (plain language, for Michael not
reviewers), a **mermaid diagram** of the change, and the flow GIFs. Open the PR (`gh pr create`)
or MR (`glab mr create`) with artifacts linked. Slack milestone: PR-open + artifacts.


## Phase 7 — Greptile + TREX fix-loop (×3 max) — then merge on clean
Monitor the PR/MR for new Greptile comments (poll via `gh api` / `glab api`). Each batch → fixer
subagent → push → **explicitly re-trigger the review** → poll for the new one.

**A push does NOT trigger a re-review. You must ask for it.** Greptile's documented default is to
review "every new PR when it is first opened, or when a user comments `@greptileai` on the PR" —
nothing else. This is the bug that stalled the whole queue on 2026-07-26: PR #2 was reviewed once at
13:45Z, three fix commits were pushed over the next ten hours, and the loop sat waiting for a
re-review that was never coming. **0 specs had ever completed.** So, after every push:
```
gh pr comment <n> --body "@greptileai"          # GitHub — the documented manual trigger
```
**Then poll the CHECK RUN on the head SHA — that is the authoritative signal, not `/reviews`.**
A clean re-review creates NO review object and NO comments; it only updates the check run. Polling
`/reviews` alone therefore shows the original review forever and reads as "still waiting" — which is
exactly how PR #2 sat "blocked" for ten hours while it was actually clean:
```
gh api repos/<owner>/<repo>/commits/<head_sha>/check-runs \
  --jq '.check_runs[] | select(.name=="Greptile Review")
        | {conclusion, started_at, completed_at, summary: .output.summary}'
```
- `conclusion: success` + `started_at` AFTER your push → reviewed and **clean**. Its `summary` states
  the count, e.g. `"31 files reviewed, 0 comments added."` Proceed to merge.
- `status: in_progress` → still running (it took ~9.5 min on a 31-file PR). Wait, don't re-trigger.
- No check run started after your push, 10 min after the trigger comment → INFRASTRUCTURE failure.
  Post to Slack and park. **Never read "no new findings arrived" as "the findings are fixed"** —
  distinguish *clean* (a green check run on this SHA) from *silent* (no check run at all).

Read `/reviews` and `/comments` only to enumerate the CONTENT of findings once a check run tells you
a review exists.

Enabling auto-review-on-new-commits in the Greptile dashboard would remove the need for the trigger
comment; until someone confirms it is on, assume it is off and always comment.

**Stop bar = severity threshold OR round cap, whichever comes first.** Loop until **no MEDIUM or
HIGH severity findings remain** — LOW/nit/style findings do NOT block the merge. Triage every
finding to HIGH / MEDIUM / LOW before fixing; if Greptile does not state a severity, the fixer
assigns one and records the reasoning in the PR thread. Endless review always finds *something*:
a loop with no severity floor will chase 1-in-a-million bugs forever and park a shippable spec.
**Exhaustion**: MEDIUM+ findings still open after 3 rounds → STOP fixing; post the PR + remaining
findings + the fixer's reasoning to Slack; park.
**Never merge with open MEDIUM or HIGH findings.** Remaining LOW findings are listed in the Slack
close-out and, if worth doing, filed as new specs in `specs/pending/` — never drive-by fixed.

**Findings live in the LINE-LEVEL comments, not the review body** (2026-07-26: PR #2's review body
was empty while three P2s sat in `pulls/<n>/comments`). Poll BOTH:
```
gh api repos/<owner>/<repo>/pulls/<n>/reviews  --jq '.[] | "\(.user.login) \(.state) \(.commit_id)"'
gh api repos/<owner>/<repo>/pulls/<n>/comments --jq '.[] | "\(.path):\(.line) \(.body[0:200])"'
```
**Always compare the review's `commit_id` to `.head.sha`** — a review on a superseded commit is not
a review of what you are about to merge, and it looks identical in the UI.

**TREX** (Greptile's execution layer — it RUNS the code, ~20% more bugs caught, **$2/run** on top of
the review cost) reports through the same comment stream, so no separate polling is needed. It is a
**dashboard-side setting, not a repo config file** — there is nothing in-tree to grep, so if TREX
coverage matters for a PR, confirm it is enabled on the Greptile dashboard rather than inferring it
from the repo.

**AUTO-MERGE ON CLEAN (Michael, 2026-07-26 — supersedes the first-5 Slack gate below for this
path).** When a re-review comes back with **zero open MEDIUM-or-HIGH findings** on the **current
head SHA**: merge without asking (open LOW/nit findings do not block — see the severity bar above).
This is the whole point of the loop — fix, re-review, confirm, merge.
Two carve-outs stay absolute, because they are not about code quality:
1. **A clean re-review is only clean if it RAN.** Zero findings because the bot never re-reviewed
   is not zero findings. Require a review whose `commit_id` equals the current head, submitted
   AFTER the fix push — never infer clean from the absence of comments.
2. **Say what merging deploys.** If merge publishes to production (Vercel on `main` for
   platform-repo), state that in the Slack close-out with the deploy target — merging is
   the deploy, and Phase 9's monitor starts immediately.


## Phase 8 — Blast-radius gate (merge decision)
A dedicated agent assesses blast radius.
**SUPERSEDED FOR THE GREPTILE-CLEAN PATH (Michael, 2026-07-26):** a PR that has been through the
Phase-7 loop and comes back with zero open findings on the current head SHA **merges without a
Slack gate**. The scorecard below still gets its verdict recorded
for calibration — it just no longer blocks. Everything that does NOT reach Greptile-clean (parked
at exhaustion, or never reviewed) follows the original gate:

**Trust-building (original)**: for the FIRST 5 specs through this system, EVERY merge is
Slack-gated regardless of size — the agent still records its would-be verdict in
`<slug>/blast-verdict.json` AND appends it to `specs/SCORECARD.md`. After 5, post the scorecard
(verdicts vs Michael's actual approve/declines): **all 5 matched → auto-merge for small changes
enables itself and announces it on Slack**; any mismatch → stays off until
Michael flips it. Steady state: small → auto-merge; big → Slack gate
with artifacts.
**Two-tier decline**: 👎 + small note → straight back to the fixer (Phase 3). 👎 structural
("wrong approach") → decline reason seeded as a new spec in `specs/pending/`, current one closed.


## Phase 9 — Post-merge monitor (1 hour)
Watch the target's logs for 1h after merge/deploy — Vercel (platform), CloudWatch (ECS matching
service), Lambda logs (OCR), Supabase logs if the stack uses it (verify, don't assume). A new
error attributable to the merge → hotfix L2 loop (explore ×3 → fix → adversarial review) →
hotfix PR → Slack; **Michael decides the hotfix merge**.


## Phase 10 — Close-out
`RESULT.md` (PR link, artifacts, evidence — real command output) moves with the spec to
`specs/done/`. Slack summary with evidence. Out-of-scope findings discovered along the way →
new specs in `specs/pending/` — NEVER drive-by fixed.


## Runaway guard (interim)
One spec exceeding ~4 hours of active work → post a status to Slack (done / left / why slow) and
continue. No hard kill.


## Standing rules (kept verbatim from /orchestrator)
- **Minimal code, maximal clarity.** Smallest amount of code that fully and correctly satisfies
  the requirement — no speculative abstraction, no "while I'm here" refactors. Never skip error
  handling or boundary validation to save lines. Pass this to every implementer.
- **Investigate before you touch it — even in scope.** Unrelated findings are reported, never
  fixed in this diff. Before changing existing code, learn why it's written that way (git
  history, tests, comments). Run the existing tests covering touched code before calling done.
- The worker never certifies its own work. Fresh context per subagent.
- Evidence, not assertions: real commands + real output, including failures.
- "Done" is an exit code, not a claim — per-step exit-0 `verify` + the repo's done-gate backstop.
- Subagents are blind to memory/CLAUDE.md: point them at on-disk files (STATE.md, the spec) explicitly.
- **Image is truth (the project repos)**: never conclude a statement/PDF is unsolvable from
  extracted text — `scripts/verify_statement.py` + Read the PNG first; tell subagents explicitly.
- **Scaffolding-removal review**: when a run finishes smoothly, name the layer the model no
  longer needed and propose trimming it.



## Slack channel
All lifecycle Slack traffic (prototype gates, variant picks, blast-radius gates, PR milestones,
escalations) goes to **#sz-loops** (channel ID `CHANNEL_ID_REDACTED`, created 2026-07-25).
