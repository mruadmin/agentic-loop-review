Correction to what I sent you, and it changes the headline.

I said the loop arms produced no working code. That was wrong. I checked each worktree with `git diff` — working tree against HEAD — which by construction can't show work that was **committed**. Checked against the base branch instead, three of the four arms had a one-line fix and a passing test sitting on a branch.

| Arm | Agents | Wall clock | Fix produced? | What the loop *reported* |
|---|---|---|---|---|
| L3 `lifecycle-run` | 54 | 79.3 min | yes — committed, test passes | STUCK |
| L3 `lifecycle-fix` | 10 | 46.8 min | yes, never committed | STUCK |
| L3 + caps/watchdog/probe | 48 | 67.5 min | yes — committed, test passes | ceiling hit, no verdict |
| One agent, own process | 1 | 20 min | yes — committed, test passes | success, accurately |

So: all three loop arms solved the bug and none of them could tell us they had. The efficiency gap is unchanged — 48 agents and 68 minutes against 1 agent and 20 minutes for the same one-line change — but "the loop can't fix a three-line bug" was false, and the single agent's real advantage was that its report matched reality.

Asking *why* they all mis-reported turned out to be worth more than the original question.

Our workflow makes a git worktree per run and threads it through as `repo_path`. The agent that runs a step's verify command is told, literally, "Run EXACTLY this command in `${repo_path}`". But the **planner** prompt never mentioned `repo_path` — the only absolute path in its context came from the main repo's STATE.md — so it wrote, for every step:

```bash
cd /path/to/main-repo && PYTHONPATH=. python3 -m pytest harness/tests/test_x.py -q
```

A `cd` inside the command overrides the working directory the runner supplies. Verify therefore ran against a checkout that didn't contain the change, failed, and kept failing until the step was declared unverifiable. 5 of 5 verify commands in the real plan file pointed at the wrong tree.

Two things about that are more interesting than the bug:

The probe is instructed "do not amend the verify command even if it looks wrong — that judgement belongs to the certifying verifier." Correct in general, a thermometer shouldn't doctor. But it meant the component standing closest to the evidence was structurally forbidden from acting on it.

And the certifying verifiers **did** diagnose it, repeatedly, in their own words — "a DISPOSABLE amendment to the verify's `cd` target", "PASSES in the correct worktree", "genuinely met ON THE CORRECT TARGET". The loop identified its own defect several times per run and had nowhere to put the finding. It wasn't a knowledge problem, it was a plumbing one.

The shape is what let it live: a mis-scoped verify command doesn't error. It runs, exits non-zero, and produces a confident, specific, wrong FAIL on work that is correct.

Fixed, and the repo is updated: the planner is now told where verify runs, there's deterministic enforcement that repoints the paths and logs every correction (a silently corrected plan reads exactly like a correct one, and then nobody fixes the prompt), and every STUCK verdict now carries the command that settles what's actually on the branch — plus a note that `git diff` is the wrong check, since that's the one we reached for.

Two things this makes me want to ask you:

Your loop reviews once on the whole PR diff. Does that also mean verify runs once, against the branch as a whole, rather than per step? Because per-step verify is what multiplied a single bad `cd` into fifteen failures and three STUCK verdicts. Reviewing once might be doing more work for you than the agent count suggests.

And when your verify phase can't confirm something, what does the message to the human actually say? Our verdict was accurate about the loop's confidence and silent about the branch, and we read it as a claim about the branch. Your rule that an honest "couldn't verify" beats a claimed pass covers the reporting agent's honesty — I'm asking about the reader's side of it.

Same link, updated: https://github.com/mruadmin/agentic-loop-review
