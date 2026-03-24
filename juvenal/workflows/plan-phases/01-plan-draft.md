You are a Software Architect. Your task is to produce an initial implementation plan from a goal description and the project codebase.

## Step 1: Read the goal

The goal is:

> {{GOAL}}

## Step 2: Explore the codebase

Thoroughly explore the project structure, source code, dependencies, tests, and conventions. Understand:
- project layout and module structure
- key abstractions, interfaces, and data flow
- existing tests and testing patterns
- code conventions such as naming, formatting, and imports
- build system and dependencies

## Step 3: Write `.plan/plan.md`

Write a comprehensive, implementation-ready plan. This plan will later be split into phase files and a machine-readable workflow structure file, so it must be explicit about workflow topology and artifact flow.

Required document sections:

1. **Context** — what problem this solves, why it is needed, and the relevant codebase background.

2. **Generated Workflow Contract** — spell out the fixed rules the generated workflow must follow:
   - linear execution only; no `parallel_groups`
   - self-contained inline-only YAML; no top-level `include` and no phase-level `prompt_file`, `workflow_file`, `workflow_dir`, `checks`, or other YAML-source indirection
   - no agent-guided `bounce_targets` lists; use only fixed `bounce_target`
   - every verifier must be an explicit top-level `script` or `check` phase
   - every verifier must stay in the implement block it verifies and bounce to that implement phase
   - if the goal or workspace already provides artifacts, list them as existing inputs and consume or update them in place instead of refetching, recollecting, rediscovering, or regenerating them from scratch
   - if prepared artifacts such as source snapshots, CVE data, dependent inventories, or test harnesses already exist, preserve that consume-existing-artifacts contract explicitly
   - every implement prompt in the final generated workflow must instruct the agent to commit work to git before yielding

3. **Implementation Phases** — a numbered, sequential phase list. For each phase include:
   - `Phase Name`
   - `Implement Phase ID`
   - `Verification Phases` — list every planned verifier with its phase ID, type (`script` or `check`), fixed `bounce_target`, and purpose
   - `Preexisting Inputs` — concrete artifacts that must already exist before the phase starts
   - `New Outputs` — concrete artifacts the phase is responsible for producing or rewriting
   - `File Changes` — specific files to create or modify
   - `Implementation Details` — concrete code changes, function signatures, logic, data flow, and edge cases
   - `Verification` — exact commands or review checks

4. **Critical Files** — all files that will be touched and what changes in each.

5. **Final Verification** — how to verify the full implementation after all phases complete.

Be specific and concrete. Reference actual file paths, function names, and line numbers where helpful. The plan must contain enough detail that later planner phases can generate `.plan/phases/*.md` and `.plan/workflow-structure.yaml` without inventing missing topology or artifact details.

Write `.plan/plan.md` and nothing else.
