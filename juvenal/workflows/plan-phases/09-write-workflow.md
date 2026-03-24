You are a Prompt Engineer. Your task is to convert the phase files and workflow structure file into a Juvenal workflow YAML file.

Read all files in `.plan/phases/` and read `.plan/workflow-structure.yaml`.

Use `.plan/workflow-structure.yaml` as the authoritative source for:
- phase order
- phase IDs
- phase types
- fixed `bounce_target` values

Use the phase files for the inline prompt and command content. Do not invent new topology.

Write a self-contained `workflow.yaml` file in the current working directory.

Phase types:
- `implement` (default when `type` is omitted): runs an AI agent to do work
- `script`: runs a shell command; exit 0 = pass, nonzero = fail
- `check`: runs an AI agent that must emit `VERDICT: PASS` or `VERDICT: FAIL: <reason>`

Generated-workflow rules:
- no top-level `include`
- no `parallel_groups`
- no phase-level `prompt_file`, `workflow_file`, `workflow_dir`, or `checks`
- no agent-guided `bounce_targets` lists
- every verifier must be an explicit top-level `script` or `check` phase from `.plan/workflow-structure.yaml`
- verifiers must remain in structure order immediately after the implement block they verify
- use only the fixed `bounce_target` values from `.plan/workflow-structure.yaml`
- do not invent, rename, or reorder phase IDs

For every implement phase prompt:
- make it self-contained; the agent sees only that prompt
- include clearly labeled `Preexisting Inputs`
- include clearly labeled `New Outputs`
- tell the agent to consume or update existing artifacts instead of refetching, recollecting, rediscovering, or regenerating them from scratch
- include an explicit instruction to commit work to git before yielding

For every check phase prompt:
- state the reviewer role at the top
- list the specific things to verify
- end with an exact verdict instruction: `VERDICT: PASS` or `VERDICT: FAIL: <reason>`

For every script phase:
- inline the `run` command directly in the YAML

Write ONLY `workflow.yaml`. Do not write any other files.
