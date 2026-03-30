"""Juvenal CLI — orchestrate AI coding agents through verified phases."""

import argparse
import sys

import yaml

from juvenal import __version__

STANDARD_CHECKERS = ["tester", "senior-tester", "senior-engineer", "architect", "pm"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="juvenal",
        description="Who guards the agents? Orchestrate AI coding agents through verified implementation phases.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--rich", action="store_true", help="Rich TUI output (default: plain text)")
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a workflow")
    run_p.add_argument("workflow", nargs="?", help="Path to workflow YAML, directory, or bare .md file")
    run_p.add_argument("--resume", action="store_true", help="Resume from last saved state")
    run_p.add_argument("--rewind", type=int, metavar="N", help="Rewind N phases back from the resume point")
    run_p.add_argument("--rewind-to", metavar="PHASE_ID", help="Rewind to a specific phase by ID")
    run_p.add_argument("--phase", help="Start from a specific phase")
    run_p.add_argument("--max-bounces", type=int, default=999, help="Max bounces across all phases (default: 999)")
    run_p.add_argument("--backend", choices=["claude", "codex"], default="codex", help="AI backend to use")
    run_p.add_argument("--working-dir", help="Working directory for the agent")
    run_p.add_argument("--state-file", help="Path to state file (default: .juvenal-state.json)")
    run_p.add_argument(
        "--backoff", type=float, default=None, help="Base backoff delay in seconds between bounces (exponential)"
    )
    run_p.add_argument("--notify", action="append", default=[], help="Webhook URL for completion/failure notifications")
    run_p.add_argument("--checker", action="append", default=[], help="Inject checker on every implement phase")
    run_p.add_argument(
        "--standard-checkers",
        action="store_true",
        help="Inject standard checkers: tester, senior-tester, senior-engineer, architect, pm",
    )
    run_p.add_argument(
        "--implementer",
        action="append",
        default=[],
        help='Implementer role, or role:"prompt" to add an inline implement phase',
    )
    run_p.add_argument(
        "--clear-context-on-bounce",
        action="store_true",
        help="Start a fresh agent session when bouncing back (default: resume session)",
    )
    run_p.add_argument(
        "--preserve-context-on-bounce",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated no-op, kept for compatibility
    )
    run_p.add_argument(
        "-D", action="append", default=[], metavar="VAR=VAL", dest="defines", help="Set template variable"
    )
    run_p.add_argument("--serialize", action="store_true", help="Disable all parallelization")

    # plan
    plan_p = sub.add_parser("plan", help="Generate a workflow from a goal description")
    plan_p.add_argument("goal", help="Goal description")
    plan_p.add_argument("-o", "--output", default="workflow.yaml", help="Output file (default: workflow.yaml)")
    plan_p.add_argument("--backend", choices=["claude", "codex"], default="codex", help="AI backend to use")
    plan_p.add_argument("--checker", action="append", default=[], help="Inject checker on every implement phase")
    plan_p.add_argument(
        "--standard-checkers",
        action="store_true",
        help="Inject standard checkers: tester, senior-tester, senior-engineer, architect, pm",
    )
    plan_p.add_argument("--implementer", help="Prepend implementer role prompt to every implement phase")
    plan_p.add_argument(
        "-i", "--interactive", action="store_true", help="Interactive mode: chat with the agent during plan refinement"
    )
    plan_p.add_argument("--resume", action="store_true", help="Resume a previously interrupted plan")

    # do
    do_p = sub.add_parser("do", help="Plan + immediately run a workflow")
    do_p.add_argument("goal", help="Goal description")
    do_p.add_argument("--backend", choices=["claude", "codex"], default="codex", help="AI backend to use")
    do_p.add_argument("--max-bounces", type=int, default=999, help="Max bounces across all phases (default: 999)")
    do_p.add_argument("--checker", action="append", default=[], help="Inject checker on every implement phase")
    do_p.add_argument(
        "--standard-checkers",
        action="store_true",
        help="Inject standard checkers: tester, senior-tester, senior-engineer, architect, pm",
    )
    do_p.add_argument("--implementer", help="Prepend implementer role prompt to every implement phase")
    do_p.add_argument(
        "-i", "--interactive", action="store_true", help="Interactive mode: chat with the agent during plan refinement"
    )
    do_p.add_argument(
        "--clear-context-on-bounce",
        action="store_true",
        help="Start a fresh agent session when bouncing back (default: resume session)",
    )
    do_p.add_argument(
        "--preserve-context-on-bounce",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated no-op, kept for compatibility
    )
    do_p.add_argument(
        "-D", action="append", default=[], metavar="VAR=VAL", dest="defines", help="Set template variable"
    )
    do_p.add_argument("--serialize", action="store_true", help="Disable all parallelization")

    # status
    status_p = sub.add_parser("status", help="Show workflow progress")
    status_p.add_argument("--state-file", help="Path to state file")

    # init
    init_p = sub.add_parser("init", help="Scaffold a workflow directory")
    init_p.add_argument("directory", nargs="?", default=".", help="Directory to scaffold (default: .)")
    init_p.add_argument("--template", default="default", help="Template to use (default: default)")

    # validate (same flags as run, but always dry-run)
    validate_p = sub.add_parser("validate", help="Validate a workflow and show execution plan")
    validate_p.add_argument("workflow", help="Path to workflow YAML, directory, or bare .md file")
    validate_p.add_argument("--max-bounces", type=int, default=999)
    validate_p.add_argument("--backend", choices=["claude", "codex"], default="codex")
    validate_p.add_argument("--working-dir")
    validate_p.add_argument("--backoff", type=float, default=None)
    validate_p.add_argument("--notify", action="append", default=[])
    validate_p.add_argument("--checker", action="append", default=[])
    validate_p.add_argument("--standard-checkers", action="store_true")
    validate_p.add_argument("--implementer")
    validate_p.add_argument("-D", action="append", default=[], metavar="VAR=VAL", dest="defines")

    return parser


