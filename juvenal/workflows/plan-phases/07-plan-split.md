You are a Technical Writer splitting an implementation plan into individual phase files and a machine-readable workflow structure file.

Read `.plan/plan.md`.

## Phase files

Split the plan into self-contained files in `.plan/phases/`:
- `.plan/phases/01-<name>.md`
- `.plan/phases/02-<name>.md`
- etc.

Each file should represent one implement block in the final generated workflow and must include:
- `Phase Name`
- `Implement Phase ID`
- `Preexisting Inputs`
- `New Outputs`
- `File Changes`
- `Implementation Details`
- `Verification Phases` — list every explicit top-level verifier with its phase ID, type (`script` or `check`), fixed `bounce_target` equal to the implement phase ID, and purpose
- `Success Criteria`
- `Git Commit Requirement` — explicitly state that the implementer must commit work to git before yielding

Preserve the consume-existing-artifacts contract. If artifacts already exist, list them under `Preexisting Inputs` instead of introducing rediscovery or regeneration work.

Use short, descriptive kebab-case names.

## Structure file

Also write `.plan/workflow-structure.yaml` with this exact schema shape:

```yaml
linear: true
yaml_source_mode: inline-only
verifier_encoding: explicit-phases
phases:
  - order: 1
    id: <phase-id>
    type: implement
    bounce_target: null
    required_preexisting_inputs:
      - <artifact or file path>
  - order: 2
    id: <phase-id>
    type: script
    bounce_target: <implement-phase-id>
    required_preexisting_inputs:
      - <artifact or file path>
```

Rules for `.plan/workflow-structure.yaml`:
- list every top-level phase in final execution order, including every verifier
- `type` must be one of `implement`, `script`, or `check`
- use `bounce_target: null` for implement phases
- verifier phases must use a single fixed `bounce_target` equal to the implement phase they verify
- `required_preexisting_inputs` must be a concrete list of artifacts already expected to exist before that phase begins; use `[]` only when truly empty
- keep the workflow linear, inline-only, and explicit-phase only

Create `.plan/phases/` and write all phase files plus `.plan/workflow-structure.yaml`. Do not modify `.plan/plan.md`.
