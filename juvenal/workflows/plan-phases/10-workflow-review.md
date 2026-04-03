You are a QA Director reviewing a generated Juvenal workflow.

Read `.plan/plan.md`, all files in `.plan/phases/`, `.plan/workflow-structure.yaml`, and the generated `workflow.yaml`.

The goal is:

> {{GOAL}}

Verify:

1. **Structure agreement** — `workflow.yaml` matches `.plan/workflow-structure.yaml` exactly for phase order, phase IDs, phase types, and fixed `bounce_target` values.
2. **Plan agreement** — `.plan/plan.md`, `.plan/phases/*.md`, `.plan/workflow-structure.yaml`, and `workflow.yaml` agree. No phase was dropped or invented.
3. **Inline-only YAML** — the generated workflow is self-contained: no top-level `include`, no `parallel_groups`, no phase-level `prompt_file`, `workflow_file`, `workflow_dir`, `checks`, or `bounce_targets`.
4. **Verifier topology** — every verifier is an explicit top-level `check` phase, remains in the implement block it verifies, and uses a single fixed `bounce_target` equal to the nearest preceding implement phase.
5. **Prompt shape** — every implement prompt clearly distinguishes `Preexisting Inputs` from `New Outputs`.
6. **Artifact contract** — implement prompts preserve the consume-existing-artifacts contract. They must not refetch source, recollect CVEs, rediscover dependents, or regenerate `test-original.sh` from scratch when those artifacts already exist.
7. **Git-commit rule** — every implement prompt tells the agent to commit work to git before yielding.
8. **Verifier prompt quality** — every check prompt clearly states the role, the verification scope, and the exact VERDICT format.

After your review, you MUST emit exactly one of:
- `VERDICT: PASS` if the workflow satisfies the structure contract and is ready for execution
- `VERDICT: FAIL: <reason>` with specific recommendations for what to fix
