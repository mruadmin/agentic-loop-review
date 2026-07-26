---
name: spec-queue
description: The L4 worker loop — drains specs/pending/ by running each spec through /lifecycle, reports milestones to Slack, parks specs at human gates without stalling the queue, and handles retries/stuck specs. Runs as an always-open session on this machine (decided 2026-07-25). Use when Michael types /spec-queue, says "start the queue" / "drain the specs", or when resuming the standing worker session.
argument-hint: (no arguments — drains specs/pending/ until empty, then idles on a wakeup timer)
allowed-tools: Bash, Read, Edit, Write, Grep, Glob, Task, Skill, Workflow, ScheduleWakeup, AskUserQuestion, Monitor, TaskCreate, TaskUpdate, TaskList
---

# /spec-queue — the L4 worker that drains the spec queue

Built 2026-07-25 from `docs/SPEC_lifecycle_spec_queue_2026-07-25.md` (§4; finalized, all
decisions Michael's). The queue lives at
`/path/to/repo/specs/` — see `specs/README.md`.
**Runs as an always-open session on THIS machine** (course pattern; revisit a server only if
machine-sleep becomes a problem).


## The loop

1. **Pick — RE-prioritise, never pre-sequence.** Read the titles/front-matter of everything in
   `specs/pending/` and choose **the single next most important spec, judged fresh each time**.
   `priority: high` still wins outright; otherwise weigh unblocking-power, staleness and risk —
   mtime is a tiebreaker, NOT the rule. Queue empty → step 6.
   **Never generate an ordering for the whole backlog.** Sequencing the queue up front freezes it
   at the moment of least knowledge (waterfall). Every completed spec unblocks work, makes other
   specs obsolete, and reveals things the original order could not know — so the question
   "what is most important *now*" gets asked again after every single completion, from scratch.
   If a spec has been made obsolete by work already merged, close it to `done/` with a
   `RESULT.md` marked obsolete rather than building it.
2. **Lease** — `git mv` (or `mv`) the spec into `specs/active/`. **One spec in `active/` at a
   time, ever.** If `active/` is non-empty on startup, resume that spec first (a previous session
   died mid-run — reload its `.claude/orchestrator/<slug>/plan.md` state and continue from the
   first unticked step).
3. **ROUTE, then DISPATCH — never execute it yourself.** There are **two** L3 loops, and picking the
   wrong one is expensive. Decide from the spec's `kind:` front-matter, or infer it if absent:

   | The spec is… | Loop | Why |
   |---|---|---|
   | a **bug fix**, red/failing test, regression, refactor, import/packaging/config repair, doc correction | `lifecycle-fix` | You already know what's wrong. It needs diagnosis and a smallest diff, not a plan. |
   | a **new feature**, new screen, new capability, new integration | `lifecycle-run` | Genuinely undetermined — needs buy-vs-build research, a reviewed plan, user-flow verification. |

   Slack milestone on start (say which loop), then:
   ```
   Workflow({ name: "lifecycle-fix", args: { spec: "<abs path to specs/active/<spec>.md>" } })
   Workflow({ name: "lifecycle-run", args: { spec: "<abs path to specs/active/<spec>.md>" } })
   ```
   **When in doubt, route to `lifecycle-fix`.** If it turns out the spec really needed a plan, the
   fix loop will go STUCK cheaply and you can re-dispatch to `lifecycle-run`. The reverse mistake is
   not cheap: on 2026-07-26 a 53-line red-test spec went through `lifecycle-run` and burned
   **1.7M tokens and 36 minutes to change six lines** — it inherited the tools gate, 3 explorers,
   4 plan reviewers, a four-persona pass per step, a security pass and an HTML artifact. The course
   designs these as two separate loops precisely so a bug fix does not pay for a feature's apparatus.
   This runs in the background and returns ONE small object:
   `{ outcome: DONE|PARKED|STUCK, slug, branch, pr_url, evidence_path, gate?, question?, monitor? }`.
   **Never invoke `/lifecycle` as a skill from this loop** — a skill loads into YOUR context, and one
   lifecycle's plan, diffs, review bodies and `gh api` dumps will end this session's ability to drain
   the queue. See "What this session is allowed to hold" below.
   ~3–4 Slack messages per spec total (start, PR-open + artifacts, merge/close, plus human
   gates/escalations) — no per-phase chatter.
4. **On outcome** (read only the returned object — do NOT open the plan, the diff, or the review
   threads to "check". That detail is in `evidence_path` on disk, which is where it belongs):
   - **DONE** → spec + its `RESULT.md` move to `specs/done/`. Slack close-out summary, naming the
     deploy target if merging shipped to production.
     **If `monitor` came back non-null, start the post-merge watch DETACHED and pick the next spec
     immediately** — schedule it with `Monitor`/`TaskCreate` against `monitor.log_source` for
     `monitor.minutes`. An hour of log-watching must never hold the queue. A new error attributable
     to the merge → file it as a hotfix spec in `pending/` marked `priority: high`; Michael decides
     the hotfix merge.
   - **PARKED** (human gate: tools-decision, prototype 👍, blast-radius approval, Greptile
     exhaustion; or vague spec) → move to `specs/waiting/` with a `WAITING.md` note (which gate,
     what was asked, Slack permalink) and **immediately pick the next spec** — park & continue;
     the queue never stalls overnight. One *active* spec, any number *waiting on Michael*.
   - **STUCK** (a lifecycle step failed verification 3 times, or the whole lifecycle failed
     3 attempts) → move to `specs/stuck/` with a `STUCK.md` report (what failed, evidence,
     best hypothesis) + Slack alert. Never silently retry forever, never declare partial work done.
5. **Resume watch** — a background Monitor (or each wakeup) checks Slack for Michael's
   👍/👎/replies on parked specs. **On consuming ANY gate answer, immediately thread-reply a
   receipt as the Jarvis bot** ("<answer> received and recorded, resuming at <step>") — Michael
   must never have to wonder whether his reply was seen (2026-07-25: a v3 pick sat unread for
   20+ min with no way for him to tell). An answer without a posted receipt is NOT consumed.
   - Gate answered → move the spec back to `pending/` front-of-queue (touch its mtime older than
     everything else or mark `priority: high`) with the answer recorded in the spec file.
   - **Two-tier decline** (blast-radius gate): 👎 + small note → back to `pending/` flagged
     "resume at fixer with note". 👎 structural → seed the decline reason as a NEW spec in
     `pending/`, move the old one to `done/` with `RESULT.md` marked declined-structural.
6. **Idle** — queue empty and nothing resumable → `ScheduleWakeup` (20–30 min) and end the turn;
   on wake, re-scan `pending/` + `waiting/`.


## What this session is allowed to hold (the reason the queue can run all night)

This worker is an **always-open session draining a 20+ spec backlog**. It only survives that if its
context stays roughly constant per spec. So:

**MAY live in this context** — the pending/active file listing, the pick reasoning, the small object
each `lifecycle-run` returns, Slack message text, and the folder moves.

**MUST NOT** — a plan, a diff, a file's contents, a review body, `gh api` output, screenshots, or a
sub-agent's full report. Every one of those belongs to a sub-agent's context and lands on disk under
`.claude/orchestrator/<slug>/`. If you catch yourself reading a file inside the target repo, you have
already left this loop's job.

**Sub-agents hand off to each other inside the workflow script, not through you.** Explorer output
feeds the planner, the planner feeds the implementer, the implementer feeds the reviewer — all as
values in `.claude/workflows/lifecycle-run.js`. You are the dispatcher and the reporter. That is all.

If context still creeps past ~50%, finish the current spec, write the queue state to disk, and start
a fresh session rather than compacting mid-spec — compaction is exactly where "which step am I on and
what did `verify` actually print" gets lost.


## Vague specs (decided: self-interview via Slack)

If a picked spec is too thin to build from (no clear behavior, no target repo, unanswerable
scope), do NOT guess: generate the clarifying questions a `/spec_developer` interview would ask,
post them to Slack, park the spec in `waiting/`, move on. When answers arrive, fold them into the
spec file and return it to `pending/`.


## Producers (who feeds `pending/`)

- Michael, via `/spec_developer` (writes the spec file directly into `pending/`).
- The log-sweep loop (§8.1 — auto-filed bug specs with log evidence, fingerprint-deduped).
- `/lifecycle` itself: out-of-scope findings become new specs here — never drive-by fixed.


## Rules

- Subagents/lifecycle runs are blind to memory — the spec file plus on-disk docs (STATE.md,
  must carry all context.
- Folder-move is the ONLY state mechanism — no separate ledger to drift out of sync.
- Spawn subagents with `mode: "bypassPermissions"` (pre-approved 2026-07-04); hard hook denials
  still apply.
- Evidence, not assertions, in every Slack message: real command output, PR links, artifacts.
- Trust-building merge gate state (first-5 scorecard) lives in `specs/SCORECARD.md` — see
  /lifecycle Phase 8.

## Slack channel & sender identity (2026-07-25 — notifications were being missed)
All worker reporting (milestones, human gates, escalations, digests) goes to **#sz-loops**
(channel ID `CHANNEL_ID_REDACTED`, created 2026-07-25). Never post loop traffic to #general.

**Send AS THE JARVIS BOT, never as Michael.** The MCP router Slack backend and the Claude plugin
both authenticate as Michael's own user (xoxp) — Slack treats those as self-messages and never
push-notifies him, so he silently missed human gates. Fix:
- Send via `code-tools/slack/notify.py` (Jarvis bot, `SLACK_JARVIS_BOT_TOKEN` xoxb):
  - **Human gates + escalations + stuck alerts** → DM Michael (`--to USER_ID_REDACTED`) — bot DMs
    ALWAYS push-notify — AND post the same to #sz-loops for the record.
  - **Milestones (start / PR-open / merge / close-out)** → #sz-loops as Jarvis; add
    `<@USER_ID_REDACTED>` only when his action is needed.
- The router/plugin user-token paths remain fine for READING Slack (checking his replies) —
  never for anything that needs him notified.
- If `notify.py` fails (missing/invalid token), do NOT fall back to the user-token path and call
  it delivered — surface the failure loudly.
