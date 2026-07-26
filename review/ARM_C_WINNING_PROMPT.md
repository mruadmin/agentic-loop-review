# Arm C — the only configuration that produced a working fix (2026-07-26)

Verbatim, so it can be re-run and compared against rather than remembered.

| arm | agents | wall clock | outcome |
|---|---|---|---|
| `lifecycle-run` baseline | 54 | 79.3 min | STUCK |
| `lifecycle-fix` | 10 | 46.8 min | STUCK |
| `lifecycle-run` + 4 levers (`wf_a34c2778-e5d`) | 48 | 67.5 min | ceiling hit, zero product code |
| **arm C (below)** | **1** | **20 min** | **working fix** |

All four were given the SAME bug: a three-line `sys.path` import fix, spec
`specs/pending/2026-07-26a-prioritize-cli-import.md`.

## Why it won — and it is NOT "less scaffolding"

The prompt is 3,814 characters. It is heavily structured. The difference is WHERE the structure
sits: every constraint is on the **goal and the evidence**, and the **process is delegated to the
agent's judgement**. `lifecycle-run` inverts this — it hard-codes the pipeline (`3 explorers ->
planner -> 4 plan reviewers -> per step: implement -> review -> refute -> fix -> verify`) and has no
mechanism to conclude that a three-line fix does not need it. Measured consequence in the levered
run: 26 review agents and 14 planners against 5 implementers — 83% deliberating, 10% building.

The load-bearing paragraph is "How to work — this part is yours to decide", and specifically
"Size the process to the actual change."

## Dispatch

    subagent_type: general-purpose
    model: opus
    isolation: dedicated git worktree (created and verified BEFORE dispatch, per
               memory: worktree-isolation-verify-first)

## The prompt, verbatim


```markdown
You are fixing ONE bug, end to end, and you own the whole process. You are blind to memory and to CLAUDE.md — everything you need is on disk at the paths below.

## Your working copy — do not leave it

`/path/to/worktrees/_wt-arm-c-prompt`

That is a dedicated git worktree already on branch `exp/arm-c-prompt`. Every command you run and every file you edit must be inside it. Two other agents are fixing this same bug in sibling worktrees right now; if you touch `/path/to/repo` or any `_wt-arm-a-run` / `_wt-arm-b-fix` path you will corrupt their work and invalidate the whole exercise. Never `cd` out of your worktree.

## Read these first

- `/path/to/repo/specs/pending/2026-07-26a-prioritize-cli-import.md` — the bug, its scope, its constraints, and its Done bar. **The Done bar is your contract.** Every numbered item must be satisfied.
- `/path/to/worktrees/_wt-arm-c-prompt/STATE.md` — what this system currently IS. Never assert what it does from priors.

## How to work — this part is yours to decide

You choose your own process. You may do it all yourself, or spawn sub-agents for whatever you judge worth delegating (exploration, review, verification). **Size the process to the actual change.** A one-line fix does not need a research phase, a plan review, or four reviewers; a subtle one might need more than you'd guess. Deciding that proportion well is the point of this exercise, so spend your judgement there rather than defaulting to either extreme.

## Non-negotiable, regardless of the process you choose

1. **RED before GREEN.** Reproduce the failure and paste its real output before you change anything. A fix you cannot demonstrate was needed is not evidence of anything.
2. **Root cause, not symptom.** Say the exact mechanical reason — which name failed to resolve, looked up on which path, under which invocation. If you cannot state the cause in one sentence, you have not found it yet, and making the error message disappear is not the same as fixing it.
3. **"Done" is an exit code, not a claim.** Run every command in the Done bar and paste its real output, including anything that failed. Never report a step as passing that you did not actually run.
4. **Verify independently of the fix.** Whoever wrote the change does not get to be the only one who says it works. Either spawn a fresh read-only agent to re-run the Done bar and inspect the diff, or — if you do it yourself — re-run every check from a clean shell after the edits, and paste that output separately from the output you got while iterating.
5. **Smallest diff that fully fixes the cause.** Never skip error handling to save lines. No "while I'm here" refactors. The spec's Scope section binds you; unrelated defects get REPORTED at the end, never fixed.
6. **Commit your work** on `exp/arm-c-prompt` with a message explaining WHY. Do not push, do not open an MR, do not merge.

## What to return

Return ONLY this, compactly — your text is data for a comparison, not a message to a person:

- `root_cause`: one sentence, with file:line.
- `diff_stat`: files changed / insertions / deletions, from `git diff --stat`.
- `done_bar`: each numbered item from the spec with PASS or FAIL and the command you ran.
- `process`: what you actually did, and how many sub-agents you spawned (0 is a fine answer).
- `verified_by`: how requirement 4 was met, concretely.
- `out_of_scope_found`: anything real you noticed and deliberately did not fix.
- `commit`: the SHA.

If you cannot satisfy the Done bar, say so plainly and say which item failed and why. A truthful FAIL is worth more here than a confident overclaim — the entire reason this comparison exists is to catch work reported done when it wasn't.
```
