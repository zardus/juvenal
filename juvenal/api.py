"""Public embedded API for Juvenal."""

from __future__ import annotations

import contextvars
import subprocess
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Iterator

from juvenal.backends import Backend, create_backend
from juvenal.engine import Engine, _plan_workflow_internal
from juvenal.workflow import Phase, Workflow, inject_checkers, load_workflow, parse_checker_string, validate_workflow


class JuvenalUsageError(Exception):
    """Raised for deterministic API misuse."""


class JuvenalExecutionError(Exception):
    """Raised when planning, loading, validation, or execution fails."""

    def __init__(self, message: str, *, run_id: str, inspection_path: str | Path):
        self.run_id = run_id
        self.inspection_path = Path(inspection_path).resolve()
        super().__init__(f"{message} [run_id={run_id}, inspect={self.inspection_path}]")


@dataclass
class GoalSession:
    """Embedded API session state."""

    goal_text: str
    working_dir: Path
    backend_name: str
    backend_instance: Backend
    max_bounces: int
    plain: bool
    serialize: bool
    clear_context_on_bounce: bool
    artifact_root: Path
    session_id: str
    session_artifact_dir: Path
    run_counter: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _GitContext:
    repo_root: Path
    exclude_file: Path


_ACTIVE_SESSION: contextvars.ContextVar[GoalSession | None] = contextvars.ContextVar(
    "juvenal_active_session",
    default=None,
)
_DO_HISTORY_LIMIT = 10
_DO_SUMMARY_LIMIT = 200


def _resolve_working_dir(working_dir: str | Path) -> Path:
    resolved = Path(working_dir).expanduser().resolve()
    if not resolved.exists():
        raise JuvenalUsageError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise JuvenalUsageError(f"Working directory is not a directory: {resolved}")
    return resolved


def _resolve_backend(backend: str | Backend) -> tuple[str, Backend]:
    if isinstance(backend, str):
        try:
            return backend, create_backend(backend)
        except ValueError as exc:
            raise JuvenalUsageError(str(exc)) from exc
    if isinstance(backend, Backend):
        return backend.name(), backend
    raise JuvenalUsageError(f"Unsupported backend value: {backend!r}")


def _resolve_artifact_root(working_dir: Path, artifact_dir: str | Path | None) -> Path:
    if artifact_dir is None:
        artifact_root = working_dir / ".juvenal-api"
    else:
        artifact_root = Path(artifact_dir)
        if not artifact_root.is_absolute():
            artifact_root = working_dir / artifact_root
    artifact_root = artifact_root.expanduser().resolve()
    try:
        artifact_root.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise JuvenalUsageError(f"Artifact root exists and is not a directory: {artifact_root}") from exc
    return artifact_root


