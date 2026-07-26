# BELIEFS — assumptions about running coding agents, and what we actually know

> One row per assumption. `log.py append` will REFUSE a ledger row whose hypothesis id is not
> listed here, so the falsifier has to be written before the run that tests it.
>
> `source`: `internal` (ours) · `course` (Agentic Coding School) · `web` · `convergent` (two or more
> independent sources agree — the strongest signal).
> `status`: `untested` · `supported` · `refuted` · `inconclusive`.
>
> Last updated 2026-07-26.

---

## H1 — More sub-agents produce a better fix

- **source:** internal (the assumption the whole L3 loop was built on)
- **falsifier:** a cheaper arm produces a fix that passes its own verify AND survives review, while
  the expensive arm does not.
- **status:** **refuted** (2026-07-26)
- **what happened:** on one three-line import fix — `lifecycle-run` 54 agents / 342k output /
  79.3 min → **STUCK**; `lifecycle-fix` 10 agents / 83k / 46.8 min → **STUCK**; a plain single-agent
  prompt 8k / 20 min → **working fix**. Quality did not scale with agent count. See E1–E4.
- **caveat that keeps this honest:** the expensive arm did write the *better* code (it guarded against
  a `sys.path` entry leaking on every call, which the cheap arm missed). More agents bought a better
  *diff* and a worse *outcome*. Both halves of that are true.

## H2 — The loop's expensive review should run after a cheap deterministic check, not before

