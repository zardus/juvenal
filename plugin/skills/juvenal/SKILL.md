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
2. One or more **agentic checkers** — verify the work and emit `VERDICT: PASS` or `VERDICT: FAIL: <reason>`

The key insight: the implementing agent and the checking agent are separate, so the implementer can't cheat by weakening tests.

## Workflow Formats

### 1. YAML (most expressive)

```yaml
name: "my-workflow"
backend: claude  # or "codex"
working_dir: "."
max_bounces: 999  # global bounce limit

phases:
  - id: setup
    prompt: "Set up the project scaffolding."
    checkers:
      - tester
      - prompt: "Run `pytest tests/ -x` and review the results."

  - id: implement
    prompt_file: phases/implement/prompt.md
    bounce_target: setup  # on failure, bounce back to setup
    checkers:
      - role: senior-engineer
      - prompt: "Run `make test` before deciding."
```

### 2. Directory convention

```
my-workflow/
  phases/
    01-setup/
      prompt.md
      check-build.md
    02-implement/
      prompt.md
      check-tests.md
      check-quality.md
```

### 3. Bare .md files

```
phases/
  01-setup.md       # gets default tester checker
  02-implement.md
```

## Checker Types

- **role checker**: built-in reviewer persona such as `tester` or `senior-engineer`
- **prompt checker**: custom agentic review prompt, which can include exact commands to run

## Built-in Roles

Agent checkers can use built-in roles: `tester`, `architect`, `pm`, `senior-tester`, `senior-engineer`

## CLI Commands

```bash
juvenal run workflow.yaml [--resume] [--backend claude|codex]
juvenal plan "goal description" [-o output.yaml]
juvenal do "goal description"
juvenal status
juvenal init [directory]
```

## Your Task

When the user invokes `/juvenal`, help them by:

1. If they provide a goal, create a `workflow.yaml` file for that goal
2. If they ask to run something, invoke `juvenal run` via Bash
3. If they need help, explain the workflow format

Always create workflows that are specific, testable, and have meaningful checkers.
