---
name: atomic
description: Execute a task ONE atomic unit at a time through the planner→implementer→tester→reviewer pipeline, marking each unit done only when a bound test passes. Use for any multi-step feature so big tasks don't get done in one undisciplined, overclaim-prone pass.
argument-hint: [spec-or-task description]
allowed-tools: Bash, Read, Edit, Write, Task, Grep, Glob
---

# /atomic — one reviewed unit at a time

The discipline that stops "done in one big pass." Drive the pipeline EXPLICITLY (auto-delegation is probabilistic — invoke each subagent yourself).

Procedure:
1. **Plan.** If there's no active `harness/loop/done_contract.json`, create one: for a spec, run `/spec-plan <spec>`; otherwise invoke the `planner` subagent (Task tool, `subagent_type: planner`) to decompose `$ARGUMENTS` into an ordered list of atomic, independently-testable units, each with a Definition-of-Done.
2. **Pop ONE unit.** Take the single next not-done requirement. State its DoD and the exact test that will prove it. Do not start a second unit.
3. **Implement.** Invoke the `implementer` subagent on THAT ONE unit only (TDD: RED test → minimal code → GREEN). Smallest diff; no scope creep.
4. **Test.** Invoke the `tester` subagent: run the unit's test + the surrounding fast suite, capture real output, strengthen the test so it would FAIL if the code were wrong.
5. **Review.** Invoke the `reviewer` subagent (read-only) on the diff vs the unit's DoD. It returns `{ok, reason, findings}`. If `ok:false`, feed `reason` back to the implementer and return to step 3.
6. **Mark done.** Only when the bound test exits 0 AND the reviewer says ok: bind that test command into the contract requirement (so `done_gate.py` will keep it green) and mark the unit done.
7. **Loop** to step 2 until every unit is done. Then let the completion gate confirm all-green.

Rules:
- One unit at a time. Never batch. Report out-of-scope findings; do not act on them.
- Nothing is done by inspection — only by a passing bound test plus a read-only review.
- Keep the full fast suite green between units (anti-regression).
- Respect the live guards: PreToolUse denies secrets/live-weights/destructive ops; PostToolUse py_compiles every edit.
