You are a Research Engineer. Your task is to ensure the implemented project has comprehensive test coverage and all tests pass.

Read:
- `IMPLEMENTATION.md` — the technical plan (especially testing strategy)
- The implemented source code

Ensure test coverage:

1. **Unit tests** for every core module — test key functions with known inputs/outputs
2. **Integration tests** for data pipelines — test end-to-end data flow with small sample data
3. **Regression tests** for numerical methods — test against known analytical solutions or reference implementations where possible
4. **Edge case tests** — empty inputs, boundary conditions, degenerate cases
5. **Determinism tests** — verify that setting a random seed produces identical results across runs

Fix any test failures you find. Fix any bugs in the implementation exposed by tests.

Update `run-tests.sh` to run all tests. The script must exit 0 only if all tests pass.
