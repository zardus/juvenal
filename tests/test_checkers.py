"""Unit tests for checker utilities."""

from juvenal.checkers import NO_VERDICT_REASON, parse_verdict
from juvenal.workflow import Phase


class TestParseVerdict:
    def test_pass(self):
        passed, reason, target = parse_verdict("notes\nVERDICT: PASS")
        assert passed is True
        assert reason == ""
        assert target is None

    def test_fail(self):
        passed, reason, target = parse_verdict("VERDICT: FAIL: broken")
        assert passed is False
        assert reason == "broken"
        assert target is None

    def test_missing_verdict(self):
        passed, reason, target = parse_verdict("no verdict here")
        assert passed is False
        assert reason == NO_VERDICT_REASON
        assert target is None


class TestRunBasedChecks:
    def test_render_check_prompt_includes_run_command(self):
        phase = Phase(id="review", type="check", run="pytest -q")
        prompt = phase.render_check_prompt()
        assert "pytest -q" in prompt
        assert "VERDICT: PASS" in prompt

    def test_render_check_prompt_substitutes_vars_in_run_command(self):
        phase = Phase(id="review", type="check", run="pytest {{TARGET}} -q")
        prompt = phase.render_check_prompt(vars={"TARGET": "tests/unit"})
        assert "pytest tests/unit -q" in prompt
