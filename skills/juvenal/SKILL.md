---
name: juvenal
description: Create and run verified AI agent workflows using Juvenal
argument-hint: "[goal or command]"
allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Edit
---

# Juvenal — Verified AI Agent Workflows

You are helping the user create and manage Juvenal workflows. Juvenal orchestrates AI coding agents through alternating implementation and verification phases, preventing agents from cheating on success criteria.

## What is Juvenal?

Juvenal is a framework where a non-agentic Python script orchestrates AI coding agents (Claude or Codex) through verified phases. Each phase has:
1. An **implementation prompt** — tells the agent what to build
2. One or more **checkers** — verify the work (scripts, agent reviewers, or both)

The key insight: the implementing agent and the checking agent are separate, so the implementer can't cheat by weakening tests.

## Workflow Formats

### 1. YAML (most expressive)

```yaml
name: "my-workflow"
backend: claude  # or "codex"
working_dir: "."
max_bounces: 999  # global bounce limit
backoff: 2.0      # exponential backoff between bounces (seconds)
max_backoff: 60.0 # cap on backoff delay
notify:
  - https://example.com/webhook  # webhook on completion/failure

include:
  - shared-phases.yaml  # merge phases from other workflows

phases:
  - id: setup
    prompt: "Set up the project scaffolding."
    timeout: 300  # seconds
    env:
      NODE_ENV: development
    checkers:
      - run: "pytest tests/ -x"  # script checker (exit 0 = pass)
      - tester                    # built-in role shorthand
      - role: senior-engineer     # role as dict
      - prompt: "Review the code for security issues."  # inline prompt

  - id: implement
    prompt_file: phases/implement/prompt.md
    bounce_target: setup  # on failure, bounce back to setup
    checkers:
      - run: "make test"
      - role: senior-engineer

parallel_groups:
  # Lanes: concurrent mini-pipelines with per-lane bounce loops
  - lanes:
      - [feature-a, check-a]   # lane 1
      - [feature-b, check-b]   # lane 2

  # Legacy flat: run implement phases concurrently (no per-phase checking)
  - phases: [independent-x, independent-y]
```

### 2. Directory convention

```
my-workflow/
  phases/
    01-setup/
      prompt.md
    02-implement/
      prompt.md
```

### 3. Bare .md files

A single `.md` file becomes a single implement phase:

```bash
juvenal run task.md
```

## Phase Types

| Type | Description |
|------|-------------|
| `implement` | Agent executes a prompt to build/modify code (default) |
| `check` | Separate agent verifies work, emits `VERDICT: PASS` or `VERDICT: FAIL: reason` |
| `script` | Shell command; exit 0 = pass, nonzero = fail |
| `workflow` | Dynamic sub-workflow: plans and executes a sub-pipeline from the prompt |

### Workflow Phases

```yaml
- id: dynamic-feature
  type: workflow
  prompt: "Build a REST API with authentication and tests."
  max_depth: 2  # recursion depth limit (default: 3)
```

## Inline Checkers Shorthand

Checkers are defined inline on implement phases. Each entry can be:

- **Bare string** — built-in role shorthand: `tester`, `architect`, `pm`, `senior-tester`, `senior-engineer`
- **`run: CMD`** — script checker (exit 0 = pass)
- **`role: NAME`** — agent checker with built-in role
- **`prompt: TEXT`** — agent checker with inline prompt
- **`prompt_file: PATH`** — agent checker with prompt from file

Checkers can also carry `timeout` and `env`.

## Bounce Targets

- **`bounce_target`** (singular, fixed): always bounces to this phase on failure
- **`bounce_targets`** (list, agent-guided): checker picks which phase to bounce to via `VERDICT: FAIL(target-id): reason`. Falls back to first in the list.

These are mutually exclusive.

```yaml
- id: review
  type: check
  bounce_targets:
    - design-experiments   # agent can bounce here
    - write-paper          # or here
```

## Parallel Groups

### Lanes (new)

Each lane is a mini-pipeline (implement + check) that runs its own internal bounce loop. All lanes run concurrently with a shared global bounce budget.

