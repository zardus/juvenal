"""Unit tests for the execution engine with mocked backend."""

from pathlib import Path

import pytest

from juvenal.checkers import parse_verdict
from juvenal.engine import Engine, _extract_yaml
from juvenal.workflow import Checker, Phase, Workflow
from tests.conftest import MockBackend


class TestVerdictParsing:
    def test_pass(self):
        assert parse_verdict("some output\nVERDICT: PASS") == (True, "")

    def test_fail_with_reason(self):
        passed, reason = parse_verdict("output\nVERDICT: FAIL: tests broken")
        assert not passed
        assert reason == "tests broken"

    def test_fail_without_reason(self):
        passed, reason = parse_verdict("VERDICT: FAIL")
        assert not passed
        assert reason == "unspecified"

    def test_no_verdict(self):
        passed, reason = parse_verdict("no verdict here")
        assert not passed
        assert "did not emit a VERDICT" in reason

    def test_verdict_scan_backwards(self):
        """Should find the last VERDICT line."""
        output = "VERDICT: FAIL: old\nmore stuff\nVERDICT: PASS"
        assert parse_verdict(output) == (True, "")


class TestEngineWithMockedBackend:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        """Create an engine with injected mock backend."""
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_single_phase_pass(self, tmp_path):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement
        workflow = Workflow(
            name="test",
            phases=[Phase(id="setup", prompt="Do it.", checkers=[Checker(name="check", type="script", run="true")])],
            max_retries=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_implementation_crash_retries(self, tmp_path):
        backend = MockBackend()
        backend.add_response(exit_code=1, output="crash")  # attempt 1 crashes
        backend.add_response(exit_code=0, output="done")  # attempt 2 succeeds
        workflow = Workflow(
            name="test",
            phases=[Phase(id="setup", prompt="Do it.", checkers=[Checker(name="check", type="script", run="true")])],
            max_retries=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_script_checker_failure_retries(self, tmp_path):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # attempt 1 implement
        backend.add_response(exit_code=0, output="done")  # attempt 2 implement
        workflow = Workflow(
            name="test",
            phases=[
                Phase(
                    id="setup",
                    prompt="Do it.",
                    checkers=[Checker(name="check", type="script", run="false")],  # always fails
                )
            ],
            max_retries=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 1  # exhausted

    def test_agent_checker_pass(self, tmp_path):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="looks good\nVERDICT: PASS")  # checker
        workflow = Workflow(
            name="test",
            phases=[Phase(id="setup", prompt="Do it.", checkers=[Checker(name="tester", type="agent", role="tester")])],
            max_retries=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_multi_phase(self, tmp_path):
        backend = MockBackend()
        # Phase 1: implement + check pass
        backend.add_response(exit_code=0, output="phase1 done")
        # Phase 2: implement + check pass
        backend.add_response(exit_code=0, output="phase2 done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", prompt="Do phase 1.", checkers=[Checker(name="c1", type="script", run="true")]),
                Phase(id="phase2", prompt="Do phase 2.", checkers=[Checker(name="c2", type="script", run="true")]),
            ],
            max_retries=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_dry_run(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="setup", prompt="Do the thing.", checkers=[Checker(name="c", type="script", run="true")])],
        )
        engine = self._make_engine(workflow, MockBackend(), tmp_path, dry_run=True)
        assert engine.run() == 0
        captured = capsys.readouterr()
        assert "test" in captured.out
        assert "setup" in captured.out


class TestExtractYaml:
    def test_yaml_code_fence(self):
        text = "Here's the workflow:\n```yaml\nname: test\nphases: []\n```\nDone."
        assert "name: test" in _extract_yaml(text)
        assert "```" not in _extract_yaml(text)

    def test_generic_code_fence(self):
        text = "Here:\n```\nname: test\nphases: []\n```\n"
        assert "name: test" in _extract_yaml(text)

    def test_no_fence_with_prose(self):
        text = "Sure, here is your workflow.\n\nname: test\nphases:\n  - id: a\n    prompt: do it\n"
        result = _extract_yaml(text)
        assert "name: test" in result
        assert "Sure, here" not in result

    def test_raw_yaml(self):
        text = "name: test\nphases:\n  - id: a\n    prompt: do it\n"
        assert _extract_yaml(text) == text


class TestPlanWorkflow:
    def test_plan_creates_temp_structure(self, tmp_path):
        """plan_workflow creates temp dir with .plan/goal.md and runs Engine."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        mock_engine_instance = MagicMock()
        mock_engine_instance.run.return_value = 0

        def fake_engine_init(self_engine, workflow, **kwargs):
            # Write a valid workflow.yaml in the working dir to simulate engine output
            wd = Path(workflow.working_dir)
            (wd / "workflow.yaml").write_text("name: test\nphases:\n  - id: a\n    prompt: do it\n")
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock()
            self_engine._start_idx = 0
            # Verify .plan/goal.md was created
            assert (wd / ".plan" / "goal.md").exists()
            assert (wd / ".plan" / "goal.md").read_text() == "build something"

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=0),
        ):
            out_path = str(tmp_path / "workflow.yaml")
            plan_workflow("build something", out_path)

        assert Path(out_path).exists()
        content = Path(out_path).read_text()
        assert "phases" in content

    def test_plan_copies_output_on_success(self, tmp_path):
        """plan_workflow copies workflow.yaml to output path and cleans up."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        yaml_content = "name: result\nphases:\n  - id: x\n    prompt: hi\n"

        def fake_engine_init(self_engine, workflow, **kwargs):
            wd = Path(workflow.working_dir)
            (wd / "workflow.yaml").write_text(yaml_content)
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock()
            self_engine._start_idx = 0

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=0),
        ):
            out_path = str(tmp_path / "out.yaml")
            plan_workflow("goal", out_path)

        import yaml

        parsed = yaml.safe_load(Path(out_path).read_text())
        assert parsed["name"] == "result"
        assert "phases" in parsed

    def test_plan_fails_on_engine_failure(self, tmp_path):
        """plan_workflow raises SystemExit if engine returns non-zero."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        def fake_engine_init(self_engine, workflow, **kwargs):
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock()
            self_engine._start_idx = 0

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=1),
        ):
            with pytest.raises(SystemExit):
                plan_workflow("goal", str(tmp_path / "out.yaml"))
