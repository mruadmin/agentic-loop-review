"""Verification against INTENT, sealed before the work starts.

Origin (2026-07-26, Michael's design). Today's evidence for why spec-literal verification is not
enough: a `lifecycle-run` arm went STUCK for 79 minutes trying to satisfy a spec whose factual claim
("there are none left") was false. The work it did was fine; the sentence it was measured against was
wrong. Verifying against the spec's INTENT — "nothing re-derives the repo root unsafely" — would have
passed it.

But "verify the intent, not the spec" is also the precise loophole that produces overclaiming: an
agent that finishes something easier and then decides THAT was the intent all along. This repo exists
to stop exactly that, so intent verification has to be HARDER to fudge than spec verification, not
softer. Three mechanisms, all tested here:

  1. SEALED BEFORE. The intent is written and hashed before any implementation. Verification refuses
     if the intent file changed after sealing. You cannot retrofit the target to the shot.
  2. BOUND TO A COMMAND. Every intent clause needs a deterministic `verify:` command, or an explicit
     `judgement:` line saying why no command can express it. A clause with neither is refused —
     silence is not a pass.
  3. DIVERGENCE IS DECLARED, NOT DISCOVERED. A clause may deliberately depart from the spec (the
     spec was wrong, or a better way was found) but must record `diverges_from_spec:` with a reason.
     An undeclared departure is scope drift wearing an improvement's clothes.

The judgement escape hatch is deliberately narrow, and it cannot be self-certified: `certified_by:`
must be EMPTY at seal time and filled in at verification time, so the thing that certifies a
judgement clause is never the thing that sealed it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import intent_contract as ic

ROOT = Path(__file__).resolve().parents[2]


# --- fixtures ------------------------------------------------------------------------------------

GOOD = """\
# INTENT — circuit-breaker-cli-import

## I1 — the CLI runs as a script without an ImportError
verify: python3 -c "print('ok')"

## I2 — no module re-derives the repo root by walking parents
verify: python3 -c "import sys; sys.exit(0)"

## I3 — the fix reads naturally to the next maintainer
judgement: readability cannot be expressed as an exit code
certified_by:
"""

DIVERGENT = """\
# INTENT — thing

