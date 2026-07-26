#!/usr/bin/env python3
"""Verify work against a SEALED statement of intent, not against the spec's literal words.

Why this exists (2026-07-26). A `lifecycle-run` arm spent 79 minutes and 54 sub-agents going STUCK
against a spec whose factual claim was false. The work was fine; the sentence measuring it was wrong.
Worse, the loop had no way to say so — a spec clause is either satisfied or the run fails, even when
the clause itself is the defect. Real engineering diverges from the spec constantly: the spec was
written with less information than the implementer ends up having.

So verification moves to INTENT. The danger is obvious and is the thing this module is built around:
"verify the intent" is also the perfect excuse for finishing something easier and calling it the goal.
Intent verification therefore has to be HARDER to fudge than spec verification:

  1. SEALED BEFORE THE WORK. Intent is written and hashed up front (`seal`). `verify` refuses if the
     intent text changed afterwards. You cannot move the target after taking the shot.
  2. BOUND TO A COMMAND. Each clause carries a deterministic `verify:` command, or an explicit
     `judgement:` line stating why no command can express it. Neither -> refused at seal time.
     Silence never reads as a pass.
  3. DIVERGENCE DECLARED, NOT DISCOVERED. A clause may deliberately depart from the spec, but must
     say so with a reason (`diverges_from_spec:`). A departure nobody wrote down is scope drift.

The `judgement:` hatch is narrow and cannot be self-certified: `certified_by:` must be EMPTY at seal
time and filled at verification time, so whoever certifies a judgement clause is never whoever sealed
it. That one field is the sole edit the seal tolerates -- otherwise the mechanism would forbid its own
escape hatch and judgement clauses could never pass at all.

FILE FORMAT

    # INTENT — <slug>

    ## I1 — <one-line statement of what must actually be true>
    verify: <shell command; exit 0 means satisfied>

    ## I2 — <intent>
    judgement: <why no command can express this>
    certified_by:                      # left EMPTY at seal; filled by the reviewer at verify time

    ## I3 — <intent>
    verify: <cmd>
    diverges_from_spec: <what the spec said> because <why this is better>

EXIT CODES (verify)
    0  every clause satisfied
    1  a bound command failed, or a judgement clause is uncertified
    2  the intent file is malformed
    3  never sealed -- there is no pre-registered intent to verify against
    4  the intent changed after sealing (beyond `certified_by:`)

Sibling tools, deliberately separate: `harness/spec_preflight.py` checks a spec's factual CLAIMS
before dispatch; this checks the OUTCOME after. `harness/done_gate.py` remains the turn-end backstop.
Command execution is delegated to `done_gate.run_requirement` rather than reimplemented -- the repo's
gate behaviour is the last thing that should exist in two versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from harness import done_gate

HEADING = re.compile(r"^##\s+(?P<id>[A-Za-z]\w*)\s*[—:-]\s*(?P<statement>.+?)\s*$")
FIELD = re.compile(r"^(?P<key>verify|judgement|certified_by|diverges_from_spec)\s*:\s*(?P<val>.*)$")

SEAL_SUFFIX = ".seal.json"


class IntentError(Exception):
    """The intent contract itself is wrong — refuse rather than verify something meaningless."""


@dataclass
class Clause:
    id: str
    statement: str
    verify: str | None = None
    judgement: str | None = None
    certified_by: str = ""
    diverges_from_spec: str | None = None
    _lines: list[int] = field(default_factory=list)

    @property
    def is_judgement(self) -> bool:
        return bool(self.judgement) and not self.verify


def parse_intent(text: str) -> tuple[list[Clause], list[str]]:
    """Parse the intent file. Returns (clauses, errors); errors are fatal, never warnings.

    A malformed clause is an ERROR and not a skip, for the same reason spec_preflight treats a
    malformed check line as an error: the failure mode being designed against is a clause that looks
    measured and isn't.
    """
    clauses: list[Clause] = []
    errors: list[str] = []
    cur: Clause | None = None

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        h = HEADING.match(line)
        if h:
            cur = Clause(id=h.group("id"), statement=h.group("statement").strip())
            clauses.append(cur)
            continue
        f = FIELD.match(line.strip())
        if not f:
            continue
        if cur is None:
            errors.append(f"line {n}: `{f.group('key')}:` appears before any `## <id> — <intent>` heading")
            continue
        key, val = f.group("key"), f.group("val").strip()
        # strip trailing comments so `certified_by:   # filled at verify time` reads as empty
        val = re.sub(r"\s+#.*$", "", val).strip()
        if key == "verify":
            cur.verify = val or None
        elif key == "judgement":
            cur.judgement = val or None
        elif key == "certified_by":
            cur.certified_by = val
        else:
            cur.diverges_from_spec = val or None
        cur._lines.append(n)

    if not clauses:
        errors.append("no intent clauses found — expected at least one `## I1 — <intent>` heading")

    seen: set[str] = set()
    for c in clauses:
        if c.id in seen:
            errors.append(f"{c.id}: duplicate clause id — ids must be unique to be referenced")
        seen.add(c.id)
        if not c.verify and not c.judgement:
            errors.append(
                f"{c.id} ({c.statement[:60]}) is bound to NOTHING. Give it a `verify:` command, or a "
                "`judgement:` line saying why no command can express it. An unbound clause cannot "
                "distinguish done from claimed."
            )
        if c.verify and c.judgement:
            errors.append(
                f"{c.id}: has BOTH `verify:` and `judgement:`. If a command can decide it, the "
                "judgement line is an unused escape hatch — delete one."
            )

    return clauses, errors


# --- sealing --------------------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical(text: str) -> str:
    """The text the seal hashes: everything EXCEPT the certified_by values.

    Certification is filled in after sealing by design, so it must not participate in the hash.
    Everything else -- statements, commands, judgement rationales, declared divergences -- does.
    """
    out = []
    for line in text.splitlines():
        m = FIELD.match(line.strip())
        if m and m.group("key") == "certified_by":
            out.append("certified_by:")          # normalise the value away, keep the line's presence
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"


def seal_path_for(intent_path: Path) -> Path:
    return intent_path.with_suffix(intent_path.suffix + SEAL_SUFFIX)


def seal(intent_path: Path, spec_path: Path | None = None) -> Path:
    """Register the intent BEFORE the work starts. Refuses an intent that cannot be verified."""
    intent_path = Path(intent_path)
    text = intent_path.read_text()
    clauses, errors = parse_intent(text)
    if errors:
        raise IntentError(
            "refusing to seal an unverifiable intent:\n  - " + "\n  - ".join(errors))
    for c in clauses:
        if c.is_judgement and c.certified_by:
            raise IntentError(
                f"{c.id}: arrived already certified by {c.certified_by!r}. `certified_by:` must be "
                "EMPTY at seal time — a judgement clause certified by whoever wrote it is exactly "
                "the self-certification this contract exists to prevent."
            )

    data = {
        "intent_path": str(intent_path),
        "intent_sha256": _sha(_canonical(text)),
        "spec_path": str(spec_path) if spec_path else None,
        "spec_sha256": _sha(Path(spec_path).read_text()) if spec_path else None,
        "clause_ids": [c.id for c in clauses],
        "judgement_clauses": [c.id for c in clauses if c.is_judgement],
        "declared_divergences": {c.id: c.diverges_from_spec
                                 for c in clauses if c.diverges_from_spec},
    }
    sp = seal_path_for(intent_path)
    sp.write_text(json.dumps(data, indent=2) + "\n")
    return sp


# --- verification ---------------------------------------------------------------------------------

def verify(intent_path: Path, cwd: Path | None = None) -> tuple[int, list[str]]:
    """Check the sealed intent against reality. Returns (exit_code, report_lines)."""
    intent_path = Path(intent_path)
    cwd = Path(cwd) if cwd else Path.cwd()
    lines: list[str] = []

    sp = seal_path_for(intent_path)
    if not sp.exists():
        return 3, [
            f"NOT SEALED: no {sp.name} beside {intent_path.name}.",
            "There is no pre-registered intent to verify against, so any verdict here would be "
            "written after the fact — which is the failure this contract exists to prevent.",
            f"Seal it before the work starts:  python3 -m harness.intent_contract seal {intent_path}",
        ]

    sealed = json.loads(sp.read_text())
    text = intent_path.read_text()
    if _sha(_canonical(text)) != sealed.get("intent_sha256"):
        return 4, [
            f"INTENT CHANGED since it was sealed: {intent_path.name}",
            "Only `certified_by:` may be edited after sealing. Something else moved — a statement, a "
            "verify command, a judgement rationale or a declared divergence.",
            f"sealed clause ids: {sealed.get('clause_ids')}",
            "If the intent genuinely needs to change, that is a decision to record and re-seal "
            "DELIBERATELY (and say why), not something to slip past verification.",
        ]

    clauses, errors = parse_intent(text)
    if errors:
        return 2, ["MALFORMED intent file:"] + [f"  - {e}" for e in errors]

    failed: list[str] = []
    for c in clauses:
        if c.is_judgement:
            if c.certified_by:
                lines.append(f"  {c.id}  CERTIFIED by {c.certified_by} — {c.statement[:70]}")
            else:
                failed.append(c.id)
                lines.append(
                    f"  {c.id}  UNCERTIFIED — {c.statement[:70]}\n"
                    f"        judgement clause, needs an independent reviewer in `certified_by:`")
            continue
        # Reuse the repo's single command runner rather than adding a second one.
        res = done_gate.run_requirement({"id": c.id, "desc": c.statement, "test": c.verify}, cwd=cwd)
        if res["status"] == "PASS":
            lines.append(f"  {c.id}  PASS — {c.statement[:70]}")
        else:
            failed.append(c.id)
            out = (res.get("output") or "").strip().splitlines()
            tail = out[-4:] if out else ["(no output)"]
            lines.append(f"  {c.id}  FAIL (exit {res.get('exit')}) — {c.statement[:70]}")
            lines.extend(f"        {t}" for t in tail)

    div = sealed.get("declared_divergences") or {}
    if div:
        lines.append("")
        lines.append("declared divergences from the spec (allowed, recorded at seal time):")
        lines.extend(f"  {k}: {v}" for k, v in div.items())

    if failed:
        lines.insert(0, f"INTENT NOT SATISFIED — {len(failed)}/{len(clauses)} clause(s) failed: "
                        f"{', '.join(failed)}")
        return 1, lines
    lines.insert(0, f"INTENT SATISFIED — all {len(clauses)} clause(s) verified")
    return 0, lines


# --- CLI ------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal", help="register the intent BEFORE the work starts")
    s.add_argument("intent")
    s.add_argument("--spec", default=None, help="the spec this intent was derived from")

    v = sub.add_parser("verify", help="check the sealed intent against reality")
    v.add_argument("intent")
    v.add_argument("--cwd", default=None, help="directory to run the verify commands in")

    a = ap.parse_args(argv)
    if a.cmd == "seal":
        try:
            sp = seal(Path(a.intent), Path(a.spec) if a.spec else None)
        except IntentError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"sealed -> {sp}")
        return 0

    code, lines = verify(Path(a.intent), Path(a.cwd) if a.cwd else None)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
