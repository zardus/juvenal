"""Public embedded API for Juvenal."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Iterator

import yaml

from juvenal.backends import Backend, create_backend
from juvenal.engine import Engine, _plan_workflow_internal
from juvenal.state import PipelineState
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
    session_name: str | None
    session_artifact_dir: Path
    manifest_path: Path
    run_counter: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return self.session_id


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
_SESSION_MANIFEST_FILENAME = "session.json"
_SESSION_MANIFEST_SCHEMA_VERSION = 1
_STAGED_PLAN_OWNER_FILENAME = "staged-plan-owner.json"
_STAGED_PLAN_WRITE_WORKFLOW_PHASE_ID = "write-workflow"
_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RESERVED_SESSION_NAME_RE = re.compile(r"^session-[0-9]+$")
_SESSION_IDENTITY_FIELDS = (
    "goal_text",
    "working_dir",
    "backend_name",
    "max_bounces",
    "serialize",
    "clear_context_on_bounce",
)


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


def _validate_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise JuvenalUsageError(f"{field_name} must be a string, got {type(value).__name__}")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise JuvenalUsageError(f"{field_name} must match ^[a-z0-9]+(?:-[a-z0-9]+)*$: {value!r}")
    return value


def _validate_session_name(session_name: str) -> str:
    validated = _validate_identifier(session_name, field_name="session_name")
    if _is_reserved_session_name(validated):
        raise JuvenalUsageError(f"session_name {session_name!r} is reserved for anonymous sessions")
    return validated


def _validate_stage_id(stage_id: str) -> str:
    return _validate_identifier(stage_id, field_name="stage_id")


def _is_reserved_session_name(session_name: str) -> bool:
    return _RESERVED_SESSION_NAME_RE.fullmatch(session_name) is not None


def _session_manifest_path(session_dir: Path) -> Path:
    return (session_dir / _SESSION_MANIFEST_FILENAME).resolve()


def _resolve_anonymous_session_dir(artifact_root: Path) -> tuple[str, Path]:
    session_id, session_dir = _allocate_session_dir(artifact_root)
    return session_id, session_dir.resolve()


def _resolve_named_session_dir(artifact_root: Path, session_name: str) -> tuple[str, Path, bool]:
    validated_name = _validate_session_name(session_name)
    session_dir = (artifact_root / validated_name).resolve()
    existed = session_dir.exists()

    if existed and not session_dir.is_dir():
        raise JuvenalUsageError(f"Session artifact path exists and is not a directory: {session_dir}")
    if not existed:
        session_dir.mkdir(exist_ok=False)
    return validated_name, session_dir, existed


def _session_identity_values(
    *,
    goal_text: str,
    working_dir: Path,
    backend_name: str,
    max_bounces: int,
    serialize: bool,
    clear_context_on_bounce: bool,
) -> dict[str, Any]:
    return {
        "goal_text": goal_text,
        "working_dir": str(working_dir),
        "backend_name": backend_name,
        "max_bounces": max_bounces,
        "serialize": serialize,
        "clear_context_on_bounce": clear_context_on_bounce,
    }


def _session_manifest_payload(session: GoalSession) -> dict[str, Any]:
    return {
        "schema_version": _SESSION_MANIFEST_SCHEMA_VERSION,
        "session_id": session.session_id,
        "session_name": session.session_name,
        **_session_identity_values(
            goal_text=session.goal_text,
            working_dir=session.working_dir,
            backend_name=session.backend_name,
            max_bounces=session.max_bounces,
            serialize=session.serialize,
            clear_context_on_bounce=session.clear_context_on_bounce,
        ),
        "run_counter": session.run_counter,
        "history": session.history,
        "stages": session.stages,
    }


def _invalid_session_manifest(manifest_path: Path, detail: str) -> JuvenalUsageError:
    return JuvenalUsageError(f"Invalid session manifest at {manifest_path}: {detail}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _save_session_manifest(session: GoalSession) -> None:
    _write_json_atomic(session.manifest_path, _session_manifest_payload(session))


def _load_session_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _invalid_session_manifest(manifest_path, "missing session.json") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid_session_manifest(manifest_path, str(exc)) from exc

    if not isinstance(raw, dict):
        raise _invalid_session_manifest(manifest_path, "manifest root must be a JSON object")
    if raw.get("schema_version") != _SESSION_MANIFEST_SCHEMA_VERSION:
        raise _invalid_session_manifest(
            manifest_path,
            f"unsupported schema_version {raw.get('schema_version')!r}",
        )

    session_id = raw.get("session_id")
    try:
        _validate_identifier(session_id, field_name="session manifest session_id")
    except JuvenalUsageError as exc:
        raise _invalid_session_manifest(manifest_path, str(exc)) from exc

    session_name = raw.get("session_name")
    if session_name is not None:
        try:
            _validate_session_name(session_name)
        except JuvenalUsageError as exc:
            raise _invalid_session_manifest(manifest_path, str(exc)) from exc
        if session_name != session_id:
            raise _invalid_session_manifest(
                manifest_path,
                f"session_name {session_name!r} must match session_id {session_id!r}",
            )
    elif not _is_reserved_session_name(session_id):
        raise _invalid_session_manifest(manifest_path, "session_name is required for named sessions")

    if session_id != manifest_path.parent.name:
        raise _invalid_session_manifest(
            manifest_path,
            f"session_id {session_id!r} does not match directory {manifest_path.parent.name!r}",
        )

    goal_text = raw.get("goal_text")
    if not isinstance(goal_text, str):
        raise _invalid_session_manifest(manifest_path, "goal_text must be a string")

    working_dir = raw.get("working_dir")
    if not isinstance(working_dir, str):
        raise _invalid_session_manifest(manifest_path, "working_dir must be a string")

    backend_name = raw.get("backend_name")
    if not isinstance(backend_name, str):
        raise _invalid_session_manifest(manifest_path, "backend_name must be a string")

    max_bounces = raw.get("max_bounces")
    if type(max_bounces) is not int:
        raise _invalid_session_manifest(manifest_path, "max_bounces must be an int")

    serialize = raw.get("serialize")
    if not isinstance(serialize, bool):
        raise _invalid_session_manifest(manifest_path, "serialize must be a bool")

    clear_context_on_bounce = raw.get("clear_context_on_bounce")
    if not isinstance(clear_context_on_bounce, bool):
        raise _invalid_session_manifest(manifest_path, "clear_context_on_bounce must be a bool")

    run_counter = raw.get("run_counter")
    if type(run_counter) is not int or run_counter < 0:
        raise _invalid_session_manifest(manifest_path, "run_counter must be a non-negative int")

    history = raw.get("history")
    if not isinstance(history, list):
        raise _invalid_session_manifest(manifest_path, "history must be a list")
    normalized_history: list[dict[str, Any]] = []
    for index, entry in enumerate(history, start=1):
        if not isinstance(entry, dict):
            raise _invalid_session_manifest(manifest_path, f"history entry {index} must be an object")
        normalized_history.append(dict(entry))

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, dict):
        raise _invalid_session_manifest(manifest_path, "stages must be an object")

    stages: dict[str, dict[str, Any]] = {}
    for stage_id, stage_data in stages_raw.items():
        if not isinstance(stage_id, str):
            raise _invalid_session_manifest(manifest_path, "stage ids must be strings")
        try:
            _validate_stage_id(stage_id)
        except JuvenalUsageError as exc:
            raise _invalid_session_manifest(manifest_path, str(exc)) from exc
        if not isinstance(stage_data, dict):
            raise _invalid_session_manifest(manifest_path, f"stage {stage_id!r} must map to an object")
        stages[stage_id] = dict(stage_data)

    return {
        "schema_version": _SESSION_MANIFEST_SCHEMA_VERSION,
        "session_id": session_id,
        "session_name": session_name,
        "goal_text": goal_text,
        "working_dir": working_dir,
        "backend_name": backend_name,
        "max_bounces": max_bounces,
        "serialize": serialize,
        "clear_context_on_bounce": clear_context_on_bounce,
        "run_counter": run_counter,
        "history": normalized_history,
        "stages": stages,
    }


def _assert_session_identity_matches(
    *,
    session_name: str,
    manifest_path: Path,
    manifest: dict[str, Any],
    goal_text: str,
    working_dir: Path,
    backend_name: str,
    max_bounces: int,
    serialize: bool,
    clear_context_on_bounce: bool,
) -> None:
    expected = _session_identity_values(
        goal_text=goal_text,
        working_dir=working_dir,
        backend_name=backend_name,
        max_bounces=max_bounces,
        serialize=serialize,
        clear_context_on_bounce=clear_context_on_bounce,
    )
    mismatches = [
        field_name for field_name in _SESSION_IDENTITY_FIELDS if manifest.get(field_name) != expected[field_name]
    ]
    if mismatches:
        mismatch_list = ", ".join(mismatches)
        raise JuvenalUsageError(
            f"Named session {session_name!r} does not match {manifest_path}: identity mismatch in {mismatch_list}"
        )


def _build_goal_session(
    *,
    goal_text: str,
    working_dir: Path,
    backend_name: str,
    backend_instance: Backend,
    max_bounces: int,
    plain: bool,
    serialize: bool,
    clear_context_on_bounce: bool,
    artifact_root: Path,
    session_id: str,
    session_name: str | None,
    session_artifact_dir: Path,
    manifest_path: Path,
    run_counter: int = 0,
    history: list[dict[str, Any]] | None = None,
    stages: dict[str, dict[str, Any]] | None = None,
) -> GoalSession:
    return GoalSession(
        goal_text=goal_text,
        working_dir=working_dir,
        backend_name=backend_name,
        backend_instance=backend_instance,
        max_bounces=max_bounces,
        plain=plain,
        serialize=serialize,
        clear_context_on_bounce=clear_context_on_bounce,
        artifact_root=artifact_root,
        session_id=session_id,
        session_name=session_name,
        session_artifact_dir=session_artifact_dir,
        manifest_path=manifest_path,
        run_counter=run_counter,
        history=list(history or []),
        stages=dict(stages or {}),
    )


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


def _stage_id_for_run(stage_prefix: str, run_id: str) -> str:
    _validate_stage_id(stage_prefix)
    return _validate_stage_id(f"{stage_prefix}-{run_id}")


def _create_stage_record(session: GoalSession, *, stage_id: str, stage_record: dict[str, Any]) -> None:
    validated_stage_id = _validate_stage_id(stage_id)
    if validated_stage_id in session.stages:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} already exists")
    session.stages[validated_stage_id] = dict(stage_record)
    _save_session_manifest(session)


def _persist_session_checkpoint(
    session: GoalSession,
    *,
    stage_id: str | None = None,
    stage_updates: dict[str, Any] | None = None,
    history_entries: Sequence[dict[str, Any]] | None = None,
) -> None:
    if stage_id is not None:
        validated_stage_id = _validate_stage_id(stage_id)
        if validated_stage_id not in session.stages:
            raise AssertionError(f"Missing stage record for checkpoint: {validated_stage_id}")
        if stage_updates:
            session.stages[validated_stage_id].update(stage_updates)

    if history_entries:
        session.history.extend(history_entries)

    _save_session_manifest(session)


def _initialize_empty_pipeline_state(state_file: Path) -> None:
    PipelineState(state_file=state_file.resolve()).save()


def _recorded_stage_path(stage_id: str, stage_record: dict[str, Any], key: str) -> Path:
    raw_path = stage_record.get(key)
    if not isinstance(raw_path, str) or not raw_path:
        raise JuvenalUsageError(f"Stage {stage_id!r} is missing recorded path {key!r}")
    return Path(raw_path).expanduser().resolve()


def _raise_missing_stage_artifact(run_id: str, path: Path, detail: str) -> JuvenalExecutionError:
    return JuvenalExecutionError(
        detail,
        run_id=_run_label(run_id),
        inspection_path=path,
    )


def _ensure_required_file_exists(run_id: str, path: Path, *, detail: str) -> None:
    try:
        if not path.exists() or not path.is_file():
            raise _raise_missing_stage_artifact(run_id, path, detail)
        path.read_bytes()
    except JuvenalExecutionError:
        raise
    except OSError as exc:
        raise _raise_missing_stage_artifact(run_id, path, f"{detail}: {exc}") from exc


def _ensure_required_state_file(run_id: str, path: Path, *, detail: str) -> None:
    if not path.exists() or not path.is_file():
        raise _raise_missing_stage_artifact(run_id, path, detail)
    try:
        PipelineState.load(path)
    except Exception as exc:
        raise _raise_missing_stage_artifact(run_id, path, f"{detail}: {exc}") from exc


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


def _build_do_stored_steps(session: GoalSession, *, run_id: str, tasks: Sequence[str]) -> list[dict[str, str]]:
    recent_history_summaries = _recent_history_summaries(session)
    stored_steps: list[dict[str, str]] = []

    for index, instruction in enumerate(tasks, start=1):
        phase_id = f"do-{run_id}-step-{index}"
        prompt = _build_do_prompt(
            session=session,
            recent_history_summaries=recent_history_summaries,
            completed_steps=tasks[: index - 1],
            instruction=instruction,
        )
        stored_steps.append(
            {
                "phase_id": phase_id,
                "instruction": instruction,
                "prompt": prompt,
            }
        )

    return stored_steps


def _workflow_from_do_stored_steps(
    session: GoalSession,
    *,
    run_id: str,
    stored_steps: Sequence[dict[str, str]],
    checker_specs: Sequence[str],
) -> tuple[Workflow, list[tuple[str, str]]]:
    base_phases: list[Phase] = []
    step_instructions: list[tuple[str, str]] = []

    for index, stored_step in enumerate(stored_steps, start=1):
        phase_id = stored_step.get("phase_id")
        instruction = stored_step.get("instruction")
        prompt = stored_step.get("prompt")
        if not isinstance(phase_id, str) or not phase_id:
            raise JuvenalUsageError(f"Stored do() step {index} is missing phase_id")
        if not isinstance(instruction, str) or not instruction:
            raise JuvenalUsageError(f"Stored do() step {index} is missing instruction")
        if not isinstance(prompt, str) or not prompt:
            raise JuvenalUsageError(f"Stored do() step {index} is missing prompt")
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

    return workflow, step_instructions


def _collect_successful_do_history_entries(
    session: GoalSession,
    *,
    run_id: str,
    workflow: Workflow,
    engine: Engine,
    step_instructions: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    seen_phase_ids = {entry.get("phase_id") for entry in session.history}
    entries: list[dict[str, Any]] = []

    for phase_id, instruction in step_instructions:
        if phase_id in seen_phase_ids:
            continue
        if not _phase_completed_with_children(workflow, engine, phase_id):
            continue

        entries.append(
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

    return entries


def _append_successful_do_history(
    session: GoalSession,
    *,
    stage_id: str | None,
    run_id: str,
    workflow: Workflow,
    engine: Engine,
    step_instructions: Sequence[tuple[str, str]],
) -> None:
    for history_entry in _collect_successful_do_history_entries(
        session,
        run_id=run_id,
        workflow=workflow,
        engine=engine,
        step_instructions=step_instructions,
    ):
        _persist_session_checkpoint(
            session,
            stage_id=stage_id,
            history_entries=[history_entry],
        )


def _staged_plan_owner_path(working_dir: Path) -> Path:
    return (working_dir / ".plan" / _STAGED_PLAN_OWNER_FILENAME).resolve()


def _planner_owner_identity(session: GoalSession, *, stage_id: str, run_id: str) -> dict[str, str]:
    if session.session_name is None:
        raise JuvenalUsageError("Staged juvenal.plan_and_do() requires a named session")
    return {
        "session_id": session.session_id,
        "session_name": session.session_name,
        "stage_id": stage_id,
        "run_id": run_id,
    }


def _planner_owner_conflict(path: Path, detail: str) -> JuvenalUsageError:
    return JuvenalUsageError(f"Invalid staged planner owner file at {path}: {detail}")


def _read_staged_plan_owner(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _planner_owner_conflict(path, "missing owner file") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise _planner_owner_conflict(path, str(exc)) from exc

    if not isinstance(raw, dict):
        raise _planner_owner_conflict(path, "owner file must be a JSON object")

    owner: dict[str, str] = {}
    for key in ("session_id", "session_name", "stage_id", "run_id"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise _planner_owner_conflict(path, f"owner field {key!r} must be a non-empty string")
        owner[key] = value

    return owner


def _write_staged_plan_owner(path: Path, owner: dict[str, str]) -> None:
    _write_json_atomic(path, owner)


def _collect_planner_asset_files(path: Path, seen_yaml: set[Path], assets: set[Path]) -> None:
    resolved = path.resolve()
    if resolved in seen_yaml:
        return
    seen_yaml.add(resolved)
    assets.add(resolved)

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise JuvenalUsageError(f"Planner asset manifest source must be a mapping: {resolved}")

    includes = raw.get("include", [])
    if includes is None:
        includes = []
    if not isinstance(includes, list):
        raise JuvenalUsageError(f"Planner include list must be a sequence in {resolved}")
    for include_entry in includes:
        if not isinstance(include_entry, str):
            raise JuvenalUsageError(f"Planner include entries must be strings in {resolved}")
        _collect_planner_asset_files(resolved.parent / include_entry, seen_yaml, assets)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            prompt_file = node.get("prompt_file")
            if isinstance(prompt_file, str):
                assets.add((resolved.parent / prompt_file).resolve())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(raw)


def _build_planner_assets_manifest() -> dict[str, Any]:
    planner_yaml = (Path(__file__).parent / "workflows" / "plan.yaml").resolve()
    assets: set[Path] = set()
    _collect_planner_asset_files(planner_yaml, set(), assets)

    files: list[dict[str, str]] = []
    digest_input_parts: list[str] = []
    for asset_path in sorted(assets):
        content = asset_path.read_bytes()
        file_digest = hashlib.sha256(content).hexdigest()
        files.append({"path": str(asset_path), "sha256": file_digest})
        digest_input_parts.append(f"{asset_path}\t{file_digest}")

    digest = hashlib.sha256("\n".join(digest_input_parts).encode("utf-8")).hexdigest()
    return {
        "root": str(planner_yaml),
        "files": files,
        "digest": digest,
    }


def _write_planner_assets_manifest(path: Path) -> dict[str, Any]:
    manifest = _build_planner_assets_manifest()
    _write_json_atomic(path, manifest)
    return manifest


def _load_planner_assets_manifest(run_id: str, path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _raise_missing_stage_artifact(run_id, path, "Planner assets manifest is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise _raise_missing_stage_artifact(run_id, path, f"Planner assets manifest is unreadable: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("digest"), str):
        raise _raise_missing_stage_artifact(run_id, path, "Planner assets manifest is unreadable")
    return raw


def _ensure_planner_assets_unchanged(run_id: str, path: Path) -> None:
    recorded_manifest = _load_planner_assets_manifest(run_id, path)
    try:
        current_manifest = _build_planner_assets_manifest()
    except Exception as exc:
        raise _raise_missing_stage_artifact(run_id, path, f"Planner assets manifest is unreadable: {exc}") from exc

    if recorded_manifest.get("digest") != current_manifest["digest"]:
        raise _raise_missing_stage_artifact(run_id, path, "Planner assets changed since the stage was created")


def _validate_inline_only_planned_workflow(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return ["workflow.yaml must be a YAML mapping"]
    phases = raw.get("phases")
    if not isinstance(phases, list):
        return ["workflow.yaml must define phases as a list"]

    errors: list[str] = []
    if "include" in raw:
        errors.append("top-level include is not allowed")

    forbidden_phase_keys = ("prompt_file", "workflow_file", "workflow_dir", "checks")
    for index, phase_data in enumerate(phases, start=1):
        if not isinstance(phase_data, dict):
            errors.append(f"phase {index} must be a YAML mapping")
            continue
        phase_label = phase_data.get("id", f"phase {index}")
        for key in forbidden_phase_keys:
            if key in phase_data:
                errors.append(f"phase {phase_label!r} must not use {key}")

    return errors


def _rewind_planner_to_write_workflow(planner_state_path: Path, failure_context: str) -> None:
    planner_state = PipelineState.load(planner_state_path)
    planner_state.invalidate_from(_STAGED_PLAN_WRITE_WORKFLOW_PHASE_ID)
    target_attempt = planner_state.phases.get(_STAGED_PLAN_WRITE_WORKFLOW_PHASE_ID)
    planner_state.set_failure_context(
        _STAGED_PLAN_WRITE_WORKFLOW_PHASE_ID,
        failure_context,
        attempt=target_attempt.attempt if target_attempt is not None and target_attempt.attempt > 0 else None,
    )


def _prepare_plan_workspace(session: GoalSession, *, plan_dir: Path, workflow_path: Path) -> None:
    plan_dir.mkdir(exist_ok=True)
    git_context = _resolve_git_context(session.working_dir)
    if git_context is not None:
        _ensure_git_excluded(git_context, plan_dir)
        _ensure_git_excluded(git_context, workflow_path)


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
    session_name: str | None = None,
) -> Iterator[GoalSession]:
    """Create an embedded Juvenal session."""

    working_dir_path = _resolve_working_dir(working_dir)
    backend_name, backend_instance = _resolve_backend(backend)
    artifact_root = _resolve_artifact_root(working_dir_path, artifact_dir)

    git_context = _resolve_git_context(working_dir_path)
    if git_context is not None:
        _ensure_git_excluded(git_context, artifact_root)

    if session_name is None:
        session_id, session_artifact_dir = _resolve_anonymous_session_dir(artifact_root)
        manifest_path = _session_manifest_path(session_artifact_dir)
        session = _build_goal_session(
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
            session_name=None,
            session_artifact_dir=session_artifact_dir,
            manifest_path=manifest_path,
        )
        _save_session_manifest(session)
    else:
        session_id, session_artifact_dir, existed = _resolve_named_session_dir(artifact_root, session_name)
        manifest_path = _session_manifest_path(session_artifact_dir)
        if existed:
            manifest = _load_session_manifest(manifest_path)
            _assert_session_identity_matches(
                session_name=session_id,
                manifest_path=manifest_path,
                manifest=manifest,
                goal_text=description,
                working_dir=working_dir_path,
                backend_name=backend_name,
                max_bounces=max_bounces,
                serialize=serialize,
                clear_context_on_bounce=clear_context_on_bounce,
            )
            session = _build_goal_session(
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
                session_name=session_id,
                session_artifact_dir=session_artifact_dir,
                manifest_path=manifest_path,
                run_counter=manifest["run_counter"],
                history=manifest["history"],
                stages=manifest["stages"],
            )
        else:
            session = _build_goal_session(
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
                session_name=session_id,
                session_artifact_dir=session_artifact_dir,
                manifest_path=manifest_path,
            )
            _save_session_manifest(session)

    token = _ACTIVE_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_SESSION.reset(token)


def _run_do_one_shot(session: GoalSession, *, tasks: Sequence[str], checker_specs: Sequence[str]) -> None:
    run_id = _allocate_run_id(session)
    state_file = (session.session_artifact_dir / f"run-{run_id}-do.json").resolve()
    auto_stage_id = _stage_id_for_run("do", run_id)
    stored_steps = _build_do_stored_steps(session, run_id=run_id, tasks=tasks)
    _create_stage_record(
        session,
        stage_id=auto_stage_id,
        stage_record={
            "kind": "do",
            "run_id": run_id,
            "status": "running",
            "tasks": list(tasks),
            "checker_specs": list(checker_specs),
            "state_file": str(state_file),
            "stored_steps": stored_steps,
        },
    )

    try:
        workflow, step_instructions = _workflow_from_do_stored_steps(
            session,
            run_id=run_id,
            stored_steps=stored_steps,
            checker_specs=checker_specs,
        )
    except JuvenalUsageError:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise

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
            stage_id=auto_stage_id,
            run_id=run_id,
            workflow=workflow,
            engine=engine,
            step_instructions=step_instructions,
        )
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Embedded engine failed: {exc}",
            run_id=_run_label(run_id),
            inspection_path=state_file,
        ) from exc

    _append_successful_do_history(
        session,
        stage_id=auto_stage_id,
        run_id=run_id,
        workflow=workflow,
        engine=engine,
        step_instructions=step_instructions,
    )

    if exit_code != 0:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Embedded engine failed with exit code {exit_code}",
            run_id=_run_label(run_id),
            inspection_path=state_file,
        )

    _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "completed"})


def _run_do_staged(
    session: GoalSession,
    *,
    stage_id: str,
    tasks: Sequence[str],
    checker_specs: Sequence[str],
) -> None:
    validated_stage_id = _validate_stage_id(stage_id)
    expected_tasks = list(tasks)
    expected_checker_specs = list(checker_specs)
    stage_record = session.stages.get(validated_stage_id)
    resume = False

    if stage_record is None:
        run_id = _allocate_run_id(session)
        state_file = (session.session_artifact_dir / f"{_run_label(run_id)}-do.json").resolve()
        stored_steps = _build_do_stored_steps(session, run_id=run_id, tasks=tasks)
        _initialize_empty_pipeline_state(state_file)
        _create_stage_record(
            session,
            stage_id=validated_stage_id,
            stage_record={
                "kind": "do",
                "run_id": run_id,
                "status": "running",
                "tasks": expected_tasks,
                "checker_specs": expected_checker_specs,
                "state_file": str(state_file),
                "stored_steps": stored_steps,
            },
        )
        stage_record = session.stages[validated_stage_id]
    else:
        if stage_record.get("kind") != "do":
            raise JuvenalUsageError(
                f"Stage {validated_stage_id!r} already exists for kind {stage_record.get('kind')!r}, not 'do'"
            )
        if stage_record.get("tasks") != expected_tasks or stage_record.get("checker_specs") != expected_checker_specs:
            raise JuvenalUsageError(f"Stage {validated_stage_id!r} was created with different do() inputs")
        if stage_record.get("status") == "completed":
            return
        resume = True

    run_id = stage_record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is missing run_id")
    state_file = _recorded_stage_path(validated_stage_id, stage_record, "state_file")
    stored_steps = stage_record.get("stored_steps")
    if not isinstance(stored_steps, list) or not stored_steps:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is missing stored_steps")
    if resume:
        _ensure_required_state_file(
            run_id,
            state_file,
            detail="Recorded do() state file is missing or unreadable",
        )

    workflow, step_instructions = _workflow_from_do_stored_steps(
        session,
        run_id=run_id,
        stored_steps=stored_steps,
        checker_specs=checker_specs,
    )
    engine = Engine(
        workflow,
        backend_instance=session.backend_instance,
        resume=resume,
        state_file=state_file,
        plain=session.plain,
        clear_context_on_bounce=session.clear_context_on_bounce,
        serialize=session.serialize,
    )

    if resume:
        _append_successful_do_history(
            session,
            stage_id=validated_stage_id,
            run_id=run_id,
            workflow=workflow,
            engine=engine,
            step_instructions=step_instructions,
        )

    try:
        exit_code = engine.run()
    except Exception as exc:
        _append_successful_do_history(
            session,
            stage_id=validated_stage_id,
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
        stage_id=validated_stage_id,
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

    _persist_session_checkpoint(session, stage_id=validated_stage_id, stage_updates={"status": "completed"})


def do(
    task_or_tasks: str | Sequence[str],
    *,
    checker: str | None = None,
    checkers: Sequence[str] | None = None,
    stage_id: str | None = None,
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

    if stage_id is None:
        _run_do_one_shot(session, tasks=tasks, checker_specs=checker_specs)
        return

    _run_do_staged(
        session,
        stage_id=stage_id,
        tasks=tasks,
        checker_specs=checker_specs,
    )


def _ensure_unstaged_plan_allowed(session: GoalSession) -> None:
    owner_path = _staged_plan_owner_path(session.working_dir)
    if owner_path.exists():
        raise JuvenalUsageError("Cannot mix staged and unstaged juvenal.plan_and_do() in the same workspace")
    for stage_record in session.stages.values():
        if stage_record.get("kind") == "plan-and-do" and isinstance(stage_record.get("planner_owner"), dict):
            raise JuvenalUsageError("Cannot mix staged and unstaged juvenal.plan_and_do() in the same workspace")


def _ensure_staged_plan_workspace_available(
    session: GoalSession,
    *,
    owner_path: Path,
    planner_state_path: Path,
) -> None:
    for stage_record in session.stages.values():
        if stage_record.get("kind") != "plan-and-do":
            continue

        planner_owner = stage_record.get("planner_owner")
        if isinstance(planner_owner, dict):
            raise JuvenalUsageError(
                "Workspace already has a staged juvenal.plan_and_do() owner in the manifest: "
                f"{planner_owner.get('session_name')!r} / {planner_owner.get('stage_id')!r}"
            )

        raise JuvenalUsageError("Cannot mix staged and unstaged juvenal.plan_and_do() in the same workspace")

    if owner_path.exists():
        owner = _read_staged_plan_owner(owner_path)
        raise JuvenalUsageError(
            "Workspace already has a staged juvenal.plan_and_do() owner: "
            f"{owner['session_name']!r} / {owner['stage_id']!r}"
        )
    if planner_state_path.exists():
        raise JuvenalUsageError(
            "Cannot start staged juvenal.plan_and_do() because planner state already exists without a staged owner"
        )


def _ensure_staged_plan_owner(expected_owner: dict[str, str], owner_path: Path) -> None:
    if owner_path.exists():
        actual_owner = _read_staged_plan_owner(owner_path)
        if actual_owner != expected_owner:
            raise JuvenalUsageError(
                "Workspace already has a different staged juvenal.plan_and_do() owner: "
                f"{actual_owner['session_name']!r} / {actual_owner['stage_id']!r}"
            )
        return
    _write_staged_plan_owner(owner_path, expected_owner)


def _load_planned_workflow_for_execution(
    session: GoalSession,
    *,
    run_id: str,
    workflow_path: Path,
) -> Workflow:
    try:
        workflow = load_workflow(workflow_path)
    except Exception as exc:
        raise JuvenalExecutionError(
            f"Planned workflow load failed: {exc}",
            run_id=_run_label(run_id),
            inspection_path=workflow_path,
        ) from exc

    workflow.working_dir = session.working_dir
    workflow.backend = session.backend_name
    workflow.max_bounces = session.max_bounces
    errors = validate_workflow(workflow)
    if errors:
        raise JuvenalExecutionError(
            f"Planned workflow validation failed: {'; '.join(errors)}",
            run_id=_run_label(run_id),
            inspection_path=workflow_path,
        )
    return workflow


def _finalize_staged_planning(
    session: GoalSession,
    *,
    stage_id: str,
    run_id: str,
    planner_state_path: Path,
    workflow_path: Path,
    archive_path: Path,
    planned_state_path: Path,
) -> Workflow:
    try:
        raw_errors = _validate_inline_only_planned_workflow(workflow_path)
    except Exception as exc:
        error = JuvenalExecutionError(
            f"Planned workflow load failed: {exc}",
            run_id=_run_label(run_id),
            inspection_path=workflow_path,
        )
        _rewind_planner_to_write_workflow(planner_state_path, str(error))
        raise error from exc

    if raw_errors:
        error = JuvenalExecutionError(
            f"Planned workflow validation failed: {'; '.join(raw_errors)}",
            run_id=_run_label(run_id),
            inspection_path=workflow_path,
        )
        _rewind_planner_to_write_workflow(planner_state_path, str(error))
        raise error

    try:
        archive_path.write_bytes(workflow_path.read_bytes())
    except OSError as exc:
        error = JuvenalExecutionError(
            f"Failed to archive planned workflow: {exc}",
            run_id=_run_label(run_id),
            inspection_path=archive_path,
        )
        _rewind_planner_to_write_workflow(planner_state_path, str(error))
        raise error from exc

    try:
        _initialize_empty_pipeline_state(planned_state_path)
    except Exception as exc:
        error = JuvenalExecutionError(
            f"Failed to initialize planned execution state: {exc}",
            run_id=_run_label(run_id),
            inspection_path=planned_state_path,
        )
        _rewind_planner_to_write_workflow(planner_state_path, str(error))
        raise error from exc

    try:
        workflow = _load_planned_workflow_for_execution(
            session,
            run_id=run_id,
            workflow_path=archive_path,
        )
    except JuvenalExecutionError as exc:
        _rewind_planner_to_write_workflow(planner_state_path, str(exc))
        raise

    _persist_session_checkpoint(session, stage_id=stage_id, stage_updates={"status": "planner_complete"})
    return workflow


def _run_plan_and_do_one_shot(
    session: GoalSession,
    *,
    stripped_goal_text: str,
    normalized_goal_text: str,
) -> None:
    plan_dir = (session.working_dir / ".plan").resolve()
    workflow_path = (session.working_dir / "workflow.yaml").resolve()
    planner_state_path = (plan_dir / ".juvenal-state.json").resolve()
    run_id = _allocate_run_id(session)
    run_label = _run_label(run_id)
    archive_path = (session.session_artifact_dir / f"{run_label}-workflow.yaml").resolve()
    state_file = (session.session_artifact_dir / f"{run_label}-planned.json").resolve()
    auto_stage_id = _stage_id_for_run("plan-and-do", run_id)
    _create_stage_record(
        session,
        stage_id=auto_stage_id,
        stage_record={
            "kind": "plan-and-do",
            "run_id": run_id,
            "status": "running",
            "goal_text": stripped_goal_text,
            "workflow_archive": str(archive_path),
            "state_file": str(state_file),
            "planner_state_file": str(planner_state_path),
        },
    )

    composed_goal = _build_plan_and_do_goal(
        session=session,
        recent_history_summaries=_recent_history_summaries(session),
        goal_text=stripped_goal_text,
    )

    try:
        _prepare_plan_workspace(session, plan_dir=plan_dir, workflow_path=workflow_path)
        plan_result = _plan_workflow_internal(
            goal=composed_goal,
            project_dir=session.working_dir,
            backend_instance=session.backend_instance,
            plain=session.plain,
        )
    except Exception as exc:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Planning failed: {exc}",
            run_id=run_label,
            inspection_path=planner_state_path,
        ) from exc

    if not plan_result.success:
        detail = plan_result.error or "unknown planning failure"
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Planning failed: {detail}",
            run_id=run_label,
            inspection_path=planner_state_path,
        )

    try:
        archive_path.write_bytes(workflow_path.read_bytes())
        workflow = _load_planned_workflow_for_execution(
            session,
            run_id=run_id,
            workflow_path=workflow_path,
        )
    except JuvenalExecutionError:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise
    except Exception as exc:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Planned workflow load failed: {exc}",
            run_id=run_label,
            inspection_path=workflow_path,
        ) from exc

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
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Planned engine failed: {exc}",
            run_id=run_label,
            inspection_path=state_file,
        ) from exc

    if exit_code != 0:
        _persist_session_checkpoint(session, stage_id=auto_stage_id, stage_updates={"status": "failed"})
        raise JuvenalExecutionError(
            f"Planned engine failed with exit code {exit_code}",
            run_id=run_label,
            inspection_path=state_file,
        )

    _persist_session_checkpoint(
        session,
        stage_id=auto_stage_id,
        stage_updates={"status": "completed"},
        history_entries=[
            {
                "kind": "plan_and_do",
                "run_id": run_id,
                "goal_text": stripped_goal_text,
                "summary": _normalize_history_summary(f"plan_and_do: {normalized_goal_text}"),
                "success": True,
            }
        ],
    )


def _run_plan_and_do_staged(
    session: GoalSession,
    *,
    stage_id: str,
    stripped_goal_text: str,
    normalized_goal_text: str,
) -> None:
    if session.session_name is None:
        raise JuvenalUsageError("Staged juvenal.plan_and_do() requires a named session")

    validated_stage_id = _validate_stage_id(stage_id)
    plan_dir = (session.working_dir / ".plan").resolve()
    workflow_path = (session.working_dir / "workflow.yaml").resolve()
    planner_state_path = (plan_dir / ".juvenal-state.json").resolve()
    owner_path = _staged_plan_owner_path(session.working_dir)
    stage_record = session.stages.get(validated_stage_id)
    workflow: Workflow | None = None
    planned_resume = False

    if stage_record is None:
        _prepare_plan_workspace(session, plan_dir=plan_dir, workflow_path=workflow_path)
        _ensure_staged_plan_workspace_available(
            session,
            owner_path=owner_path,
            planner_state_path=planner_state_path,
        )

        run_id = _allocate_run_id(session)
        run_label = _run_label(run_id)
        archive_path = (session.session_artifact_dir / f"{run_label}-workflow.yaml").resolve()
        planned_state_path = (session.session_artifact_dir / f"{run_label}-planned.json").resolve()
        planner_assets_path = (session.session_artifact_dir / f"{run_label}-planner-assets.json").resolve()
        planning_goal = _build_plan_and_do_goal(
            session=session,
            recent_history_summaries=_recent_history_summaries(session),
            goal_text=stripped_goal_text,
        )
        owner = _planner_owner_identity(session, stage_id=validated_stage_id, run_id=run_id)

        _initialize_empty_pipeline_state(planner_state_path)
        _write_planner_assets_manifest(planner_assets_path)
        _create_stage_record(
            session,
            stage_id=validated_stage_id,
            stage_record={
                "kind": "plan-and-do",
                "run_id": run_id,
                "status": "running",
                "goal_text": normalized_goal_text,
                "planning_goal": planning_goal,
                "planner_owner": owner,
                "planner_owner_path": str(owner_path),
                "planner_state_path": str(planner_state_path),
                "planner_assets_path": str(planner_assets_path),
                "workflow_archive_path": str(archive_path),
                "planned_state_path": str(planned_state_path),
            },
        )
        _write_staged_plan_owner(owner_path, owner)
        stage_record = session.stages[validated_stage_id]
        planning_resume = False
    else:
        if stage_record.get("kind") != "plan-and-do":
            raise JuvenalUsageError(
                f"Stage {validated_stage_id!r} already exists for kind {stage_record.get('kind')!r}, not 'plan-and-do'"
            )
        if stage_record.get("goal_text") != normalized_goal_text:
            raise JuvenalUsageError(f"Stage {validated_stage_id!r} was created with a different plan_and_do() goal")
        planning_resume = stage_record.get("status") == "running"

    run_id = stage_record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is missing run_id")
    run_label = _run_label(run_id)
    planning_goal = stage_record.get("planning_goal")
    if not isinstance(planning_goal, str) or not planning_goal:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is missing planning_goal")
    owner = stage_record.get("planner_owner")
    if not isinstance(owner, dict):
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is missing planner_owner")
    expected_owner = _planner_owner_identity(session, stage_id=validated_stage_id, run_id=run_id)
    if owner != expected_owner:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} is owned by a different planner session")

    owner_path = _recorded_stage_path(validated_stage_id, stage_record, "planner_owner_path")
    planner_state_path = _recorded_stage_path(validated_stage_id, stage_record, "planner_state_path")
    planner_assets_path = _recorded_stage_path(validated_stage_id, stage_record, "planner_assets_path")
    archive_path = _recorded_stage_path(validated_stage_id, stage_record, "workflow_archive_path")
    planned_state_path = _recorded_stage_path(validated_stage_id, stage_record, "planned_state_path")
    stage_status = stage_record.get("status")

    if stage_status == "completed":
        _ensure_staged_plan_owner(expected_owner, owner_path)
        return

    if stage_status == "running":
        if planning_resume:
            _ensure_required_state_file(
                run_id,
                planner_state_path,
                detail="Recorded planner state file is missing or unreadable",
            )
            _ensure_planner_assets_unchanged(run_id, planner_assets_path)
        _ensure_staged_plan_owner(expected_owner, owner_path)
        _prepare_plan_workspace(session, plan_dir=plan_dir, workflow_path=workflow_path)

        try:
            plan_result = _plan_workflow_internal(
                goal=planning_goal,
                project_dir=session.working_dir,
                backend_instance=session.backend_instance,
                plain=session.plain,
                resume=planning_resume,
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

        workflow = _finalize_staged_planning(
            session,
            stage_id=validated_stage_id,
            run_id=run_id,
            planner_state_path=planner_state_path,
            workflow_path=workflow_path,
            archive_path=archive_path,
            planned_state_path=planned_state_path,
        )
    elif stage_status == "planner_complete":
        _ensure_required_file_exists(
            run_id,
            archive_path,
            detail="Recorded archived workflow is missing or unreadable",
        )
        _ensure_required_state_file(
            run_id,
            planned_state_path,
            detail="Recorded planned execution state file is missing or unreadable",
        )
        _ensure_staged_plan_owner(expected_owner, owner_path)
        workflow = _load_planned_workflow_for_execution(
            session,
            run_id=run_id,
            workflow_path=archive_path,
        )
        planned_resume = True
    else:
        raise JuvenalUsageError(f"Stage {validated_stage_id!r} has unsupported status {stage_status!r}")

    engine = Engine(
        workflow,
        backend_instance=session.backend_instance,
        resume=planned_resume,
        state_file=planned_state_path,
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
            inspection_path=planned_state_path,
        ) from exc

    if exit_code != 0:
        raise JuvenalExecutionError(
            f"Planned engine failed with exit code {exit_code}",
            run_id=run_label,
            inspection_path=planned_state_path,
        )

    _persist_session_checkpoint(
        session,
        stage_id=validated_stage_id,
        stage_updates={"status": "completed"},
        history_entries=[
            {
                "kind": "plan_and_do",
                "run_id": run_id,
                "goal_text": stripped_goal_text,
                "summary": _normalize_history_summary(f"plan_and_do: {normalized_goal_text}"),
                "success": True,
            }
        ],
    )


def plan_and_do(goal_text: str, *, stage_id: str | None = None) -> None:
    """Plan a workflow into the session working directory, then execute it as written."""

    session = _require_active_session("plan_and_do")
    stripped_goal_text, normalized_goal_text = _normalize_plan_goal_text(goal_text)

    if stage_id is None:
        _ensure_unstaged_plan_allowed(session)
        _run_plan_and_do_one_shot(
            session,
            stripped_goal_text=stripped_goal_text,
            normalized_goal_text=normalized_goal_text,
        )
        return

    _run_plan_and_do_staged(
        session,
        stage_id=stage_id,
        stripped_goal_text=stripped_goal_text,
        normalized_goal_text=normalized_goal_text,
    )


__all__ = [
    "GoalSession",
    "JuvenalExecutionError",
    "JuvenalUsageError",
    "do",
    "goal",
    "plan_and_do",
]
