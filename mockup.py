import subprocess
import tempfile

import juvenal

LIBNAME = "libzstd"
DISTRO = "ubuntu 24.04"


def main() -> str:
    tmpdir = tempfile.mkdtemp()
    subprocess.run(["git", "init"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.email", "juvenal@example.com"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.name", "Juvenal Sample"], cwd=tmpdir, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmpdir, check=True)

    with juvenal.goal(f"port the {LIBNAME} library from C to Rust", working_dir=tmpdir):
        juvenal.do(
            (
                f"retrieve and unpack the source package of the {DISTRO} {LIBNAME} package "
                f"into {tmpdir}/original and commit to git"
            ),
            checker="pm",
        )
        juvenal.do(
            [
                (
                    f"retrieve historical and current CVEs affecting {LIBNAME}, "
                    f"including full text information, into {tmpdir}/all_cves.json "
                    "and commit that to git"
                ),
                (
                    f"analyze CVEs in {tmpdir}/all_cves.json and identify "
                    "non-memory-corruption CVEs that may affect a Rust "
                    f"reimplementation. Store the result into {tmpdir}/relevant_cves.json "
                    "and commit that to git"
                ),
            ],
            checkers=["security-engineer"],
        )

        # test case prep
        juvenal.do(
            [
                (
                    f"analyze the test cases of {LIBNAME} in {tmpdir}/original, "
                    "identify test cases that use private/non-imported API, and "
                    "rewrite these test cases to use the public APIs instead, even "
                    "if test coverage decreases in doing so. Test coverage must only "
                    "decrease when necessary to avoid using private APIs, not "
                    "needlessly out of laziness or corner-cutting. Make sure all "
                    "tests pass and commit to git."
                ),
                (
                    f"analyze the test cases of {LIBNAME} in {tmpdir}/original, "
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
                    f"functionality (if any) depends on {LIBNAME} in {tmpdir}/dependents.json "
                    "and commit to git"
                ),
                (
                    f"write {tmpdir}/test-original.sh that uses docker to "
                    f"build/install/test, as appropriate, the {LIBNAME}-dependent "
                    f"software described in {tmpdir}/dependents.json. Make sure all "
                    "these tests pass and commit to git."
                ),
            ],
            checkers=["tester", "senior-tester"],
        )

        # implementation
        juvenal.plan_and_do(
            f"""
            Thoroughly analyze the {LIBNAME} code in {tmpdir}/original to
            develop a plan to port the {LIBNAME} library from C to Rust into
            {tmpdir}/safe. The library should be:

            - **source-compatible**, so a C program that uses {LIBNAME} should
              be able to compile against {LIBNAME}-safe,
              meaning that all public APIs should remain exported and compatible. All test cases in {tmpdir}/original
              should continue to pass. Programs in {tmpdir}/dependents.json (as harnessed in {tmpdir}/test-original.sh)
              should continue to compile.
            - **link-compatible**, so an object file previously compiled
              against the original {LIBNAME} should be able to
              link against {LIBNAME}-safe, meaning all symbols should be identically exported. Test file objects from
              {tmpdir}/original should continue to link against {LIBNAME}-safe and run properly.
            - **runtime-compatible**, so a program that relies on the original
              {LIBNAME} should run perfectly when the library is replaced with
              {LIBNAME}-safe. Programs in {tmpdir}/dependents.json (as harnessed in {tmpdir}/test-original.sh)
              should continue to function with {LIBNAME}-safe just as they did with the original {LIBNAME}.
            - **reasonably safe**: unsafe Rust is okay as an intermediate step,
              but all code in the final result should be safe
              unless it MUST be unsafe (e.g., to interface with C application code or the OS).
            - **drop-in replaceable**: {LIBNAME}-safe should ship as a package
              for {DISTRO}. {tmpdir}/test-original.sh and related files should
              be modified to install the {LIBNAME}-safe package and ensure continued functionality of all
              software described in {tmpdir}/dependents.json.

            Priorities, from most to least (but still) important, are:

            1. perfect compile and runtime interoperability. This is a must-have.
            2. security, both memory safety and resilience against
               previously-identified non-memory vulnerabilities such as
               all those in {tmpdir}/relevant_cves.json, which must be mitigated in {LIBNAME}-safe.
            3. performance. Good to have, but not at the expense of the other two.

            The library should be contained in {tmpdir}/safe as a standard Rust package.
            For testing, the test cases in {tmpdir}/original must be ported over.

            Each implementation phase should commit to git so that the
            succeeding checkers can reason about what was changed.
            Ensure that this workflow is linear: checkers must only bounce
            back to the previous implementor. This means that each major
            testing step (e.g., each class of test cases) will probably
            require its own implementation phase followed by checking phase,
            and will likely require a general "fix everything remaining" sort
            of catch-all implementation phase toward the end. Make sure all
            the test cases for all the above properties are thoroughly
            checked at the end.
            """
        )

    return tmpdir


if __name__ == "__main__":
    main()
