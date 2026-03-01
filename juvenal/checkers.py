"""Checker utilities — verdict parsing and script execution."""

from __future__ import annotations

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


def parse_verdict(output: str) -> tuple[bool, str]:
    """Parse VERDICT from agent output, scanning backwards.

    Returns (passed, reason).
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("VERDICT: PASS"):
            return True, ""
        if line.startswith("VERDICT: FAIL"):
            reason = line.split("VERDICT: FAIL:", 1)[-1].strip() if "FAIL:" in line else "unspecified"
            return False, reason
    return False, "checker did not emit a VERDICT line"
