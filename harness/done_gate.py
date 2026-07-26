"""COMPLETION GATE — the Stop-hook gate that won't let the agent declare a task done until every
requirement is proven by an EXECUTED test (exit 0), not by assertion. This is the anti-overclaiming
floor: the failure mode it kills is "I described the intended design and called it built."

How it works (Michael's design, 2026-06-19):
  1. A task started against a prompt/spec drops a CONTRACT at harness/loop/done_contract.json:
       {"task": "...", "origin_prompt": "...", "spec": "PATH.md"?, "requirements": [
           {"id": "...", "desc": "...", "test": "<shell cmd>" | null}, ...]}
  2. On every Stop, this gate re-derives the Definition-of-Done from that contract (plus, if the
     contract names a spec, every checkable clause of that spec) and RUNS each bound test.
  3. A requirement whose test EXITS NON-ZERO -> FAIL. A requirement with NO test bound -> UNVERIFIED.
     Either one BLOCKS the stop: the gate returns {"decision":"block","reason":...} and Claude is
     forced to keep working. Only when EVERY requirement is PASS (exit 0) does it allow the stop.
  4. No contract file present -> allow immediately (casual turns aren't trapped, and the floor stays
     fast — the standing proofs only run when a task is genuinely in flight).

The grader never reads code and "decides" something looks done — DONE is an exit code. The only
judgement an LLM is trusted with elsewhere (test-auditor) is whether a test is *real*; here, the
verdict is mechanical.

Entry points:
  - gate(hook_input) -> {"decision": "allow"} | {"decision": "block", "reason": ...}  (Stop hook)
  - open_contract(...) seeds a contract from the triggering prompt at task start
  - run as a module:  python3 harness/done_gate.py            # evaluate the current contract
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTRACT = REPO / "harness" / "loop" / "done_contract.json"
ARCHIVE = REPO / "harness" / "loop" / "done_contract.archive.jsonl"
TIMEOUT = int(os.environ.get("RR_DONE_GATE_TIMEOUT", "600"))

# The repo's STANDING proofs — always required before a stop while a task contract is active. These
# are the load-bearing floors that must never silently break. Kept fast/cheap on purpose.
STANDING = [
    {"id": "invariant_check", "desc": "load-bearing core golden master still SIGNs at exact CB",
     "test": "python3 scripts/invariant_check.py"},
]


def extract_clauses(spec_text: str) -> list[str]:
    """Pull the checkable clauses out of a spec: pending markdown checkboxes (`- [ ]`) and normative
    MUST/SHALL/REQUIRED lines. Satisfied boxes (`- [x]`) and plain prose are ignored. Each returned
    clause becomes an UNVERIFIED requirement until a real test is bound to it."""
    out: list[str] = []
    for line in spec_text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r'^[-*]\s+\[\s\]\s+(.*)$', s)        # unchecked checkbox
        if m:
            out.append(m.group(1).strip())
            continue
        if re.match(r'^[-*]\s+\[[xX]\]', s):              # checked checkbox -> already satisfied
            continue
        if re.search(r'\b(MUST NOT|SHALL NOT|MUST|SHALL|REQUIRED)\b', s):
            out.append(s)
    return out


def run_requirement(req: dict, cwd: Path = REPO) -> dict:
    """EXECUTE a requirement's bound test and report PASS/FAIL/UNVERIFIED by exit code. No test bound
    -> UNVERIFIED (cannot be asserted done). This is where 'tested, not read-and-wondered' lives."""
    test = req.get("test")
    if not test:
        return {**req, "status": "UNVERIFIED", "exit": None, "output": "no executable test bound"}
    try:
        p = subprocess.run(test, shell=True, cwd=str(cwd), capture_output=True, text=True,
                           timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {**req, "status": "FAIL", "exit": None, "output": f"TIMEOUT after {TIMEOUT}s"}
    except Exception as e:  # a broken test command is a FAIL, never a silent pass
        return {**req, "status": "FAIL", "exit": None, "output": f"could not run: {e!s}"[:300]}
    status = "PASS" if p.returncode == 0 else "FAIL"
    return {**req, "status": status, "exit": p.returncode,
            "output": (p.stdout + p.stderr)[-1500:]}


def evaluate(results: list[dict]) -> dict:
    """ALLOW iff every requirement is PASS. Otherwise BLOCK, naming exactly what is unproven."""
    bad = [r for r in results if r.get("status") != "PASS"]
    if not bad:
        return {"decision": "allow", "results": results}
    lines = []
    for r in bad:
        tag = "NO TEST bound" if r["status"] == "UNVERIFIED" else f"test FAILED (exit {r.get('exit')})"
        lines.append(f"  - [{r['status']}] {r.get('id', '?')}: {r.get('desc', '')}  <- {tag}")
    reason = (
        "STOP REFUSED — the task is not proven complete. These requirements are not GREEN by an "
        "executed test:\n" + "\n".join(lines) + "\n\n"
        "For each FAILED item: fix the code until its test passes. For each UNVERIFIED item: it has "
        "NO executable proof — write a failing test, make it pass, and bind the command. I do not get "
        "to assert any of these done; the gate only clears on exit-0 tests. Keep working."
    )
    return {"decision": "block", "reason": reason, "results": results}


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (REPO / q)


def load_contract(path: Path = CONTRACT) -> tuple[list[dict], dict]:
    """Build the requirement list for the active contract: STANDING floor + the contract's own
    requirements + (if it names a spec) every checkable clause of that spec as UNVERIFIED."""
    reqs: list[dict] = list(STANDING)
    data: dict = {}
    if Path(path).exists():
        try:
            data = json.loads(Path(path).read_text())
        except Exception:
            data = {}
        reqs += list(data.get("requirements", []))
        spec = data.get("spec")
        if spec and _resolve(spec).exists():
            for i, clause in enumerate(extract_clauses(_resolve(spec).read_text())):
                reqs.append({"id": f"{Path(spec).name}#{i}", "desc": clause,
                             "test": None, "source": spec})
    return reqs, data


def extract_origin(transcript_path: str) -> tuple[str, list[str]]:
    """Read a Stop-hook transcript (JSONL) and return (first user prompt, [referenced spec paths
    that exist in the repo]). Used to seed a contract from the message that triggered the work."""
    p = Path(transcript_path)
    if not p.exists():
        return ("", [])
    first = ""
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "user" or obj.get("role") == "user":
            text = _text_of(obj.get("message", obj))
            if text.strip():
                first = text.strip()
                break
    return (first, _spec_refs(first))


def _text_of(message) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            blk.get("text", "") for blk in content
            if isinstance(blk, dict) and blk.get("type") == "text"
        )
    return ""


def _spec_refs(text: str) -> list[str]:
    seen, out = set(), []
    for ref in re.findall(r'([\w./-]+\.(?:md|feature|spec|txt|json))', text or ""):
        if ref not in seen and _resolve(ref).exists():
            seen.add(ref)
            out.append(ref)
    return out


def open_contract(task: str, origin_prompt: str, requirements: list[dict] | None = None,
                  spec: str | None = None, path: Path = CONTRACT) -> dict:
    """Seed a Definition-of-Done contract at task start. requirements default to UNVERIFIED so the
    gate forces a real test to be bound to each before the task can be declared done."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    contract = {"task": task, "origin_prompt": origin_prompt,
                "requirements": requirements or [], "spec": spec}
    Path(path).write_text(json.dumps(contract, indent=2))
    return contract


