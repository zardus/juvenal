"""Checker utilities — verdict parsing and script execution."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class ScriptResult:
    """Result from running a shell command."""

    exit_code: int
    output: str


def run_script(command: str, working_dir: str, timeout: int = 600) -> ScriptResult:
    """Run a shell command. Returns exit code and combined stdout+stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ScriptResult(
            exit_code=result.returncode,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return ScriptResult(exit_code=1, output=f"Script timed out after {timeout}s")


# Pattern: VERDICT: FAIL(bounce-target): reason
_FAIL_WITH_TARGET = re.compile(r"^VERDICT:\s*FAIL\(([^)]+)\):\s*(.*)")
# Pattern: VERDICT: FAIL: reason  (no target)
_FAIL_PLAIN = re.compile(r"^VERDICT:\s*FAIL:\s*(.*)")
# Pattern: VERDICT: FAIL  (no reason, no target)
_FAIL_BARE = re.compile(r"^VERDICT:\s*FAIL\s*$")


def parse_verdict(output: str) -> tuple[bool, str, str | None]:
    """Parse VERDICT from agent output, scanning backwards.

    Supports two FAIL formats:
    - VERDICT: FAIL: reason           (no bounce target)
    - VERDICT: FAIL(target-id): reason  (agent-guided bounce target)

    Returns (passed, reason, bounce_target).
    bounce_target is None for PASS or when the agent didn't specify one.
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("VERDICT: PASS"):
            return True, "", None

        m = _FAIL_WITH_TARGET.match(line)
        if m:
            return False, m.group(2).strip() or "unspecified", m.group(1).strip()

        m = _FAIL_PLAIN.match(line)
        if m:
            return False, m.group(1).strip() or "unspecified", None

        if _FAIL_BARE.match(line):
            return False, "unspecified", None

    return False, "checker did not emit a VERDICT line", None