def _parse_define_value(raw: str) -> object:
    if not raw:
        return raw
    if (lowered := raw.lower()) in {"true", "false", "null", "none", "~"} or raw[0] in "[{\"'":
        try:
            return yaml.safe_load("null" if lowered == "none" else raw)
        except yaml.YAMLError as exc:
            raise SystemExit(f"Error: invalid -D value {raw!r}: {exc}") from exc
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def _parse_defines(defines: list[str]) -> dict[str, list[str]]:
    """Parse -D VAR=VAL arguments, accumulating multiple values per key."""
    result: dict[str, list[str]] = {}
    for d in defines:
        if "=" not in d:
            print(f"Error: invalid -D value {d!r}: must be VAR=VAL")
            sys.exit(1)
        key, val = d.split("=", 1)
        result.setdefault(key, []).append(_parse_define_value(val))
    return result


def _apply_defines(workflow, all_vars: dict[str, list[str]]):
    """Apply parsed -D vars: single-value go into workflow.vars, multi-value expand phases."""
    from juvenal.workflow import expand_multi_vars

    single = {k: v[0] for k, v in all_vars.items() if len(v) == 1}
    multi = {k: v for k, v in all_vars.items() if len(v) > 1}
    workflow.vars.update(single)
    if multi:
        workflow = expand_multi_vars(workflow, multi)
    return workflow


def _load_workflow_or_exit(path: str):
    """Load a workflow, printing a clean error and exiting on failure."""
    from juvenal.workflow import load_workflow

    try:
        return load_workflow(path)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}")
        sys.exit(1)


def _parse_implementer(spec: str) -> tuple[str, str | None]:
    """Parse --implementer value into (role, inline_prompt | None).

    Accepts either 'role' or 'role:prompt text'.
    """
    if ":" in spec:
        role, prompt = spec.split(":", 1)
        return role, prompt
    return spec, None


def _expand_standard_checkers(args: argparse.Namespace) -> None:
    """If --standard-checkers is set, prepend the standard checker roles to args.checker."""
    if getattr(args, "standard_checkers", False):
        args.checker = list(STANDARD_CHECKERS) + args.checker


