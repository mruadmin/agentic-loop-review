#!/usr/bin/env python3
"""PostToolUse SMOKE — a deterministic hook that, immediately AFTER an Edit/Write to a *.py file, runs
a fast static smoke check on the changed file and surfaces any breakage back to the agent. This is the
"silent break" catcher: it turns a syntax error or undefined-name slipped in by an edit into immediate
feedback, instead of letting it sit until something downstream blows up.

Checks (cheapest first, all OPTIONAL beyond py_compile):
  1. `python3 -m py_compile <file>`  — always; catches syntax errors.
  2. `ruff check <file>`             — if ruff is installed; catches lint/undefined-name/unused.
  3. `flake8 <file>`                 — if ruff absent and flake8 present.

Input (PostToolUse hook JSON on stdin): {"tool_name": ..., "tool_input": {"file_path": ...}}.
Output: PostToolUse decision protocol on stdout. On a failure it BLOCKS with the captured compiler/
linter output so the agent must fix it; on success it stays silent (allow). Non-Python or non-edit
tools are a no-op allow.

Determinism: the verdict is the compiler's exit code, never a model's opinion.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Update"}
TIMEOUT = 60


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT running {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    return p.returncode, (p.stdout + p.stderr).strip()


def smoke_check(file_path: str, *, run=_run, has_tool=shutil.which) -> dict:
    """Run the smoke checks on one python file. Returns {"ok": bool, "checks": [...], "report": str}.
    `run` and `has_tool` are injectable so tests stay deterministic and offline."""
    checks: list[dict] = []

    # 1) py_compile — always.
    code, out = run([sys.executable, "-m", "py_compile", file_path])
    checks.append({"check": "py_compile", "ok": code == 0, "output": out})
    if code != 0:
        # A syntax error makes further linting noise pointless; report it alone.
        return _result(checks)

    # 2) ruff (preferred) else flake8 — only if available.
    if has_tool("ruff"):
        code, out = run(["ruff", "check", file_path])
        checks.append({"check": "ruff", "ok": code == 0, "output": out})
    elif has_tool("flake8"):
        code, out = run(["flake8", file_path])
        checks.append({"check": "flake8", "ok": code == 0, "output": out})

    return _result(checks)


def _result(checks: list[dict]) -> dict:
    failed = [c for c in checks if not c["ok"]]
    ok = not failed
    if ok:
        report = "smoke OK: " + ", ".join(c["check"] for c in checks)
    else:
        lines = [f"[{c['check']}] FAILED:\n{c['output'][:1500]}" for c in failed]
        report = "POST-EDIT SMOKE FAILED on the file you just changed:\n" + "\n\n".join(lines)
    return {"ok": ok, "checks": checks, "report": report}


def is_target(tool_name: str, file_path: str) -> bool:
    return tool_name in EDIT_TOOLS and file_path.endswith(".py")


def to_hook_output(result: dict) -> dict:
    """Wrap into the PostToolUse protocol. Failure -> block with the compiler output as the reason so
    the agent is forced to fix what it just broke; success -> silent allow.

    NOTE: PostToolUse `decision` only accepts "block" (or omission). Emitting {"decision":"allow"}
    fails Claude Code's hook-output schema ("(root): Invalid input") on EVERY successful edit, so the
    success case must be an empty object (the canonical silent allow)."""
    if result["ok"]:
        return {}
    return {
        "decision": "block",
        "reason": result["report"],
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": result["report"],
        },
    }


def main() -> int:
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook = {}
    tool_name = hook.get("tool_name", "")
    tool_input = hook.get("tool_input", {}) or {}
    file_path = (tool_input.get("file_path") or tool_input.get("path")
                 or tool_input.get("notebook_path") or "")
    if not is_target(tool_name, str(file_path)):
        print(json.dumps({}))
        return 0
    result = smoke_check(str(file_path))
    out = to_hook_output(result)
    if not result["ok"]:
        sys.stderr.write(result["report"] + "\n")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