## I1 — the import works
verify: python3 -c "print('ok')"
diverges_from_spec: spec said patch sys.path; a package-relative import is correct instead
"""


def _write(tmp_path, text, name="INTENT.md"):
    p = tmp_path / name
    p.write_text(text)
    return p


# --- parsing -------------------------------------------------------------------------------------

def test_parses_clauses_with_ids_and_statements(tmp_path):
    clauses, errs = ic.parse_intent(_write(tmp_path, GOOD).read_text())
    assert not errs, errs
    assert [c.id for c in clauses] == ["I1", "I2", "I3"]
    assert clauses[0].statement.startswith("the CLI runs as a script")


def test_a_clause_with_neither_verify_nor_judgement_is_an_error(tmp_path):
    """Silence must not read as a pass — that is the whole failure mode being designed against."""
    bad = "# INTENT — x\n\n## I1 — something vague and unmeasured\n"
    clauses, errs = ic.parse_intent(bad)
    assert errs, "a clause bound to nothing was accepted"
    assert "I1" in " ".join(errs)


def test_parse_accepts_a_filled_certified_by(tmp_path):
    """Parsing must accept certification, because at VERIFY time the field is legitimately filled.

    Rejecting it at parse time would make the field impossible to ever use. The prohibition belongs
    at SEAL time only — see the next test. (This distinction was got wrong in the first draft of this
    file, and the tool refused the bad usage, which is the behaviour being pinned here.)
    """
    filled = ("# INTENT — x\n\n## I1 — subjective thing\n"
              "judgement: cannot be an exit code\ncertified_by: reviewer-agent\n")
    clauses, errs = ic.parse_intent(filled)
    assert not errs, errs
    assert clauses[0].certified_by == "reviewer-agent"


def test_seal_refuses_a_pre_certified_judgement_clause(tmp_path):
    """certified_by is filled at VERIFY time. Sealed-with-a-certificate is self-certification."""
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    pre = _write(tmp_path, "# INTENT — x\n\n## I1 — subjective thing\n"
                           "judgement: cannot be an exit code\ncertified_by: the implementer\n")
    with pytest.raises(ic.IntentError) as e:
        ic.seal(pre, spec)
    assert "certified_by" in str(e.value).lower()
    assert "I1" in str(e.value)


def test_divergence_is_recorded_when_declared(tmp_path):
    clauses, errs = ic.parse_intent(DIVERGENT)
    assert not errs, errs
    assert clauses[0].diverges_from_spec
    assert "package-relative" in clauses[0].diverges_from_spec


# --- sealing -------------------------------------------------------------------------------------

def test_seal_writes_a_sidecar_with_both_hashes(tmp_path):
    spec = _write(tmp_path, "# spec\nmake the CLI work\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)
    seal_path = ic.seal(intent, spec)
    data = json.loads(seal_path.read_text())
    assert data["intent_sha256"] and data["spec_sha256"]
    assert data["clause_ids"] == ["I1", "I2", "I3"]


def test_seal_refuses_an_unbound_clause(tmp_path):
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, "# INTENT — x\n\n## I1 — unmeasured\n")
    with pytest.raises(ic.IntentError) as e:
        ic.seal(intent, spec)
    assert "I1" in str(e.value)


# --- verification --------------------------------------------------------------------------------

def test_verify_passes_when_every_bound_command_exits_zero(tmp_path):
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)
    ic.seal(intent, spec)
    # certify the judgement clause, as a separate reviewer would at verify time
    intent.write_text(GOOD.replace("certified_by:", "certified_by: reviewer-agent"))
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code == 0, "\n".join(lines)


def test_verify_fails_when_a_bound_command_exits_nonzero(tmp_path):
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    text = GOOD.replace('verify: python3 -c "print(\'ok\')"',
                        'verify: python3 -c "import sys; sys.exit(1)"', 1)
    intent = _write(tmp_path, text)
    ic.seal(intent, spec)
    intent.write_text(text.replace("certified_by:", "certified_by: reviewer-agent"))
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code != 0
    assert any("I1" in l and "FAIL" in l for l in lines), lines


def test_verify_refuses_when_the_intent_was_never_sealed(tmp_path):
    """No pre-registered intent means nothing to verify against — refuse, don't improvise."""
    intent = _write(tmp_path, GOOD)
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code == 3, lines
    assert any("seal" in l.lower() for l in lines)


def test_verify_detects_intent_edited_after_sealing(tmp_path):
    """THE anti-fudge test. Moving the target after the shot must be caught."""
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)
    ic.seal(intent, spec)
    intent.write_text(GOOD.replace(
        "## I2 — no module re-derives the repo root by walking parents",
        "## I2 — something much easier that I actually did"))
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code == 4, lines
    blob = " ".join(lines).lower()
    assert "changed" in blob or "tamper" in blob or "edited" in blob


def test_editing_only_certified_by_is_allowed(tmp_path):
    """Certification is the one field verification itself must be able to fill.

    Otherwise the seal makes judgement clauses impossible to ever certify — the mechanism would
    forbid its own escape hatch.
    """
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)
    ic.seal(intent, spec)
    intent.write_text(GOOD.replace("certified_by:", "certified_by: some-reviewer"))
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code == 0, "\n".join(lines)


def test_an_uncertified_judgement_clause_blocks_the_pass(tmp_path):
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)
    ic.seal(intent, spec)
    code, lines = ic.verify(intent, cwd=tmp_path)   # I3 left uncertified
    assert code != 0
    assert any("I3" in l for l in lines), lines


