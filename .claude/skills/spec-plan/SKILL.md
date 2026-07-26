---
name: spec-plan
description: Turn a spec/feature doc into an enforced Definition-of-Done contract — one requirement per MUST/SHALL/checkbox clause — so the completion gate holds every part of the spec. Use at the start of any spec-driven feature.
argument-hint: <path-to-spec.md>
allowed-tools: Bash, Read, Edit, Write
---

# /spec-plan — spec → enforced contract

Goal: make the completion gate (`harness/done_gate.py`) hold the WHOLE spec, so no part can be silently skipped.

Steps:
1. Generate the contract from the spec (one UNVERIFIED requirement per checkable clause):
   `python3 scripts/spec_to_contract.py "$ARGUMENTS" --task "<short task name>"`
   This writes `harness/loop/done_contract.json`. Every clause starts UNVERIFIED → the gate blocks until a real test is bound to each.
2. Read the generated contract. For each requirement, decide the EXACT test command that will prove it (a pytest file, a script, a sentinel). If a test doesn't exist yet, that's the implementer's first job for that unit.
3. Hand the contract to `/atomic` (or work it unit by unit): bind a real test to each requirement, make it pass, and the gate clears only when ALL are green.
4. Do NOT mark anything done by inspection. A requirement is done when its bound test exits 0 — that is the entire point.

Notes:
- Clause extraction is single-sourced from `done_gate.extract_clauses` (MUST / SHALL / `- [ ]`; satisfied `- [x]` and prose are ignored).
- The gate's STANDING floor (`scripts/invariant_check.py`) is always appended while the contract is active.