def cmd_run(args: argparse.Namespace) -> int:
    from juvenal.engine import Engine
    from juvenal.workflow import Phase, Workflow, inject_checkers, inject_implementer, validate_workflow

    # Parse --implementer flags into role-only vs inline phases
    inline_phases: list[Phase] = []
    role_only: str | None = None
    for spec in args.implementer:
        role, prompt = _parse_implementer(spec)
        if prompt is not None:
            inline_phases.append(Phase(id=f"implement-{len(inline_phases)}", prompt=prompt))
            if role:
                # Apply role preamble to just this phase
                mini = Workflow(name="tmp", phases=[inline_phases[-1]])
                mini = inject_implementer(mini, role)
                inline_phases[-1] = mini.phases[0]
        else:
            role_only = role

    # Build workflow: inline phases + workflow path phases
    if args.workflow:
        file_workflow = _load_workflow_or_exit(args.workflow)
        if role_only:
            file_workflow = inject_implementer(file_workflow, role_only)
        all_phases = inline_phases + file_workflow.phases
        workflow = Workflow(
            name=file_workflow.name,
            phases=all_phases,
            backend=file_workflow.backend,
            working_dir=file_workflow.working_dir,
            max_bounces=file_workflow.max_bounces,
            parallel_groups=file_workflow.parallel_groups,
            backoff=file_workflow.backoff,
            max_backoff=file_workflow.max_backoff,
            notify=list(file_workflow.notify),
            vars=dict(file_workflow.vars),
        )
    elif inline_phases:
        workflow = Workflow(name="inline", phases=inline_phases)
    else:
        print('Error: workflow path is required (or use --implementer role:"prompt")')
        return 1

    if args.defines:
        workflow = _apply_defines(workflow, _parse_defines(args.defines))
    _expand_standard_checkers(args)
    if args.checker:
        workflow = inject_checkers(workflow, args.checker)
    errors = validate_workflow(workflow)
    if errors:
        print(f"Workflow validation failed with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    if args.backend:
        workflow.backend = args.backend
    if args.max_bounces:
        workflow.max_bounces = args.max_bounces
    if args.working_dir:
        workflow.working_dir = args.working_dir
    if args.backoff is not None:
        workflow.backoff = args.backoff
    if args.notify:
        workflow.notify.extend(args.notify)

    state_file = getattr(args, "state_file", None)
    engine = Engine(
        workflow,
        resume=args.resume,
        rewind=args.rewind,
        rewind_to=args.rewind_to,
        start_phase=args.phase,
        state_file=state_file,
        plain=args.plain,
        clear_context_on_bounce=args.clear_context_on_bounce,
        serialize=args.serialize,
    )
    return engine.run()


def cmd_plan(args: argparse.Namespace) -> int:
    from juvenal.engine import plan_workflow

    plan_workflow(
        args.goal, args.output, args.backend, plain=args.plain, interactive=args.interactive, resume=args.resume
    )
    if args.implementer:
        _inject_implementer_into_yaml(args.output, args.implementer)
    _expand_standard_checkers(args)
    if args.checker:
        _inject_checkers_into_yaml(args.output, args.checker)
    return 0


def _inject_checkers_into_yaml(yaml_path: str, checker_specs: list[str]) -> None:
    """Post-process a generated YAML file to append checkers: entries to each implement phase."""
    import yaml

    from juvenal.workflow import parse_checker_string

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    parsed = [parse_checker_string(s) for s in checker_specs]

    for phase in data.get("phases", []):
        if phase.get("type", "implement") == "implement":
            existing = phase.get("checkers", [])
            phase["checkers"] = existing + parsed

    with open(yaml_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _inject_implementer_into_yaml(yaml_path: str, role: str) -> None:
    """Post-process a generated YAML file to prepend an implementer role prompt to each implement phase."""
    import yaml

    from juvenal.workflow import _load_implementer_prompt

    preamble = _load_implementer_prompt(role)

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    for phase in data.get("phases", []):
        if phase.get("type", "implement") == "implement":
            phase["prompt"] = preamble + phase.get("prompt", "")

    with open(yaml_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def cmd_do(args: argparse.Namespace) -> int:
    import tempfile

    from juvenal.engine import Engine, plan_workflow
    from juvenal.workflow import inject_checkers, inject_implementer

    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        plan_workflow(args.goal, f.name, args.backend, plain=args.plain, interactive=args.interactive)
        workflow = _load_workflow_or_exit(f.name)

    if args.defines:
        workflow = _apply_defines(workflow, _parse_defines(args.defines))
    if args.implementer:
        workflow = inject_implementer(workflow, args.implementer)
    _expand_standard_checkers(args)
    if args.checker:
        workflow = inject_checkers(workflow, args.checker)
    if args.backend:
        workflow.backend = args.backend
    if args.max_bounces:
        workflow.max_bounces = args.max_bounces

    engine = Engine(
        workflow, plain=args.plain, clear_context_on_bounce=args.clear_context_on_bounce, serialize=args.serialize
    )
    return engine.run()


def cmd_status(args: argparse.Namespace) -> int:
    from juvenal.state import PipelineState

    state = PipelineState.load(args.state_file)
    state.print_status()

    if not state.completed_at:
        return 1
    if any(ps.status == "failed" for ps in state.phases.values()):
        return 1
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from juvenal.workflow import scaffold_workflow

    scaffold_workflow(args.directory, args.template)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from juvenal.engine import Engine
    from juvenal.workflow import inject_checkers, inject_implementer

    workflow = _load_workflow_or_exit(args.workflow)
    if args.defines:
        workflow = _apply_defines(workflow, _parse_defines(args.defines))
    if args.implementer:
        workflow = inject_implementer(workflow, args.implementer)
    _expand_standard_checkers(args)
    if args.checker:
        workflow = inject_checkers(workflow, args.checker)
    if args.backend:
        workflow.backend = args.backend
    if args.max_bounces:
        workflow.max_bounces = args.max_bounces
    if args.working_dir:
        workflow.working_dir = args.working_dir
    if args.backoff is not None:
        workflow.backoff = args.backoff
    if args.notify:
        workflow.notify.extend(args.notify)

    engine = Engine(workflow, dry_run=True, plain=args.plain)
    return engine.run()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # --rich opts into Rich TUI; default is plain text
    args.plain = not getattr(args, "rich", False)

    handlers = {
        "run": cmd_run,
        "plan": cmd_plan,
        "do": cmd_do,
        "status": cmd_status,
        "init": cmd_init,
        "validate": cmd_validate,
    }
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
