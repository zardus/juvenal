"""Unit tests for CLI argument parsing and commands."""

import subprocess
import sys
import time

from juvenal.cli import build_parser, cmd_status
from juvenal.state import PipelineState


class TestArgumentParsing:
    def test_run_basic(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.command == "run"
        assert args.workflow == "workflow.yaml"
        assert args.backend == "codex"
        assert args.max_bounces == 999
        assert not args.resume
        assert not args.dry_run

    def test_run_all_flags(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "workflow.yaml",
                "--resume",
                "--phase",
                "implement",
                "--max-bounces",
                "5",
                "--backend",
                "codex",
                "--dry-run",
                "--working-dir",
                "/tmp",
                "--state-file",
                "custom-state.json",
            ]
        )
        assert args.resume
        assert args.phase == "implement"
        assert args.max_bounces == 5
        assert args.backend == "codex"
        assert args.dry_run
        assert args.working_dir == "/tmp"
        assert args.state_file == "custom-state.json"

    def test_run_rewind(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--rewind", "2"])
        assert args.rewind == 2
        assert args.rewind_to is None

    def test_run_rewind_to(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--rewind-to", "phase-a"])
        assert args.rewind_to == "phase-a"
        assert args.rewind is None

    def test_run_defaults_no_rewind(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.rewind is None
        assert args.rewind_to is None

    def test_run_state_file_default(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.state_file is None

    def test_plan(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app"])
        assert args.command == "plan"
        assert args.goal == "build a web app"
        assert args.output == "workflow.yaml"

    def test_plan_output(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app", "-o", "my.yaml"])
        assert args.output == "my.yaml"

    def test_do(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a web app"])
        assert args.command == "do"
        assert args.goal == "build a web app"

    def test_status(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_status_with_state_file(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--state-file", "custom.json"])
        assert args.state_file == "custom.json"

    def test_init_default(self):
        parser = build_parser()
        args = parser.parse_args(["init"])
        assert args.command == "init"
        assert args.directory == "."
        assert args.template == "default"

    def test_init_custom(self):
        parser = build_parser()
        args = parser.parse_args(["init", "myproject", "--template", "basic"])
        assert args.directory == "myproject"
        assert args.template == "basic"

    def test_validate(self):
        parser = build_parser()
        args = parser.parse_args(["validate", "workflow.yaml"])
        assert args.command == "validate"
        assert args.workflow == "workflow.yaml"

    def test_run_checker_single(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--checker", "tester"])
        assert args.checker == ["tester"]

    def test_run_checker_multiple(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--checker", "tester", "--checker", "run:pytest -x"])
        assert args.checker == ["tester", "run:pytest -x"]

    def test_run_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.checker == []

    def test_plan_checker(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API", "--checker", "senior-tester"])
        assert args.checker == ["senior-tester"]

    def test_plan_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API"])
        assert args.checker == []

    def test_do_checker(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--checker", "tester", "--checker", "run:make lint"])
        assert args.checker == ["tester", "run:make lint"]

    def test_do_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing"])
        assert args.checker == []

    def test_run_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--implementer", "software-engineer"])
        assert args.implementer == "software-engineer"

    def test_run_implementer_default_none(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.implementer is None

    def test_plan_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API", "--implementer", "software-engineer"])
        assert args.implementer == "software-engineer"

    def test_do_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--implementer", "software-engineer"])
        assert args.implementer == "software-engineer"

    def test_run_preserve_context_on_bounce(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--preserve-context-on-bounce"])
        assert args.preserve_context_on_bounce is True

    def test_run_preserve_context_on_bounce_default(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.preserve_context_on_bounce is False

    def test_do_preserve_context_on_bounce(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--preserve-context-on-bounce"])
        assert args.preserve_context_on_bounce is True

    def test_run_defines_single(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "ENV=prod"])
        assert args.defines == ["ENV=prod"]

    def test_run_defines_multiple(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "ENV=prod", "-D", "REGION=us-east-1"])
        assert args.defines == ["ENV=prod", "REGION=us-east-1"]

    def test_run_defines_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.defines == []

    def test_run_defines_with_equals_in_value(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "CMD=a=b=c"])
        assert args.defines == ["CMD=a=b=c"]

    def test_do_defines(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "-D", "ENV=prod"])
        assert args.defines == ["ENV=prod"]

    def test_no_command(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_version(self, capsys):
        parser = build_parser()
        try:
            parser.parse_args(["--version"])
        except SystemExit:
            pass
        captured = capsys.readouterr()
        from juvenal import __version__

        assert __version__ in captured.out


class TestStatusExitCode:
    def _make_args(self, state_file):
        import argparse

        return argparse.Namespace(state_file=str(state_file))

    def test_status_returns_0_when_all_completed(self, tmp_path):
        """Fully successful pipeline returns exit code 0."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.set_attempt("test", 1)
        state.mark_completed("test")
        state.completed_at = time.time()
        state.save()

        assert cmd_status(self._make_args(state_file)) == 0

    def test_status_returns_1_when_failed(self, tmp_path):
        """Pipeline with a failed phase returns exit code 1."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.set_attempt("test", 1)
        state.mark_failed("test")
        state.completed_at = time.time()
        state.save()

        assert cmd_status(self._make_args(state_file)) == 1

    def test_status_returns_1_when_incomplete(self, tmp_path):
        """Pipeline still running (no completed_at) returns exit code 1."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time()
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.save()

        assert cmd_status(self._make_args(state_file)) == 1

    def test_status_returns_1_when_no_state(self, tmp_path):
        """No state file (never run) returns exit code 1."""
        state_file = tmp_path / "state.json"
        assert cmd_status(self._make_args(state_file)) == 1


class TestStatusExitCodeSubprocess:
    """Test that exit codes actually propagate through the real entry point."""

    def test_status_subprocess_exit_0_on_success(self, tmp_path):
        """Successful pipeline exits 0 as a real process."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.completed_at = time.time()
        state.save()

        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_status_subprocess_exit_1_on_no_state(self, tmp_path):
        """No state file exits 1 as a real process."""
        state_file = tmp_path / "nonexistent.json"
        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 1

    def test_status_subprocess_exit_1_on_failure(self, tmp_path):
        """Failed pipeline exits 1 as a real process."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_failed("build")
        state.completed_at = time.time()
        state.save()

        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 1
