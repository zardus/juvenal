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

Juvenal is a framework where a deterministic Python runtime orchestrates AI coding agents (Claude or Codex) through verified phases. Each phase has:
1. An **implementation prompt** — tells the agent what to build
2. One or more **checkers** — agentic verifiers that can review code and run commands when instructed

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
    checks:
      - prompt: "Run `pytest tests/ -x` and emit `VERDICT: PASS` only if it succeeds."
      - tester

  - id: implement
    prompt_file: phases/implement/prompt.md
    bounce_target: setup  # on failure, bounce back to setup
    checks:
      - prompt: "Run `make test` and emit `VERDICT: PASS` only if it succeeds."
      - role: senior-engineer
      - prompt: "Run `pytest tests/ --tb=long`, review the output, and emit a verdict."
```

### 2. Directory convention

```
my-workflow/
  phases/
    01-setup/
      prompt.md
      check-build.md      # check phase
    02-implement/
      prompt.md
      check-quality.md   # agent checker
```

### 3. Bare .md files

```
phases/
  01-setup.md       # gets default tester checker
  02-implement.md
```

## Checker Types

- **role / prompt**: AI agent that must emit `VERDICT: PASS` or `VERDICT: FAIL: reason`
- If a checker needs to run a command, spell that command out in the prompt.

## Built-in Roles

Agent checkers can use built-in roles: `tester`, `architect`, `pm`, `senior-tester`, `senior-engineer`, `security-engineer`, `technical-writer`, `professor`, `grant-reviewer`

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