def _run_git(working_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(working_dir), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _resolve_git_context(working_dir: Path) -> _GitContext | None:
    repo_root_output = _run_git(working_dir, "rev-parse", "--show-toplevel")
    if repo_root_output is None:
        return None

    repo_root = Path(repo_root_output).resolve()
    exclude_output = _run_git(working_dir, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    exclude_file: Path | None = None

    if exclude_output:
        candidate = Path(exclude_output)
        if candidate.is_absolute():
            exclude_file = candidate.resolve()
        else:
            exclude_file = (working_dir / candidate).resolve()

    if exclude_file is None:
        raw_output = _run_git(working_dir, "rev-parse", "--git-path", "info/exclude")
        if raw_output is None:
            return None
        exclude_file = (working_dir / raw_output).resolve()

    return _GitContext(repo_root=repo_root, exclude_file=exclude_file)


def _git_ignore_entry(repo_root: Path, path: Path) -> str:
    rel_path = path.resolve().relative_to(repo_root).as_posix()
    if not rel_path:
        return "/" if not path.is_dir() else "/"
    if path.is_dir():
        return f"/{rel_path}/"
    return f"/{rel_path}"


def _ensure_git_excluded(git_context: _GitContext, path: Path) -> None:
    try:
        path.resolve().relative_to(git_context.repo_root)
    except ValueError:
        return

    entry = _git_ignore_entry(git_context.repo_root, path)
    git_context.exclude_file.parent.mkdir(parents=True, exist_ok=True)
    if git_context.exclude_file.exists():
        existing_text = git_context.exclude_file.read_text()
        existing_lines = existing_text.splitlines()
    else:
        existing_text = ""
        existing_lines = []

    if entry in existing_lines:
        return

    prefix = ""
    if existing_text and not existing_text.endswith("\n"):
        prefix = "\n"
    with git_context.exclude_file.open("a") as f:
        f.write(f"{prefix}{entry}\n")


def _allocate_session_dir(artifact_root: Path) -> tuple[str, Path]:
    for index in count(1):
        session_id = f"session-{index:03d}"
        session_dir = artifact_root / session_id
        try:
            session_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return session_id, session_dir
    raise AssertionError("unreachable")


def _require_active_session(api_name: str) -> GoalSession:
    session = _ACTIVE_SESSION.get()
    if session is None:
        raise JuvenalUsageError(f"juvenal.{api_name}() requires an active juvenal.goal(...) session")
    return session


def _allocate_run_id(session: GoalSession) -> str:
    session.run_counter += 1
    return f"{session.run_counter:03d}"


def _run_label(run_id: str) -> str:
    return f"run-{run_id}"


def _normalize_do_tasks(task_or_tasks: str | Sequence[str]) -> list[str]:
    if isinstance(task_or_tasks, str):
        raw_tasks = [task_or_tasks]
    elif isinstance(task_or_tasks, Sequence):
        raw_tasks = list(task_or_tasks)
        if not raw_tasks:
            raise JuvenalUsageError("juvenal.do() requires at least one task")
    else:
        raise JuvenalUsageError("juvenal.do() expects a task string or a sequence of task strings")

    tasks: list[str] = []
    for index, raw_task in enumerate(raw_tasks, start=1):
        if not isinstance(raw_task, str):
            raise JuvenalUsageError(f"juvenal.do() task {index} must be a string, got {type(raw_task).__name__}")
        task = raw_task.strip()
        if not task:
            raise JuvenalUsageError(f"juvenal.do() task {index} is empty after stripping whitespace")
        tasks.append(task)
    return tasks


def _normalize_checker_specs(checker: str | None, checkers: Sequence[str] | None) -> list[str]:
    if checker is not None and checkers is not None:
        raise JuvenalUsageError("juvenal.do() accepts either checker= or checkers=, not both")
    if checker is not None and not isinstance(checker, str):
        raise JuvenalUsageError(f"juvenal.do() checker= must be a string, got {type(checker).__name__}")
    if checkers is None:
        return [checker] if checker is not None else []
    if isinstance(checkers, str):
        raise JuvenalUsageError("juvenal.do() checkers= must be a sequence of checker specs, not a bare string")
    if not isinstance(checkers, Sequence):
        raise JuvenalUsageError("juvenal.do() checkers= must be a sequence of checker specs")

    normalized = list(checkers)
    for index, spec in enumerate(normalized, start=1):
        if not isinstance(spec, str):
            raise JuvenalUsageError(f"juvenal.do() checker spec {index} must be a string, got {type(spec).__name__}")
    return normalized


def _normalize_history_summary(text: str, limit: int = _DO_SUMMARY_LIMIT) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _build_history_summary(phase_id: str, instruction: str) -> str:
    phase_label = phase_id.replace("-", " ")
    return _normalize_history_summary(f"{phase_label}: {instruction}")


def _recent_history_summaries(session: GoalSession) -> list[str]:
    summaries = [entry["summary"] for entry in session.history if isinstance(entry.get("summary"), str)]
    return summaries[-_DO_HISTORY_LIMIT:]


def _format_recent_history_block(recent_history_summaries: Sequence[str]) -> str:
    if recent_history_summaries:
        return "\n".join(f"- {summary}" for summary in recent_history_summaries)
    return "- None."


def _build_do_prompt(
    *,
    session: GoalSession,
    recent_history_summaries: Sequence[str],
    completed_steps: Sequence[str],
    instruction: str,
) -> str:
    recent_history_block = _format_recent_history_block(recent_history_summaries)

    if completed_steps:
        completed_steps_block = "\n".join(
            f"- Step {index}: {step}" for index, step in enumerate(completed_steps, start=1)
        )
    else:
        completed_steps_block = "- None."

    return (
        "You are executing one validated Juvenal embedded implementation step.\n\n"
        "Session Goal:\n"
        f"{session.goal_text}\n\n"
        "Absolute Working Directory:\n"
        f"{session.working_dir}\n\n"
        "Recent Successful Session History Summaries:\n"
        f"{recent_history_block}\n\n"
        "Earlier steps in this do() call:\n"
        f"{completed_steps_block}\n\n"
        "Current Instruction:\n"
        f"{instruction}\n"
    )


def _normalize_plan_goal_text(goal_text: str) -> tuple[str, str]:
    if not isinstance(goal_text, str):
        raise JuvenalUsageError(
            f"juvenal.plan_and_do() goal_text must be a string, got {type(goal_text).__name__}"
        )

    stripped_goal_text = goal_text.strip()
    if not stripped_goal_text:
        raise JuvenalUsageError("juvenal.plan_and_do() goal_text is empty after stripping whitespace")

    normalized_goal_text = " ".join(stripped_goal_text.split())
    return stripped_goal_text, normalized_goal_text


def _build_plan_and_do_goal(
    *,
    session: GoalSession,
    recent_history_summaries: Sequence[str],
    goal_text: str,
) -> str:
    recent_history_block = _format_recent_history_block(recent_history_summaries)
    return (
        "Session Goal:\n"
        f"{session.goal_text}\n\n"
        "Recent Successful Session History Summaries:\n"
        f"{recent_history_block}\n\n"
        "Local Planning Request:\n"
        f"{goal_text}\n"
    )


def _phase_completed_with_children(workflow: Workflow, engine: Engine, phase_id: str) -> bool:
    phase_state = engine.state.phases.get(phase_id)
    if phase_state is None or phase_state.status != "completed":
        return False

    child_prefix = f"{phase_id}~"
    child_ids = [phase.id for phase in workflow.phases if phase.id.startswith(child_prefix)]
    for child_id in child_ids:
        child_state = engine.state.phases.get(child_id)
        if child_state is None or child_state.status != "completed":
            return False
    return True


def _append_successful_do_history(
    session: GoalSession,
    *,
    run_id: str,
    workflow: Workflow,
    engine: Engine,
    step_instructions: Sequence[tuple[str, str]],
) -> None:
    seen_phase_ids = {entry.get("phase_id") for entry in session.history}

    for phase_id, instruction in step_instructions:
        if phase_id in seen_phase_ids:
            continue
        if not _phase_completed_with_children(workflow, engine, phase_id):
            continue

        session.history.append(
            {
                "kind": "do",
                "run_id": run_id,
                "phase_id": phase_id,
                "instruction": instruction,
                "summary": _build_history_summary(phase_id, instruction),
                "success": True,
            }
        )
        seen_phase_ids.add(phase_id)


@contextmanager
def goal(
    description: str,
    *,
    working_dir: str | Path = ".",
    backend: str | Backend = "codex",
    max_bounces: int = 999,
    plain: bool = False,
    serialize: bool = False,
    clear_context_on_bounce: bool = False,
    artifact_dir: str | Path | None = None,
) -> Iterator[GoalSession]:
    """Create an embedded Juvenal session."""

    working_dir_path = _resolve_working_dir(working_dir)
    backend_name, backend_instance = _resolve_backend(backend)
    artifact_root = _resolve_artifact_root(working_dir_path, artifact_dir)

    git_context = _resolve_git_context(working_dir_path)
    if git_context is not None:
        _ensure_git_excluded(git_context, artifact_root)

    session_id, session_artifact_dir = _allocate_session_dir(artifact_root)
    session = GoalSession(
        goal_text=description,
        working_dir=working_dir_path,
        backend_name=backend_name,
        backend_instance=backend_instance,
        max_bounces=max_bounces,
        plain=plain,
        serialize=serialize,
        clear_context_on_bounce=clear_context_on_bounce,
        artifact_root=artifact_root,
        session_id=session_id,
        session_artifact_dir=session_artifact_dir.resolve(),
    )

    token = _ACTIVE_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_SESSION.reset(token)


def do(
    task_or_tasks: str | Sequence[str],
    *,
    checker: str | None = None,
    checkers: Sequence[str] | None = None,
) -> None:
    """Execute one or more embedded implement steps using the existing runtime."""

    session = _require_active_session("do")
    tasks = _normalize_do_tasks(task_or_tasks)
    checker_specs = _normalize_checker_specs(checker, checkers)

    try:
        for spec in checker_specs:
            parse_checker_string(spec)
    except Exception as exc:
        raise JuvenalUsageError(str(exc)) from exc

    run_id = _allocate_run_id(session)
    recent_history_summaries = _recent_history_summaries(session)
    step_instructions: list[tuple[str, str]] = []
    base_phases: list[Phase] = []

    for index, instruction in enumerate(tasks, start=1):
        phase_id = f"do-{run_id}-step-{index}"
        completed_steps = tasks[: index - 1]
        prompt = _build_do_prompt(
            session=session,
            recent_history_summaries=recent_history_summaries,
            completed_steps=completed_steps,
            instruction=instruction,
        )
        base_phases.append(Phase(id=phase_id, type="implement", prompt=prompt))
        step_instructions.append((phase_id, instruction))

    workflow = Workflow(
        name=f"do-{run_id}",
        phases=base_phases,
        backend=session.backend_name,
        working_dir=session.working_dir,
        max_bounces=session.max_bounces,
    )

    try:
        workflow = inject_checkers(workflow, checker_specs)
    except Exception as exc:
        raise JuvenalUsageError(str(exc)) from exc

    errors = validate_workflow(workflow)
    if errors:
        raise JuvenalUsageError(f"Embedded do() workflow validation failed: {'; '.join(errors)}")

    state_file = session.session_artifact_dir / f"run-{run_id}-do.json"
    engine = Engine(
        workflow,
        backend_instance=session.backend_instance,
        state_file=state_file,
        plain=session.plain,
        clear_context_on_bounce=session.clear_context_on_bounce,
        serialize=session.serialize,
    )

    try:
        exit_code = engine.run()
    except Exception as exc:
        _append_successful_do_history(
            session,
            run_id=run_id,
            workflow=workflow,
            engine=engine,
            step_instructions=step_instructions,
        )
        raise JuvenalExecutionError(
            f"Embedded engine failed: {exc}",
            run_id=_run_label(run_id),
            inspection_path=state_file,
        ) from exc

    _append_successful_do_history(
        session,
        run_id=run_id,
        workflow=workflow,
        engine=engine,
        step_instructions=step_instructions,
    )

    if exit_code != 0:
        raise JuvenalExecutionError(
            f"Embedded engine failed with exit code {exit_code}",
            run_id=_run_label(run_id),
            inspection_path=state_file,
        )


def plan_and_do(goal_text: str) -> None:
    """Plan a workflow into the session working directory, then execute it as written."""

    session = _require_active_session("plan_and_do")
    stripped_goal_text, normalized_goal_text = _normalize_plan_goal_text(goal_text)

    run_id = _allocate_run_id(session)
    run_label = _run_label(run_id)
    recent_history_summaries = _recent_history_summaries(session)
    composed_goal = _build_plan_and_do_goal(
        session=session,
        recent_history_summaries=recent_history_summaries,
        goal_text=stripped_goal_text,
    )

    plan_dir = (session.working_dir / ".plan").resolve()
    workflow_path = (session.working_dir / "workflow.yaml").resolve()
    planner_state_path = (plan_dir / ".juvenal-state.json").resolve()

    try:
        plan_dir.mkdir(exist_ok=True)
        git_context = _resolve_git_context(session.working_dir)
        if git_context is not None:
            _ensure_git_excluded(git_context, plan_dir)
            _ensure_git_excluded(git_context, workflow_path)

        plan_result = _plan_workflow_internal(
            goal=composed_goal,
            project_dir=session.working_dir,
            backend_instance=session.backend_instance,
            plain=session.plain,
        )
    except Exception as exc:
        raise JuvenalExecutionError(
            f"Planning failed: {exc}",
            run_id=run_label,
            inspection_path=planner_state_path,
        ) from exc

    if not plan_result.success:
        detail = plan_result.error or "unknown planning failure"
        raise JuvenalExecutionError(
            f"Planning failed: {detail}",
            run_id=run_label,
            inspection_path=planner_state_path,
        )

    archive_path = session.session_artifact_dir / f"{run_label}-workflow.yaml"
    try:
        archive_path.write_bytes(workflow_path.read_bytes())
        workflow = load_workflow(workflow_path)
        workflow.working_dir = session.working_dir
        workflow.backend = session.backend_name
        workflow.max_bounces = session.max_bounces
        errors = validate_workflow(workflow)
    except JuvenalExecutionError:
        raise
    except Exception as exc:
        raise JuvenalExecutionError(
            f"Planned workflow load failed: {exc}",
            run_id=run_label,
            inspection_path=workflow_path,
        ) from exc

    if errors:
        raise JuvenalExecutionError(
            f"Planned workflow validation failed: {'; '.join(errors)}",
            run_id=run_label,
            inspection_path=workflow_path,
        )

    state_file = session.session_artifact_dir / f"{run_label}-planned.json"
    engine = Engine(
        workflow,
        backend_instance=session.backend_instance,
        state_file=state_file,
        plain=session.plain,
        clear_context_on_bounce=session.clear_context_on_bounce,
        serialize=session.serialize,
    )

    try:
        exit_code = engine.run()
    except Exception as exc:
        raise JuvenalExecutionError(
            f"Planned engine failed: {exc}",
            run_id=run_label,
            inspection_path=state_file,
        ) from exc

    if exit_code != 0:
        raise JuvenalExecutionError(
            f"Planned engine failed with exit code {exit_code}",
            run_id=run_label,
            inspection_path=state_file,
        )

    session.history.append(
        {
            "kind": "plan_and_do",
            "run_id": run_id,
            "goal_text": stripped_goal_text,
            "summary": _normalize_history_summary(f"plan_and_do: {normalized_goal_text}"),
            "success": True,
        }
    )


__all__ = [
    "GoalSession",
    "JuvenalExecutionError",
    "JuvenalUsageError",
    "do",
    "goal",
    "plan_and_do",
]
