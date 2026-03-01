"""Juvenal CLI — orchestrate AI coding agents through verified phases."""

import argparse
import sys

from juvenal import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="juvenal",
        description="Who guards the agents? Orchestrate AI coding agents through verified implementation phases.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--plain", action="store_true", help="Plain text output (no Rich TUI)")
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a workflow")
    run_p.add_argument("workflow", help="Path to workflow YAML, directory, or bare .md file")
    run_p.add_argument("--resume", action="store_true", help="Resume from last saved state")
    run_p.add_argument("--phase", help="Start from a specific phase")
    run_p.add_argument("--max-retries", type=int, default=999, help="Max retries per phase (default: 999)")
    run_p.add_argument("--backend", choices=["claude", "codex"], default="claude", help="AI backend to use")
    run_p.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    run_p.add_argument("--working-dir", help="Working directory for the agent")

    # plan
    plan_p = sub.add_parser("plan", help="Generate a workflow from a goal description")
    plan_p.add_argument("goal", help="Goal description")
    plan_p.add_argument("-o", "--output", default="workflow.yaml", help="Output file (default: workflow.yaml)")
    plan_p.add_argument("--backend", choices=["claude", "codex"], default="claude", help="AI backend to use")

    # do
    do_p = sub.add_parser("do", help="Plan + immediately run a workflow")
    do_p.add_argument("goal", help="Goal description")
    do_p.add_argument("--backend", choices=["claude", "codex"], default="claude", help="AI backend to use")
    do_p.add_argument("--max-retries", type=int, default=999, help="Max retries per phase (default: 999)")

    # status
    status_p = sub.add_parser("status", help="Show workflow progress")
    status_p.add_argument("--state-file", help="Path to state file")

    # init
    init_p = sub.add_parser("init", help="Scaffold a workflow directory")
    init_p.add_argument("directory", nargs="?", default=".", help="Directory to scaffold (default: .)")
    init_p.add_argument("--template", default="default", help="Template to use (default: default)")

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    from juvenal.engine import Engine
    from juvenal.workflow import load_workflow

    workflow = load_workflow(args.workflow)
    if args.backend:
        workflow.backend = args.backend
    if args.max_retries:
        workflow.max_retries = args.max_retries
    if args.working_dir:
        workflow.working_dir = args.working_dir

    engine = Engine(workflow, resume=args.resume, start_phase=args.phase, dry_run=args.dry_run, plain=args.plain)
    return engine.run()


def cmd_plan(args: argparse.Namespace) -> int:
    from juvenal.engine import plan_workflow

    plan_workflow(args.goal, args.output, args.backend, plain=args.plain)
    return 0


def cmd_do(args: argparse.Namespace) -> int:
    import tempfile

    from juvenal.engine import Engine, plan_workflow
    from juvenal.workflow import load_workflow

    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        plan_workflow(args.goal, f.name, args.backend, plain=args.plain)
        workflow = load_workflow(f.name)

    if args.backend:
        workflow.backend = args.backend
    if args.max_retries:
        workflow.max_retries = args.max_retries

    engine = Engine(workflow, plain=args.plain)
    return engine.run()


def cmd_status(args: argparse.Namespace) -> int:
    from juvenal.state import PipelineState

    state = PipelineState.load(args.state_file)
    state.print_status()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from juvenal.workflow import scaffold_workflow

    scaffold_workflow(args.directory, args.template)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    handlers = {
        "run": cmd_run,
        "plan": cmd_plan,
        "do": cmd_do,
        "status": cmd_status,
        "init": cmd_init,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
