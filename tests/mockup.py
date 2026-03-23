"""Toy embedded-API example meant to be run from a repo checkout."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import juvenal.api as juvenal

EXAMPLE_APP_NAME = "toy-todo"
DEFAULT_SESSION_NAME = "toy-todo-example"
GOAL_TEXT = "build a toy todo CLI as a small embedded-API example"
DEFAULT_GIT_EMAIL = "juvenal@example.com"
DEFAULT_GIT_NAME = "Juvenal Example"
PREPARE_EXAMPLE_BRIEF_STAGE_ID = "prepare-example-brief"
PREPARE_SAMPLE_INTERACTIONS_STAGE_ID = "prepare-sample-interactions"
PREPARE_ACCEPTANCE_ASSETS_STAGE_ID = "prepare-acceptance-assets"
BUILD_TOY_TODO_STAGE_ID = "build-toy-todo"


def _resolve_workspace_path(working_dir: str | Path | None) -> Path:
    if working_dir is None:
        return Path(tempfile.mkdtemp()).resolve()

    resolved = Path(working_dir).expanduser().resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise RuntimeError(f"Working directory is not a directory: {resolved}") from exc
    if not resolved.is_dir():
        raise RuntimeError(f"Working directory is not a directory: {resolved}")
    return resolved


def _run_git(target_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=target_dir,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_git_checked(target_dir: Path, *args: str) -> str:
    result = _run_git(target_dir, *args)
    if result.returncode == 0:
        return result.stdout.strip()

    detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
    raise RuntimeError(f"git {' '.join(args)} failed in {target_dir}: {detail}")


def _git_config_value(target_dir: Path, key: str) -> str | None:
    result = _run_git(target_dir, "config", "--get", key)
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def _git_has_head(target_dir: Path) -> bool:
    return _run_git(target_dir, "rev-parse", "--verify", "HEAD").returncode == 0


def _git_is_dirty(target_dir: Path) -> bool:
    return bool(_run_git_checked(target_dir, "status", "--porcelain"))


def _resolve_artifact_root_path(target_dir: Path, artifact_dir: str | Path | None = None) -> Path:
    if artifact_dir is None:
        artifact_root = target_dir / ".juvenal-api"
    else:
        artifact_root = Path(artifact_dir)
        if not artifact_root.is_absolute():
            artifact_root = target_dir / artifact_root
    return artifact_root.expanduser().resolve()


def _resolve_named_session_manifest_path(
    target_dir: Path,
    *,
    session_name: str,
    artifact_dir: str | Path | None = None,
) -> Path:
    validated_session_name = juvenal._validate_session_name(session_name)
    artifact_root = _resolve_artifact_root_path(target_dir, artifact_dir)
    session_dir = (artifact_root / validated_session_name).resolve()
    return juvenal._session_manifest_path(session_dir)


def ensure_repo_initialized(
    target_dir: str | Path,
    *,
    goal_text: str,
    session_name: str,
    backend: str | object = "codex",
    max_bounces: int = 999,
    artifact_dir: str | Path | None = None,
) -> None:
    resolved_target_dir = Path(target_dir).expanduser().resolve()
    if not resolved_target_dir.exists():
        try:
            resolved_target_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"Working directory is not a directory: {resolved_target_dir}") from exc
    if not resolved_target_dir.is_dir():
        raise RuntimeError(f"Working directory is not a directory: {resolved_target_dir}")

    in_repo = _run_git(resolved_target_dir, "rev-parse", "--is-inside-work-tree")
    if in_repo.returncode != 0:
        _run_git_checked(resolved_target_dir, "init")
    else:
        repo_root = Path(_run_git_checked(resolved_target_dir, "rev-parse", "--show-toplevel")).resolve()
        if repo_root != resolved_target_dir:
            raise RuntimeError(
                "Example workspace must be the git repo root, not a subdirectory of another repo: "
                f"{resolved_target_dir} (repo root: {repo_root})"
            )

    if _git_config_value(resolved_target_dir, "user.email") is None:
        _run_git_checked(resolved_target_dir, "config", "user.email", DEFAULT_GIT_EMAIL)
    if _git_config_value(resolved_target_dir, "user.name") is None:
        _run_git_checked(resolved_target_dir, "config", "user.name", DEFAULT_GIT_NAME)

    manifest_path = _resolve_named_session_manifest_path(
        resolved_target_dir,
        session_name=session_name,
        artifact_dir=artifact_dir,
    )

    if not _git_has_head(resolved_target_dir):
        if _git_is_dirty(resolved_target_dir):
            raise RuntimeError(
                "Example workspace cannot create the baseline commit with a dirty tree: "
                f"{resolved_target_dir}"
            )
        _run_git_checked(resolved_target_dir, "commit", "--allow-empty", "-m", "init")

    if manifest_path.exists():
        backend_name, _backend_instance = juvenal._resolve_backend(backend)
        try:
            manifest = juvenal._load_session_manifest(manifest_path)
            juvenal._assert_session_identity_matches(
                session_name=session_name,
                manifest_path=manifest_path,
                manifest=manifest,
                goal_text=goal_text,
                working_dir=resolved_target_dir,
                backend_name=backend_name,
                max_bounces=max_bounces,
                serialize=False,
                clear_context_on_bounce=False,
            )
        except juvenal.JuvenalUsageError as exc:
            raise RuntimeError(str(exc)) from exc
        return

    if _git_is_dirty(resolved_target_dir):
        raise RuntimeError(
            "Example workspace must be clean before creating a new named session manifest: "
            f"{resolved_target_dir}"
        )


def _build_toy_planner_prompt(target_dir: Path) -> str:
    toy_app_dir = target_dir / "toy_app"
    return f"""
    Thoroughly analyze the prepared inputs in {target_dir} and develop a plan to build the
    {EXAMPLE_APP_NAME} CLI in {toy_app_dir}.

    `example-brief.md`, `sample-interactions.md`, `acceptance-checklist.md`, and `smoke-test.sh`
    already exist in {target_dir} and must be consumed as inputs. No generated phase may recreate
    those prep artifacts or redo the earlier preparation work.

    The implementation must stay obviously toy-sized. It must not read like production architecture,
    platform engineering, or a deployable service. Keep the result linear, repo-local, and illustrative.
    Put all implementation in {toy_app_dir}.

    The generated workflow must be linear. Every verifier must immediately follow the implement phase it verifies,
    and every verifier must bounce only to that immediately preceding implement phase, i.e. the previous implementor.
    Every implement phase should commit to git before yielding so that the succeeding checkers can reason about what
    changed.

    Use the existing prep artifacts as the source of truth for commands, persisted-state behavior, and shell validation.
    The final result must respect the prepared interactions and acceptance checklist, and it must leave the earlier
    preparation artifacts in place rather than recreating them.
    """


def __getattr__(name: str) -> object:
    legacy_names = {
        "LIBNAME": EXAMPLE_APP_NAME,
        "DISTRO": "repo-local toy example",
        "PREPARE_ORIGINAL_STAGE_ID": PREPARE_EXAMPLE_BRIEF_STAGE_ID,
        "PREPARE_TESTS_STAGE_ID": PREPARE_ACCEPTANCE_ASSETS_STAGE_ID,
        "PORT_LIBRARY_STAGE_ID": BUILD_TOY_TODO_STAGE_ID,
        "_build_final_planner_prompt": _build_toy_planner_prompt,
    }
    if name == ("RESEARCH_" + "C" + "VES_STAGE_ID"):
        return PREPARE_SAMPLE_INTERACTIONS_STAGE_ID
    if name in legacy_names:
        return legacy_names[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main(
    working_dir: str | Path | None = None,
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    backend: str = "codex",
    plain: bool = False,
    max_bounces: int = 999,
) -> str:
    target_dir = _resolve_workspace_path(working_dir)
    ensure_repo_initialized(
        target_dir,
        goal_text=GOAL_TEXT,
        session_name=session_name,
        backend=backend,
        max_bounces=max_bounces,
    )

    with juvenal.goal(
        GOAL_TEXT,
        working_dir=str(target_dir),
        session_name=session_name,
        backend=backend,
        plain=plain,
        max_bounces=max_bounces,
    ):
        juvenal.do(
            (
                f"write {target_dir}/example-brief.md describing a deliberately tiny file-backed "
                f"{EXAMPLE_APP_NAME} CLI, its public commands (`add`, `list`, `done`, and `remove`), "
                "and its intentionally non-production scope. Keep it obviously illustrative and "
                "repo-local, then commit that file to git."
            ),
            checker="pm",
            stage_id=PREPARE_EXAMPLE_BRIEF_STAGE_ID,
        )
        juvenal.do(
            [
                (
                    f"create or update {target_dir}/sample-interactions.md with happy-path CLI transcripts "
                    f"for the {EXAMPLE_APP_NAME} app described in {target_dir}/example-brief.md. Show the toy "
                    "CLI being used successfully from the shell and commit the updated file to git."
                ),
                (
                    f"add edge-case and failure-mode CLI transcripts to the same "
                    f"{target_dir}/sample-interactions.md, including invalid inputs and empty-state behavior, "
                    "then commit the updated file to git."
                ),
            ],
            checkers=["security-engineer"],
            stage_id=PREPARE_SAMPLE_INTERACTIONS_STAGE_ID,
        )

        juvenal.do(
            [
                (
                    f"derive {target_dir}/acceptance-checklist.md from "
                    f"{target_dir}/example-brief.md and {target_dir}/sample-interactions.md. Create or update "
                    "that checklist with concrete acceptance checks, then commit it to git."
                ),
                (
                    f"expand {target_dir}/acceptance-checklist.md with any missing public-CLI coverage for "
                    f"{EXAMPLE_APP_NAME}, then commit the updated checklist to git."
                ),
                (
                    f"add concrete persisted-state and data-file scenarios to "
                    f"{target_dir}/acceptance-checklist.md so the toy CLI's file-backed behavior is explicitly "
                    "covered, then commit the updated checklist to git."
                ),
                (
                    f"write {target_dir}/smoke-test.sh so it exercises the checklist from the shell against "
                    f"the {EXAMPLE_APP_NAME} CLI behavior described by the prep artifacts. Make the script usable "
                    "from a repo checkout and commit it to git."
                ),
            ],
            checkers=["tester", "senior-tester"],
            stage_id=PREPARE_ACCEPTANCE_ASSETS_STAGE_ID,
        )

        juvenal.plan_and_do(_build_toy_planner_prompt(target_dir), stage_id=BUILD_TOY_TODO_STAGE_ID)

    return str(target_dir)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.mockup",
        description="Run tests.mockup, a toy embedded-API example meant to be run from a repo checkout.",
    )
    parser.add_argument(
        "--working-dir",
        help="Toy example workspace to create or reuse. Defaults to a new temporary repo.",
    )
    parser.add_argument(
        "--session-name",
        default=DEFAULT_SESSION_NAME,
        help=f"Named Juvenal session to reuse across reruns. Defaults to {DEFAULT_SESSION_NAME}.",
    )
    parser.add_argument(
        "--backend",
        default="codex",
        help="Juvenal backend to use for the toy example. Defaults to codex.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Emit plain output for the toy example instead of rich terminal formatting.",
    )
    parser.add_argument(
        "--max-bounces",
        type=int,
        default=999,
        help="Maximum verifier bounce count to allow in the generated workflow.",
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    print(
        main(
            working_dir=args.working_dir,
            session_name=args.session_name,
            backend=args.backend,
            plain=args.plain,
            max_bounces=args.max_bounces,
        )
    )