def _archive(contract_path: Path, data: dict, results: list[dict]) -> None:
    """A completed (all-green) contract is moved to <dir>/done_contract.archive.jsonl and the live
    file removed — so finishing a task clears the gate and the next build prompt seeds fresh."""
    try:
        archive = Path(contract_path).parent / "done_contract.archive.jsonl"
        rec = {"task": data.get("task"), "origin_prompt": data.get("origin_prompt"),
               "outcome": "completed",
               "requirements": [{"id": r.get("id"), "status": r.get("status")} for r in results]}
        with open(archive, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        Path(contract_path).unlink()
    except Exception:
        pass


def is_misseeded(contract_path: Path) -> bool:
    """True when this contract should never have existed, so the gate must RETIRE it.

    2026-07-26: autoseed mis-seeded a contract from an analysis question ("hypothesize any way to
    get the lifecycle run faster"). The classifier was then fixed, but the already-written contract
    kept blocking the Stop hook forever -- its one requirement was {"test": null} on a deliverable
    that cannot exist, so no work could turn it green. The only escapes were deleting the file or
    RR_DONE_GATE_ONESHOT: both bypasses, and a gate people learn to bypass protects nothing.

    The general defect matters more than that one stuck turn: every future improvement to the
    classifier orphans the contracts its previous version mis-seeded. A gate that cannot recognise
    its own false positives is a gate that teaches its own circumvention.

    Deliberately narrow -- ALL of these must hold:
      - `origin_prompt` is present AND the CURRENT classifier rejects it, and
      - no requirement carries an executable test (nothing real was ever bound), and
      - the contract names no spec.
    A contract with a bound test, or one naming a spec, is REAL work and stays enforced regardless
    of what the classifier now makes of the wording that started it. Absent provenance means we
    cannot PROVE it was mis-seeded, so it is enforced too -- "unprovable" resolves toward the gate,
    never away from it.

    Classification is DELEGATED to autoseed.is_gateable, never re-implemented here: a rule living
    in two copies diverges, and the copy nobody updated is the one that fires.
    """
    try:
        data = json.loads(Path(contract_path).read_text())
    except Exception:
        return False
    origin = (data.get("origin_prompt") or "").strip()
    if not origin:
        return False
    if data.get("spec"):
        return False
    for r in data.get("requirements") or []:
        if (r.get("test") or "").strip():
            return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import autoseed
        return autoseed.is_gateable(origin)[0] is False
    except Exception:
        # Cannot consult the classifier -> cannot prove mis-seeding -> keep enforcing.
        return False


# The user withdrawing a request they made. Two shapes: "I'll take it over" and "drop it".
#
# Requires a FIRST-PERSON marker followed by a TAKEOVER VERB within one clause -- not merely a verb
# somewhere in the message. That distinction is what keeps ordinary follow-ups out: "I'll review it
# once you're done" and "Let me know when it's finished" both carry the marker, and neither is a
# retraction, because `review` and `know` are not takeover verbs.
RETRACTION = re.compile(
    r"\b(?:i'?ll|i will|let me|i'?m going to|i'?ve got|i have got)\b[^.!?\n]{0,80}?"
    r"\b(?:do|doing|write|writing|handle|handling|take|taking|start|starting|spec|build|create)\b"
    r"|\bnever\s?mind\b"
    r"|\bforget it\b"
    r"|\bdon'?t worry about it\b"
    r"|\bleave it (?:with|to) me\b"
    r"|\bi'?ve got this\b",
    re.I)


def _user_messages(transcript_path: str) -> list[str]:
    """Every USER message in order. Assistant turns are excluded deliberately: only the person who
    made a request may withdraw it, or the model could talk itself out of the gate."""
    try:
        p = Path(transcript_path)
        if not p.exists():
            return []
    except Exception:
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        role = obj.get("type") or obj.get("role")
        if role != "user":
            continue
        # A user-role envelope can also carry tool results; only real text counts.
        text = _text_of(obj.get("message", obj))
        if text.strip():
            out.append(text.strip())
    return out


def is_retracted(contract_path: Path, transcript_path) -> bool:
    """True when the USER explicitly took this work over, so the contract must be retracted.

    2026-07-26: Michael asked "you can write the spec in that folder" -- a genuine build request,
    correctly gated -- and in the next message said "Let me open up a new terminal and I'll start
    doing the spec for it in that folder." The deliverable moved to him, but gate() only knows how to
    ask whether requirements are green, so the turn blocked on work that was no longer mine. The only
    escapes were deleting the file or RR_DONE_GATE_ONESHOT: both bypasses.

    This is NOT the mis-seeding case. That prompt was never a build request; this one was, and was
    withdrawn. Keeping them separate stops a real request being waved through as "mis-seeded".

    Narrow, and the evidence requirement is the design:
      - the retraction must be a REAL USER message, found in the transcript, that appears AFTER the
        message the contract was seeded from. Mid-turn messages are routine here, so an early
        "I'll do it myself" must not retroactively cancel a later request.
      - no requirement may carry an executable test, and no spec may be named -- if either holds,
        real work is in flight and it stays enforced.
      - no usable transcript means unprovable, and unprovable resolves toward the gate.
    """
    try:
        data = json.loads(Path(contract_path).read_text())
    except Exception:
        return False
    if data.get("spec"):
        return False
    for r in data.get("requirements") or []:
        if (r.get("test") or "").strip():
            return False
    origin = (data.get("origin_prompt") or "").strip()
    if not origin or not transcript_path:
        return False

    msgs = _user_messages(str(transcript_path))
    if not msgs:
        return False

    # Locate the request, then consider only what the user said AFTER it.
    key = origin[:60]
    start = next((i for i, m in enumerate(msgs) if key and key in m), None)
    if start is None:
        return False
    return any(RETRACTION.search(m) for m in msgs[start + 1:])


def _retire(contract_path: Path, outcome: str = "retired", reason: str | None = None,
            evidence: str | None = None) -> None:
    """Archive a contract that should not be enforced, WITH its reason, then remove it.

    Recorded rather than silently deleted: a gate that quietly drops contracts is
    indistinguishable from a broken gate, and the archive is the only place a false positive can
    be counted. If these records start piling up, the classifier -- not the gate -- is the bug.

    `evidence` carries the user's own words for a retraction. A retraction recorded without the
    words that justified it cannot be audited, which makes it a bypass wearing a label.
    """
    data = {}
    try:
        data = json.loads(Path(contract_path).read_text())
    except Exception:
        pass
    archive = Path(contract_path).parent / "done_contract.archive.jsonl"
    rec = {"task": data.get("task"), "origin_prompt": data.get("origin_prompt"),
           "outcome": outcome,
           "reason": reason or
                     "mis-seeded: the current classifier does not consider this prompt a build "
                     "request, and the contract bound no test and named no spec"}
    if evidence:
        rec["user_message_that_retracted_it"] = evidence[:400]
    with open(archive, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    Path(contract_path).unlink()


def contract_path_for(hook_input: dict) -> Path:
    """Resolve which contract file this hook invocation should read/write. An explicit "contract"
    override always wins (that's what the whole test_done_gate.py suite pins). Otherwise, scope by
    the hook payload's `session_id` -- a stable per-conversation UUID Claude Code sends on every
    hook call -- so concurrent terminals against the same repo never clobber or block on each
    other's in-flight task. No session_id at all (a manual `python3 harness/done_gate.py` run with
    no stdin) falls back to the bare global CONTRACT, unchanged from before this fix."""
    override = hook_input.get("contract")
    if override:
        return Path(override)
    session_id = hook_input.get("session_id")
    if session_id:
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', str(session_id))
        return CONTRACT.parent / f"done_contract.{safe}.json"
    return CONTRACT


def gate(hook_input: dict) -> dict:
    """Stop-hook entry. ALLOW fast when no contract is active; otherwise RUN every requirement and
    BLOCK until all are GREEN. stop_hook_active does NOT auto-pass incomplete work — the platform's
    block-cap is the loop guard. Explicit RR_DONE_GATE_ONESHOT=1 is the only escape hatch.
    On all-green, the contract is archived (the task is, by definition, complete)."""
    contract_path = contract_path_for(hook_input)
    if not contract_path.exists():
        return {"decision": "allow"}
    if hook_input.get("stop_hook_active") and os.environ.get("RR_DONE_GATE_ONESHOT") == "1":
        return {"decision": "allow"}
    # A contract the classifier now rejects was never a task. Retire it rather than enforce an
    # unsatisfiable requirement forever -- see is_misseeded() for why this is narrow on purpose.
    if is_misseeded(contract_path):
        try:
            _retire(contract_path)
        except Exception:
            pass
        return {"decision": "allow"}
    # The user took the work over. Distinct from mis-seeding: this WAS a build request, and it was
    # withdrawn. Archived with the user's own words so the decision is auditable.
    tpath = hook_input.get("transcript_path")
    if is_retracted(contract_path, tpath):
        quote = ""
        try:
            data = json.loads(Path(contract_path).read_text())
            msgs = _user_messages(str(tpath))
            key = (data.get("origin_prompt") or "")[:60]
            start = next((i for i, m in enumerate(msgs) if key and key in m), -1)
            quote = next((m for m in msgs[start + 1:] if RETRACTION.search(m)), "")
        except Exception:
            pass
        try:
            _retire(contract_path, outcome="retracted",
                    reason="the user explicitly took this work over in a later message; the "
                           "deliverable is no longer the assistant's and the contract bound no test",
                    evidence=quote)
        except Exception:
            pass
        return {"decision": "allow"}
    reqs, data = load_contract(contract_path)
    results = [run_requirement(r) for r in reqs]
    verdict = evaluate(results)
    if verdict["decision"] == "allow":
        _archive(contract_path, data, results)
    return verdict


def _emit(verdict: dict) -> None:
    """Print the Stop-hook decision protocol to stdout. block -> {"decision":"block","reason":...}.
    The Stop-hook JSON schema only accepts "block" (or omitted) for `decision` -- there is no "allow"
    value. On the allow path we print an empty object, which the schema accepts and means "continue,
    no comment" (see https://code.claude.com/docs/en/hooks.md)."""
    if verdict.get("decision") == "block":
        print(json.dumps({"decision": "block", "reason": verdict["reason"]}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
    hook_input = {}
    if raw.strip():
        try:
            hook_input = json.loads(raw)
        except Exception:
            hook_input = {}
    verdict = gate(hook_input)
    # human-readable to stderr (shown in hook logs), protocol JSON to stdout
    if verdict.get("decision") == "block":
        sys.stderr.write(verdict["reason"] + "\n")
    _emit(verdict)
    sys.exit(0)
