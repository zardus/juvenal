from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import juvenal.api as juvenal

LIBNAME = "libzstd"
DISTRO = "ubuntu 24.04"
GOAL_TEXT = f"port the {LIBNAME} library from C to Rust"
DEFAULT_GIT_EMAIL = "juvenal@example.com"
DEFAULT_GIT_NAME = "Juvenal Sample"
PREPARE_ORIGINAL_STAGE_ID = "prepare-original"
RESEARCH_CVES_STAGE_ID = "research-cves"
PREPARE_TESTS_STAGE_ID = "prepare-tests"
PORT_LIBRARY_STAGE_ID = "port-library"


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
                "Mockup workspace must be the git repo root, not a subdirectory of another repo: "
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
                "Mockup workspace cannot create the baseline commit with a dirty tree: "
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
            "Mockup workspace must be clean before creating a new named session manifest: "
            f"{resolved_target_dir}"
        )


def _build_final_planner_prompt(target_dir: Path) -> str:
    return f"""
    Thoroughly analyze the {LIBNAME} code in {target_dir}/original to
    develop a plan to port the {LIBNAME} library from C to Rust into
    {target_dir}/safe. The library should be:

    - **source-compatible**, so a C program that uses {LIBNAME} should
      be able to compile against {LIBNAME}-safe,
      meaning that all public APIs should remain exported and compatible. All test cases in {target_dir}/original
      should continue to pass. Programs in {target_dir}/dependents.json (as harnessed in {target_dir}/test-original.sh)
      should continue to compile.
    - **link-compatible**, so an object file previously compiled
      against the original {LIBNAME} should be able to
      link against {LIBNAME}-safe, meaning all symbols should be identically exported. Test file objects from
      {target_dir}/original should continue to link against {LIBNAME}-safe and run properly.
    - **runtime-compatible**, so a program that relies on the original
      {LIBNAME} should run perfectly when the library is replaced with
      {LIBNAME}-safe. Programs in {target_dir}/dependents.json (as harnessed in {target_dir}/test-original.sh)
      should continue to function with {LIBNAME}-safe just as they did with the original {LIBNAME}.
    - **reasonably safe**: unsafe Rust is okay as an intermediate step,
      but all code in the final result should be safe
      unless it MUST be unsafe (e.g., to interface with C application code or the OS).
    - **drop-in replaceable**: {LIBNAME}-safe should ship as a package
      for {DISTRO}. {target_dir}/test-original.sh and related install/package artifacts must be updated in place
      for *-safe, not regenerated from scratch, so they install the {LIBNAME}-safe package and ensure continued
      functionality of all software described in {target_dir}/dependents.json.

    `original/`, `relevant_cves.json`, `dependents.json`, and `test-original.sh` already exist in {target_dir}
    and must be consumed as inputs. No generated phase may redo source retrieval, CVE collection, dependent discovery,
    or earlier test preparation.

    Priorities, from most to least (but still) important, are:

    1. perfect compile and runtime interoperability. This is a must-have.
    2. security, both memory safety and resilience against
       previously-identified non-memory vulnerabilities such as
       all those in {target_dir}/relevant_cves.json, which must be mitigated in {LIBNAME}-safe.
    3. performance. Good to have, but not at the expense of the other two.

    The library should be contained in {target_dir}/safe as a standard Rust package.
    For testing, the test cases in {target_dir}/original must be ported over.

    The generated workflow must be linear. Every verifier must immediately follow the implement phase it verifies,
    and every verifier must bounce only to that immediately preceding implement phase, i.e. the previous implementor.
    Each implementation phase should commit to git before yielding so that the succeeding checkers can reason about what
    was changed. Make sure all the test cases for all the above properties are thoroughly checked at the end.
    """


def main(
    working_dir: str | Path | None = None,
    *,
    session_name: str = LIBNAME,
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
                f"retrieve and unpack the source package of the {DISTRO} {LIBNAME} package "
                f"into {target_dir}/original and commit to git"
            ),
            checker="pm",
            stage_id=PREPARE_ORIGINAL_STAGE_ID,
        )
        juvenal.do(
            [
                (
                    f"retrieve historical and current CVEs affecting {LIBNAME}, "
                    f"including full text information, into {target_dir}/all_cves.json "
                    "and commit that to git"
                ),
                (
                    f"analyze CVEs in {target_dir}/all_cves.json and identify "
                    "non-memory-corruption CVEs that may affect a Rust "
                    f"reimplementation. Store the result into {target_dir}/relevant_cves.json "
                    "and commit that to git"
                ),
            ],
            checkers=["security-engineer"],
            stage_id=RESEARCH_CVES_STAGE_ID,
        )

        # test case prep
        juvenal.do(
            [
                (
                    f"analyze the test cases of {LIBNAME} in {target_dir}/original, "
                    "identify test cases that use private/non-imported API, and "
                    "rewrite these test cases to use the public APIs instead, even "
                    "if test coverage decreases in doing so. Test coverage must only "
                    "decrease when necessary to avoid using private APIs, not "
                    "needlessly out of laziness or corner-cutting. Make sure all "
                    "tests pass and commit to git."
                ),
                (
                    f"analyze the test cases of {LIBNAME} in {target_dir}/original, "
                    "identify functionality lacking test coverage, and write test "
                    "cases covering such functionality using the public APIs. Tests "
                    "must not use any non-exported APIs, even if test coverage is "
                    "less than optimal as a result, though a best effort must be "
                    "made for good test coverage. Make sure all tests pass and "
                    "commit to git."
                ),
                (
                    f"identify a diverse set of software in the {DISTRO} "
                    f"repositories that depends on {LIBNAME} for either compile-time "
                    "or runtime use. Document the names and what runtime "
                    f"functionality (if any) depends on {LIBNAME} in {target_dir}/dependents.json "
                    "and commit to git"
                ),
                (
                    f"write {target_dir}/test-original.sh that uses docker to "
                    f"build/install/test, as appropriate, the {LIBNAME}-dependent "
                    f"software described in {target_dir}/dependents.json. Make sure all "
                    "these tests pass and commit to git."
                ),
            ],
            checkers=["tester", "senior-tester"],
            stage_id=PREPARE_TESTS_STAGE_ID,
        )

        # implementation
        juvenal.plan_and_do(_build_final_planner_prompt(target_dir), stage_id=PORT_LIBRARY_STAGE_ID)

    return str(target_dir)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the embedded workflow mockup sample.")
    parser.add_argument("--working-dir")
    parser.add_argument("--session-name", default=LIBNAME)
    parser.add_argument("--backend", default="codex")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--max-bounces", type=int, default=999)
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