- **source:** **convergent** — Boris Cherny (creator of Claude Code, his most-cited tip: "cheap,
  rapid verification checks before expensive reviews"); the "Loops Win Where Verification Is Cheap"
  write-up; and our own per-agent trace showing four build cycles each paying a ~50k review round on
  code that never ran.
- **falsifier:** re-running the same spec with the probe-first ordering costs the same or more.
- **status:** **untested** — mechanism built 2026-07-26 (`ff30d30`), measurement pending (E5).

## H3 — Plan review is waste on a tiny plan

- **source:** **convergent** — the course's own scope note on "Automatic Plan Reviewing with
  Subagents" ("not for tiny changes where hook overhead dominates"), plus our trace showing ~80k
  tokens and ~14 min of planner + 4 lenses + a full re-plan spent on a ONE-step plan.
- **falsifier:** a tiny-plan run that skips plan review ships a defect the plan review would have
  caught.
- **status:** **untested** — mechanism built 2026-07-26, measurement pending (E5).

## H4 — The four levers together make the loop cheaper without making it worse

- **source:** internal (synthesis of H2, H3, the watchdog, and spec preflight)
- **falsifier:** the levered run costs ≥ half the baseline (342k output / 79.3 min / 54 agents), OR
  it ships a fix that fails its own verify or is blocked by review.
- **status:** **untested** — E5 running.

## H5 — Bad specs cost more than bad loops

- **source:** internal (2026-07-26 post-mortem)
- **falsifier:** a run dispatched from a preflight-clean spec still fails for reasons traceable to
  the spec rather than the code.
- **status:** **supported, weakly** — both STUCK arms traced to one false sentence in a spec
  ("this is the last file with this defect… there are none"), not to loop behaviour. ~3.7M subagent
  tokens. One incident is not a law; recorded as weak until a preflight-clean run completes.

## H6 — A silent agent is hung, not slow

- **source:** internal (observed twice: three Explore agents emitting ~0 output for 13.4 min then
  finishing in 2.5 on respawn; and a mermaid-cli call that hung 7.6 hours on 2026-07-25)
- **falsifier:** a run where abandoning a silent agent loses work that would have completed.
- **status:** **untested** — watchdog built 2026-07-26; the 6-minute threshold is a guess, not a
  measurement, and is the first thing to tune if it misfires.

## H7 — A prompt asking for prior art produces reuse without a dedicated reviewer

- **source:** internal
- **falsifier:** the one-sentence prior-art prompt misses reuse that a dedicated lens catches.
- **status:** **inconclusive** (2026-07-26). The one-sentence arm DID find `config.REPO` and cited
  three call sites — then deliberately chose the other idiom. Inconclusive because the repo has **no
  house style** to be right about: 19 files re-derive the repo root, 15 import `config.REPO`. The
  experiment could not separate "missed the prior art" from "picked the other valid pattern."
  **Action this implies is a code change, not another test:** pick one pattern (the course's
  "One-Pattern Rule for Agents") so the question becomes answerable.

## H8 — A PreToolUse guard is still the right place for destructive-command safety

- **source:** internal, challenged by Michael 2026-07-26 ("written months ago based on an old best
  practice")
- **falsifier:** native `permissions.deny` rules cover the same cases.
- **status:** **partially refuted.** Native rules DO cover path denies — but only for `Read()` and
  `Edit()`; `Write(path)` deny rules are accepted and **never enforced**, and no pattern can express
  "`rm -rf` confined to this directory." So content-based and stateful logic still needs a hook.
  Narrowed both guards 13 → 8 DENY rules; the real culprit was a **divergent global copy** running
  pre-narrowing rules that had never received any of Michael's corrections.

## H9 — A very fast open-weights model on the CHEAP stages cuts wall-clock without lowering pass rate

The version Michael proposed (2026-07-26) was broader — Claude as planner, Qwen3-235B on Cerebras
(~800+ tok/s) as the coder. Narrowed to the cheap stages for two reasons found while checking it.

- **source:** internal (measured: **87.5% of loop wall-clock is model generation**, 12.1% tool
  execution — so generation speed is the dominant term and nothing else on the lever list attacks it)
  + Michael, web (Cerebras throughput claims)
- **falsifier:** the loop's wall-clock does not drop materially, OR a fast-model probe/classifier
  returns a wrong verdict that a Claude-run stage would have got right (in which case the saving is
  paid for in a re-run, or worse, a false green).
- **status:** **narrowed before testing.** Two findings shrank it:
  1. **The proposed coder is not purchasable.** Cerebras' *public* endpoints carry exactly three
     models (verified against their model catalog and Michael's own playground dropdown,
     2026-07-26): `gpt-oss-120b` (~3000 tok/s, the only **production** one), `zai-glm-4.7` (355B,
     ~1000 tok/s, **preview**, and **scheduled for deprecation 2026-08-17**), `gemma-4-31b`
     (~1850 tok/s, preview). **Qwen3-235B-A22B and Qwen3-Coder-480B are Dedicated-Endpoints only** —
     reserved enterprise capacity, "contact us". A model that can be discontinued "on short notice"
     cannot sit in the critical path of a daily loop.
  2. **Speed was not the failure mode.** All three arms on 2026-07-26 failed on *judgement*:
     `lifecycle-run` went STUCK because the planner adjudicated "re-derive the repo root" and the
     implementer used `config.REPO`; the 3.7M-token afternoon came from a spec whose factual claim
     was false. A 20×-faster model reaches a wrong answer 20× sooner. Under this project's moat rule
     (abstain over guess) cheap wrongness is not a saving.
- **the testable residue:** put a fast model only where the output is short, mechanical, and
  immediately checked by the next gate — the **verify probe** (`passes: true/false` from one command),
  the **tier/blast-radius classifier**, **Explore-style file location**. Measure wall-clock delta AND
  probe-verdict agreement against a Claude-run probe on the same steps.
- **blockers to settle first:** (a) the harness — our implementer is a Claude Code subagent and
  `model` accepts only sonnet/opus/haiku/fable, so a third-party model must be called out-of-band
  (the `code-tools/review/openrouter_review.py` pattern) and a Claude agent still applies + verifies,
  which spends back part of the win; (b) **engine-adjacent code must not be sent to third-party
  inference** — probe/classifier prompts carry command output, not source, which is why those stages
  are the safe ones to try first.
- **cheaper levers to exhaust before this one, both already measured and both free:** concurrency is
  running at only **1.67×**, and the baseline lost **27.7% of wall-clock (40.4 agent-minutes across
  52 gaps) to stalls**.
- **DEFERRED BY DECISION (Michael, 2026-07-26): single-model Opus arms only, for now.** The Fireworks
  route was checked and is real — Kimi K2.7 Code ($0.95/$4.00 per M, 262k ctx), GLM 5.2 ($1.40/$4.40,
  1M ctx), Qwen3-Coder-480B and gpt-oss-120b ($0.15/$0.60) are all self-serve per-token, and the
  plumbing already exists (`code-tools/review/openrouter_review.py`, OpenRouter → Fireworks, already
  sets `provider.data_collection="deny"`). A 342k-output arm costs $0.21–$1.50 there, so cost is not
  the blocker.
  **The blocker is attribution.** Every hypothesis on this list is about SCAFFOLD shape. Changing the
  model at the same time makes a result unattributable to either variable, and there is a large
  backlog of scaffold tests (H1–H8, C1–C6) that need no second model at all. Revisit only once
  single-model arms stop distinguishing between scaffolds.
  Cost-of-arms is the one argument that could reopen it early — cheap arms to RANK scaffolds, one
  Opus arm to confirm the winner — but that trade only pays off when the arm count per day is the
  binding constraint, and it isn't yet.

---

## M1 — MEASURED, not hypothesised: token cost is context re-supply, not generation

Measured directly from the 40 agent transcripts of `wf_a34c2778-e5d` (levered `lifecycle-run`,
2026-07-26, 40 agents / ~52 min):

| | tokens |
|---|---|
| **output (generation)** | **236,936** |
| cache read + creation | **39,989,065** |
| uncached input | 1,696 |

**Generation is 0.6% of token throughput. Context re-supply is 99.4%.**

The mechanism, and why it is three levers rather than one:

    cost ≈ agents × turns_per_agent × prefix_size
           40      ×      21.3       ×    46,880

- 853 model turns across 40 agents = **21.3 turns/agent**. Cache read accrues **per API call**, so
  every turn of an agent's tool loop re-reads its entire prefix.
- **mean prefix on an agent's FIRST call: 34,919 tokens**, rising to a 46,880 mean across all turns.
  Two distinct tiers are visible: the three costliest agents carry a **43k** prefix, the rest **24k**.
  The expensive tier pays ~1.8× per turn for its whole life.

**Why this matters more than anything else on this list:** the sub-agent cap built on 2026-07-26
attacks only the FIRST multiplier (54 → 40 agents, a 26% cut). The other two are entirely unattacked
and multiply against each other. Halving the prefix and halving turns/agent would be a ~4× cost
reduction — an order of magnitude more than any agent-count change can deliver.

**Do not conflate this with the wall-clock finding.** They are different resources with opposite
shapes, and both measurements are real:

| bottleneck | measurement | the lever |
|---|---|---|
| **wall clock** | 87.5% model generation, 12.1% tool exec | concurrency (idling at 1.67×), fewer serial stages, the 27.7% stall loss |
| **token cost** | 99.4% context re-supply, 0.6% generation | smaller prefix, fewer turns/agent, then agent count |

"The loop is model-bound" is true of TIME and false of COST. Optimising for one does not optimise
the other, and a lever that trades between them needs to say which it is buying.

### What this reorders
- **C6 (cache Explore output across build cycles) is promoted to the highest-value untested claim.**
  It attacks prefix size AND turn count at once, and it was near the bottom of the list before this.
- New candidate: find why three agents get a 43k prefix and the rest 24k, and whether the extra 19k
  is load-bearing for those roles or just the CTX block accumulating.
- New candidate: measure turns-to-answer per agent ROLE. A probe that should read one exit code has
  no business taking 21 turns, and today's cheap-probe lever was never checked for turn count.

### Comparable-metric comparison vs this morning's baseline
Only OUTPUT tokens are comparable (the status line's "2.1m" is total spend incl. cache, a different
measurement — do not compare it to the 342k figure):

| | agents | output | wall clock | outcome |
|---|---|---|---|---|
| baseline `lifecycle-run` | 54 | 342k | 79.3 min | STUCK |
| levered `lifecycle-run` | **40** | **237k** | **~52 min** | 39/40 agents done |

−26% agents, −31% output, −34% wall clock. **Caveat, unchanged:** this run's planner produced ≥3
steps against the baseline's 2, so the plan-review gate (lever 3) never fired. It is not a clean A/B,
and the outcome (DONE vs STUCK) was still pending at the time of writing.

---

## Untested claims harvested but not yet tried

| id | claim | source |
|---|---|---|
| C1 | "Re-plan from scratch, don't patch a broken plan" | web (Cherny) |
| C2 | One "Staff Engineer" reviewer beats four persona lenses | web (Cherny) |
| C3 | Haiku/Sonnet for research + verify passes is enough | course ("Quick Spawning Subagents") |
| C4 | Worktrees + 10–15 concurrent sessions is the real throughput lever | web (Cherny) |
| C5 | Verifiers should "demand proof" rather than ask for confirmation | web |
| C6 | Explore output can be cached across build cycles without quality loss | internal |
| C7 | Cerebras Code ($50/mo Pro = 24M tok/day, $200/mo Max = 120M tok/day) serves a coding-grade open model at wafer-scale speed — but their pricing page never names WHICH model, and the public catalog has no coder in it. Resolve the model identity before costing this. | web (Cerebras pricing page, 2026-07-26) |