```yaml
parallel_groups:
  - lanes:
      - [feature-a, check-a]
      - [feature-b, check-b]
      - [feature-c, check-c]
```

### Legacy flat format

Run implement phases concurrently with no per-phase checking. A single failure aborts the group.

```yaml
parallel_groups:
  - phases: [a, b, c]
```

## Workflow Includes

Compose workflows from reusable pieces:

```yaml
# main.yaml
include:
  - shared/setup.yaml
  - shared/linting.yaml
phases:
  - id: feature
    prompt: "Build the feature."
```

Included phases, parallel groups, and other settings are merged. Circular includes are detected.

## CLI Commands

```bash
juvenal run <workflow> [--resume] [--rewind N] [--rewind-to PHASE_ID] [--phase X]
                       [--max-bounces N] [--backend claude|codex] [--dry-run]
                       [--backoff SECONDS] [--notify URL] [--working-dir DIR]
                       [--state-file PATH] [--checker SPEC] [--implementer ROLE]
                       [--preserve-context-on-bounce]
juvenal plan "goal" [-o output.yaml] [--backend claude|codex]
juvenal do "goal" [--backend claude|codex] [--max-bounces N]
juvenal status [--state-file path]
juvenal init [directory] [--template name]
juvenal validate <workflow>
```

### Key flags

- **`--checker SPEC`**: Inject a checker on every implement phase. SPEC is a role name (`tester`), `run:CMD`, or `prompt:TEXT`. Repeatable.
- **`--implementer ROLE`**: Prepend an implementer role prompt to every implement phase (e.g., `software-engineer`).
- **`--preserve-context-on-bounce`**: Resume the agent's session on bounce instead of starting fresh — preserves conversation context.
- **`--backoff SECONDS`**: Exponential backoff between bounces (base delay, doubles each bounce, capped at `--max-backoff` or workflow's `max_backoff`).
- **`--notify URL`**: Webhook URL for JSON notifications on completion/failure. Repeatable.
- **`--dry-run`**: Print execution plan, validation, and phase summary without running.

## Canned Workflows

### `research-paper` — Write a research paper

A 14-phase workflow for producing a research paper from an initial idea, with 6 agent roles: professor, postdoc, graduate researcher, research engineer, and two academic reviewers (one positive, one skeptical).

**Setup:** Create an `IDEA.md` in your working directory with the research idea, then run:

```bash
juvenal run $(python -c "from pathlib import Path; print(Path(__import__('juvenal').__file__).parent / 'workflows' / 'research-paper.yaml')") --backend claude
```

**Phases:**

| # | Phase | Agent | Type |
|---|-------|-------|------|
| 1 | `research-plan` | Professor | implement |
| 2 | `design-experiments` | Graduate Researcher | implement |
| 3 | `design-implementation` | Research Engineer | implement |
| 4 | `implement-project` | Research Engineer | implement |
| 5 | `ensure-tests` | Research Engineer | implement |
| 5b | `run-tests` | — | script |
| 6 | `implement-experiments` | Graduate Researcher | implement |
| 7 | `run-experiments` | Graduate Researcher | implement |
| 8 | `results-review` | Postdoc | check -> `design-experiments` |
| 9 | `design-paper` | Professor | implement |
| 10 | `write-paper` | Graduate Researcher | implement |
| 11 | `professor-review` | Professor | check -> `[design-experiments, write-paper]` |
| 12 | `reviewer-a-review` | Reviewer A (positive) | check -> `[design-experiments, write-paper]` |
| 13 | `reviewer-b-review` | Reviewer B (skeptical) | check -> `[design-experiments, write-paper]` |

**Artifacts produced:** `PLAN.md`, `DESIGN.md`, `IMPLEMENTATION.md`, `RESULTS.md`, `OUTLINE.md`, `PAPER.md`, `reviews/`

## Your Task

When the user invokes `/juvenal`, help them by:

1. If they provide a goal, create a `workflow.yaml` file for that goal
2. If they ask to run something, invoke `juvenal run` via Bash
3. If they ask about canned workflows (e.g. "research paper"), explain the workflow and help them set it up
4. If they need help, explain the workflow format

Always create workflows that are specific, testable, and have meaningful checkers.
