"""Unit tests for CLI argument parsing."""

from juvenal.cli import build_parser


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
