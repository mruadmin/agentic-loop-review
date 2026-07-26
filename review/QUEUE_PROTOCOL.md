# specs/ — the L4 spec queue

> Created 2026-07-25 per `docs/SPEC_lifecycle_spec_queue_2026-07-25.md` (§4). Drained by the
> `/spec-queue` worker; each spec is processed end-to-end by `/lifecycle`.

## Folders (folder-move = state transition; the move into `active/` is the lease)

| Folder | Meaning |
|---|---|
| `pending/` | One `.md` per spec, waiting to be picked up. Michael (via `/spec_developer`) or producers (log-sweep loop, the lifecycle filing out-of-scope findings) drop specs here. |
| `active/` | The ONE spec being actively worked. Never more than one file here. |
| `waiting/` | Parked at a human gate (prototype 👍, big/sacred blast radius, vague-spec questions, Greptile exhaustion). A Monitor watches Slack for Michael's reply and resumes. |
| `done/` | Completed, each with a `RESULT.md` (PR link, artifacts, evidence). |
| `stuck/` | Failed 3 lifecycle attempts → `STUCK.md` report + Slack alert. |

## Spec front-matter

```yaml
---
repo: platform-repo        # target repo name (directory under ~/Documents/Agent or ~/Documents)
priority: high                     # optional; default normal
kind: fix                          # fix | feature — picks the L3 loop (see Route rule); default inferred
---
```

Platform is derived from the repo's git remote: GitHub → `gh`, GitLab → `glab`.

## Rules (decided 2026-07-25, all Michael's)

- **Route rule (added 2026-07-26)**: there are TWO L3 loops. Bug fixes / red tests / regressions /
  refactors / config+doc repairs go to `lifecycle-fix` (diagnose -> fix -> verify -> one review -> PR).
  New features / new screens / new capabilities go to `lifecycle-run` (tools gate -> reviewed plan ->
  build -> user flows -> artifacts -> PR). Declare it with `kind: fix` or `kind: feature` in
  front-matter; when unsure, route to `lifecycle-fix` — being wrong that way is cheap.
- **Pick rule**: **re-prioritise, never pre-sequence.** Judge "what is most important *now*" fresh
  after every completion — `priority: high` wins outright, otherwise weigh unblocking-power,
  staleness and risk; mtime is a tiebreaker, not the rule. Never order the whole backlog up front
  (that freezes it at the moment of least knowledge). One in `active/` at a time.
- **Park & continue**: a spec at an unanswered human gate moves to `waiting/` and the worker
  starts the next spec — one *active* spec, any number *waiting on Michael*. The queue never
  stalls overnight.
- **Vague specs**: too thin to build → worker generates the `/spec_developer`-style clarifying
  questions, posts them to Slack, parks in `waiting/`, moves on. Answers sharpen the spec →
  back to `pending/`.
- **Slack cadence**: milestones + escalations only (~3–4 messages per spec: start, PR-open with
  artifacts, merge/close, plus human gates/escalations). No per-phase chatter.
