"""Checker utilities — verdict parsing."""

import re

# Pattern: VERDICT: FAIL(bounce-target): reason
_FAIL_WITH_TARGET = re.compile(r"^VERDICT:\s*FAIL\(([^)]+)\):\s*(.*)")
# Pattern: VERDICT: FAIL: reason  (no target)
_FAIL_PLAIN = re.compile(r"^VERDICT:\s*FAIL:\s*(.*)")
# Pattern: VERDICT: FAIL  (no reason, no target)
_FAIL_BARE = re.compile(r"^VERDICT:\s*FAIL\s*$")


NO_VERDICT_REASON = "checker did not emit a VERDICT line"


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

    return False, NO_VERDICT_REASON, None