def test_verify_reports_every_clause_not_just_the_first_failure(tmp_path):
    """A partial report invites 'fix one, re-run, repeat' — the slowest possible loop."""
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    text = ("# INTENT — x\n\n"
            '## I1 — a\nverify: python3 -c "import sys; sys.exit(1)"\n\n'
            '## I2 — b\nverify: python3 -c "import sys; sys.exit(1)"\n')
    intent = _write(tmp_path, text)
    ic.seal(intent, spec)
    code, lines = ic.verify(intent, cwd=tmp_path)
    assert code != 0
    blob = " ".join(lines)
    assert "I1" in blob and "I2" in blob, f"only reported some clauses: {lines}"


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_seal_then_verify_roundtrip(tmp_path):
    """The real order of operations: seal UNcertified, do the work, certify, then verify."""
    spec = _write(tmp_path, "# spec\n", "SPEC.md")
    intent = _write(tmp_path, GOOD)                     # sealed with certified_by EMPTY
    r = subprocess.run([sys.executable, "-m", "harness.intent_contract", "seal",
                        str(intent), "--spec", str(spec)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout + r.stderr
    # ... work happens ... then an independent reviewer certifies the judgement clause
    intent.write_text(GOOD.replace("certified_by:", "certified_by: reviewer"))
    r = subprocess.run([sys.executable, "-m", "harness.intent_contract", "verify", str(intent)],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout + r.stderr


def test_reuses_done_gate_for_running_commands():
    """Do not hand-roll a second subprocess runner.

    `done_gate.run_requirement` already runs a requirement's command with the repo's conventions
    (cwd, timeout, output capture). A parallel implementation would drift from it, and this repo's
    gate behaviour is the last thing that should have two versions.
    """
    src = (ROOT / "harness" / "intent_contract.py").read_text()
    assert "done_gate" in src, (
        "intent_contract does not reuse done_gate — the command runner would be duplicated"
    )


# --- the /solve skill must keep the paragraph that makes it work ----------------------------------

SKILL = ROOT / ".claude" / "skills" / "solve" / "SKILL.md"


def test_the_solve_skill_exists():
    assert SKILL.exists(), f"missing {SKILL}"


def test_the_skill_delegates_the_process_and_says_to_size_it():
    """This is the entire finding. Everything else in the skill is scaffolding around it.

    Measured 2026-07-26: three fixed-pipeline arms produced nothing on a three-line import fix; one
    agent told to choose its own process fixed it in 20 minutes. If a future edit tidies this
    paragraph away, the skill becomes another fixed pipeline and the finding is lost.
    """
    txt = SKILL.read_text()
    assert "yours to decide" in txt, "the skill no longer delegates the process to the lead agent"
    assert "Size the process to the actual change" in txt, (
        "the sizing instruction is gone — without it 'choose your own process' collapses back to "
        "the default of running every phase"
    )
    assert "0 is a fine answer" in txt, (
        "the skill no longer tells the lead that spawning ZERO sub-agents is acceptable, which is "
        "what the winning arm actually did"
    )


def test_the_skill_requires_sealing_before_work():
    """Intent verification without a seal is just permission to redefine the goal afterwards."""
    txt = SKILL.read_text()
    assert "intent_contract seal" in txt, "the skill never seals the intent"
    assert "EMPTY at seal time" in txt, "the no-self-certification rule is not stated"
    # Anchor on the HEADINGS, not the first occurrence of the words: "Phase 2" also appears in the
    # mermaid diagram near the top of the file, which would make this assertion trivially true.
    seal_at = txt.index("intent_contract seal")
    phase2_at = txt.index("## Phase 2")
    assert seal_at < phase2_at, (
        "sealing is documented after the Phase 2 heading — it must happen BEFORE any work starts, "
        "or the seal proves nothing"
    )


def test_the_skill_keeps_the_moat_carve_out():
    """The one place the 83/10 deliberation ratio is worth paying for."""
    txt = SKILL.read_text()
    assert "confident_wrong" in txt, "the moat guardrail is missing"
    assert "lifecycle" in txt.lower(), (
        "the skill does not say when to keep /lifecycle's full fan-out instead — without that this "
        "reads as 'always use one agent', which is the opposite mistake"
    )
