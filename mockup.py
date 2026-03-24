"""Toy embedded-API example (standalone copy, see tests/mockup.py)."""

from __future__ import annotations

import subprocess
import tempfile

import juvenal.api as juvenal


def main() -> str:
    tmpdir = tempfile.mkdtemp()
    subprocess.run(["git", "init"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.email", "juvenal@example.com"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.name", "Juvenal Example"], cwd=tmpdir, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmpdir, check=True)

    with juvenal.goal("build a toy todo CLI as a small embedded-API example", working_dir=tmpdir):
        juvenal.do(
            (
                f"write {tmpdir}/example-brief.md describing a deliberately tiny file-backed "
                "toy-todo CLI, its public commands (`add`, `list`, `done`, and `remove`), "
                "and its intentionally non-production scope. Keep it obviously illustrative and "
                "repo-local, then commit that file to git."
            ),
            checker="pm",
        )
        juvenal.do(
            [
                (
                    f"create {tmpdir}/sample-interactions.md with happy-path CLI transcripts "
                    "for the toy-todo app described in example-brief.md. Show the toy "
                    "CLI being used successfully from the shell and commit to git."
                ),
                (
                    f"add edge-case and failure-mode CLI transcripts to {tmpdir}/sample-interactions.md, "
                    "including invalid inputs and empty-state behavior, then commit to git."
                ),
            ],
            checkers=["security-engineer"],
        )

        # acceptance assets
        juvenal.do(
            [
                (
                    f"derive {tmpdir}/acceptance-checklist.md from example-brief.md and "
                    "sample-interactions.md with concrete acceptance checks, then commit to git."
                ),
                (
                    f"expand {tmpdir}/acceptance-checklist.md with any missing public-CLI coverage "
                    "for toy-todo, then commit to git."
                ),
                (
                    f"add concrete persisted-state and data-file scenarios to "
                    f"{tmpdir}/acceptance-checklist.md so the toy CLI's file-backed behavior is "
                    "explicitly covered, then commit to git."
                ),
                (
                    f"write {tmpdir}/smoke-test.sh so it exercises the checklist from the shell "
                    "against the toy-todo CLI behavior described by the prep artifacts. Make the "
                    "script usable from a repo checkout and commit to git."
                ),
            ],
            checkers=["tester", "senior-tester"],
        )

        # implementation
        juvenal.plan_and_do(
            f"""
            Thoroughly analyze the prepared inputs in {tmpdir} and develop a plan to build the
            toy-todo CLI in {tmpdir}/toy_app.

            `example-brief.md`, `sample-interactions.md`, `acceptance-checklist.md`, and `smoke-test.sh`
            already exist in {tmpdir} and must be consumed as inputs. No generated phase may recreate
            those prep artifacts or redo the earlier preparation work.

            The implementation must stay obviously toy-sized. Put all implementation in {tmpdir}/toy_app.

            The generated workflow must be linear. Every verifier must immediately follow the implement
            phase it verifies, and every verifier must bounce only to that immediately preceding implement
            phase, i.e. the previous implementor. Every implement phase should commit to git before
            yielding so that the succeeding checkers can reason about what changed.
            """
        )

    return tmpdir


if __name__ == "__main__":
    main()
